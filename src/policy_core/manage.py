"""python -B -m policy_core.manage inventory|status|publish [--regions ...]."""
import argparse
import collections
import concurrent.futures
import json
from .catalog import refresh,applicable_region
from . import store
from .publishing import publish_batch

def status():
    with store.connection() as c:
        counts=dict(c.execute('SELECT p.status,count(*) FROM publication p JOIN active_source a ON a.digest=p.digest GROUP BY p.status').fetchall())
        errors=[dict(r) for r in c.execute("SELECT p.status,p.attempts,p.audit FROM publication p JOIN active_source a ON a.digest=p.digest WHERE p.status='retry' ORDER BY p.updated DESC LIMIT 5")]
    return dict(states=counts,recent_errors=errors)

def main():
    parser=argparse.ArgumentParser();parser.add_argument('command',choices=['inventory','status','publish']);parser.add_argument('--regions',nargs='*',default=[]);parser.add_argument('--batches',type=int,default=4);parser.add_argument('--workers',type=int,default=2);parser.add_argument('--batch-size',type=int,default=2)
    args=parser.parse_args()
    if args.command=='status':print(json.dumps(status(),ensure_ascii=False));return
    catalog=refresh(force=True);print(json.dumps(catalog['inventory'],ensure_ascii=False),flush=True)
    if args.command=='inventory':return
    from kg_retrieval import _load,_cache,match_region_nodes
    from kb_rag import _llm_answer
    nodes=_load()[0][0];index=_cache['region_idx'];priority=set()
    for region in args.regions:
        hh_nodes=match_region_nodes(region,nodes,index)[0]
        priority.update(i['digest'] for i in catalog['instances'] if i['pilot'] and applicable_region(i,region,nodes,index,hh_nodes) is True)
    def work(_):
        batch=store.queue(args.batch_size,priority)
        if batch:publish_batch(batch,_llm_answer)
        return len(batch)
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        for count in pool.map(work,range(args.batches)):print('processed',count,flush=True)
    print(json.dumps(status(),ensure_ascii=False),flush=True)

if __name__=='__main__':main()
