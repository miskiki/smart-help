"""Exact embedding budgets and durable, non-executable vector backups."""
import base64
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import threading
from datetime import datetime
from uuid import uuid4

_lock = threading.RLock()


@lru_cache(maxsize=4)
def tokenizer_for(model):
    from tokenizers import Tokenizer
    configured = os.environ.get('EMBED_TOKENIZER_PATH')
    if configured:
        if os.environ.get('EMBED_TOKENIZER_MODEL') != model:
            raise ValueError('EMBED_TOKENIZER_MODEL must match the selected embedding model')
        root = Path(configured)
    else:
        raise ValueError('未配置 EMBED_TOKENIZER_PATH（需与模型匹配）；当前模型: ' + model)
    tok = Tokenizer.from_file(str(root / 'tokenizer.json'))
    tok.no_truncation()
    tok.no_padding()
    config_path = root / 'sentence_bert_config.json'
    if config_path.exists():
        limit = int(json.loads(config_path.read_text(encoding='utf-8'))['max_seq_length'])
    else:
        limit = int(os.environ['EMBED_MAX_TOKENS'])
    return tok, limit


def token_count(value, model):
    with _lock:
        tok, _ = tokenizer_for(model)
        return len(tok.encode(value, add_special_tokens=True).ids)


def validate_inputs(texts, model):
    _, limit = tokenizer_for(model)
    for i, value in enumerate(texts):
        count = token_count(value, model)
        if count > limit:
            raise ValueError('Embedding input %d has %d tokens; limit %d; refusing truncation' % (i, count, limit))


def split_tokens(value, model, budget=480, overlap=60):
    """Preserve every source character. Prefer sentence boundaries; count final input exactly."""
    _, limit = tokenizer_for(model)
    budget = min(budget, limit)
    if budget < 8:
        raise ValueError('Embedding token budget too small')
    chunks, start = [], 0
    while start < len(value):
        lo, hi = start + 1, len(value)
        best = start
        while lo <= hi:
            mid = (lo + hi) // 2
            if token_count(value[start:mid], model) <= budget:
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1
        if best == start:
            raise ValueError('A source character cannot fit the embedding budget')
        if best < len(value):
            boundary = max(value.rfind(mark, start + (best-start)//2, best)
                           for mark in ('\n', '。', '；'))
            if boundary >= 0:
                best = boundary + 1
        chunk = value[start:best]
        if token_count(chunk, model) > budget:
            raise ValueError('Exact token budget check failed')
        chunks.append(chunk)
        if best == len(value):
            break
        next_start = best
        while next_start > start + 1 and token_count(value[next_start-1:best], model) <= overlap:
            next_start -= 1
        start = max(start + 1, next_start)
    validate_inputs(chunks, model)
    return chunks


_BACKUP_TABLES = ('policy_vector', 'policy_vector_qwen3')


def _vector_table_name(table=None):
    """备份/恢复只允许已知向量表名，避免把表名拼进 SQL 时被注入。"""
    name = str(table or os.environ.get('POLICY_VECTOR_TABLE') or 'policy_vector')
    if name not in _BACKUP_TABLES:
        raise ValueError('unknown vector table: %r' % (name,))
    return name


def backup_vectors(connection, policy_code, table=None):
    from sqlalchemy import text
    table = _vector_table_name(table)
    rows = []
    for row in connection.execute(text('SELECT * FROM ' + table + ' WHERE policy_code=:code'),
                                  {'code': policy_code}).mappings():
        item = {}
        for key, value in row.items():
            if isinstance(value, (bytes, bytearray, memoryview)):
                value = {'base64': base64.b64encode(bytes(value)).decode('ascii')}
            elif isinstance(value, datetime):
                value = value.isoformat()
            item[key] = value
        rows.append(item)
    folder = Path(os.environ.get('VECTOR_BACKUP_DIR', str(Path(__file__).parent / 'vector_backups')))
    folder.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({'format': 1, 'policy_code': policy_code, 'table': table, 'rows': rows},
                         ensure_ascii=False, default=str).encode('utf-8')
    name = datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + uuid4().hex
    path = folder / (name + '.json')
    with path.open('xb') as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    path.with_suffix('.sha256').write_text(hashlib.sha256(payload).hexdigest(), encoding='ascii')
    return str(path)
