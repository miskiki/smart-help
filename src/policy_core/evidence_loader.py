"""Read current, complete source records; graph is only a source/region tool."""
import datetime
import json
from collections import Counter
from sqlalchemy import text,bindparam
from .catalog import source_record,digest,applicable_region
from .local_evidence import load_bundle
from .full_runtime import event

def today():return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).date().isoformat()

def valid_dates(*records):
    uncertain=False
    for record in records:
        for field,before in [('effect_date',True),('expire_date',False)]:
            value=record.get(field)
            if not value:continue
            try:day=datetime.date.fromisoformat(str(value)[:10]).isoformat()
            except ValueError:uncertain=True;continue
            if (before and day>today()) or (not before and day<today()):return False
    return None if uncertain else True

def map_policy(raw):
    raw=json.loads(json.dumps(dict(raw),ensure_ascii=False,default=str));p=dict(raw)
    for to,src in dict(pid='policy_id',code='policy_code',name='policy_name',cat1='category1',cat2='category2',cat3='category3',doc='doc_ref',version='cur_version').items():p[to]=raw.get(src)
    return p

_FAMILY_GENERIC = ('补贴', '补助', '救助', '保险', '保障', '津贴', '服务', '管理', '办法', '通知',
                   '标准', '生活', '最低', '临时', '安置', '援助', '扶持', '发放', '资格', '认定',
                   '工作', '项目', '计划', '政策', '实施', '细则', '方案', '费用', '资金')


def _lcs(a, b):
    """最长公共连续子串（短名比对，O(n*m) 政策名足够短）。"""
    best = ''
    for i in range(len(a)):
        for j in range(len(b)):
            k = 0
            while i + k < len(a) and j + k < len(b) and a[i + k] == b[j + k]:
                k += 1
            if k > len(best):
                best = a[i:i + k]
    return best


def _name_family(name, loaded_names):
    """判定 name 是否与任一已加载政策同名家族（同主题政策族的异地同名条目）。"""
    a = str(name or '')
    for other in loaded_names:
        b = str(other or '')
        if not a or not b:
            continue
        shared = _lcs(a, b)
        if len(shared) >= 2 and shared not in _FAMILY_GENERIC and not all(ch in ''.join(_FAMILY_GENERIC) for ch in shared):
            return True
    return False


def _trace_event(name,**fields):
    """T4 新增领域事件统一出口：只走 chain_trace 旧 event 接口，异常全吞（观测不能影响主流程）。"""
    try:
        from chain_trace import event as trace_event
        trace_event(name,**fields)
    except Exception:
        pass


def _region_semantics():
    """地区语义层容错加载：任何导入/接口异常都让调用方回落旧地区行为。"""
    try:
        from . import region as region_semantics
        return region_semantics
    except Exception:
        return None


def load(candidates,region,config):
    semantics=_region_semantics()
    soft=bool(semantics is not None and semantics.soft_filter_enabled())
    # 旧行为：家庭地区为空直接返回空结果。软过滤开启后不再整批丢弃：
    # 用户没说地区≠不符合，带 region_status=unknown 继续加载并标注，由追问环节补齐。
    if not region and not soft:return [],dict(reason='region_unknown',dropped={})
    from kb_rag import _db
    codes=list(dict.fromkeys(c['policy_code'] for c in candidates));drops=Counter();packs=[];unknown_kept=0
    if not codes:return [],dict(dropped={},eligible_candidates=0)
    engine=_db()
    try:
        with engine.connect() as conn:
            conn.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ'))
            conn.execute(text('SET TRANSACTION READ ONLY'))
            stmt=text('SELECT * FROM policy WHERE policy_code IN :codes').bindparams(bindparam('codes',expanding=True))
            policies={r['policy_code']:map_policy(dict(r)) for r in conn.execute(stmt,dict(codes=codes)).mappings()}
            pids=[p['pid'] for p in policies.values()]
            details={}
            if pids:
                stmt=text('SELECT * FROM policy_detail WHERE policy_id IN :pids').bindparams(bindparam('pids',expanding=True))
                for row in conn.execute(stmt,dict(pids=pids)).mappings():details.setdefault(row['policy_id'],[]).append(json.loads(json.dumps(dict(row),ensure_ascii=False,default=str)))
    finally:engine.dispose()
    context=load_bundle();seen=set();per_policy=Counter();region_dropped={}
    branch_limit=int(config.get('per_policy_limit') or 2)
    for candidate in candidates:
        p=policies.get(candidate['policy_code'])
        if not p or p.get('status')!='在用':drops['inactive']+=1;continue
        if str(p.get('version') or '')!=str(candidate.get('policy_version') or ''):drops['stale_index']+=1;continue
        all_details=details.get(p['pid'],[])
        targets=[d for d in all_details if str(d['detail_id'])==str(candidate['detail_id'])] if candidate['detail_id'] is not None else all_details or [{}]
        if not targets:drops['missing_detail']+=1;continue
        for d in targets:
            key=(p['code'],d.get('detail_id'))
            if key in seen:continue
            if valid_dates(p,d) is False:drops['date']+=1;continue
            scope=applicable_region(dict(policy=p,detail=d),region)
            if scope is False:
                # P0-5：地区过滤保留政策名单，空结果时如实告知用户"库里有什么、为什么没核验"。
                drops['region']+=1
                if p['code'] not in region_dropped:region_dropped[p['code']]=dict(code=p['code'],name=p['name'],regions=p.get('regions'))
                continue
            # P0-1：地区未确认(scope=None)不再丢弃——用户没说地区≠不符合。
            # 候选保留下来交给审核阶段用"地区未确认"标注，由追问环节补齐地区后再确认。
            if scope is None:drops['region_unconfirmed_kept']=drops.get('region_unconfirmed_kept',0)+1
            # T4：保留候选的同时标注地区信号（applicable/unconfirmed/unknown）。
            # region_status 仍是 True/False/None 三态——compile_rules 依赖 is True/is None 的判定。
            region_signal='applicable' if scope is True else ('unknown' if not region else 'unconfirmed')
            if region_signal=='unknown':unknown_kept+=1
            if per_policy[p['code']]>=branch_limit:drops['policy_branch_budget']+=1;continue
            seen.add(key);per_policy[p['code']]+=1
            source=source_record(p,d,context)
            source_hash=digest(dict(source=source,policy=p,detail=d))
            packs.append(dict(id=p['code']+':'+str(d.get('detail_id') or 'main'),policy=p,detail=d,source=source,source_hash=source_hash,
                              retrieval=candidate,date_status=valid_dates(p,d),region_status=scope,region_signal=region_signal))
    # P1-1：此处不再做 max_candidates 截断。截断统一移到重排(prioritize)之后由
    # full_flow 执行——重排能看到完整候选池，避免"未排序先砍头"把相关条款截在门外。
    total=len(packs)
    # 与已成功加载政策同名家族的异地条目不进"被地区排除"名单：
    # 本地同族条目已在核验范围内时，把外地同名条目列成被排除会误导用户
    # 以为本地的政策（如新建区雨露计划）被丢掉了。
    loaded_names=[pk['policy'].get('name') or '' for pk in packs]
    dropped_list=[d for d in region_dropped.values() if not _name_family(d.get('name'),loaded_names)]
    stats=dict(dropped=dict(drops),eligible_candidates=total,selected_sources=total,candidate_budget_truncated=0,
               region_dropped=dropped_list[:6])
    if soft:
        # 观测：家庭地区归一结果 + 软过滤统计。字段只用安全标量（省/市/县/关系/来源），不放用户原话。
        if region and semantics is not None:
            try:
                normalized=semantics.canonical_levels(region)
                _trace_event('region.normalized',province=normalized.get('province') or '',
                             city=normalized.get('city') or '',county=normalized.get('county') or '',
                             relation='explicit_other' if semantics.explicit_other(region) else 'unknown',
                             source='household:region')
            except Exception:
                pass
        _trace_event('region_soft_filter',kept=total,dropped=int(drops.get('region',0)),unknown=unknown_kept)
        stats['region_unknown_kept']=unknown_kept
    event('evidence_load',**stats)
    return packs,stats
