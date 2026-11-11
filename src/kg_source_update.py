"""Build graph from confirmed ledger/details; publish atomically and consume durable queue.

CLI defaults to read-only preview. --apply requires a unique backup suffix.
The automatic consumer only runs after the provenance schema has been installed.
"""
import argparse
import hashlib
import json
import logging
import re
import threading
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from sqlalchemy import create_engine, inspect, text
import config
from kg_normalization import normalize_label, document_numbers, is_invalid_document
from kg_rebuild import node_id, rebuild_same_doc, rebuild_similarity, verify
from risk_vocabulary import canonical_risk

BUILD_VERSION = 'kg-source-v1-detail-provenance'
_wake = threading.Event()
_worker = None
_guard = threading.Lock()
EMPTY = {'', '无', '暂无', '未知', '无限制', '不限', '未提供', '无文号'}


def value(v):
    return str(v or '').strip()


def extra_of(detail):
    raw = detail.get('extra') or {}
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw) or {}
    except (ValueError, TypeError):
        return {}


def source_issues(source):
    # Do not guess corrections to corrupted source text.
    return [word for word in ('王度残疾人', '特因供养', '严兰困难', '防止防贫致贫对象', '官颈癌') if word in source]


def is_placeholder(policy, details):
    # Require several explicit template markers, never reject on a short name alone.
    strings = [value(policy.get(k)) for k in ('policy_name', 'doc_ref', 'conditions', 'process')]
    strings += [value(d.get(k)) for d in details for k in ('conditions', 'process')]
    return bool(re.fullmatch(r'金融\d+', strings[0])) and bool(re.fullmatch(r'文号\s*\d+', strings[1])) and {'条件', '流程'} <= set(strings)


def build_graph(policies, details, checks, created_at=None, organization_names=()):
    created_at = created_at or datetime.now()
    byid = defaultdict(list)
    for d in details:
        byid[d['policy_id']].append(d)
    nodes, grouped = {}, {}
    issues = []
    org_pattern=r'(?:江西省|[\u4e00-\u9fff]{2,4}市|[\u4e00-\u9fff]{2,4}县|省|市|县)(?:民政|财政|教育|卫生健康|医疗保障|农业农村)(?:厅|局|委员会)'
    organizations=set(organization_names)
    references=[value(p.get('doc_ref')) for p in policies.values()]+[value(extra_of(d).get('doc')) for d in details]
    for reference in references:
        organizations.update(re.findall(org_pattern,reference))

    def ensure(kind, label, field, attrs=None, scoped=None):
        label = normalize_label(kind, label)
        nid = node_id(kind, label) if scoped is None else kind+':branch:'+hashlib.sha256(scoped.encode()).hexdigest()
        if nid not in nodes:
            nodes[nid] = dict(id=nid, node_type=kind, name=label, source_table='policy_detail' if scoped else 'policy',
                              source_field=field, attrs=attrs or {}, created_at=created_at)
        return nid

    def add(p, kind, label, et, field, raw=None, d=None, attrs=None, scoped=None):
        if value(label) in EMPTY:
            return
        dest = ensure(kind, label, field, attrs, scoped)
        pid = 'POLICY:'+p['policy_code']
        key = (pid, dest, et)
        proof = dict(policy_code=p['policy_code'], policy_version=p.get('cur_version'),
                     detail_id=d.get('detail_id') if d else None,
                     source_table='policy_detail' if d else 'policy', source_field=field,
                     raw_evidence=value(raw if raw is not None else label))
        if d:
            proof['branch'] = {k:d.get(k) for k in ('region', 'target', 'conditions', 'benefit')}
            ex=extra_of(d)
            proof['branch']['doc_ref']=ex.get('doc') or p.get('doc_ref')
            if ex.get('benefit_source'):
                proof['branch']['benefit_source']=ex['benefit_source']
        if key not in grouped:
            grouped[key] = dict(src_id=pid, dst_id=dest, src_type='POLICY', dst_type=kind,
                                edge_type=et, weight=1.0, source_field=field,
                                evidence=proof['raw_evidence'], provenance=[], created_at=created_at)
        if proof not in grouped[key]['provenance']:
            grouped[key]['provenance'].append(proof)

    for p in sorted(policies.values(), key=lambda x:x['policy_code']):
        ds = sorted(byid[p['policy_id']], key=lambda x:x['detail_id'])
        if p['status'] != '在用':
            continue
        if is_placeholder(p, ds):
            issues.append({'code':p['policy_code'], 'issue':'template_placeholder', 'blocking':True})
            continue
        pid = 'POLICY:'+p['policy_code']
        nodes[pid] = dict(id=pid, node_type='POLICY', name=p['policy_name'], source_table='policy',
            source_field='policy_code', created_at=created_at,
            attrs={k:p.get(k) for k in ('policy_code','level','status','category1','category2','category3')} |
                  {'policy_version':p.get('cur_version'), 'build_version':BUILD_VERSION})
        add(p, 'CATEGORY', p.get('category1'), 'BELONGS_TO', 'category1')
        doc = value(p.get('doc_ref'))
        if doc not in EMPTY:
            # Preserve explicit institution mentions, without asserting issuer identity.
            for organization in sorted(organizations):
                if normalize_label('DOC',organization) in normalize_label('DOC',doc):
                    add(p,'ORG',organization,'REFERENCES_ORG','doc_ref',doc)
            numbers = document_numbers(doc)
            if numbers:
                for number in numbers:
                    add(p, 'DOC', number, 'FROM_DOC', 'doc_ref', doc)
                    attrs = nodes[node_id('DOC', number)]['attrs']
                    aliases = attrs.setdefault('source_references', [])
                    if doc not in aliases:
                        aliases.append(doc)
            elif not is_invalid_document(doc) and not re.search(r'文号|\d+\s*号',doc) and re.search(r'(通知|办法|方案|意见|细则|明白纸|规定|标准)(?:[）)》”\s]|[（(]\d{4}[）)])*$',doc):
                add(p, 'DOC', doc, 'FROM_DOC', 'doc_ref', doc)
            else:
                add(p, 'SOURCE_NOTE', doc, 'SOURCE_REFERENCE', 'doc_ref', doc, attrs={'review_required':True})
                issues.append({'code':p['policy_code'], 'issue':'document_reference_review', 'source':doc})
        # Category-to-risk links describe recall only, never establish eligibility.
        for ck in checks:
            must = value(ck.get('must'))
            cat = value(p.get('category1'))
            if cat and cat in must:
                add(p, 'RISK', canonical_risk(ck['risk_type']), 'COVERS', 'rule_check.must', must,
                    attrs={'recall_only':True})
        rows = ds or [None]
        for d in rows:
            ex = extra_of(d) if d else {}
            detail_doc=value(ex.get('doc'))
            if detail_doc not in EMPTY:
                for organization in sorted(organizations):
                    if normalize_label('DOC',organization) in normalize_label('DOC',detail_doc):
                        add(p,'ORG',organization,'REFERENCES_ORG','extra.doc',detail_doc,d)
                citations=document_numbers(detail_doc)
                for number in citations:
                    add(p,'DOC',number,'FROM_DOC','extra.doc',detail_doc,d)
                    aliases=nodes[node_id('DOC',number)]['attrs'].setdefault('source_references',[])
                    if detail_doc not in aliases:aliases.append(detail_doc)
                if not citations and detail_doc!=doc:
                    add(p,'SOURCE_NOTE',detail_doc,'SOURCE_REFERENCE','extra.doc',detail_doc,d,attrs={'review_required':True})
                    issues.append({'code':p['policy_code'],'detail_id':(d or {}).get('detail_id'),'issue':'detail_document_reference_review','source':detail_doc})
                elif not citations and node_id('DOC',normalize_label('DOC',detail_doc)) in nodes:
                    add(p,'DOC',detail_doc,'FROM_DOC','extra.doc',detail_doc,d)
            # Explicit inheritance is recorded; an absent region never becomes all-province.
            region = value((d or {}).get('region')) or value(p.get('regions'))
            for label in re.split(r'[,，、;；\n]',region):
                add(p, 'REGION', label, 'IN_REGION', 'region' if d else 'regions', region, d)
            target = value((d or {}).get('target')) or value(p.get('target'))
            add(p, 'POPULATION', target, 'APPLIES_TO', 'target', target, d)
            source = value((d or {}).get('conditions')) or value(ex.get('conditions')) or value(p.get('conditions'))
            problems = source_issues(source)
            if problems:
                add(p, 'SOURCE_NOTE', source, 'SOURCE_REFERENCE', 'conditions', source, d,
                    attrs={'review_required':True,'source_issues':problems})
                issues.append({'code':p['policy_code'],'detail_id':(d or {}).get('detail_id'),
                               'issue':'source_text_review','terms':problems})
            # Editorial notes have source provenance but do not become qualification features.
            condition = source.split('原文对象写作')[0].rstrip('。；;，, ') if '原文对象写作' in source else source
            if not problems or '原文对象写作' in source:
                add(p, 'CONDITION', condition, 'REQUIRES', 'conditions', source, d)
            for key,prefix in (('lab','劳动能力:'),('health','健康状况:'),('age','劳动力年龄:')):
                structured = value(ex.get(key))
                if structured in EMPTY:
                    continue
                labels = [structured] if key=='age' else re.split(r'[,，、;；]',structured)
                for label in labels:
                    if key=='age' and not re.fullmatch(r'\d{1,3}-\d{1,3}',label):
                        issues.append({'code':p['policy_code'],'detail_id':(d or {}).get('detail_id'),'issue':'age_range_review','source':label})
                        continue
                    add(p, 'CONDITION', prefix+label, 'REQUIRES', 'extra.'+key, structured, d)
            # The source's explicit risk constraint can safely supply the missing five risks.
            for risk in re.split(r'[,，、;；]',value(ex.get('pov'))):
                if risk and risk not in EMPTY:
                    add(p, 'RISK', canonical_risk(risk), 'COVERS', 'extra.pov', value(ex.get('pov')), d,
                        attrs={'recall_only':True})
            benefit = value((d or {}).get('benefit')) or value(p.get('benefit'))
            bare_number = bool(re.fullmatch(r'[\d\s.,，；;—\-]+',benefit))
            metadata=ex.get('benefit_source') or {}
            has_unit=bool(metadata.get('currency') and metadata.get('unit') and metadata.get('cycle'))
            label=benefit
            if bare_number and has_unit:
                from kg_source_metadata import describe_benefit
                label=describe_benefit(benefit,ex)
            needs_review=bare_number and not has_unit
            scope = '%s/%s/%s' % (pid, (d or {}).get('detail_id'), benefit) if needs_review else None
            add(p, 'SUBSIDY', label, 'HAS_SUBSIDY', 'benefit', benefit, d,
                attrs={'requires_unit_review':needs_review,'unit':metadata.get('unit'),'cycle':metadata.get('cycle')}, scoped=scope)
            if needs_review and benefit:
                issues.append({'code':p['policy_code'],'detail_id':(d or {}).get('detail_id'),'issue':'benefit_unit_review'})
    return nodes, list(grouped.values()), issues


def schema_ready(c):
    return inspect(c).has_table('kg_update_queue') and 'provenance' in {x['name'] for x in inspect(c).get_columns('kg_edge')}


def install_schema(c):
    # Called only by the backed-up administrator migration, never implicitly in HTTP.
    c.execute(text("ALTER TABLE kg_edge ADD COLUMN IF NOT EXISTS provenance jsonb"))
    for column,typ in [('attempts','integer DEFAULT 0'),('last_error','text'),('completed_at','varchar(32)')]:
        c.execute(text('ALTER TABLE kg_update_queue ADD COLUMN IF NOT EXISTS '+column+' '+typ))


def publish(c, nodes, edges, same, sim):
    # Derivations and all provenance become visible in one transaction.
    for table in ('kg_policy_sim','kg_policy_edge','kg_edge','kg_node'):
        if table == 'kg_policy_edge':
            if c.execute(text("SELECT count(*) FROM kg_policy_edge WHERE edge_type<>'SAME_DOC'")).scalar():
                raise RuntimeError('Unmanaged policy relations require migration before source publication')
        c.execute(text('DELETE FROM '+table))
    nsql = text('INSERT INTO kg_node(id,node_type,name,source_table,source_field,attrs,created_at) VALUES(:id,:node_type,:name,:source_table,:source_field,CAST(:attrs AS jsonb),:created_at)')
    esql = text('INSERT INTO kg_edge(src_id,dst_id,src_type,dst_type,edge_type,weight,source_field,evidence,provenance,created_at) VALUES(:src_id,:dst_id,:src_type,:dst_type,:edge_type,:weight,:source_field,:evidence,CAST(:provenance AS jsonb),:created_at)')
    for sql,rows,key in ((nsql,list(nodes.values()),'attrs'),(esql,edges,'provenance')):
        for start in range(0,len(rows),250):
            c.execute(sql,[dict(r,**{key:json.dumps(r[key],ensure_ascii=False)}) for r in rows[start:start+250]])
    for table,rows in (('kg_policy_edge',same),('kg_policy_sim',sim)):
        fields=['src_id','dst_id','edge_type','weight','shared_attrs','jaccard']
        if table=='kg_policy_sim':fields.append('algo_version')
        sql=text('INSERT INTO '+table+'('+','.join(fields)+') VALUES('+','.join('CAST(:shared_attrs AS jsonb)' if f=='shared_attrs' else ':'+f for f in fields)+')')
        for start in range(0,len(rows),250):
            c.execute(sql,[dict(r,shared_attrs=json.dumps(r['shared_attrs'],ensure_ascii=False)) for r in rows[start:start+250]])
    actual = {t:c.execute(text('SELECT count(*) FROM '+t)).scalar() for t in ('kg_node','kg_edge','kg_policy_edge','kg_policy_sim')}
    if list(actual.values()) != [len(nodes),len(edges),len(same),len(sim)]:
        raise RuntimeError('Published graph counts differ; transaction rolled back')


def rebuild(c, apply=False, quarantine=False, hydrate=False):
    policies={'POLICY:'+r['policy_code']:dict(r) for r in c.execute(text('SELECT * FROM policy')).mappings()}
    details=[dict(r) for r in c.execute(text('SELECT * FROM policy_detail')).mappings()]
    checks=[dict(r) for r in c.execute(text('SELECT * FROM rule_check')).mappings()]
    metadata_report={}
    if hydrate:
        from kg_source_metadata import recover
        source_path=Path(__file__).resolve().parents[2]/'data/县级帮扶工具箱_更新至9月22日.xlsx'
        recovered,ambiguous=recover(details,source_path)
        installed=0
        for d in details:
            if d['detail_id'] in recovered:
                ex=extra_of(d)
                if ex.get('benefit_source'):
                    continue
                ex['benefit_source']=recovered[d['detail_id']]
                installed+=1
                d['extra']=json.dumps(ex,ensure_ascii=False)
                if apply:
                    c.execute(text('UPDATE policy_detail SET extra=:extra WHERE detail_id=:id'),{'extra':d['extra'],'id':d['detail_id']})
        metadata_report={'matched_details':len(recovered),'new_metadata_details':installed,'ambiguous_details':ambiguous}
    organizations=[r[0] for r in c.execute(text("SELECT name FROM kg_node WHERE node_type='ORG'"))]
    nodes,edges,issues=build_graph(policies,details,checks,organization_names=organizations)
    blockers=[i for i in issues if i.get('blocking')]
    if apply and blockers and not quarantine:
        raise ValueError('Placeholder policy requires review before publication')
    if apply:
        for i in blockers:
            c.execute(text("UPDATE policy SET status='停用',updated_at=:ts,updated_by='graph_source_review' WHERE policy_code=:code"),{'ts':datetime.now().isoformat(timespec='seconds'),'code':i['code']})
            c.execute(text("INSERT INTO audit_log(action,target_code,target_name,detail,operator,created_at) VALUES('停用政策',:code,:name,:reason,'graph_source_review',:ts)"),
                {'code':i['code'],'name':policies['POLICY:'+i['code']]['policy_name'],'reason':'原始 Excel 与台账均包含金融/文号序号及条件、流程模板占位，隔离后待原件确认','ts':datetime.now().isoformat(timespec='seconds')})
    active={pid for pid,n in nodes.items() if n['node_type']=='POLICY'}
    same,large=rebuild_same_doc(nodes,edges,active,config.KG_SAME_DOC_MAX_POLICIES)
    sim,sim_report=rebuild_similarity(nodes,edges,policies,active)
    verify(nodes,edges,same,sim)
    report=dict(build_version=BUILD_VERSION,nodes=len(nodes),attribute_edges=len(edges),same_doc=len(same),sim=len(sim),
                active_policies=len(active),issues=issues,large_documents=large,similarity=sim_report,
                provenance_edges=sum(bool(e['provenance']) for e in edges),source_metadata=metadata_report)
    if apply:
        publish(c,nodes,edges,same,sim)
        # Ledger/details/queue are locked: no newer request can be consumed accidentally.
        c.execute(text("UPDATE kg_update_queue SET status='ready',last_error=NULL,completed_at=:ts WHERE status IN ('pending','failed')"),{'ts':datetime.now().isoformat(timespec='seconds')})
    return report


def lock_tables(c):
    c.execute(text("SET LOCAL lock_timeout='5s'"))
    c.execute(text('LOCK TABLE policy,policy_detail,kg_update_queue,kg_node,kg_edge,kg_policy_edge,kg_policy_sim IN SHARE ROW EXCLUSIVE MODE'))


def sync_job_vectors(engine, jobs=None):
    """图谱发布事务提交后，为批量导入的政策补向量索引（kb_rag.sync_policy 按明细写 detail_id）。
    失败仅记录（audit_log），不影响图谱发布结果；无全文时 sync_policy 用结构化明细构造文档。"""
    if not jobs:
        return {'ok': True, 'synced': 0, 'failed': 0}
    import kb_rag
    synced = failed = 0
    for job in jobs:
        code = job.get('policy_code')
        if not code:
            failed += 1
            continue
        try:
            with engine.connect() as c:
                r = c.execute(text(
                    "SELECT policy_name, doc_ref, cur_version, status FROM policy WHERE policy_code=:pc"),
                    {'pc': code}).fetchone()
            if not r:
                failed += 1
                continue
            res = kb_rag.sync_policy(code, r[0], r[1] or '', r[3], r[2] or '', None)
            if not res.get('ok'):
                failed += 1
                continue
            synced += 1
        except Exception:
            failed += 1
            logging.getLogger(__name__).exception('向量索引同步失败 policy_code=%s（不影响图谱）', code)
    return {'ok': True, 'synced': synced, 'failed': failed}


def process_pending(engine=None):
    engine=engine or create_engine(config.DATABASE_URL)
    result = None
    jobs = []
    try:
        with engine.begin() as c:
            if not schema_ready(c):
                return {'status':'schema_required'}
            # Multiple WSGI processes may poll; only one can publish.
            if not c.execute(text('SELECT pg_try_advisory_xact_lock(2026101201)')).scalar():
                return {'status':'busy'}
            if not c.execute(text("SELECT count(*) FROM kg_update_queue WHERE status='pending'")).scalar():
                return {'status':'idle'}
            lock_tables(c)
            jobs=[dict(r) for r in c.execute(text("SELECT * FROM kg_update_queue WHERE status='pending'")).mappings()]
            if not jobs:
                return {'status':'idle'}
            report=rebuild(c,apply=True)
            for job in jobs:
                c.execute(text("INSERT INTO audit_log(action,target_code,target_name,detail,operator,created_at) VALUES('更新图谱',:code,:name,:detail,'graph_update_worker',:ts)"),
                    {'code':job['policy_code'],'name':job['policy_code'],
                     'detail':BUILD_VERSION+'；版本 '+str(job['policy_version'])+'；属性边 '+str(report['attribute_edges'])+'；SIM '+str(report['sim']),
                     'ts':datetime.now().isoformat(timespec='seconds')})
            result={'status':'ready','jobs':len(jobs),'report':report}
        # 事务已提交：补向量索引（失败不阻塞图谱发布）
        if result and result['status'] == 'ready':
            vs = sync_job_vectors(engine, jobs)
            if vs.get('failed'):
                with engine.begin() as c:
                    for job in jobs:
                        c.execute(text("INSERT INTO audit_log(action,target_code,target_name,detail,operator,created_at) VALUES('向量索引同步',:code,:name,:detail,'graph_update_worker',:ts)"),
                            {'code':job['policy_code'],'name':job['policy_code'],
                             'detail':'向量同步失败 %d 条（可重试 sync_policy）' % vs['failed'],
                             'ts':datetime.now().isoformat(timespec='seconds')})
            result['vector_sync'] = vs
        return result
    except Exception as exc:
        # Failure leaves the old graph intact. Failed policies remain excluded from recall.
        if 'jobs' in locals():
            with engine.begin() as c:
                for job in jobs:
                    c.execute(text("UPDATE kg_update_queue SET status='failed',attempts=COALESCE(attempts,0)+1,last_error=:error WHERE policy_code=:code AND policy_version=:version AND requested_at=:ts AND status='pending'"),
                        {'code':job['policy_code'],'version':job['policy_version'],'ts':job['requested_at'],'error':type(exc).__name__+': '+str(exc)[:500]})
        raise


def start_worker():
    global _worker
    if not getattr(config,'KG_AUTO_UPDATE',False):
        return
    with _guard:
        if _worker and _worker.is_alive():
            return
        def work():
            engine=create_engine(config.DATABASE_URL,pool_pre_ping=True)
            while True:
                try:
                    result=process_pending(engine)
                    if result['status']=='ready':
                        from kg_retrieval import refresh_cache
                        from smart_matcher_kb import invalidate_db_cache
                        refresh_cache()
                        invalidate_db_cache()
                except Exception:
                    logging.getLogger(__name__).exception('Graph update failed; transaction rolled back')
                _wake.wait(15)
                _wake.clear()
        _worker=threading.Thread(target=work,name='policy-graph-update',daemon=True)
        _worker.start()


def request_update():
    _wake.set()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--backup-suffix',default='source_20261012')
    args=parser.parse_args()
    if not re.fullmatch(r'[a-z][a-z0-9_]{7,40}',args.backup_suffix):
        raise ValueError('Invalid backup suffix')
    engine=create_engine(config.DATABASE_URL)
    with engine.begin() as c:
        if args.apply:
            lock_tables(c)
            for t in ('policy','policy_detail','kg_node','kg_edge','kg_policy_edge','kg_policy_sim','kg_update_queue'):
                backup=t+'_'+args.backup_suffix
                if inspect(c).has_table(backup):raise RuntimeError('Backup exists: '+backup)
                c.execute(text('CREATE TABLE '+backup+' AS SELECT * FROM '+t))
            install_schema(c)
        else:
            c.execute(text('SET TRANSACTION READ ONLY'))
        report=rebuild(c,args.apply,quarantine=args.apply,hydrate=True)
    outfile=Path(__file__).resolve().parents[2]/'outputs'/('kg_source_apply_20261012.json' if args.apply else 'kg_source_preview_20261012.json')
    outfile.write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k not in ('issues','large_documents')},ensure_ascii=False,default=str))


if __name__=='__main__':main()
