# -*- coding: utf-8 -*-
"""M0 端到端验收：真实模型链路跑一轮问答，验证 SSE v2 事件 / 回放 / qa_trace 落库。

用法（在项目根目录）：
  /home/adminroot/miniconda3/envs/smart-help-web/bin/python tests/e2e_m0_events.py
  ... --question '自定义问题' --keep   # --keep 保留测试数据，默认清理

说明：直接 in-process 调 Flask（create_app + test_client），不走 gunicorn；
但链路、模型、Kingbase、chain_trace、SSE 帧全部真实。生产重载前后都可用于验证。
"""
import argparse
import hashlib
import json
import os
import secrets
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_Q = ('家庭所在地江西省萍乡市莲花县。户内家庭成员2人；成员1，62岁，非健康，'
             '弱劳动力或半劳动力，二级残疾，肢体残疾，当月新增残疾人员；成员2，60岁，'
             '健康，全劳动力。全家医保参保0人；')


def load_env():
    path = ROOT / 'deploy' / 'web.env'
    if path.is_file():
        for line in path.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                v = v.strip()
                # deploy/start.sh 用 shell source 会去掉引号，这里保持一致
                if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
                    v = v[1:-1]
                os.environ.setdefault(k.strip(), v)


def parse_sse(raw):
    frames = []
    for block in raw.split('\n\n'):
        if not block.strip():
            continue
        frame = {'id': None, 'event': None, 'data': None, 'ping': False}
        for line in block.split('\n'):
            if line.startswith(':'):
                frame['ping'] = True
            elif line.startswith('id: '):
                frame['id'] = line[4:].strip()
            elif line.startswith('event: '):
                frame['event'] = line[7:].strip()
            elif line.startswith('data: '):
                try:
                    frame['data'] = json.loads(line[6:])
                except ValueError:
                    frame['data'] = line[6:]
        frames.append(frame)
    return frames


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--question', default=DEFAULT_Q)
    ap.add_argument('--keep', action='store_true')
    args = ap.parse_args()

    load_env()
    sys.path.insert(0, str(ROOT / 'src'))
    import config  # noqa: F401
    import jwt
    from app import create_app
    from models import db, AdminUser, LoginSession, QaLog, QaTrace

    app = create_app()
    session_id = 'm0e2e-' + uuid.uuid4().hex[:10]
    login_sid = secrets.token_urlsafe(32)
    sqlite_session = None
    with app.app_context():
        u = AdminUser.query.filter_by(username='admin').first()
        if not u or not u.active or u.must_change_password or not u.real_name:
            print('FAIL: 没有可用于 e2e 的 admin 账号')
            return 2
        now = int(time.time())
        csrf = secrets.token_urlsafe(32)
        db.session.add(LoginSession(id=login_sid, user_id=u.id, expires_at=now + 900,
                                    revoked=False,
                                    csrf_hash=hashlib.sha256(csrf.encode()).hexdigest()))
        db.session.commit()
        token = jwt.encode(dict(sub=str(u.id), jti=login_sid, ver=u.auth_version,
                                iat=now, exp=now + 900, iss='smart-help', aud='smart-help-web'),
                           app.config['AUTH_JWT_SECRET'], algorithm='HS256')

    client = app.test_client()
    headers = {'Authorization': 'Bearer ' + token}
    payload = {'question': args.question, 'session_id': session_id, 'source': 'm0-e2e'}
    t0 = time.monotonic()
    try:
        resp = client.post('/api/qa/stream', json=payload, headers=headers)
        raw = resp.get_data(as_text=True)
    except Exception as exc:  # 服务端异常要原样暴露，不能吞
        print('FAIL: 请求 /api/qa/stream 异常: %s: %s' % (type(exc).__name__, exc))
        return 1
    elapsed = time.monotonic() - t0

    frames = parse_sse(raw)
    datas = [f['data'] for f in frames if isinstance(f.get('data'), dict)]
    errors = [d for d in datas if d.get('type') == 'error']
    progress = [d for d in datas if d.get('type') == 'progress']
    enveloped = [d for d in progress if isinstance(d.get('event'), dict)]
    user_env = [d['event'] for d in enveloped if d['event'].get('exposure') == 'user']
    session = next((d for d in datas if d.get('type') == 'session'), None)
    done = next((d for d in datas if d.get('type') == 'done'), None)
    sse_v2 = sum(1 for f in frames if f.get('id') and f.get('event'))
    pings = sum(1 for f in frames if f.get('ping'))

    answer_deltas = [d for d in datas if d.get('type') == 'answer.delta']
    answer_full = next((d for d in datas if d.get('type') == 'answer'), None)
    delta_text = ''.join(str(d.get('content') or '') for d in answer_deltas)
    texts = [str(e.get('text') or '') for e in user_env]
    result = {
        'status': 'unknown', 'elapsed_s': round(elapsed, 1), 'http': resp.status_code,
        'frames': len(frames), 'sse_v2_frames': sse_v2, 'pings': pings,
        'progress': len(progress), 'enveloped': len(enveloped),
        'user_events': len(user_env), 'event_kinds': sorted({e.get('kind') for e in user_env if e.get('kind')}),
        'has_session_trace': bool(session and session.get('trace_id')),
        'done': bool(done), 'qa_id': (done or {}).get('qa_id'),
        'trace_id': (done or {}).get('trace_id') or (session or {}).get('trace_id'),
        'error_frames': [e.get('msg') for e in errors],
        # P0：事件人话文本 + 答案逐段（与前端时间线/流式渲染同源）
        'text_missing': sum(1 for t in texts if not t),
        'text_has_stage_word': sum(1 for t in texts if '阶段' in t),
        'text_too_long': sum(1 for t in texts if len(t) > 80),
        'text_samples': [t for t in texts if t][:8],
        'answer_deltas': len(answer_deltas),
        'answer_delta_join_ok': bool(answer_deltas) and answer_full is not None
                                and delta_text == answer_full.get('content'),
        'answer_delta_last_done': bool(answer_deltas) and answer_deltas[-1].get('done') is True,
    }

    failures = []
    if resp.status_code != 200:
        failures.append('HTTP %s' % resp.status_code)
    if errors:
        failures.append('收到 error 帧')
    if not session or not session.get('trace_id'):
        failures.append('session 帧缺 trace_id')
    if not done:
        failures.append('缺 done 帧')
    if not enveloped:
        failures.append('progress 未携带 event envelope')
    if len(user_env) < 20:
        failures.append('用户可见事件过少（验收>=20）: %s' % len(user_env))
    if sse_v2 != len(progress):
        failures.append('progress 帧未全部带 id/event（v2=%s/%s）' % (sse_v2, len(progress)))
    if result['text_missing']:
        failures.append('用户可见事件缺人话 text: %s 条' % result['text_missing'])
    if result['text_has_stage_word']:
        failures.append('事件文本出现"阶段"字样，违反去阶段化')
    if result['text_too_long']:
        failures.append('事件文本超过 80 字: %s 条' % result['text_too_long'])
    if not answer_deltas:
        failures.append('没有 answer.delta 帧（答案未逐段流式）')
    elif not result['answer_delta_join_ok']:
        failures.append('answer.delta 拼接与最终 answer 不一致')
    else:
        ordered = [d for d in datas if d.get('type') in ('answer.delta', 'answer')]
        if ordered[-1].get('type') != 'answer':
            failures.append('answer 全文帧不在逐段帧之后')

    qa_id = result['qa_id']
    if qa_id:
        with app.app_context():
            q = db.session.get(QaLog, qa_id)
            tr = db.session.get(QaTrace, qa_id)
            result['qa_log_status'] = q.status if q else None
            result['qa_trace_id'] = tr.trace_id if tr else None
            # M0 验收：真实一轮的模型调用预算与卡片数（来自冻结快照，非响应体推断）。
            try:
                snap = json.loads(q.match_json) if (q and q.match_json) else {}
            except Exception:
                snap = {}
            diag = snap.get('diagnostics') or {}
            cov = snap.get('coverage') or {}
            result['calls'] = diag.get('calls')
            result['seconds'] = diag.get('seconds')
            result['cards'] = len(snap.get('table_recs') or [])
            result['rule_state'] = ((cov.get('rule_engine') or {}).get('state'))
            if isinstance(result['calls'], int) and result['calls'] > 60:
                failures.append('模型调用超预算: %s > 60' % result['calls'])
            if result.get('rule_state') == 'used' and not result['cards']:
                failures.append('规则路径 0 卡（地区/规则覆盖回归）')
        if not tr:
            failures.append('qa_trace 无映射行')
        elif tr.trace_id != result['trace_id']:
            failures.append('qa_trace.trace_id 与响应不一致')
        rv = client.get('/api/qa/%s/events?since=0' % qa_id, headers=headers)
        replay = rv.get_json() or {}
        result['replay_http'] = rv.status_code
        result['replay_events'] = len(replay.get('events') or [])
        kinds = [((e.get('kind') or (e.get('event') or {}).get('kind')) if isinstance(e, dict) else None)
                 for e in (replay.get('events') or [])]
        result['replay_has_turn_complete'] = 'turn.complete' in kinds
        replay_env = [e for e in (replay.get('events') or []) if isinstance(e, dict) and e.get('kind')]
        replay_user = [e for e in replay_env if e.get('exposure') == 'user']
        result['replay_user_events'] = len(replay_user)
        result['replay_text_missing'] = sum(1 for e in replay_user if not e.get('text'))
        if replay_user and result['replay_text_missing']:
            failures.append('回放事件缺 text: %s 条' % result['replay_text_missing'])
        # P3-1：实时与回放的用户事件必须同源同量（turn.complete 不能在退订后才发）。
        if not result['replay_has_turn_complete']:
            failures.append('回放缺 turn.complete')
        if replay_user and len(replay_user) != len(user_env):
            failures.append('回放用户事件数(%d) != 实时用户事件数(%d)' % (len(replay_user), len(user_env)))
        if rv.status_code != 200 or not replay.get('ok'):
            failures.append('回放接口失败')
        if not replay.get('events'):
            failures.append('回放事件为空')
        if (replay.get('trace_id') or '') != (result['trace_id'] or ''):
            failures.append('回放 trace_id 不一致')

    # 清理测试数据（登录会话/queries/qa_trace），保留 JSONL 日志便于排查
    if not args.keep:
        with app.app_context():
            try:
                db.session.query(QaLog).filter(QaLog.session_id == session_id).delete()
                db.session.query(QaTrace).filter(QaTrace.qa_id.in_(
                    [qa_id] if qa_id else [])).delete(synchronize_session=False)
                db.session.query(LoginSession).filter(LoginSession.id == login_sid).delete()
                db.session.commit()
            except Exception as exc:
                db.session.rollback()
                result['cleanup_error'] = str(exc)[:200]

    result['failures'] = failures
    result['status'] = 'PASS' if not failures else 'FAIL'
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not failures else 1


if __name__ == '__main__':
    sys.exit(main())
