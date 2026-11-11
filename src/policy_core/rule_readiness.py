# -*- coding: utf-8 -*-
"""规则路径可用性判定。

为什么需要：政策库按县区收录政策，但并非每个县区都有县级政策。对只有省级政策的
县区，旧门槛（县级规则 >= 20）永远不满足，每次请求都回退 170-240s 的全模型链路；
而编译任务已完成（done）说明该地区适用政策集合已经算全，此时用现有规则求值不会
漏掉县级政策，只是没有而已。

判定只看两件事实：现有规则分布（县级/省级）与编译任务状态。
  - 县级规则 >= min_local            → ready（旧行为）
  - 编译任务 done 且有可用规则        → ready_province_only / ready_sparse
  - 其余（未登记/排队/编译中/失败）   → 不放行，继续回退旧链路
红线：编译未完成时放行会漏掉尚未入库的县级政策；因此只有 done 才放行。
开关：RULE_PROVINCE_ONLY=0 回退旧门槛。
"""
import os

MIN_LOCAL = 20


def enabled():
    return os.environ.get('RULE_PROVINCE_ONLY', '1').strip() != '0'


def _scope(rule):
    return (rule.get('region_scope') or '').strip()


def assess(rules, job=None, min_local=MIN_LOCAL):
    """返回 {ready, state, local, province}；纯函数，不读库、不改状态。"""
    rules = list(rules or [])
    local = sum(1 for r in rules if _scope(r) not in ('*', ''))
    province = len(rules) - local
    base = dict(local=local, province=province)
    if local >= min_local:
        return dict(base, ready=True, state='ready')
    if not enabled():
        return dict(base, ready=False, state='disabled')
    if not rules:
        return dict(base, ready=False, state='no_rules')
    state = str((job or {}).get('state') or 'pending')
    if state != 'done':
        return dict(base, ready=False, state=state)
    if local:
        # 该县适用政策已全部编译入库，条数少是这个县真实政策少，不是没编。
        return dict(base, ready=True, state='ready_sparse')
    return dict(base, ready=True, state='ready_province_only')
