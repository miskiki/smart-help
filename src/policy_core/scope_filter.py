# -*- coding: utf-8 -*-
"""运行期地区范围过滤器（policy_scope 的读侧封装）。

用途：在全模型/规则链路拿到候选政策后，按 policy_scope 结构化范围做三态过滤，
替代自由文本 regions 的 LIKE/硬编码判断。默认关闭（POLICY_SCOPE=0），
关闭时本模块不参与任何行为，旧路径保持不变。

三态与 region.py 一致：
- applicable=True  -> 保留（政策范围覆盖家庭地区）
- applicable=False -> 丢弃（明确不适用，如外省）
- applicable=None  -> 保留但标记 unknown（家庭地区太粗、功能区重叠等），不清空候选
- 政策没有 scope 行 -> 保留并标记 no_scope（宁可多召回，不静默丢政策）
"""
import os

from . import admin_division
from . import policy_scope

VERSION = 'scope-filter-1'

_TRUTHY = ('1', 'on', 'true', 'yes', 'y')


def enabled():
    """POLICY_SCOPE 开关，默认 0（关闭）。"""
    return str(os.environ.get('POLICY_SCOPE', '0')).strip().lower() in _TRUTHY


def filter_codes(codes, household_region, conn=None):
    """按结构化范围过滤一批政策编号。

    返回 dict：
      household  家庭地区 canonical
      kept       保留的编号（applicable=True）
      dropped    [{'code','relation','reason'}] 明确不适用
      unknown    [{'code','relation','reason'}] 无法判定，保留待核
      no_scope   没有结构化范围行的编号（保留）
      applicable {code: True/False/None} 便于调用方记录
    """
    codes = [c for c in dict.fromkeys(codes or []) if c]
    household = admin_division.canonical(household_region or '')
    out = {'version': VERSION, 'household': household, 'kept': [], 'dropped': [],
           'unknown': [], 'no_scope': [], 'applicable': {}}
    if not codes:
        return out
    own = conn is None
    try:
        if own:
            conn = policy_scope.engine().connect()
        scopes = policy_scope.load_scopes(conn, codes)
    except Exception as exc:
        # 结构化范围表缺失/不可读（例如未迁移的库、sqlite 回退）：一律按 no_scope 保留，
        # 绝不因为读不到范围而静默丢候选（与"宁可多召回"同一保守原则）。
        out['no_scope'] = list(codes)
        out['applicable'] = {code: None for code in codes}
        out['error'] = type(exc).__name__ + ':' + str(exc)[:120]
        return out
    finally:
        if own and conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    for code in codes:
        scope = scopes.get(code) or {'policy': [], 'detail': []}
        if not scope.get('policy') and not scope.get('detail'):
            out['no_scope'].append(code)
            out['applicable'][code] = None
            continue
        result = policy_scope.match(scope, household)
        applicable = result.get('applicable')
        out['applicable'][code] = applicable
        item = {'code': code, 'relation': result.get('relation'),
                'reason': result.get('reason')}
        if applicable is True:
            out['kept'].append(code)
        elif applicable is False:
            out['dropped'].append(item)
        else:
            out['unknown'].append(item)
    return out


def filter_packs(packs, household_region, conn=None, code_key='policy_code'):
    """按范围过滤 retrieval pack 列表；返回 (kept_packs, report)。

    没有任何 pack 时也返回空报告，方便调用方统一记事件。
    """
    packs = list(packs or [])
    report = filter_codes([p.get(code_key) for p in packs], household_region, conn=conn)
    dropped = {item['code'] for item in report['dropped']}
    kept = [p for p in packs if p.get(code_key) not in dropped]
    report['input'] = len(packs)
    report['output'] = len(kept)
    return kept, report


def summary(report):
    """紧凑摘要，写事件/coverage 用。"""
    return {'version': VERSION, 'input': report.get('input', 0),
            'output': report.get('output', 0),
            'kept': len(report.get('kept') or []),
            'dropped': len(report.get('dropped') or []),
            'unknown': len(report.get('unknown') or []),
            'no_scope': len(report.get('no_scope') or []),
            'dropped_codes': [item['code'] for item in (report.get('dropped') or [])][:12],
            'unknown_codes': [item['code'] for item in (report.get('unknown') or [])][:12]}
