# -*- coding: utf-8 -*-
"""P1 端到端验收：输入理解层四路由（chat / consultation / eligibility / clarify）。

用法（项目根目录）：
  /home/adminroot/miniconda3/envs/smart-help-web/bin/python tests/e2e_p1_intake.py

说明：in-process Flask test_client，链路/模型/Kingbase 全部真实；每轮独立 session，
默认跑完清理 qa 数据（--keep 保留）。断言以 SSE 实时事件中的 intake.parsed 为准，
卡片数取 qa_log.match_json 冻结快照，不响应体推断。
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
HOUSEHOLD_Q = ('家庭所在地江西省萍乡市莲花县。户内家庭成员2人；成员1，62岁，非健康，'
               '弱劳动力或半劳动力，二级残疾，肢体残疾，当月新增残疾人员；成员2，60岁，'
               '健康，全劳动力。全家医保参保0人；')
CASES = [
    ('chat', '你好，你是谁？', 0),
    ('consultation', '低收入家庭认定条件是什么？', 0),
    ('eligibility', HOUSEHOLD_Q, 1),
    ('clarify', '我想申请补贴', 0),
]


def load_env():
    path = ROOT / 'deploy' / 'web.env'
    if path.is_file():
        for line in path.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                v = v.strip()
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
    ap.add_argument('--keep', action='store_true')
    args = ap.parse_args()

    load_env()
    sys.path.insert(0, str(ROOT / 'src'))
    import config  # noqa: F401
    import jwt
    from app import create_app
    from models import db, AdminUser, LoginSession, QaLog, QaTrace

    app = create_app()
    login_sid = secrets.token_urlsafe(32)
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
    failures, results = [], []
    qa_ids = []
    for expected_route, question, min_cards in CASES:
        session_id = 'p1e2e-' + uuid.uuid4().hex[:10]
        t0 = time.monotonic()
        try:
            resp = client.post('/api/qa/stream',
                               json={'question': question, 'session_id': session_id, 'source': 'p1-e2e'},
                               headers=headers)
            raw = resp.get_data(as_text=True)
        except Exception as exc:
            failures.append('%s: 请求异常 %s: %s' % (expected_route, type(exc).__name__, exc))
            results.append({'expected': expected_route, 'status': 'EXC'})
            continue
        elapsed = time.monotonic() - t0
        frames = parse_sse(raw)
        datas = [f['data'] for f in frames if isinstance(f.get('data'), dict)]
        errors = [d for d in datas if d.get('type') == 'error']
        session = next((d for d in datas if d.get('type') == 'session'), None)
        done = next((d for d in datas if d.get('type') == 'done'), None)
        user_env = [d['event'] for d in datas
                    if d.get('type') == 'progress' and isinstance(d.get('event'), dict)
                    and d['event'].get('exposure') == 'user']
        intake_envs = [e for e in user_env if e.get('kind') == 'intake.parsed']
        detail = intake_envs[-1].get('detail') if intake_envs else {}
        route = (detail or {}).get('route')
        kinds = [e.get('kind') for e in user_env]
        qa_id = (done or {}).get('qa_id')
        cards, matched, kind = None, (done or {}).get('matched'), None
        if qa_id:
            qa_ids.append(qa_id)
            with app.app_context():
                q = db.session.get(QaLog, qa_id)
                try:
                    snap = json.loads(q.match_json) if (q and q.match_json) else {}
                except Exception:
                    snap = {}
                cards = len(snap.get('table_recs') or [])
                kind = snap.get('kind') or snap.get('route') or snap.get('mode')
        row = {'expected': expected_route, 'route': route, 'http': resp.status_code,
               'elapsed_s': round(elapsed, 1), 'events': len(datas), 'user_events': len(user_env),
               'intake_events': len(intake_envs), 'errors': [e.get('msg') for e in errors],
               'done': bool(done), 'qa_id': qa_id, 'cards': cards,
               'matched': len(matched or []), 'match_kind': kind,
               'topic': (detail or {}).get('topic'), 'source': (detail or {}).get('source'),
               'clarify_events': kinds.count('intake.clarify')}
        # 断言
        if resp.status_code != 200:
            failures.append('%s: HTTP %s' % (expected_route, resp.status_code))
        if errors:
            failures.append('%s: error 帧 %s' % (expected_route, [e.get('msg') for e in errors]))
        if not done:
            failures.append('%s: 缺 done 帧' % expected_route)
        if not intake_envs:
            failures.append('%s: 没有 intake.parsed 实时事件' % expected_route)
        elif route != expected_route:
            failures.append('%s: intake 路由=%s，期望 %s' % (expected_route, route, expected_route))
        if cards is None:
            failures.append('%s: 未拿到 qa_log 快照' % expected_route)
        elif cards < min_cards:
            failures.append('%s: 卡片数 %s < %s' % (expected_route, cards, min_cards))
        if expected_route in ('chat', 'consultation', 'clarify') and (matched or []):
            failures.append('%s: 不应匹配政策，却返回 %s 条' % (expected_route, len(matched or [])))
        if expected_route == 'clarify':
            if not row['clarify_events']:
                failures.append('clarify: 缺 intake.clarify 事件')
            if elapsed > 30:
                failures.append('clarify: 耗时 %.1fs > 30s（应走零匹配追问）' % elapsed)
        results.append(row)
        with app.app_context():
            if args.keep:
                continue
            try:
                db.session.query(QaLog).filter(QaLog.session_id == session_id).delete()
                db.session.query(QaTrace).filter(QaTrace.qa_id.in_([qa_id] if qa_id else [])).delete(
                    synchronize_session=False)
                db.session.commit()
            except Exception as exc:
                db.session.rollback()
                failures.append('%s: 清理失败 %s' % (expected_route, str(exc)[:120]))

    if not args.keep:
        with app.app_context():
            try:
                db.session.query(LoginSession).filter(LoginSession.id == login_sid).delete()
                db.session.commit()
            except Exception:
                db.session.rollback()

    print(json.dumps({'status': 'PASS' if not failures else 'FAIL',
                      'cases': results, 'failures': failures}, ensure_ascii=False, indent=2))
    return 0 if not failures else 1


if __name__ == '__main__':
    sys.exit(main())
