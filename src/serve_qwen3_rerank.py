# -*- coding: utf-8 -*-
"""Qwen3-Reranker 兼容层：对外 DashScope 格式 /v1/rerank，对内用 yes/no 概率打分。

背景：Qwen3-Reranker 是「生成式重排」（判断 Document 是否满足 Query，回答 yes/no），
llama.cpp 的 --rerank 端点只支持分类头模型（如 bge-reranker），直接调用得分为 0，
故用本层转换：
    对外  POST /v1/rerank  {model?, input:{query, documents:[...]}}
    对内  POST 127.0.0.1:8097/v1/chat/completions  逐条打分（enable_thinking=false）
    返回  {output:{results:[{index, relevance_score}]}}

打分 = P(yes) / (P(yes)+P(no))，按 logsumexp 合并大小写变体（yes/Yes/YES 等）。
用法：
    python serve_qwen3_rerank.py --port 8096 --llm http://127.0.0.1:8097 --workers 8
"""
import argparse
import json
import math
import threading
from concurrent.futures import ThreadPoolExecutor

import requests
from flask import Flask, jsonify, request

SYS_PROMPT = ('Judge whether the Document meets the requirements based on the Query and the Instruct '
              'provided. Note that the answer can only be "yes" or "no".')
INSTRUCT = ('Given a household situation description, retrieve relevant Chinese government '
            'assistance policies')
MAX_DOC_CHARS = 1600
_session = requests.Session()
_session.trust_env = False
LLM_URL = 'http://127.0.0.1:8097'
POOL = None
STATS = {'calls': 0, 'fallbacks': 0, 'lock': threading.Lock()}


def _logsumexp(values):
    m = max(values)
    return m + math.log(sum(math.exp(v - m) for v in values))


def score_pair(query, document, timeout=60):
    """返回 (分数, 是否降级)。分数∈[0,1]，越大越相关。"""
    doc = (document or '')[:MAX_DOC_CHARS]
    user = '<Instruct>: %s\n<Query>: %s\n<Document>: %s' % (INSTRUCT, query, doc)
    body = {'messages': [{'role': 'system', 'content': SYS_PROMPT},
                         {'role': 'user', 'content': user}],
            'max_tokens': 1, 'temperature': 0, 'logprobs': True, 'top_logprobs': 20,
            'chat_template_kwargs': {'enable_thinking': False}}
    try:
        r = _session.post(LLM_URL + '/v1/chat/completions', json=body, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        lp = ((data.get('choices') or [{}])[0].get('logprobs') or {}).get('content') or [{}]
        tops = lp[0].get('top_logprobs') or []
        yes, no = [], []
        for t in tops:
            tok = (t.get('token') or '').strip().lower()
            if tok == 'yes':
                yes.append(t.get('logprob'))
            elif tok == 'no':
                no.append(t.get('logprob'))
        if yes and no:
            py, pn = _logsumexp(yes), _logsumexp(no)
            m = max(py, pn)
            return math.exp(py - m) / (math.exp(py - m) + math.exp(pn - m)), False
        if yes:      # 明确 yes，no 被挤出 top-20
            return 0.995, False
        if no:       # 明确 no
            return 0.005, False
    except Exception:
        pass
    return 0.5, True


app = Flask(__name__)


@app.get('/health')
def health():
    return jsonify(ok=True, service='qwen3-rerank-shim', llm=LLM_URL,
                   stats={k: v for k, v in STATS.items() if k != 'lock'})


@app.get('/v1/models')
def models():
    return jsonify({'object': 'list', 'data': [{'id': 'qwen3-reranker-4b', 'object': 'model'}]})


@app.post('/v1/rerank')
def rerank():
    data = request.get_json(silent=True) or {}
    inp = data.get('input') or {}
    query = inp.get('query') or data.get('query') or ''
    documents = inp.get('documents') or data.get('documents') or []
    if not query or not documents:
        return jsonify(error='query/documents 为空'), 400
    futures = [POOL.submit(score_pair, query, d) for d in documents]
    results, degraded = [], 0
    for i, f in enumerate(futures):
        score, fallback = f.result()
        degraded += int(fallback)
        results.append({'index': i, 'relevance_score': float(score)})
    with STATS['lock']:
        STATS['calls'] += 1
        STATS['fallbacks'] += degraded
    return jsonify({'output': {'results': results},
                    'meta': {'model': 'qwen3-reranker-4b', 'degraded': degraded,
                             'docs': len(documents)}})


def main():
    global LLM_URL, POOL
    ap = argparse.ArgumentParser(description='Qwen3-Reranker DashScope 兼容层')
    ap.add_argument('--port', type=int, default=8096)
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--llm', default='http://127.0.0.1:8097')
    ap.add_argument('--workers', type=int, default=8)
    a = ap.parse_args()
    LLM_URL = a.llm.rstrip('/')
    POOL = ThreadPoolExecutor(max_workers=a.workers)
    print('[qwen3-rerank] listening :%d -> %s (workers=%d)' % (a.port, LLM_URL, a.workers), flush=True)
    app.run(host=a.host, port=a.port, threaded=True)


if __name__ == '__main__':
    main()
