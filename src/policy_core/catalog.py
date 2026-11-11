import hashlib
import json
import re
import threading
import time
from pathlib import Path
from chain_trace import traced
from . import store
from .settings import settings,PUBLISHER_VERSION

_lock=threading.Lock()
_cache={}

def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode('utf8')).hexdigest()

def pilot(policy):
    cfg=settings()
    return policy.get('cat1') in cfg['pilot_categories'] or any(w in str(policy.get('name') or '') for w in cfg['pilot_subjects'])

def source_record(p,d,evidence_context=None):
    clean=lambda v:re.sub(r'\s+',' ',str(v or '')).strip()
    extra=d.get('extra')
    if isinstance(extra,str):
        try:extra=json.loads(extra)
        except (ValueError,TypeError):pass
    if isinstance(extra,dict):
        # Import-wide usage counts are neither eligibility nor family facts.
        # Keep all other source attributes; the original detail is untouched.
        extra=json.dumps({k:v for k,v in extra.items() if k not in ('enjoyed_count','person_count')},ensure_ascii=False,sort_keys=True)
    source={k:clean(v) for k,v in dict(title=p.get('name'),region=d.get('region') or p.get('regions'),parent_target=p.get('target'),parent_conditions=p.get('conditions'),target=d.get('target') or p.get('target'),conditions=d.get('conditions') or p.get('conditions'),benefit=d.get('benefit') or p.get('benefit'),process=d.get('process') or p.get('process'),reference=p.get('doc'),extra=extra).items()}
    source.update({k:clean(v) for k,v in dict(scope_category1=p.get('cat1'),scope_category2=d.get('category2') or p.get('cat2'),scope_category3=d.get('category3') or p.get('cat3')).items() if v})
    from .local_evidence import enrich
    return enrich(p,d,source,evidence_context)

@traced('policy_core.catalog')
def refresh(force=False):
    with _lock:
        from .local_evidence import load_bundle
        evidence_context=load_bundle()
        evidence_revision=digest(dict(revision=(evidence_context[0] or {}).get('revision'),invalid=(evidence_context[0] or {}).get('invalid_files'),errors=evidence_context[1]))
        if not force and _cache and _cache.get('evidence_revision')==evidence_revision and time.monotonic()-_cache['loaded']<settings()['scan_seconds']:return _cache['value']
        import smart_matcher_kb as sm
        from sqlalchemy import text
        with sm._db().connect() as conn:
            conn.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ'))
            data=sm._load_db_full(conn)
            extras={str(r['detail_id']):dict(r) for r in conn.execute(text('SELECT detail_id,extra,effect_date,expire_date FROM policy_detail')).mappings()}
            vector_revisions={r[0]:str(r[1] or '') for r in conn.execute(text('SELECT policy_code,max(updated_at) FROM policy_vector GROUP BY policy_code'))}
        from .fields import dictionary
        schema_revision=digest(dictionary())
        compiler_revision=digest([PUBLISHER_VERSION,settings().get('compiler_strategy','direct')]+[(Path(__file__).parent/name).read_text('utf8') for name in ('publishing.py','engine.py','fields.py','source_tests.py','source_grounding.py','llm_contract.py','local_evidence.py','subject_contract.py','clause_ledger.py')])
        supplements=store.supplements()
        instances=[];sources={};counts={}
        for p in data['policies']:
            active=pilot(p);category=p.get('cat1') or '未分类';counts[category]=counts.get(category,0)+1
            for raw in data['by_policy'].get(p['pid'],[]) or [{}]:
                d=dict(raw,**{k:v for k,v in extras.get(str(raw.get('detail_id')),{}).items() if k!='detail_id'})
                src=source_record(p,d,evidence_context) if active else source_record(p,d,(None,[]))
                base=digest(dict(source=src,schema=schema_revision,compiler=compiler_revision,version=p.get('version'),parent_revision=vector_revisions.get(p['code'],'')))
                if base in supplements:src['reference_text']=supplements[base]
                key=digest(dict(base=base,source=src))
                if active:sources[key]=src
                instances.append(dict(id=str(p['code'])+':'+str(d.get('detail_id') or 'main'),digest=key,base_digest=base,pilot=active,policy=p,detail=d))
        revision=digest([(r['id'],r['digest'],r['policy'].get('version'),r['detail'].get('region'),r['policy'].get('regions'),r['policy'].get('effect_date'),r['policy'].get('expire_date'),r['detail'].get('effect_date'),r['detail'].get('expire_date')) for r in instances])
        value=dict(revision=revision,instances=instances,inventory=dict(policies=len(data['policies']),details=len(instances),pilot_policies=sum(pilot(p) for p in data['policies']),pilot_details=sum(i['pilot'] for i in instances),source_groups=len(sources),categories=counts))
        store.enqueue(sources);store.save_catalog(revision,value);_cache.update(value=value,loaded=time.monotonic(),evidence_revision=evidence_revision)
        return value

def current():
    if _cache:return _cache['value']
    saved=store.load_catalog()
    if saved:return saved['data']
    return refresh()

def applicable_region(instance,region,nodes=None,region_idx=None,hh_nodes=None):
    """Exact administrative ancestry using the existing region data, not eligibility."""
    if not region:return None
    if not instance['policy'].get('regions') and not instance['detail'].get('region'):return None
    # T4 地区软过滤：先走确定性的行政区层级判定（policy_core.region 纯函数）。
    # 只有在关系明确时才裁决：政策地区覆盖/等于家庭地区→True，双方明确互不包含→False；
    # 其余（家庭地区写法无法解析、政策地区更细、区县归属歧义）返回 None，
    # 继续走原图谱判定，保证 FP=0 且开关关闭时行为与旧实现完全一致。
    try:
        from . import region as region_semantics
        if region_semantics.soft_filter_enabled():
            verdict=region_semantics.policy_region_verdict(instance['policy'],instance['detail'],region)
            if verdict is not None:return verdict
    except Exception:
        pass  # 程序化判定任何异常都回落旧图谱判定，不影响生产请求
    from kg_retrieval import region_judge,_load
    if nodes is None:nodes=_load()[0][0]
    status,_=region_judge(instance['policy'],instance['detail'],region,nodes,hh_nodes=hh_nodes,region_idx=region_idx)
    return True if status=='适用' else False if status=='不适' else None
