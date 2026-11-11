"""懒编译 worker：用到哪个县，编哪个县。

为什么要懒编译：全库一次性编译约 7.4 小时 GPU，而大部分县短期不会被问到。
改成首次请求登记任务、后台编译该县 60-70 条（约 6 分钟）、之后毫秒级命中。

用法：
  python rule_worker.py --status             查看规则库与任务队列
  python rule_worker.py --ensure 万安        模拟生产钩子：该县就绪？没有则登记任务
  python rule_worker.py --once               处理一个待编译任务后退出
  python rule_worker.py --watch              常驻，轮询任务队列
  python rule_worker.py --prewarm 莲花 吉安  立即编译（不等别人问）
  python rule_worker.py --check-stale 莲花   检查政策改版导致的过期规则
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import compile_rules  # noqa: E402
from policy_core import rule_eval, rule_store  # noqa: E402


def ensure_rules(region, min_expected=20):
    """生产钩子：该县规则是否就绪。

    返回 (rules, state)：
      state=ready           → 直接求值（含"该县本来就没有县级政策、编译已完成"的情形）
      其他                  → 'pending'/'running'/'failed'，调用方本次走旧链路兜底
    """
    try:
        rules = rule_eval.load_rules(region)
    except Exception as exc:
        return [], 'store_error:' + str(exc)[:80]
    # 与 rule_flow.try_rule_run 共用同一判定：编译任务 done 后，省级规则就是该县
    # 适用政策的全集，不再因为县级规则不足 20 条而永远回退旧链路。
    from policy_core import rule_readiness
    decision = rule_readiness.assess(rules, rule_store.job(region), min_local=min_expected)
    if decision['ready']:
        return rules, 'ready'
    state = rule_store.enqueue(region, message='首次请求触发，待编译地区适用规则')
    return rules, state


def check_stale(region):
    """政策文本变了 → 该县规则过期条数。用于改版后触发重编译。"""
    from policy_core.fields import dictionary
    from policy_core.full_settings import limits
    _packs, unique, _stats = compile_rules.packs_for_county(
        region, compile_rules.region_text_for(region), dict(limits(), per_policy_limit=999))
    current = {p['_text_hash'] for p in unique}
    stored = {r.get('text_hash') for r in rule_store.load_rules(region) if r.get('text_hash')}
    return dict(region=region, current=len(current), stored=len(stored),
                stale=len(stored - current), fresh=len(current & stored))


def work_once(limit=0, workers=4):
    region = rule_store.claim()
    if not region:
        return None
    print('认领任务:', region, flush=True)
    try:
        result = compile_rules.compile_regions([region], limit=limit, workers=workers)
        rule_store.finish(region, 'done', total=result.get('total', 0),
                          done=result.get('done', 0), failed=result.get('failed', 0),
                          message='完成')
        print('任务完成:', region, result, flush=True)
        return region
    except Exception as exc:
        rule_store.finish(region, 'failed', message='%s: %s' % (type(exc).__name__, str(exc)[:300]))
        print('任务失败:', region, type(exc).__name__, str(exc)[:200], flush=True)
        return region


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--status', action='store_true')
    ap.add_argument('--ensure', default='')
    ap.add_argument('--once', action='store_true')
    ap.add_argument('--watch', action='store_true')
    ap.add_argument('--interval', type=int, default=30)
    ap.add_argument('--prewarm', nargs='*', default=[])
    ap.add_argument('--check-stale', default='')
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--workers', type=int, default=4)
    ap.add_argument('--shadow-summary', action='store_true')
    a = ap.parse_args()

    if a.shadow_summary:
        from policy_core.shadow import summary, LOG_PATH
        print('日志:', LOG_PATH)
        print(summary())
        return
    if a.status:
        print('规则库:', rule_store.stats())
        for job in rule_store.jobs():
            print('  任务 %-6s %-8s %d/%d 失败%d 尝试%d %s' % (
                job['region'], job['state'], job['done'], job['total'],
                job['failed'], job['attempts'], (job['message'] or '')[:40]))
        return
    if a.ensure:
        rules, state = ensure_rules(a.ensure)
        local = [r for r in rules if (r.get('region_scope') or '') not in ('*', '')]
        print('区域 %s: 状态=%s 规则=%d（本地 %d）' % (a.ensure, state, len(rules), len(local)))
        return
    if a.check_stale:
        print(check_stale(a.check_stale))
        return
    if a.prewarm:
        for region in a.prewarm:
            rules, state = ensure_rules(region)
            if state == 'ready':
                print('%s 已就绪（%d 条），跳过' % (region, len(rules)))
                continue
            print('%s 开始编译…' % region)
            result = compile_rules.compile_regions([region], limit=a.limit, workers=a.workers)
            rule_store.finish(region, 'done', total=result.get('total', 0),
                              done=result.get('done', 0), failed=result.get('failed', 0), message='预热完成')
            print('%s 完成: %s' % (region, result))
        return
    if a.once:
        print('处理结果:', work_once(limit=a.limit, workers=a.workers))
        return
    if a.watch:
        print('worker 启动，每 %ds 轮询一次（Ctrl-C 退出）' % a.interval, flush=True)
        while True:
            done = work_once(limit=a.limit, workers=a.workers)
            if not done:
                time.sleep(a.interval)
        return
    ap.print_help()


if __name__ == '__main__':
    main()
