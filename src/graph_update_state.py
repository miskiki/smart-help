"""Durable graph rebuild queue; prevents changed ledger facts using stale topology."""
from sqlalchemy import inspect, text


def mark_pending(session, policy, reason):
    if not inspect(session.get_bind()).has_table('kg_update_queue'):
        return
    # Acquire ledger write locks before queue locks, matching the publisher lock order.
    session.flush()
    from datetime import datetime
    session.execute(text('INSERT INTO kg_update_queue(policy_code,policy_version,requested_at,reason,status) '
        "VALUES(:code,:version,:ts,:reason,'pending') ON CONFLICT(policy_code) DO UPDATE SET "
        "policy_version=:version,requested_at=:ts,reason=:reason,status='pending'"),
        {'code':policy.policy_code,'version':policy.cur_version,
         'ts':datetime.now().isoformat(timespec='seconds'),'reason':reason})


def pending(connection):
    if not inspect(connection).has_table('kg_update_queue'):
        return []
    return [dict(r) for r in connection.execute(text("SELECT policy_code,policy_version,reason FROM kg_update_queue WHERE status IN ('pending','failed') ORDER BY policy_code")).mappings()]


def status(connection, code):
    if not code or not inspect(connection).has_table('kg_update_queue'):
        return {'status':'unavailable'}
    row=connection.execute(text('SELECT * FROM kg_update_queue WHERE policy_code=:code'),{'code':code}).mappings().first()
    return dict(row) if row else {'policy_code':code,'status':'ready','reason':'已纳入正式图谱基线'}
