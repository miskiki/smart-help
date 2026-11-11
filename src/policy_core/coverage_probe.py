# -*- coding: utf-8 -*-
"""政策覆盖探测（只读）：判断当前政策库是否收录某个家庭地区的政策。

背景：只读全国模式下，家庭地区在江西省外时旧链路口径只有"仅支持江西省"，
无法区分"确实没有收录该地区政策"与"服务范围限制"。本模块只读查询
policy_scope 表（policy_scope.backfill 回填的结构化地区范围），按行政区层级
做祖先/同级匹配：全国 CN、省 P:<省>、市 C:<省>/<市>、县 X:<省>/<市>/<县>。

原则：
- 只读：不建表、不写库；表缺失或查询异常一律 covered=None + error，
  绝不因为查询失败断言"未收录"；
- 解析不明（unknown/ambiguous）同样 covered=None，交上层追问或保守答复；
- 功能区（Z:）只覆盖它自己，不参与祖先匹配，也不把 host 县区的范围当成覆盖；
- 默认关闭（NATIONAL_READONLY=0），由调用方开关接线后才生效。

对外接口：
  enabled()                 开关（NATIONAL_READONLY）
  coverage(region_text, conn=None) -> dict
  household_coverage(hh, conn=None) -> dict
  summary(cov)              -> 一行中文摘要
"""
import os

from sqlalchemy import bindparam, text

from . import admin_division, policy_scope

VERSION = 'coverage-probe-1'

_TRUE = ('1', 'on', 'true', 'yes')


def enabled():
    """新行为开关：默认关闭，保证现网零变化。"""
    return (os.environ.get('NATIONAL_READONLY', '0') or '').strip().lower() in _TRUE


def _region_keys(parsed):
    """canonical 结果 -> 能覆盖该地区的 policy_scope.region_key 列表。

    返回顺序按层级从宽到窄，便于调用方审计；不包含 U: 未知键，
    因为未知范围不能断言覆盖。
    """
    level = parsed.get('level')
    province = parsed.get('province') or ''
    city = parsed.get('city') or ''
    county = parsed.get('county') or ''
    if parsed.get('national') or level == admin_division.NATION:
        return ['CN']
    if level == admin_division.PROVINCE:
        return ['CN', 'P:' + province]
    if level == admin_division.CITY:
        return ['CN', 'P:' + province, 'C:%s/%s' % (province, city)]
    if level == admin_division.COUNTY:
        keys = ['CN', 'P:' + province, 'C:%s/%s' % (province, city)]
        if parsed.get('zone'):
            # 功能区政策只覆盖该功能区自身；host 县区的 X: 范围与其是否重叠取决于
            # 具体地块，不能当成确定覆盖，因此只给精确的 Z: key。
            keys.append('Z:%s/%s/%s' % (province, city, county))
        else:
            keys.append('X:%s/%s/%s' % (province, city, county))
        return keys
    return []


def _shape(parsed):
    """统一的返回骨架（解析失败时用空 parsed）。"""
    parsed = parsed or {}
    level = parsed.get('level') or admin_division.UNKNOWN
    province, city, county = parsed.get('province'), parsed.get('city'), parsed.get('county')
    zone = bool(parsed.get('zone'))
    return {
        'version': VERSION,
        'region': {'level': level, 'province': province, 'city': city,
                   'county': county, 'zone': zone,
                   'ambiguous': bool(parsed.get('ambiguous'))},
        'level': level, 'province': province, 'city': city, 'county': county,
        'zone': zone, 'rows': 0, 'policies': 0, 'covered': None,
        'ancestor_keys': [], 'reason': '',
    }


def _query(conn, keys):
    statement = text('''
        SELECT count(*) AS rows, count(DISTINCT policy_code) AS policies
          FROM policy_scope WHERE region_key IN :keys
    ''').bindparams(bindparam('keys', expanding=True))
    row = conn.execute(statement, {'keys': keys}).mappings().first()
    rows = int((row or {}).get('rows') or 0)
    policies = int((row or {}).get('policies') or 0)
    return rows, policies


def _label(cov):
    for key in ('county', 'city', 'province'):
        if cov.get(key):
            return cov[key]
    return '该地区'


def coverage(region_text, conn=None):
    """给定地区文本，判断政策库是否有政策覆盖该地区（只读）。

    covered: True=有覆盖行；False=有可查询的表但无覆盖行；None=无法判断
    （地区解析不明 / 表缺失 / 查询异常），此时调用方不得声称"未收录"。
    """
    try:
        parsed = admin_division.canonical(region_text or '')
    except Exception as exc:  # 解析库异常时同样不能断言未收录
        result = _shape({})
        result.update(covered=None, reason='parse_error',
                      error='%s: %s' % (type(exc).__name__, exc))
        return result

    result = _shape(parsed)
    level = parsed.get('level')
    if level == admin_division.UNKNOWN or parsed.get('ambiguous') or not parsed.get('matched', ''):
        result.update(covered=None, reason='region_unresolved')
        return result

    keys = _region_keys(parsed)
    result['ancestor_keys'] = keys
    if not keys:
        result.update(covered=None, reason='region_unresolved')
        return result

    own = conn is None
    try:
        if own:
            conn = policy_scope.engine().connect()
        rows, policies = _query(conn, keys)
    except Exception as exc:  # 表不存在/连接失败等：不能据此说未收录
        result.update(covered=None, reason='query_error',
                      error='%s: %s' % (type(exc).__name__, exc))
        return result
    finally:
        if own and conn is not None:
            conn.close()

    result.update(rows=rows, policies=policies, covered=rows > 0,
                  reason='covered' if rows > 0 else 'no_scope_rows')
    return result


def _region_text(hh):
    """从家庭输入里取地区文本：兼容字符串、hh['region'] 与 facts 里的已知值。"""
    if isinstance(hh, str):
        return hh.strip()
    if not isinstance(hh, dict):
        return ''
    value = hh.get('region')
    if isinstance(value, str) and value.strip():
        return value.strip()
    for facts in (hh.get('facts'), (hh.get('policy_core') or {}).get('facts')):
        fact = (facts or {}).get('household:region')
        if isinstance(fact, dict) and isinstance(fact.get('value'), str) and fact['value'].strip():
            return fact['value'].strip()
    household = hh.get('household')
    if isinstance(household, dict):
        value = household.get('region')
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ''


def household_coverage(hh, conn=None):
    """从家庭对象/地区文本探测覆盖。"""
    return coverage(_region_text(hh), conn=conn)


def summary(cov):
    """一行中文摘要，供只读全国模式组织"暂无收录"类答复。"""
    if not isinstance(cov, dict):
        return '地区覆盖探测结果无效。'
    label = _label(cov)
    if cov.get('error'):
        return '地区覆盖探测失败（%s），暂不能判断政策库是否收录 %s 的政策。' % (
            cov.get('error'), label)
    if cov.get('covered') is None:
        return '家庭所在地区无法解析，暂不能判断政策库是否收录该地区政策。'
    if cov.get('covered'):
        return '政策库收录了覆盖 %s 的政策 %d 条（%d 行地区范围记录）。' % (
            label, cov.get('policies') or 0, cov.get('rows') or 0)
    return '政策库暂未收录覆盖 %s 的政策；这不代表家庭不符合任何政策，具体以经办机构核定为准。' % label
