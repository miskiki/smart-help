# -*- coding: utf-8 -*-
"""模型注册表管理端 API（docs/redesign/30-custom-model-registry.md §4.4）。

全部 admin_required。凭证永不回显（api_key 恒 ''，只回 configured 布尔）；
PUT /models/:id 永不覆盖凭证（防改名冲掉 key），凭证走独立子资源。
"""
import logging
import re
import secrets
import threading

from flask import Blueprint, jsonify, request

from authentication import admin_required, ApiError, require_user
from models import db
from model_creds import encrypt, is_configured
from model_providers import (PROVIDERS, VALID_TYPES, validate_base_url,
                             row_meta, test_chat, test_embed, test_rerank,
                             test_with_cfg, row_to_embed_cfg)

_LOG = logging.getLogger(__name__)
model_bp = Blueprint('model_admin', __name__, url_prefix='/api/admin/models')

# 模型编号字符集（_row_by_id 用 fullmatch 校验）：放行 [A-Za-z0-9_]、短横线 '-'、
# 点号 '.'，另放行 CJK 统一表意文字（一-鿿，U+4E00–U+9FFF），长度 1–64
# （与 models.id VARCHAR(64) 一致）。不放行：路径分隔符 '/'、反斜杠、引号、
# 空格与控制字符（均不在字符集内，必被拒）。
# 涉及文件：本行改 _ID_RE；配套改动见 src/models.py 的 Model.id / AuditLog
# .target_code 与 deploy/migrate_models.py 的 audit_log 生产迁移。下游消费点
# src/vector_reindex.py:_cursor_key 仅把 id 作为普通字符串拼成 'reindex_cursor_%s'
# 键（LIKE 'reindex_cursor_%' 前缀扫描），无 ASCII 假定，无需改动。
# 已知偏差（Task 1 review low）：deploy/seed_models.py:_slug 用 [^\w.-] 保留任意
# Unicode 词字符，本正则收窄为 ASCII 字母数字 + _.- + CJK 统一表意文字块；当前 7 条
# seed id 只含 ASCII 字母数字/点/短横线/中文，全覆盖不触发。若将来 yaml 出现重音拉丁
# 或其它文字的平台或模型名，_slug 生成的 id 会被本正则拒 → 该模型编辑 400。届时放宽。
_ID_RE = re.compile(r'[A-Za-z0-9_.\-\u4e00-\u9fff]{1,64}', re.UNICODE)

# 后台重建任务注册表：mid -> {'thread','running','result'}。防同模型并发重建。
_REINDEX_TASKS = {}
_REINDEX_LOCK = threading.Lock()


def _row_by_id(mid):
    from models import Model
    if not _ID_RE.fullmatch(mid or ''):
        raise ApiError('模型编号无效')
    row = Model.query.filter_by(id=mid).first()
    if not row:
        raise ApiError('模型不存在', 404)
    return row


def _audit(action, row, detail=''):
    from datetime import datetime
    from models import AuditLog
    db.session.add(AuditLog(action=action, target_code=row.id,
                            target_name=row.name, detail=detail,
                            operator=require_user().username,
                            created_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S')))


@model_bp.get('')
@admin_required
def list_models():
    """模型列表（api_key 恒空串）。?type=chat|embed|rerank 过滤。"""
    from models import Model
    q = Model.query
    type_ = (request.args.get('type') or '').strip()
    if type_:
        if type_ not in VALID_TYPES:
            raise ApiError('type 只能是 %s' % '/'.join(VALID_TYPES))
        q = q.filter_by(type=type_)
    rows = q.order_by(Model.type, Model.created_at.desc()).all()
    return jsonify(ok=True, items=[row_meta(r) for r in rows], total=len(rows))


@model_bp.get('/providers')
@admin_required
def providers():
    """支持 provider 清单 + 默认 URL 预填（前端编辑器用）。"""
    return jsonify(ok=True, providers=[
        {'id': pid, 'label': p['label'], 'chat': p['chat'], 'embed': p['embed'],
         'rerank': p['rerank'], 'default_base_url': p['default_base_url']}
        for pid, p in PROVIDERS.items()])


def _parse_payload(d, is_update=False, row=None):
    """公共字段校验，返回可入库 dict。"""
    from model_providers import PROVIDERS as _P
    type_ = (d.get('type') or (row.type if row else '') or '').strip()
    if not is_update and type_ not in VALID_TYPES:
        raise ApiError('type 必填且只能是 %s' % '/'.join(VALID_TYPES))
    if is_update and type_:
        type_ = row.type  # type 不可改（换类型=删了重建）
    name = (d.get('name') or (row.name if row else '') or '').strip()
    if not name or len(name) > 100:
        raise ApiError('name 必填且不超过 100 字')
    provider = (d.get('provider') or (row.provider if row else '') or 'openai').strip()
    if provider not in _P:
        raise ApiError('provider 不支持：%s（可选 %s）'
                       % (provider, '/'.join(_P)))
    base_url = (d.get('base_url') or (row.base_url if row else '') or '').strip()
    ok, reason = validate_base_url(base_url, type_)
    if not ok:
        raise ApiError(reason)
    model_name = (d.get('model_name') or (row.model_name if row else '') or '').strip()
    if not model_name:
        raise ApiError('model_name 必填（服务端调用的模型名）')
    dim = d.get('dim', row.dim if row else None)
    if dim is not None:
        dim = int(dim)
        if type_ == 'embed' and not (128 <= dim <= 4096):
            raise ApiError('嵌入模型 dim 须在 128–4096 之间')
    return {
        'type': type_, 'name': name, 'provider': provider, 'base_url': base_url,
        'model_name': model_name, 'dim': dim,
        'source': (d.get('source') or (row.source if row else 'remote') or 'remote').strip(),
        'query_instruction': (d.get('query_instruction')
                              or (row.query_instruction if row else '') or '').strip(),
        'json_mode': bool(d.get('json_mode', row.json_mode if row else False)),
    }


@model_bp.post('')
@admin_required
def create_model():
    """创建模型。is_default=true 时原子清同类型兄弟默认（部分唯一索引兜底）。"""
    from models import Model, db
    d = request.get_json(silent=True) or {}
    f = _parse_payload(d)
    # 对齐智枢 applyDefaultSelection：该类型尚无默认时，新模型自动成为默认
    is_default = bool(d.get('is_default'))
    if not is_default:
        existing_default = Model.query.filter_by(type=f['type'], is_default=True).first()
        is_default = existing_default is None
    row = Model(id='m-' + secrets.token_hex(8),
                api_key_enc=encrypt(d.get('api_key')),
                is_default=is_default, **f)
    db.session.add(row)
    if row.is_default:
        Model.query.filter(Model.type == f['type'], Model.id != row.id).update(
            {Model.is_default: False}, synchronize_session=False)
    db.session.commit()
    _audit('新增模型', row, 'type=%s provider=%s' % (row.type, row.provider))
    db.session.commit()
    return jsonify(ok=True, model=row_meta(row))


@model_bp.put('/<mid>')
@admin_required
def update_model(mid):
    """更新模型；api_key 永不在此覆盖（凭证走 /credentials 子资源）。"""
    from models import db
    row = _row_by_id(mid)
    d = request.get_json(silent=True) or {}
    f = _parse_payload(d, is_update=True, row=row)
    for key, value in f.items():
        setattr(row, key, value)
    db.session.commit()
    _audit('更新模型', row, 'name=%s base_url=%s' % (row.name, row.base_url))
    db.session.commit()
    return jsonify(ok=True, model=row_meta(row))


@model_bp.put('/<mid>/credentials')
@admin_required
def set_credentials(mid):
    """凭证子资源：{api_key?} 设置/更换；不带字段返回 {api_key:{configured}} 元数据。"""
    from models import db
    row = _row_by_id(mid)
    d = request.get_json(silent=True) or {}
    if 'api_key' not in d:
        return jsonify(ok=True, fields={'api_key': {'configured': is_configured(row.api_key_enc)}})
    row.api_key_enc = encrypt(d.get('api_key'))
    db.session.commit()
    _audit('更新模型凭证', row, 'api_key 已更换' if row.api_key_enc else 'api_key 已清空')
    db.session.commit()
    return jsonify(ok=True, fields={'api_key': {'configured': is_configured(row.api_key_enc)}})


@model_bp.delete('/<mid>/credentials')
@admin_required
def clear_credentials(mid):
    """清空凭证。"""
    from models import db
    row = _row_by_id(mid)
    row.api_key_enc = None
    db.session.commit()
    _audit('清空模型凭证', row)
    db.session.commit()
    return jsonify(ok=True)


@model_bp.delete('/<mid>')
@admin_required
def delete_model(mid):
    """删除；默认模型拒删（先切默认）。被 selected_model 引用的会话走既有"已移除请重选"。"""
    from models import db
    row = _row_by_id(mid)
    if row.is_default:
        raise ApiError('默认模型不可直接删除，请先把该类型默认切到其他模型')
    db.session.delete(row)
    db.session.commit()
    _audit('删除模型', row, 'id=%s type=%s' % (mid, row.type))
    db.session.commit()
    return jsonify(ok=True)


@model_bp.post('/<mid>/test')
@admin_required
def test_model(mid):
    """连通性测试（对齐智枢 /models/:id/debug）。chat→chat/completions，embed→embeddings（P2），rerank→重排打分（P3）。"""
    row = _row_by_id(mid)
    if row.type == 'chat':
        result = test_chat(row)
    elif row.type == 'embed':
        result = test_embed(row)
    elif row.type == 'rerank':
        result = test_rerank(row)
    else:
        raise ApiError('该类型连通性测试在后续阶段提供')
    return jsonify(ok=result['ok'], latency_ms=result['latency_ms'], detail=result['detail'])


# 保存前连通性测试（不写库、不缓存、不触发 reindex）。
# 必填字段：base_url、model_name；type=embed 另需 dim。
# 校验顺序（全部先于实网调用）：type → provider → base_url → model_name → dim。
# 契约（写死）：输入校验失败 → 400 {ok:false, latency_ms:0, detail:<原因>}；
# 输入合法真正探测 → 200 同形状 {ok, latency_ms, detail}。两路 body 形状一致。
@model_bp.post('/test-draft')
@admin_required
def test_draft():
    """保存前连通性测试：草稿参数现场校验 + 复用 test_with_cfg 探测。
    不写库（不生成 id、不存凭证）、不缓存、不触发 reindex。"""
    d = request.get_json(silent=True) or {}
    type_ = (d.get('type') or '').strip()
    if type_ not in VALID_TYPES:
        return jsonify(ok=False, latency_ms=0,
                       detail='type 必填且只能是 %s' % '/'.join(VALID_TYPES)), 400
    provider = (d.get('provider') or 'openai').strip()
    if provider not in PROVIDERS:
        return jsonify(ok=False, latency_ms=0,
                       detail='provider 不支持：%s（可选 %s）' % (provider, '/'.join(PROVIDERS))), 400
    # provider 能力位：如 dashscope_rerank 只支持 rerank，chat/embed 组合直接 400（不打网络）
    if not PROVIDERS[provider].get(type_):
        return jsonify(ok=False, latency_ms=0,
                       detail='provider %s 不支持 %s 类型' % (provider, type_)), 400
    base_url = (d.get('base_url') or '').strip()
    ok, reason = validate_base_url(base_url, type_)
    if not ok:
        return jsonify(ok=False, latency_ms=0, detail=reason), 400
    model_name = (d.get('model_name') or '').strip()
    if not model_name:
        return jsonify(ok=False, latency_ms=0,
                       detail='model_name 必填（服务端调用的模型名）'), 400
    dim = d.get('dim')
    dim_int = None
    if type_ == 'embed':
        if not dim:
            return jsonify(ok=False, latency_ms=0,
                           detail='嵌入模型需填写 dim（向量维度）'), 400
        try:
            dim_int = int(dim)
        except (TypeError, ValueError):
            return jsonify(ok=False, latency_ms=0,
                           detail='dim 必须是整数：%s' % dim), 400
        if not 128 <= dim_int <= 4096:
            return jsonify(ok=False, latency_ms=0,
                           detail='dim 必须在 128–4096 之间：%s' % dim_int), 400
    api_key = d.get('api_key') or ''
    # 编辑态草稿测试：前端拿不到明文密钥（凭证永不回显），带上已存模型的 id 让后端
    # 用库里解密后的 key 兜底。这是一次「把已存密钥发到 base_url」的动作，必须锁死：
    # 只有 type/provider/base_url 与库中该行完全一致时才允许兜底，否则要求显式密钥，
    # 免得一次请求就把密钥外发到任意地址；兜底发生时要留审计。
    if not api_key and d.get('id'):
        from models import Model
        from model_creds import decrypt as _decrypt
        row = Model.query.filter_by(id=str(d.get('id'))).first()
        if row is None:
            return jsonify(ok=False, latency_ms=0,
                           detail='模型编号不存在：%s' % d.get('id')), 400
        if (row.type, row.provider) != (type_, provider):
            return jsonify(ok=False, latency_ms=0,
                           detail='类型/提供方与已保存模型不一致，请填 api_key 后单独测试'), 400
        if (row.base_url or '').rstrip('/') != base_url.rstrip('/'):
            return jsonify(ok=False, latency_ms=0,
                           detail='base_url 与已保存模型不同，为避免密钥外发请填 api_key 后单独测试'), 400
        api_key = _decrypt(row.api_key_enc) or ''
        _audit('test_draft_stored_key', row, {'type': type_, 'provider': provider})
    base = base_url.rstrip('/')
    if type_ == 'chat':
        cfg = {'kind': 'chat', 'platform': 'draft', 'base_url': base,
               'api_key': api_key, 'llm_model': model_name, 'embed_model': '',
               'rerank_model': '', 'embed_dim': 1536, 'rerank_endpoint': '',
               'quality_tier': 'high', 'embed_query_instruction': '', 'json_mode': False}
    elif type_ == 'embed':
        cfg = {'kind': 'embed', 'platform': 'draft', 'base_url': base,
               'api_key': api_key, 'llm_model': '', 'embed_model': model_name,
               'rerank_model': '', 'embed_dim': dim_int, 'rerank_endpoint': '',
               'quality_tier': 'high', 'embed_query_instruction': '', 'json_mode': False}
    else:  # rerank：端点按 provider（openai 拼 /rerank，dashscope_rerank 即完整 URL）
        endpoint = base + '/rerank' if provider == 'openai' else base
        cfg = {'kind': 'rerank', 'rerank_model': model_name, 'rerank_endpoint': endpoint,
               'api_key': api_key, 'provider': provider}
    result = test_with_cfg(cfg, timeout=60)
    return jsonify(ok=result['ok'], latency_ms=result['latency_ms'], detail=result['detail'])


@model_bp.get('/index-status')
@admin_required
def index_status():
    """各 embed 模型的向量索引状态（行数/维度/是否已建/重建进度），管理页轮询。"""
    from vector_reindex import index_status as _status
    return jsonify(ok=True, items=_status())


@model_bp.post('/<mid>/reindex')
@admin_required
def reindex_model(mid):
    """重建该 embed 模型的向量索引（后台任务，立即返回；进度走 /index-status 轮询）。"""
    from vector_reindex import reindex_for_model, vector_table
    row = _row_by_id(mid)
    if row.type != 'embed':
        raise ApiError('只有嵌入模型可以重建向量索引')
    cfg = row_to_embed_cfg(row)
    if not cfg.get('api_key') and row.provider == 'openai' and \
            not any(h in (cfg.get('base_url') or '') for h in ('127.0.0.1', 'localhost')):
        raise ApiError('远端嵌入模型缺少 api_key，请先在凭证里填写')
    with _REINDEX_LOCK:
        task = _REINDEX_TASKS.get(mid)
        if task and task['running']:
            raise ApiError('该模型索引正在重建中，请稍候')
    default_name = _default_embed_model_name()
    state = {'running': True, 'result': None, 'error': None}
    _REINDEX_TASKS[mid] = state

    def _run():
        try:
            state['result'] = reindex_for_model(cfg, row.id, row.model_name,
                                                default_model_name=default_name)
        except Exception as exc:
            _LOG.exception('reindex %s failed', row.model_name)
            state['error'] = str(exc)
        finally:
            state['running'] = False

    t = threading.Thread(target=_run, name='reindex-%s' % row.model_name, daemon=True)
    state['thread'] = t
    t.start()
    _audit('重建向量索引', row, 'model=%s table=%s' % (row.model_name, vector_table()))
    db.session.commit()
    return jsonify(ok=True, started=True, model_id=row.id, model_name=row.model_name)


def _default_embed_model_name():
    """当前默认 embed 模型的 model_name（reindex 源切片取自它）；无默认返回 None。"""
    from models import Model
    row = Model.query.filter_by(type='embed', is_default=True).first()
    return (row.model_name or None) if row else None


@model_bp.post('/<mid>/default')
@admin_required
def set_default(mid):
    """设为该类型默认（部分唯一索引原子切换：先清兄弟再置自己，一个事务内完成）。"""
    from models import db, Model
    row = _row_by_id(mid)
    # 同一 commit 内先清兄弟再置自己：两次 flush 一个事务，部分唯一索引兜底并发
    Model.query.filter(Model.type == row.type, Model.id != row.id).update(
        {Model.is_default: False}, synchronize_session=False)
    row.is_default = True
    db.session.commit()
    _audit('设为默认模型', row, 'type=%s' % row.type)
    db.session.commit()
    return jsonify(ok=True, model=row_meta(row))
