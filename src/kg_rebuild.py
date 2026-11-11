"""Executable graph repair: preview -> transactional backup/rebuild -> verification.

Run from admin_panel: python kg_rebuild.py --apply --backup-suffix rebuild_20261012
No model calls. Never rewrites policy text, details or vectors.
"""
import argparse
import hashlib
import itertools
import json
import math
import re
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime
from pathlib import Path

import config
from sqlalchemy import create_engine, text, inspect
from kg_normalization import (normalize_label, is_document_organization,
    is_invalid_document, document_numbers, INCOMPLETE_CONDITIONS, NORMALIZATION_VERSION)
from risk_vocabulary import canonical_risk

ALGO_VERSION = 'sim-v4-source-detail-idf-maxdegree20'
FEATURE_WEIGHTS = {'CATEGORY':1, 'CATEGORY2':2, 'CATEGORY3':3, 'REGION':1,
                  'POPULATION':1, 'RISK':1, 'CONDITION':2, 'SUBSIDY':1}


def node_id(kind, label):
    nid=kind+':'+label
    return nid if len(nid)<=512 else kind+':normalized:'+hashlib.sha256(label.encode()).hexdigest()


def normalize_graph(nodes, edges, policies):
    """Normalize all nodes, restoring known fragments from their own policy source."""
    out, mapping, report = {}, {}, Counter()
    for old,n in nodes.items():
        label=normalize_label(n['node_type'],n['name'])
        kind=n['node_type']
        if kind=='DOC' and is_document_organization(label):
            kind='ORG'
        if kind in ('DOC','FILE','CONDITION','POPULATION','ORG') or (kind=='SUBSIDY' and not n.get('attrs',{}).get('requires_unit_review')):
            new=node_id(kind,label)
        else:
            new=old
        mapping[old]=new
        out.setdefault(new,dict(n,id=new,name=label,node_type=kind))
        if new!=old or n['name']!=label:
            report['normalized_'+n['node_type']]+=1
    def ensure(kind,label,template):
        new=node_id(kind,label)
        out.setdefault(new,dict(template,id=new,node_type=kind,name=label))
        return new
    updated=[]
    restored_docs=[]
    for original in edges:
        edge=deepcopy(original)
        source=policies.get(original['src_id'],{})
        old=nodes[original['dst_id']]
        label=normalize_label(old['node_type'],old['name'])
        edge['src_id']=mapping[edge['src_id']]
        edge['dst_id']=mapping[edge['dst_id']]
        if old['node_type']=='CONDITION' and old['name'].strip() in INCOMPLETE_CONDITIONS:
            full=source.get('conditions')
            if full and original['source_field']=='conditions':
                edge['dst_id']=ensure('CONDITION',normalize_label('CONDITION',full),old)
                edge['evidence']=full
                report['condition_edges_restored']+=1
            else:
                # Retain unresolvable source evidence without presenting a partial condition.
                edge['dst_id']=ensure('SOURCE_NOTE',original['evidence'] or old['name'],old)
                edge['edge_type']='SOURCE_REFERENCE'
                report['conditions_pending_source']+=1
        if old['node_type'] in ('DOC','FILE') and is_invalid_document(label):
            full=source.get('doc_ref') or original['evidence'] or old['name']
            citations=document_numbers(full)
            targets=[ensure('DOC',citation,old) for citation in citations]
            if not targets:
                targets=[ensure('SOURCE_NOTE',full,old)]
                report['doc_source_notes']+=1
            else:
                report['doc_fragment_edges_restored']+=1
            restored_docs.append({'policy':original['src_id'],'fragment':old['name'],
                'source':full,'targets':targets})
            for target in targets:
                new=deepcopy(edge)
                new.update(dst_id=target,dst_type=out[target]['node_type'],evidence=full,
                    edge_type='FROM_DOC' if targets and out[target]['node_type']=='DOC' else 'SOURCE_REFERENCE')
                updated.append(new)
            continue
        edge['dst_type']=out[edge['dst_id']]['node_type']
        edge['src_type']=out[edge['src_id']]['node_type']
        if edge['dst_type']=='ORG' and edge['edge_type']=='FROM_DOC':
            edge['edge_type']='REFERENCES_ORG'
        updated.append(edge)
    grouped={}
    for edge in updated:
        key=(edge['src_id'],edge['dst_id'],edge['edge_type'])
        if key not in grouped:
            grouped[key]=edge
        elif edge.get('evidence') and edge['evidence'] not in (grouped[key].get('evidence') or ''):
            grouped[key]['evidence']=(grouped[key].get('evidence') or '')+'\n'+edge['evidence']
    used={edge[k] for edge in grouped.values() for k in ('src_id','dst_id')}
    # Keep unrelated existing orphan nodes; retire only confirmed invalid/replaced fragments.
    retired={mapping[old] for old,n in nodes.items() if
        (n['node_type'] in ('DOC','FILE') and is_invalid_document(n['name'])) or
        (n['node_type']=='CONDITION' and n['name'].strip() in INCOMPLETE_CONDITIONS)}
    for nid in retired-used:
        out.pop(nid,None)
    return out,list(grouped.values()),dict(report),restored_docs


def rebuild_same_doc(nodes,edges,active,max_policies=20):
    groups=defaultdict(set)
    for edge in edges:
        if edge['edge_type']=='FROM_DOC' and nodes[edge['dst_id']]['node_type'] in ('DOC','FILE'):
            groups[edge['dst_id']].add(edge['src_id'])
    pairs=defaultdict(list)
    large=[]
    for doc,group in sorted(groups.items()):
        if len(group)>max_policies:
            large.append({'document':doc,'policies':len(group)})
            continue
        for a,b in itertools.combinations(sorted(group & active),2):
            pairs[a,b].append('同文件:'+nodes[doc]['name'])
    rows=[dict(src_id=a,dst_id=b,edge_type='SAME_DOC',weight=1.0,
        shared_attrs=sorted(set(attrs)),jaccard=1.0) for (a,b),attrs in sorted(pairs.items())]
    return rows,large


def policy_features(nodes,edges,policies,active):
    features={pid:set() for pid in active}
    for edge in edges:
        pid=edge['src_id']
        if pid not in features:
            continue
        n=nodes[edge['dst_id']]
        kind=n['node_type']
        if kind not in FEATURE_WEIGHTS:
            continue
        name=normalize_label(kind,n['name'])
        if kind=='RISK':
            name=canonical_risk(name)
        if kind=='REGION' and name in ('江西省全省','全省','江西省'):
            continue
        if kind=='POPULATION' and name in ('一般农户','所有农户','家庭','帮扶对象'):
            continue
        if kind=='CONDITION':
            if name in ('无','不限','无限制'):
                continue
        if kind=='SUBSIDY' and n.get('attrs',{}).get('requires_unit_review'):
            continue
            age=re.search(r'劳动力年龄:(\d+)-(\d+)',name)
            if age and (int(age[2])>=100 or int(age[2])-int(age[1])>=60):
                continue
        if name:
            features[pid].add(kind+':'+name)
    for pid in active:
        p=policies[pid]
        for key,kind in (('category2','CATEGORY2'),('category3','CATEGORY3')):
            label=re.sub(r'\s+','',p.get(key) or '')
            if label and label not in ('无','其他','不限'):
                features[pid].add(kind+':'+label)
    return features


def rebuild_similarity(nodes,edges,policies,active,threshold=.4,max_degree=20):
    """Auditable new algorithm; does not claim to reproduce the missing v2 builder."""
    features=policy_features(nodes,edges,policies,active)
    inverted=defaultdict(list)
    for pid,attrs in features.items():
        for attr in attrs:
            inverted[attr].append(pid)
    weights={attr:FEATURE_WEIGHTS[attr.split(':',1)[0]]*(1+math.log((len(active)+1)/(len(pids)+1)))
        for attr,pids in inverted.items()}
    shared_count=Counter()
    for pids in inverted.values():
        by_category=defaultdict(list)
        for pid in pids:
            by_category[policies[pid].get('category1')].append(pid)
        for group in by_category.values():
            for pair in itertools.combinations(sorted(group),2):
                shared_count[pair]+=1
    totals={pid:sum(weights[a] for a in attrs) for pid,attrs in features.items()}
    candidates=[]
    for (a,b),count in shared_count.items():
        if count<2 or policies[a].get('category1')!=policies[b].get('category1'):
            continue
        # Different explicit industry subtypes must not score as interchangeable policies.
        if policies[a].get('category1')=='产业帮扶':
            ca=re.sub(r'\s+','',policies[a].get('category3') or '')
            cb=re.sub(r'\s+','',policies[b].get('category3') or '')
            if ca and cb and ca!='其他' and cb!='其他' and ca!=cb:
                continue
        common=features[a]&features[b]
        if not any(x.split(':',1)[0] in ('CATEGORY2','CATEGORY3','CONDITION','SUBSIDY') for x in common):
            continue
        intersection=sum(weights[x] for x in common)
        union=totals[a]+totals[b]-intersection
        score=intersection/union if union else 0
        if score>=threshold:
            candidates.append((a,b,score,sorted(common)))
    degree=Counter()
    rows=[]
    for a,b,score,common in sorted(candidates,key=lambda x:(-x[2],x[0],x[1])):
        if degree[a]>=max_degree or degree[b]>=max_degree:
            continue
        rows.append(dict(src_id=a,dst_id=b,edge_type='SIM_ATTR',weight=score,
            shared_attrs=common,jaccard=score,algo_version=ALGO_VERSION))
        degree[a]+=1;degree[b]+=1
    fingerprint=hashlib.sha256(json.dumps({p:sorted(a) for p,a in sorted(features.items())},
        ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    return rows,dict(feature_fingerprint=fingerprint,threshold=threshold,max_degree=max_degree,
        candidate_pairs=len(candidates),degree_max=max(degree.values(),default=0),
        edge_count=len(rows),algo_version=ALGO_VERSION,feature_weights=FEATURE_WEIGHTS)


def verify(nodes,edges,same_doc,sim):
    for table in (edges,same_doc,sim):
        keys=set()
        for edge in table:
            a,b=edge['src_id'],edge['dst_id']
            key=(a,b,edge['edge_type'])
            if a not in nodes or b not in nodes or a==b or key in keys:
                raise RuntimeError('Graph integrity violation: '+str(key))
            if table is edges and not str(edge.get('evidence') or '').strip():
                raise RuntimeError('Missing evidence: '+str(key))
            keys.add(key)
    if any(n['node_type'] in ('DOC','FILE') and is_invalid_document(n['name']) for n in nodes.values()):
        raise RuntimeError('Document fragment remains')
    if any(n['node_type']=='CONDITION' and n['name'].strip() in INCOMPLETE_CONDITIONS for n in nodes.values()):
        raise RuntimeError('Condition fragment remains')


def run(apply=False,suffix='rebuild_20261012'):
    if not re.fullmatch(r'[a-z][a-z0-9_]{7,40}',suffix):
        raise ValueError('Invalid backup suffix')
    engine=create_engine(config.DATABASE_URL)
    with engine.begin() as c:
        if apply:
            if 'provenance' in {col['name'] for col in inspect(c).get_columns('kg_edge')}:
                raise RuntimeError('正式图谱已启用明细溯源，请使用 kg_source_update.py 重建，避免旧迁移清空溯源字段')
            with create_engine(config.AUTH_DATABASE_URL).connect() as ac:
                if ac.execute(text("SELECT count(*) FROM qa_log WHERE status='generating'")).scalar():
                    raise RuntimeError('Active answer turns; apply deferred')
            c.execute(text("SET LOCAL lock_timeout='5s'"))
            c.execute(text('LOCK TABLE kg_node,kg_edge,kg_policy_edge,kg_policy_sim,policy,kg_update_queue IN SHARE ROW EXCLUSIVE MODE'))
        else:
            c.execute(text('SET TRANSACTION READ ONLY'))
        nodes={r['id']:dict(r) for r in c.execute(text('SELECT * FROM kg_node')).mappings()}
        edges=[dict(r) for r in c.execute(text('SELECT * FROM kg_edge')).mappings()]
        policies={'POLICY:'+r['policy_code']:dict(r) for r in c.execute(text('SELECT policy_code,category1,category2,category3,conditions,doc_ref,status FROM policy')).mappings()}
        dirty={'POLICY:'+r[0] for r in c.execute(text("SELECT policy_code FROM kg_update_queue WHERE status='pending'"))}
        active={pid for pid,p in policies.items() if p['status']=='在用' and pid in nodes and pid not in dirty}
        newnodes,newedges,normalization,docs=normalize_graph(nodes,edges,policies)
        same_doc,large=rebuild_same_doc(newnodes,newedges,active,config.KG_SAME_DOC_MAX_POLICIES)
        sim,sim_report=rebuild_similarity(newnodes,newedges,policies,active)
        verify(newnodes,newedges,same_doc,sim)
        report={'apply':apply,'normalization_version':NORMALIZATION_VERSION,
            'normalization':normalization,'restored_document_fragments':docs,
            'same_doc_count':len(same_doc),'large_documents_without_pair_edges':large,
            'same_doc_max_policies':config.KG_SAME_DOC_MAX_POLICIES,
            'similarity':sim_report,'node_count':len(newnodes),'attribute_edge_count':len(newedges),
            'detail_cap':config.KG_DETAIL_CAP}
        if apply:
            report['backups']=[]
            for table in ('kg_node','kg_edge','kg_policy_edge','kg_policy_sim'):
                backup=table+'_'+suffix
                if inspect(c).has_table(backup):
                    raise RuntimeError('Backup exists; refuse repeated apply')
                c.execute(text('CREATE TABLE '+backup+' AS SELECT * FROM '+table))
                report['backups'].append(backup)
            for nid,n in newnodes.items():
                if nid in nodes:
                    continue
                c.execute(text('INSERT INTO kg_node(id,node_type,name,source_table,source_field,attrs,created_at) VALUES(:id,:node_type,:name,:source_table,:source_field,CAST(:attrs AS jsonb),:created_at)'),
                    dict(n,attrs=json.dumps(n.get('attrs') or {},ensure_ascii=False)))
            c.execute(text('DELETE FROM kg_edge'))
            fields=('edge_id','src_type','src_id','dst_type','dst_id','edge_type','weight','source_field','evidence','created_at')
            rows=[dict(edge,edge_id=i) for i,edge in enumerate(newedges,1)]
            sql='INSERT INTO kg_edge('+','.join(fields)+') VALUES('+','.join(':'+f for f in fields)+')'
            for start in range(0,len(rows),500):
                c.execute(text(sql),rows[start:start+500])
            for nid in set(nodes)-set(newnodes):
                c.execute(text('DELETE FROM kg_node WHERE id=:n'),{'n':nid})
            # Update names of stable identity nodes only when their label changed.
            for nid,n in newnodes.items():
                if nid in nodes and n['name']!=nodes[nid]['name']:
                    c.execute(text('UPDATE kg_node SET name=:name WHERE id=:id'),{'name':n['name'],'id':nid})
            for table,rows in (('kg_policy_edge',same_doc),('kg_policy_sim',sim)):
                c.execute(text('DELETE FROM '+table+" WHERE edge_type=:et"),{'et':'SAME_DOC' if table=='kg_policy_edge' else 'SIM_ATTR'})
                fields=('src_id','dst_id','edge_type','weight','shared_attrs','jaccard')
                if table=='kg_policy_sim':fields+=('algo_version',)
                sql='INSERT INTO '+table+'('+','.join(fields)+') VALUES('+','.join('CAST(:shared_attrs AS jsonb)' if f=='shared_attrs' else ':'+f for f in fields)+')'
                for start in range(0,len(rows),500):
                    c.execute(text(sql),[dict(r,shared_attrs=json.dumps(r['shared_attrs'],ensure_ascii=False)) for r in rows[start:start+500]])
            # Keep identity sequences above the replacement rows for future builders.
            for table,key in (('kg_edge','edge_id'),('kg_policy_edge','edge_id'),('kg_policy_sim','edge_id')):
                seq=c.execute(text('SELECT pg_get_serial_sequence(:table,:key)'),{'table':table,'key':key}).scalar()
                if seq:
                    c.execute(text('SELECT setval(:seq,(SELECT COALESCE(MAX(edge_id),1) FROM '+table+'),true)'),{'seq':seq})
            checknodes={r['id']:dict(r) for r in c.execute(text('SELECT * FROM kg_node')).mappings()}
            checkedges=[dict(r) for r in c.execute(text('SELECT * FROM kg_edge')).mappings()]
            checksame=[dict(r) for r in c.execute(text("SELECT * FROM kg_policy_edge WHERE edge_type='SAME_DOC'")).mappings()]
            checksim=[dict(r) for r in c.execute(text("SELECT * FROM kg_policy_sim WHERE edge_type='SIM_ATTR'")).mappings()]
            verify(checknodes,checkedges,checksame,checksim)
            if (len(checksame),len(checksim))!=(len(same_doc),len(sim)):
                raise RuntimeError('Published counts differ; roll back')
    root=Path(__file__).resolve().parents[2]
    outfile=root/'outputs'/('kg_rebuild_result.json' if apply else 'kg_rebuild_preview.json')
    outfile.write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k not in ('restored_document_fragments','large_documents_without_pair_edges')},ensure_ascii=False,indent=2))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true',help='Default is read-only preview')
    parser.add_argument('--backup-suffix',default='rebuild_20261012')
    args=parser.parse_args()
    run(args.apply,args.backup_suffix)


if __name__=='__main__':
    main()
