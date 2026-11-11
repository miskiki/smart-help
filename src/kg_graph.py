# -*- coding: utf-8 -*-
"""知识图谱展示只读模块：图规模统计 + 政策子图导出。
仅读取 kg_retrieval 已加载的正式图谱内存数据与 policy 台账，不写库、不接问答链路。
"""
import config
from models import db, Policy
import kg_retrieval

# 边类型 → 单节点单跳截断上限（防超级节点/图爆炸；SIM 权重排序见 kg_retrieval._load）
EDGE_TOP = {
    'APPLIES_TO': 25, 'COVERS': 20, 'REQUIRES': 25, 'IN_REGION': 40,
    'BELONGS_TO': 5, 'FROM_DOC': 10, 'HAS_SUBSIDY': 10,
    'SAME_DOC': 12, 'SIM_ATTR': 15,
}
NODE_TYPE_CN = {
    'POLICY': '政策', 'POPULATION': '对象', 'REGION': '地区', 'RISK': '风险',
    'CONDITION': '条件', 'CATEGORY': '分类', 'FILE': '文件', 'DOC': '文件', 'ORG': '文号字段列示机构',
}


def graph_stats():
    """图规模统计（只读内存 + policy 台账）"""
    nodes, edges, same_doc, sim = kg_retrieval._load()[0]
    node_types, edge_types = {}, {}
    for nid, meta in nodes.items():
        node_types[meta['node_type']] = node_types.get(meta['node_type'], 0) + 1
    for et, m in edges.items():
        n = sum(len(v) for v in m.values())
        if n:
            edge_types[et] = n
    sim_weights = {'0.3-0.5': 0, '0.5-0.7': 0, '0.7-0.9': 0, '0.9-1.0': 0}
    for src, lst in sim.items():
        for dst, w in lst:
            if src >= dst:
                continue
            if w < 0.5:
                sim_weights['0.3-0.5'] += 1
            elif w < 0.7:
                sim_weights['0.5-0.7'] += 1
            elif w < 0.9:
                sim_weights['0.7-0.9'] += 1
            else:
                sim_weights['0.9-1.0'] += 1
    isolated = 0
    isolated_ids = set()
    for nid in nodes:
        if nid.startswith('POLICY:') and not same_doc.get(nid) and not sim.get(nid):
            isolated += 1
            isolated_ids.add(nid)
    connected = set()
    for mapping in edges.values():
        for dst, rows in mapping.items():
            connected.add(dst)
            connected.update(src for src, _ev, _sf in rows)
    isolated_categories = {}
    for dst, rows in edges.get('BELONGS_TO', {}).items():
        name = nodes.get(dst, {}).get('name', dst)
        count = len({src for src, _ev, _sf in rows if src in isolated_ids})
        if count:
            isolated_categories[name] = count
    stat = kg_retrieval.cache_status()
    sim_total = sum(sim_weights.values())
    return {
        'nodes': len(nodes),
        'node_types': node_types,
        'edges': sum(edge_types.values()),
        'edge_types': edge_types,
        'same_doc': sum(len(v) for v in same_doc.values()) // 2,
        'sim_attr': sim_total,
        'sim_weights': sim_weights,
        'isolated_policies': isolated,
        'isolated_by_category': isolated_categories,
        'fully_isolated_policies': len(isolated_ids - connected),
        'cache': stat,
    }


def search_policies(kw, limit=20):
    """政策起点搜索：编号/名称/文号 模糊（仅用于图谱展示起点选择）"""
    kw = (kw or '').strip()
    q = Policy.query.order_by(Policy.policy_code)
    if kw:
        like = '%%%s%%' % kw
        q = q.filter(db.or_(Policy.policy_name.like(like),
                            Policy.policy_code.like(like),
                            Policy.doc_ref.like(like)))
    items = []
    for p in q.limit(limit).all():
        items.append({'code': p.policy_code, 'name': p.policy_name,
                      'level': p.level, 'category1': p.category1, 'status': p.status})
    return items


def list_nodes(node_type=None, kw=None, limit=100):
    """按节点类型浏览（RISK/CATEGORY/POPULATION/REGION/CONDITION…），带政策关联数。
    政策关联数 = 指向该节点的属性边中去重 POLICY 源数（供前端排序展示）。"""
    nodes, edges, _sd, _sim = kg_retrieval._load()[0]
    policy_cnt = {}
    for et in ('APPLIES_TO', 'COVERS', 'REQUIRES', 'IN_REGION', 'BELONGS_TO', 'HAS_SUBSIDY'):
        for dst, lst in edges.get(et, {}).items():
            n = set(s for s, _e, _f in lst if s.startswith('POLICY:'))
            if n:
                policy_cnt[dst] = policy_cnt.get(dst, 0) + len(n)
    kw = (kw or '').strip()
    items = []
    for nid, meta in nodes.items():
        if node_type and meta['node_type'] != node_type:
            continue
        if kw and kw not in meta['name']:
            continue
        items.append({'id': nid, 'type': meta['node_type'], 'name': meta['name'],
                      'policies': policy_cnt.get(nid, 0)})
    items.sort(key=lambda x: -x['policies'])
    return items[:limit]


def build_subgraph(seed_nid, hops=2, limit=300):
    """以 seed 节点为中心构建 1~2 跳子图。
    返回 {'nodes':[{id,type,name}], 'edges':[{src,dst,type,weight,evidence}]}
    hops∈{1,2}；SIM 边带 weight；属性边带 evidence（原文片段，截断 80 字）。
    """
    hops = 1 if hops < 2 else 2
    nodes, edges, same_doc, sim = kg_retrieval._load()[0]
    if seed_nid not in nodes:
        return None
    # 正向索引（原 edges 为 dst→src 倒排）
    fwd = {}
    for et, m in edges.items():
        for dst, lst in m.items():
            for src, ev, sf in lst:
                fwd.setdefault(et, {}).setdefault(src, []).append((dst, ev, sf))

    out_nodes, out_edges, seen_edges = [], [], set()
    visited = {seed_nid}
    out_nodes.append({'id': seed_nid, 'type': nodes[seed_nid]['node_type'],
                      'name': nodes[seed_nid]['name']})
    frontier = [seed_nid]
    node_ids = [seed_nid]
    omitted_edges = set()
    omitted_nodes = set()

    def _add_edge(src, dst, et, ev, w):
        if et in ('SAME_DOC', 'SIM_ATTR'):
            src, dst = sorted((src, dst))
        key = (src, dst, et)
        if key in seen_edges:
            return
        seen_edges.add(key)
        out_edges.append({'src': src, 'dst': dst, 'type': et,
                          'weight': w, 'evidence': (ev or '')[:80]})

    def _visit(dst):
        if dst in visited or dst not in nodes or len(node_ids) >= limit:
            return False
        visited.add(dst)
        node_ids.append(dst)
        out_nodes.append({'id': dst, 'type': nodes[dst]['node_type'], 'name': nodes[dst]['name']})
        return True

    for hop in range(hops):
        nxt = []
        for nid in frontier:
            # 属性边（双向：出边 fwd + 入边 edges）
            for et, m in edges.items():
                top = EDGE_TOP.get(et, 20)
                outgoing = sorted(fwd.get(et, {}).get(nid, []))
                incoming = sorted(m.get(nid, []))
                for dst, _ev, _sf in outgoing[top:]:
                    omitted_edges.add((nid,dst,et))
                    omitted_nodes.add(dst)
                for src, _ev, _sf in incoming[top:]:
                    omitted_edges.add((src,nid,et))
                    omitted_nodes.add(src)
                for dst, ev, _sf in outgoing[:top]:
                    _add_edge(nid, dst, et, ev, None)
                    if _visit(dst):
                        nxt.append(dst)
                for src, ev, _sf in incoming[:top]:
                    _add_edge(src, nid, et, ev, None)
                    if _visit(src):
                        nxt.append(src)
            # Shared documents are shown as policy -> document -> policy stars.
            # Keep pair edges only for legacy graphs without a document node.
            has_document = bool(fwd.get('FROM_DOC', {}).get(nid))
            # 政策间边（仅 POLICY 节点）
            if nid.startswith('POLICY:'):
                for p in ([] if has_document else same_doc.get(nid, []))[EDGE_TOP['SAME_DOC']:]:
                    omitted_edges.add((*sorted((nid,p)), 'SAME_DOC'))
                    omitted_nodes.add(p)
                for p, _w in sim.get(nid, [])[EDGE_TOP['SIM_ATTR']:]:
                    omitted_edges.add((*sorted((nid,p)), 'SIM_ATTR'))
                    omitted_nodes.add(p)
                for p in sorted([] if has_document else same_doc.get(nid, []))[:EDGE_TOP['SAME_DOC']]:
                    _add_edge(nid, p, 'SAME_DOC', None, 1.0)
                    if _visit(p):
                        nxt.append(p)
                for p, w in sim.get(nid, [])[:EDGE_TOP['SIM_ATTR']]:
                    _add_edge(nid, p, 'SIM_ATTR', None, round(w, 3))
                    if _visit(p):
                        nxt.append(p)
        frontier = nxt
    for node in out_nodes:
        if node['type'] in ('DOC', 'FILE'):
            node['linked_policies'] = len({src for src, _ev, _sf in edges.get('FROM_DOC', {}).get(node['id'], [])})
    retained = []
    for edge in out_edges:
        if edge['src'] in visited and edge['dst'] in visited:
            retained.append(edge)
        else:
            omitted_edges.add((edge['src'],edge['dst'],edge['type']))
            omitted_nodes.update((edge['src'],edge['dst']))
    retained_keys = {(e['src'],e['dst'],e['type']) for e in retained}
    omitted_edges -= retained_keys
    return {'nodes': out_nodes, 'edges': retained,
            'truncated': bool(omitted_edges),
            'omitted_edges': len(omitted_edges),
            'omitted_nodes': len(omitted_nodes - visited)}
