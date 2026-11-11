# -*- coding: utf-8 -*-
"""政策生命周期 outbox 消费者（P0 修复）。

为什么需要：生命周期（停用/归档/删除/发布/回滚）与批量导入都只写业务表 + outbox 事件，
侧效应（向量重建、规则失效、图谱入队、缓存失效）由本 worker 幂等重放。此前后台同步
写的是旧表 policy_vector，而生产检索读 policy_vector_qwen3，导致导入/停用不生效；
kb_rag.sync_policy/remove_policy 现已参数化为 _VECTOR_TABLE（= qwen3），这里直接复用。

设计约束：
- 默认关闭（POLICY_WORKER=0），关闭时 app.py 不启线程、CLI 不连库，现网零行为变化；
- 事件消费失败走 outbox.fail 重试，达到 OUTBOX_MAX_ATTEMPTS 进 dead，不无限重试；
- 每个 handler 自己打开短事务，失败只影响该事件；worker 异常不打断进程；
- 无 handler 的事件由 outbox.drain 直接置 dead（部署配置错误）。
"""
import logging
import os
import threading
import time

from . import outbox

VERSION = 'policy-worker-1'

_log = logging.getLogger(__name__)
_thread = None
_rule_thread = None
_guard = threading.Lock()

_ON_VALUES = ('1', 'on', 'true', 'yes')


def enabled():
    """POLICY_WORKER=1/on/true/yes 才启动；默认 0。"""
    return str(os.environ.get('POLICY_WORKER', '') or '').strip().lower() in _ON_VALUES


def interval():
    try:
        value = int(str(os.environ.get('POLICY_WORKER_INTERVAL', '') or '').strip())
    except (TypeError, ValueError):
        return 15
    return value if value >= 1 else 15


def batch_size():
    try:
        value = int(str(os.environ.get('POLICY_WORKER_BATCH', '') or '').strip())
    except (TypeError, ValueError):
        return 10
    return value if value >= 1 else 10


def rule_compile_enabled():
    """POLICY_WORKER_RULE_COMPILE=1/on/true/yes 才启动规则编译线程；默认 0。

    默认关闭的原因：单县规则编译约 6 分钟 GPU，必须由运维显式打开，现网零行为变化。
    """
    return str(os.environ.get('POLICY_WORKER_RULE_COMPILE', '') or '').strip().lower() in _ON_VALUES


def rule_compile_interval():
    """规则编译轮询间隔（秒），默认 60；非法值回退 60。"""
    try:
        value = int(str(os.environ.get('POLICY_WORKER_RULE_COMPILE_INTERVAL', '') or '').strip())
    except (TypeError, ValueError):
        return 60
    return value if value >= 1 else 60


def rule_compile_batch():
    """每轮最多编译几个县，默认 1（GPU 单县约 6 分钟，避免长时间占满显存）。"""
    try:
        value = int(str(os.environ.get('POLICY_WORKER_RULE_COMPILE_BATCH', '') or '').strip())
    except (TypeError, ValueError):
        return 1
    return value if value >= 1 else 1


# ---------------- handlers ----------------

def _code(payload):
    return str((payload or {}).get('policy_code') or '').strip()


def _policy_row(policy_code):
    """读政策当前状态；kb_rag 库连接不可用时返回 None。"""
    import kb_rag
    from sqlalchemy import text
    db = kb_rag._db()
    with db.connect() as conn:
        row = conn.execute(text(
            'SELECT policy_name, doc_ref, cur_version, status FROM policy WHERE policy_code=:pc'),
            {'pc': policy_code}).fetchone()
    return row


def handle_vector_rebuild(payload):
    """policy.vector_rebuild：按政策当前状态重建/清理 qwen3 向量索引。

    - create/update/publish/rollback → kb_rag.sync_policy 重新切块+嵌入+覆盖写；
    - suspend/archive/delete/hard_delete → 直接清索引（检索层也按 status 过滤，双保险）；
    - 政策已不存在 → 清残留索引；
    - 非"在用"状态一律不重建，避免停用政策被写回可检索索引。
    """
    import kb_rag
    code = _code(payload)
    if not code:
        return {'ok': False, 'msg': '缺少 policy_code'}
    action = str(payload.get('action') or 'rebuild')
    if action in ('delete', 'hard_delete', 'suspend', 'archive'):
        return kb_rag.remove_policy(code)
    row = _policy_row(code)
    if row is None:
        return kb_rag.remove_policy(code)
    name, doc_ref, version, status = row[0], row[1] or '', row[2] or '', row[3]
    if status not in ('在用',):
        return kb_rag.remove_policy(code)
    return kb_rag.sync_policy(code, name, doc_ref, status, version, None)


def handle_rule_invalidate(payload):
    """policy.rule_invalidate：把该政策涉及的县区重新排进规则编译队列。

    不删已编译规则：读侧已按 policy.status 过滤（T21），删行只会让规则路径暂时缺规则；
    重新入队后由 rule_worker/autopilot 按最新政策文本覆盖编译（rule_store.save 幂等 upsert）。
    region_scope='*' 是省级/全国规则，没有可入队的县，跳过但如实返回。
    """
    from . import rule_store
    from sqlalchemy import text
    code = _code(payload)
    if not code:
        return {'ok': False, 'msg': '缺少 policy_code'}
    with rule_store.engine().connect() as conn:
        rows = conn.execute(text(
            'SELECT DISTINCT region_scope FROM policy_rule WHERE policy_code=:code'),
            {'code': code}).fetchall()
    regions = list(dict.fromkeys(r[0] for r in rows if r[0] and r[0] != '*'))
    queued = []
    for region in regions:
        try:
            state = rule_store.enqueue(region, message='政策变更待重编译: %s' % code, force=True)
        except Exception:  # noqa: BLE001 - 单县入队失败不阻塞其它县
            _log.exception('规则重编译入队失败 region=%s policy=%s', region, code)
            continue
        if state == 'pending':
            queued.append(region)
    return {'ok': True, 'policy_code': code, 'regions': len(regions), 'queued': len(queued),
            'global_rules': sum(1 for r in rows if r[0] == '*')}


def handle_kg_sync(payload):
    """policy.kg_sync：写入 kg_update_queue，由图谱 worker 重建（表不存在则如实跳过）。"""
    from sqlalchemy import inspect, text
    from . import rule_store
    from datetime import datetime
    code = _code(payload)
    if not code:
        return {'ok': False, 'msg': '缺少 policy_code'}
    eng = rule_store.engine()
    with eng.begin() as conn:
        if not inspect(conn).has_table('kg_update_queue'):
            return {'ok': True, 'skipped': 'kg_update_queue_missing'}
        version = conn.execute(text('SELECT cur_version FROM policy WHERE policy_code=:pc'),
                               {'pc': code}).scalar() or ''
        now = datetime.now().isoformat(timespec='seconds')
        conn.execute(text(
            'INSERT INTO kg_update_queue(policy_code,policy_version,requested_at,reason,status) '
            "VALUES(:code,:version,:ts,:reason,'pending') ON CONFLICT(policy_code) DO UPDATE SET "
            "policy_version=:version,requested_at=:ts,reason=:reason,status='pending'"),
            {'code': code, 'version': version, 'ts': now, 'reason': '生命周期 outbox 同步'})
    return {'ok': True, 'policy_code': code}


def handle_cache_invalidate(payload):
    """policy.cache_invalidate：清向量/匹配/图谱内存缓存；单个失败不影响其余。"""
    cleared = []
    for module_name, attr in (('kb_rag', '_invalidate_cache'),
                              ('smart_matcher_kb', 'invalidate_db_cache'),
                              ('kg_retrieval', 'refresh_cache')):
        try:
            module = __import__(module_name)
            getattr(module, attr)()
            cleared.append(module_name)
        except Exception:  # noqa: BLE001 - 缓存清理失败不能让事件永久重试
            _log.warning('缓存清理失败 module=%s', module_name, exc_info=True)
    return {'ok': True, 'cleared': cleared}


def default_handlers():
    return {
        'policy.vector_rebuild': handle_vector_rebuild,
        'policy.rule_invalidate': handle_rule_invalidate,
        'policy.kg_sync': handle_kg_sync,
        'policy.cache_invalidate': handle_cache_invalidate,
    }


# ---------------- 消费循环 ----------------

def process_once(engine_obj=None, handlers=None, limit=None):
    """消费一批 pending 事件；返回 outbox.drain 的统计 + 队列计数。"""
    if engine_obj is None:
        from . import rule_store
        engine_obj = rule_store.engine()
    size = limit or batch_size()
    with engine_obj.begin() as conn:
        outbox.ensure_schema(conn)
        before = outbox.counts(conn)
        summary = outbox.drain(conn, default_handlers() if handlers is None else handlers,
                               limit=size)
        after = outbox.counts(conn)
    summary['before'] = before
    summary['after'] = after
    return summary


# ---------------- 规则编译执行器（T28，默认关） ----------------
# 为什么单独线程：outbox handler handle_rule_invalidate 只负责入队（不能让 drain 阻塞在
# GPU 编译上），真正编译由这里的常驻线程按 policy_rule_job 队列消费；生产此前没有常驻
# 编译器，队列停在 pending，生命周期变更对规则链路不生效。

def compile_once(engine_obj=None, batch=None, work_once=None):
    """编译最多 batch 个已入队县区规则；返回 {claimed, compiled, failed[, error]}。

    - work_once 懒加载 src/rule_worker.work_once：该模块依赖 compile_rules/GPU 栈，
      import 较重，只在真正执行编译时加载；也可由单测注入 fake。
    - engine_obj 保持与 process_once 同签名（编译链路内部用 rule_store.engine()），
      这里不直接使用，留给将来注入引擎。
    - work_once 返回 None 表示队列已空：立即停止本轮，不空转。
    - work_once 抛异常（连库/编译栈故障）不打断线程：failed+1 并结束本轮；不自动
      重试——失败 job 由 rule_worker 置 failed，等下一次 enqueue(force=True) 重新排队，
      避免故障时高频猛打 GPU/数据库。
    """
    size = rule_compile_batch() if batch is None else max(1, int(batch))
    if work_once is None:
        import rule_worker
        work_once = rule_worker.work_once
    claimed = []
    compiled = 0
    failed = 0
    error = None
    for _ in range(size):
        try:
            region = work_once()
        except Exception as exc:  # noqa: BLE001 - 单轮失败不能让后台线程退出
            failed += 1
            error = '%s: %s' % (type(exc).__name__, str(exc)[:300])
            _log.exception('规则编译执行失败')
            break
        if not region:
            break
        claimed.append(region)
        compiled += 1
    result = {'claimed': claimed, 'compiled': compiled, 'failed': failed}
    if error:
        result['error'] = error
    return result


def _rule_compile_step(engine_obj=None):
    """执行一轮编译并记录日志；异常在这里兜住，保证循环不退出。"""
    try:
        summary = compile_once(engine_obj)
    except Exception:  # noqa: BLE001 - 极端情况（如 import 失败）也不退出线程
        _log.exception('规则编译执行器单轮失败')
        return {'claimed': [], 'compiled': 0, 'failed': 1}
    if summary.get('compiled') or summary.get('failed'):
        _log.info('规则编译执行器：%s', summary)
    return summary


def _rule_compile_loop(engine_obj=None):
    while True:
        _rule_compile_step(engine_obj)
        time.sleep(rule_compile_interval())


def start(engine_obj=None):
    """在应用进程里启动后台线程（幂等）。

    POLICY_WORKER=1 启动 outbox 消费线程；POLICY_WORKER_RULE_COMPILE=1 额外启动
    rule-compiler 线程（消费 policy_rule_job 队列）。两个开关都关时返回 False，
    不建线程、不连库，保持现网零行为变化。
    """
    global _thread, _rule_thread
    if not enabled() and not rule_compile_enabled():
        return False
    started = False
    with _guard:
        if enabled() and (_thread is None or not _thread.is_alive()):
            thread = threading.Thread(target=_loop, args=(engine_obj,), name='policy-worker', daemon=True)
            thread.start()
            _thread = thread
            started = True
        if rule_compile_enabled() and (_rule_thread is None or not _rule_thread.is_alive()):
            rule_thread = threading.Thread(target=_rule_compile_loop, args=(engine_obj,),
                                           name='rule-compiler', daemon=True)
            rule_thread.start()
            _rule_thread = rule_thread
            started = True
    return started


def stop():
    global _thread, _rule_thread
    with _guard:
        _thread = None
        _rule_thread = None


def _loop(engine_obj=None):
    while True:
        try:
            summary = process_once(engine_obj)
            if summary.get('processed'):
                _log.info('policy worker 消费 outbox: %s', summary)
        except Exception:  # noqa: BLE001 - 后台线程不能因单批异常退出
            _log.exception('policy worker 消费失败')
        time.sleep(interval())


def run_forever(max_batches=None, engine_obj=None, handlers=None, sleep_seconds=None):
    """CLI 入口：阻塞消费；max_batches 用于一次性/测试。返回累计统计。"""
    total = {'batches': 0, 'processed': 0, 'done': 0, 'retried': 0, 'dead': 0, 'skipped': 0}
    while True:
        summary = process_once(engine_obj, handlers=handlers)
        total['batches'] += 1
        for key in ('processed', 'done', 'retried', 'dead', 'skipped'):
            total[key] += int(summary.get(key) or 0)
        if max_batches is not None and total['batches'] >= max_batches:
            return total
        time.sleep(interval() if sleep_seconds is None else sleep_seconds)


def main(argv=None):
    """CLI 入口：默认消费一批 outbox；--rule-compile-once 编译一个县后退出。

    返回进程退出码（0 成功 / 2 对应开关未开启），便于单测直接调用。
    """
    import argparse
    import json
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once', action='store_true', help='只消费一批后退出（默认）')
    parser.add_argument('--limit', type=int, default=None)
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--rule-compile-once', action='store_true',
                        help='编译一个已入队县区规则后退出（需 POLICY_WORKER_RULE_COMPILE=1）')
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    if args.rule_compile_once:
        if not rule_compile_enabled():
            print('POLICY_WORKER_RULE_COMPILE 未开启（默认 0），未连接数据库、未启动编译。'
                  '设置该开关后重试。')
            return 2
        result = compile_once()
        print(json.dumps(result, ensure_ascii=False, default=str) if args.json else result)
        return 0
    if not enabled():
        print('POLICY_WORKER 未开启（默认 0），未连接数据库。设置 POLICY_WORKER=1 后重试。')
        return 2
    result = process_once(limit=args.limit)
    print(json.dumps(result, ensure_ascii=False, default=str) if args.json else result)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
