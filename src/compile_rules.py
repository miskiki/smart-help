# -*- coding: utf-8 -*-
"""C1 批量编译入口：把某县地区适用条款编译入库。

用法：
  python compile_rules.py --county 莲花 --limit 3          # 小样本验证
  python compile_rules.py --county 莲花 --county 吉安      # 批量
  python compile_rules.py --stats
"""
import sys, os, json, time, argparse
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
import config, kb_rag
from conversations import selected_config
from policy_core import compiler
from policy_core.fields import dictionary
from policy_core.evidence_loader import load as load_evidence
from policy_core.full_settings import limits
from policy_core.full_runtime import Budget
from policy_core.reasoning import _SCOPE_CACHE
from sqlalchemy import bindparam

ROOT_PROJECT = os.path.dirname(os.path.dirname(ROOT))


def candidates_for_county(county):
    """取全部在用政策及其明细；地区过滤交给 evidence_loader 的 applicable_region 统一判定。

    注意三类容易被 SQL 预过滤误杀的记录（本项目都踩过）：
      * detail.region='江西省全省'（省级规则，字符串不等于'江西省'）
      * 政策本身没有明细行（conditions 挂在 policy 上）
      * 同一政策第 3+ 个明细分支（编译阶段需要全量，不受每政策 2 分支限制）
    """
    from kb_rag import _db
    from sqlalchemy import text
    engine = _db()
    try:
        with engine.connect() as conn:
            conn.execute(text('SET TRANSACTION READ ONLY'))
            policies = [dict(r) for r in conn.execute(text(
                "SELECT policy_id, policy_code, cur_version FROM policy WHERE status='在用'"
            )).mappings()]
            details = {}
            pids = [p['policy_id'] for p in policies]
            for i in range(0, len(pids), 500):
                chunk = pids[i:i + 500]
                for row in conn.execute(text(
                        'SELECT policy_id, detail_id FROM policy_detail WHERE policy_id IN :pids'
                ).bindparams(bindparam('pids', expanding=True)), dict(pids=chunk)).mappings():
                    details.setdefault(row['policy_id'], []).append(row['detail_id'])
    finally:
        engine.dispose()
    out = []
    for p in policies:
        for detail_id in (details.get(p['policy_id']) or [None]):
            out.append(dict(policy_code=p['policy_code'], policy_version=p['cur_version'], detail_id=detail_id))
    return out


def packs_for_county(county, region_text, config_like):
    cands = candidates_for_county(county)
    packs, stats = load_evidence(cands, region_text, config_like)
    # 编译只针对"地区明确适用"的条款；scope=None（地区未确认）留给在线追问，不编进本县规则集。
    def compilable(pack):
        if pack.get('region_status') is True:
            return True
        # 省级政策未写适用地区（regions IS NULL）且无明细行：按全省适用处理。
        # 这类恰好是资助参保、医疗救助、危房改造、免学杂费等核心政策，漏掉会直接丢卡。
        return (pack.get('region_status') is None and not pack['policy'].get('regions')
                and str(pack['policy'].get('level')) == 'S')
    packs = [p for p in packs if compilable(p)]
    # 按条款文本去重：同一文本只编译一次
    seen, unique = {}, []
    for pack in packs:
        h = compiler.text_hash_of(pack)
        if h in seen:
            seen[h]['_detail_ids'].append(pack['detail'].get('detail_id') or 'main')
            continue
        pack['_text_hash'] = h
        pack['_detail_ids'] = [pack['detail'].get('detail_id') or 'main']
        seen[h] = pack
        unique.append(pack)
    return packs, unique, stats


def region_text_for(county):
    """县名（短名或全名）→ 完整行政区划文本（懒编译给任意县用，不再写死莲花/吉安）。

    短名必须按 geo 里的真实后缀解析：新建 → 新建区（不是 新建县）。
    写死后缀会让 evidence_loader 地区匹配把该县全部政策判为不适用
    （踩过：新建区 63 条政策全被丢，规则库只编到省级，规则模式永远不就绪）。
    """
    from policy_core.geo import JIANGXI_COUNTY
    name = str(county or '').strip()
    if not name:
        return '江西省'
    base = name
    if base.endswith(('县', '区', '市')):
        base = base[:-1]
    for city, counties in JIANGXI_COUNTY.items():
        for full in counties:
            if full == name or (len(base) >= 2 and full[:-1] == base):
                return '江西省' + city + full
    if name.endswith(('县', '区', '市')):
        return '江西省' + name
    return '江西省' + name + '县'


def compile_regions(counties, limit=0, workers=4, dry=False, dump='', quiet=False):
    """编译指定县的地区适用规则，返回 dict(total, done, failed)。

    写入两处：原型 SQLite（eval/compiled_rules.db）+ 金仓 policy_rule。
    金仓写的失败不影响编译本身（迁移期两边并行）。
    """
    import policy_core.rule_store as rule_store
    counties = counties or ['莲花']
    _, cfg = selected_config('本地Qwen3.6-35B / qwen3.6-35b-a3b')
    log = (lambda *a, **k: None) if quiet else print
    log('编译模型端点:', cfg['base_url'], cfg.get('llm_model'))

    fields = dictionary()
    config_like = dict(limits(), per_policy_limit=999)
    # 已编译过的条款文本直接复用（省级/全国规则跨县共享）；两边取并集
    existing = {}
    conn = compiler._conn()
    try:
        for row in conn.execute('SELECT text_hash, county FROM policy_rule WHERE status!="failed"'):
            existing[row[0]] = row[1]
    finally:
        conn.close()
    try:
        existing.update(rule_store.all_hashes())
    except Exception:
        pass

    all_tasks = []
    for county in counties:
        region_text = region_text_for(county)
        packs, unique, stats = packs_for_county(county, region_text, config_like)
        log('[%s] 地区适用切片 %d → 文本去重后 %d 条待编译 (dropped=%s)' % (
            county, len(packs), len(unique), stats.get('dropped')))
        reused = 0
        for pack in unique:
            if pack['_text_hash'] in existing:
                reused += 1
                continue
            region_tag = str(pack['detail'].get('region') or '') or str(pack['policy'].get('regions') or '')
            pack['_county_tag'] = '*' if region_tag in ('全省', '全国', '国家', '省级', '江西省', '') else county
            all_tasks.append((county, pack))
        log('[%s] 复用已编译 %d 条，新增待编译 %d 条' % (county, reused, len(unique) - reused))

    seen_hash = set()
    deduped = []
    for county, pack in all_tasks:
        if pack['_text_hash'] in seen_hash:
            continue
        seen_hash.add(pack['_text_hash'])
        deduped.append((county, pack))
    all_tasks = deduped
    if limit:
        all_tasks = all_tasks[:limit]
    log('本轮待编译总数:', len(all_tasks))
    if dry:
        for county, pack in all_tasks:
            log('  %s %s %s %s' % (pack['policy']['code'], (pack['detail'].get('detail_id') or 'main'),
                                   (pack['policy']['name'] or '')[:26], pack['detail'].get('region')))
        return dict(total=len(all_tasks), done=0, failed=0)

    budget_cfg = dict(config_like, deadline=10 ** 7, max_calls=10 ** 7)
    budget = Budget(kb_rag._llm_answer, budget_cfg)

    def llm(*args, **kw):
        kw['cfg'] = cfg
        return kb_rag._llm_answer(*args, **kw)
    budget.llm = llm

    done = failed = 0
    t0 = time.monotonic()
    out = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {}
        for county, pack in all_tasks:
            futures[pool.submit(compiler.compile_pack, pack, fields, budget, 2)] = (county, pack)
        for future in as_completed(futures):
            county, pack = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                result = dict(ok=False, status='failed', errors=[type(exc).__name__ + ':' + str(exc)[:120]],
                              rule=None, attempts=0)
            record = dict(key='%s:%s:%s' % (pack['policy']['code'], (pack['detail'].get('detail_id') or 'main'), pack['policy'].get('version')),
                          policy_code=pack['policy']['code'], detail_id=(pack['detail'].get('detail_id') or 'main'),
                          version=pack['policy'].get('version') or '',
                          text_hash=pack['_text_hash'],
                          county=(pack.get('_county_tag') or county),
                          status=result.get('status') or ('complete' if result.get('ok') else 'failed'),
                          rule=result.get('rule'), errors=result.get('errors') or [],
                          compiler_model=str(cfg.get('llm_model')), compiled_at=time.strftime('%Y-%m-%dT%H:%M:%S'),
                          attempts=result.get('attempts') or 0)
            compiler.save(record)
            try:
                rule_store.save(record)
            except Exception as exc:
                log('  金仓写入失败 %s: %s' % (record['key'], str(exc)[:100]))
            out.append(dict(policy_code=record['policy_code'], detail_id=record['detail_id'],
                            name=pack['policy'].get('name'), county=county, status=record['status'],
                            errors=record['errors'][:3], rule=record['rule']))
            if result.get('ok'):
                done += 1
                log('  OK  %-10s %-7s %s' % (record['policy_code'], record['status'],
                                             (pack['policy']['name'] or '')[:30]))
            else:
                failed += 1
                log('  FAIL %-10s %s' % (record['policy_code'], (record['errors'] or ['?'])[:2]))
    elapsed = time.monotonic() - t0
    log('\n完成 %d / 失败 %d，耗时 %.1f 分钟，成功率 %.0f%%' % (
        done, failed, elapsed / 60, 100 * done / max(1, done + failed)))
    log('入库统计:', compiler.stats())
    if dump:
        json.dump(out, open(os.path.join(ROOT, 'eval', dump), 'w'), ensure_ascii=False, indent=1)
        log('已 dump:', dump)
    return dict(total=len(all_tasks), done=done, failed=failed)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--county', action='append', default=[])
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--workers', type=int, default=4)
    ap.add_argument('--stats', action='store_true')
    ap.add_argument('--dry', action='store_true', help='只打印待编译清单')
    ap.add_argument('--dump', default='', help='把编译结果 dump 到 json 文件')
    a = ap.parse_args()
    if a.stats:
        print('已入库统计:', compiler.stats())
        return
    compile_regions(a.county, limit=a.limit, workers=a.workers, dry=a.dry, dump=a.dump)


if __name__ == '__main__':
    main()
