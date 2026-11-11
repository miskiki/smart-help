"""T5 冲突口径程序化裁决与审计缓存的单元测试。

覆盖：三分类（program/source_missing/semantic）、程序 route 的 schema 合法性、
真实 _scope_value 三值求值、exclude 只在严格 False 时发生、模型批次最小化、
CONFLICT_SCOPE_PROGRAM 关闭回退、EVIDENCE_CACHE 开关与 LRU 容量、缓存命中/过期。
全部用标准库 unittest，不连接模型、不读生产数据。
"""
import copy
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT/'src') not in sys.path:sys.path.insert(0,str(ROOT/'src'))

from policy_core import conflict_scope_program as csp
from policy_core import reasoning

FIELDS={
    'age':{'type':'number','scope':'member','min':0,'max':120,'label':'年龄'},
    'hukou_region':{'type':'string','scope':'member','label':'户籍所在地'},
    'household_income':{'type':'number','scope':'household','min':0,'max':1000000,'label':'家庭年收入'},
}
QUOTE_AGE='申请人年龄60周岁以上。'
QUOTE_REGION='申请人户籍在江西省。'


def state(age=45,hukou='湖南省长沙市',age_state='known'):
    return {'members':['张三'],
            'facts':{'张三:age':{'state':age_state,'value':age},
                     '张三:hukou_region':{'state':'known','value':hukou},
                     'household:household_income':{'state':'known','value':20000}},
            'fact_revision':'rev-1'}


def pack(quote=QUOTE_AGE,source='original_conditions',pack_id='p1',source_hash='hash-1'):
    return {'id':pack_id,'source':{source:quote},'source_hash':source_hash}


def numeric_claim(value=60,fact_key='张三:age',op='gte'):
    return {'citation':{'source':'original_conditions','segment_id':0,'quote':QUOTE_AGE},
            'fact_keys':[fact_key],'test':{'fact_key':fact_key,'op':op,'value':value},
            'meaning':'已知家庭事实与该原文明示条件冲突'}


def region_claim(value='江西省',fact_key='张三:hukou_region'):
    return {'citation':{'source':'original_conditions','segment_id':0,'quote':QUOTE_REGION},
            'fact_keys':[fact_key],'test':{'fact_key':fact_key,'op':'eq','value':value},
            'meaning':'已知家庭事实与该原文明示条件冲突'}


def decision(claims=None,pack_id='p1',member='张三',**kw):
    d={'id':pack_id+':'+member,'source_id':pack_id,'member':member,
       'conflicts':copy.deepcopy(claims or []),'object_matches':[],'matches':[],
       'unknowns':[],'source_gaps':[],
       'status':'exclude' if claims else 'unconfirmed'}
    d.update(kw)
    return d


def review(did='p1:张三',scope_status='supported',issues=None):
    return {'id':did,'scope_status':scope_status,'issues':list(issues or []),
            'source_ambiguities':[]}


class TestClassify(unittest.TestCase):
    def test_program_numeric_hard_conflict(self):
        d=decision([numeric_claim()])
        result=csp.classify(d,pack(),state(age=45),FIELDS)
        self.assertEqual(result['category'],csp.PROGRAM)
        self.assertEqual(result['reason'],'all_conflict_atoms_programmatic')
        atom=result['claims'][0]
        self.assertEqual(atom['fact_key'],'张三:age')
        self.assertIs(atom['satisfied'],False)
        self.assertIs(atom['equivalent'],False)
        self.assertIs(atom['actual'],45)
        self.assertEqual(atom['required'],60)
        self.assertEqual(atom['route_value'],60)

    def test_program_equivalent_region_variant_is_satisfied(self):
        d=decision([region_claim()])
        st=state(hukou='江西省南昌市新建区')
        result=csp.classify(d,pack(QUOTE_REGION),st,FIELDS)
        self.assertEqual(result['category'],csp.PROGRAM)
        atom=result['claims'][0]
        self.assertIs(atom['satisfied'],True)
        self.assertIs(atom['equivalent'],True)
        self.assertEqual(atom['route_value'],'江西省南昌市新建区')

    def test_reverse_region_variant_remains_hard_conflict_not_equivalent(self):
        d=decision([region_claim(value='江西省南昌市新建区')])
        st=state(hukou='江西省')
        result=csp.classify(d,pack(QUOTE_REGION),st,FIELDS)
        self.assertEqual(result['category'],csp.PROGRAM)
        atom=result['claims'][0]
        self.assertIs(atom['satisfied'],False)
        self.assertIs(atom['equivalent'],False)
        self.assertEqual(atom['route_value'],'江西省南昌市新建区')

    def test_true_atom_is_never_classified_program(self):
        # conflicts 组要求比较严格为 False；若事实其实满足条件，程序不得据此
        # 生成任何排除路线，必须交回模型。
        d=decision([numeric_claim()])
        result=csp.classify(d,pack(),state(age=65),FIELDS)
        self.assertEqual(result['category'],csp.SEMANTIC)
        self.assertEqual(result['reason'],'conflict_atom_not_false')

    def test_unknown_fact_is_semantic(self):
        d=decision([numeric_claim()])
        result=csp.classify(d,pack(),state(age_state='unknown'),FIELDS)
        self.assertEqual(result['category'],csp.SEMANTIC)
        self.assertEqual(result['reason'],'unknown_fact')

    def test_related_only_decision_is_semantic(self):
        d=decision([],status='related',object_matches=[{'fact_keys':['张三:age']}])
        result=csp.classify(d,pack(),state(),FIELDS)
        self.assertEqual(result['category'],csp.SEMANTIC)
        self.assertEqual(result['reason'],'no_verified_conflicts')

    def test_source_gaps_is_source_missing(self):
        d=decision([numeric_claim()],source_gaps=['政策原文缺少年龄口径'])
        result=csp.classify(d,pack(),state(),FIELDS)
        self.assertEqual(result['category'],csp.SOURCE_MISSING)
        self.assertEqual(result['reason'],'decision_has_source_gaps')

    def test_metadata_citation_is_source_missing(self):
        claim=numeric_claim()
        claim['citation']={'source':'title','segment_id':0,'quote':'某某补助政策'}
        d=decision([claim])
        result=csp.classify(d,pack(),state(),FIELDS)
        self.assertEqual(result['category'],csp.SOURCE_MISSING)
        self.assertEqual(result['reason'],'conflict_cites_metadata')

    def test_unsupported_operator_is_semantic(self):
        claim=numeric_claim()
        claim['test']['op']='ne'
        d=decision([claim])
        result=csp.classify(d,pack(),state(),FIELDS)
        self.assertEqual(result['category'],csp.SEMANTIC)
        self.assertEqual(result['reason'],'unsupported_test_operator')

    def test_undefined_fact_key_is_semantic(self):
        d=decision([numeric_claim(fact_key='李四:age')])
        result=csp.classify(d,pack(),state(),FIELDS)
        self.assertEqual(result['category'],csp.SEMANTIC)
        self.assertEqual(result['reason'],'unknown_fact_key')


class TestBuildRoute(unittest.TestCase):
    def test_single_atom_leaf_schema_is_valid(self):
        d=decision([numeric_claim()])
        route=csp.build_route(d,pack(),state(age=45),FIELDS)
        self.assertNotIn('all',route)
        self.assertEqual(route['test']['fact_key'],'张三:age')
        self.assertEqual(route['evidence_id'] if 'evidence_id' in route else route.get('source'),'original_conditions')
        valid,error=reasoning._validate_logic_structure(route)
        self.assertTrue(valid,error)

    def test_multiple_atoms_use_all_and_are_valid(self):
        claims=[numeric_claim(),
                {'citation':{'source':'original_conditions','segment_id':0,'quote':QUOTE_AGE},
                 'fact_keys':['household:household_income'],
                 'test':{'fact_key':'household:household_income','op':'lte','value':10000},
                 'meaning':'已知家庭事实与该原文明示条件冲突'}]
        d=decision(claims)
        route=csp.build_route(d,pack(),state(),FIELDS)
        self.assertIn('all',route)
        self.assertEqual(len(route['all']),2)
        valid,error=reasoning._validate_logic_structure(route)
        self.assertTrue(valid,error)

    def test_no_conflicts_raises(self):
        with self.assertRaises(ValueError):
            csp.build_route(decision([],status='related'),pack(),state(),FIELDS)

    def test_real_scope_value_evaluates_numeric_route_false(self):
        # 程序 route 必须经受 _scope_value 的完整校验（_bind + validate_decisions
        # + engine），不能绕过。数值硬冲突应严格得到 False。
        d=decision([numeric_claim()])
        route=csp.build_route(d,pack(),state(age=45),FIELDS)
        value=reasoning._scope_value(route,pack(),d,state(age=45),FIELDS)
        self.assertIs(value,False)

    def test_real_scope_value_evaluates_equivalent_region_route_true(self):
        d=decision([region_claim()])
        st=state(hukou='江西省南昌市新建区')
        route=csp.build_route(d,pack(QUOTE_REGION),st,FIELDS)
        value=reasoning._scope_value(route,pack(QUOTE_REGION),d,st,FIELDS)
        self.assertIs(value,True)


class TestResolveEquivalentAndApply(unittest.TestCase):
    def test_resolve_equivalent_reports_more_specific_value(self):
        d=decision([region_claim()])
        st=state(hukou='江西省南昌市新建区')
        entries=csp.resolve_equivalent(d,st)
        self.assertEqual(len(entries),1)
        self.assertIs(entries[0]['satisfied'],True)
        self.assertEqual(entries[0]['adjusted'],'江西省南昌市新建区')

    def test_apply_false_excludes(self):
        d=decision([numeric_claim()])
        route=csp.build_route(d,pack(),state(age=45),FIELDS)
        result=csp.apply_program_result(d,route,False)
        self.assertEqual(result,'exclude')
        self.assertEqual(d['status'],'exclude')
        self.assertIs(d['conflict_scope_value'],False)
        self.assertEqual(d['conflicts'],[numeric_claim()])

    def test_apply_true_never_excludes(self):
        d=decision([region_claim()],object_matches=[{'fact_keys':['张三:hukou_region']}])
        route=csp.build_route(d,pack(QUOTE_REGION),state(hukou='江西省南昌市新建区'),FIELDS)
        result=csp.apply_program_result(d,route,True)
        self.assertEqual(result,'related')
        self.assertEqual(d['status'],'related')
        self.assertEqual(d['conflicts'],[])
        self.assertEqual(len(d['branch_conflicts']),1)

    def test_apply_none_falls_back_without_mutation(self):
        d=decision([numeric_claim()])
        before=copy.deepcopy(d)
        result=csp.apply_program_result(d,{'test':{}},None)
        self.assertEqual(result,'fallback')
        self.assertEqual(d,before)


class TestResolveWrapper(unittest.TestCase):
    def setUp(self):
        reasoning._SCOPE_AUDIT_CACHE.clear() if hasattr(reasoning,'_SCOPE_AUDIT_CACHE') else None
        self.env=mock.patch.dict(os.environ,{'CONFLICT_SCOPE_PROGRAM':'1'})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_empty_contested_returns_decisions_without_model(self):
        decisions=[decision([],status='unconfirmed')]
        with mock.patch.object(reasoning,'_resolve_conflict_routes_model',side_effect=AssertionError('no model expected')) as model_fn:
            result=reasoning._resolve_conflict_routes(decisions,[pack()],state(),FIELDS,object(),{})
        self.assertFalse(model_fn.called)
        self.assertEqual([d['id'] for d in result],['p1:张三'])

    def test_program_and_source_missing_skip_model(self):
        program=decision([numeric_claim()])
        missing=decision([region_claim()],pack_id='p2',member='李四')
        missing['source_gaps']=['政策原文缺少年龄口径']
        missing_pack=pack(pack_id='p2')
        with mock.patch.object(reasoning,'_resolve_conflict_routes_model',side_effect=AssertionError('no model expected')) as model_fn:
            with mock.patch.object(reasoning,'event') as emit:
                result=reasoning._resolve_conflict_routes([program,missing],[pack(),missing_pack],state(),FIELDS,object(),{})
        self.assertFalse(model_fn.called)
        by_id={d['id']:d for d in result}
        self.assertEqual(by_id['p1:张三']['status'],'exclude')
        self.assertIs(by_id['p1:张三']['conflict_scope_value'],False)
        self.assertEqual(by_id['p2:李四']['status'],'unconfirmed')
        self.assertEqual(len(by_id['p2:李四']['conflicts']),1)
        self.assertNotIn('conflict_scope',by_id['p2:李四'])
        names=[call.args[0] for call in emit.call_args_list]
        self.assertIn('conflict.scope.classified',names)
        self.assertIn('conflict.scope.program_route',names)

    def test_semantic_subset_only_calls_model(self):
        program=decision([numeric_claim()])
        related=decision([],pack_id='p2',member='李四',status='related',
                         object_matches=[{'fact_keys':['李四:age']}])
        st=state();st['members'].append('李四')
        st['facts']['李四:age']={'state':'known','value':30}
        packs=[pack(),pack(pack_id='p2')]
        seen={}
        def fake_model(subset,*args,**kwargs):
            seen['ids']=[d['id'] for d in subset]
            return list(subset)
        with mock.patch.object(reasoning,'_resolve_conflict_routes_model',side_effect=fake_model) as model_fn:
            result=reasoning._resolve_conflict_routes([program,related],packs,st,FIELDS,object(),{})
        self.assertTrue(model_fn.called)
        self.assertEqual(seen['ids'],['p2:李四'])
        by_id={d['id']:d for d in result}
        self.assertEqual(by_id['p1:张三']['status'],'exclude')
        self.assertIn('p2:李四',by_id)

    def test_program_failure_falls_back_to_model(self):
        target=decision([numeric_claim()])
        with mock.patch.object(reasoning,'_scope_value',side_effect=ValueError('scope_value_exploded')):
            with mock.patch.object(reasoning,'_resolve_conflict_routes_model',side_effect=lambda subset,*a,**k: list(subset)) as model_fn:
                with mock.patch.object(reasoning,'event') as emit:
                    result=reasoning._resolve_conflict_routes([target],[pack()],state(),FIELDS,object(),{})
        self.assertTrue(model_fn.called)
        self.assertEqual([d['id'] for d in result],['p1:张三'])
        names=[call.args[0] for call in emit.call_args_list]
        self.assertIn('conflict.scope.fallback',names)

    def test_switch_off_uses_original_model_path_only(self):
        target=decision([numeric_claim()])
        with mock.patch.dict(os.environ,{'CONFLICT_SCOPE_PROGRAM':'0'}):
            with mock.patch.object(reasoning,'classify',side_effect=AssertionError('program path must be bypassed')):
                with mock.patch.object(reasoning,'_resolve_conflict_routes_model',side_effect=lambda subset,*a,**k: list(subset)) as model_fn:
                    result=reasoning._resolve_conflict_routes([target],[pack()],state(),FIELDS,object(),{})
        self.assertTrue(model_fn.called)
        self.assertEqual([d['id'] for d in result],['p1:张三'])


class TestScopeAuditCache(unittest.TestCase):
    def setUp(self):
        reasoning._SCOPE_AUDIT_CACHE.clear()
        self.env=mock.patch.dict(os.environ,{'EVIDENCE_CACHE':'1'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(reasoning._SCOPE_AUDIT_CACHE.clear)
        self.pack=pack(source_hash='hash-cache')
        self.st=state()
        self.d=decision([numeric_claim()])
        self.d['conflict_scope']={'test':{'fact_key':'张三:age','op':'gte','value':60},
                                  'source':'original_conditions','segment_id':0}
        self.expected=review()

    def test_cache_hit_returns_deepcopy(self):
        key=reasoning._scope_audit_cache_key(self.d,self.pack,self.st)
        self.assertTrue(reasoning.add_scope_audit_cache(key,self.expected))
        got=reasoning.get_scope_audit_cache(key)
        self.assertEqual(got,self.expected)
        got['scope_status']='tampered'
        self.assertEqual(reasoning.get_scope_audit_cache(key)['scope_status'],'supported')

    def test_cache_expires_when_fact_revision_changes(self):
        key=reasoning._scope_audit_cache_key(self.d,self.pack,self.st)
        reasoning.add_scope_audit_cache(key,self.expected)
        changed=dict(self.st,fact_revision='rev-2')
        self.assertNotEqual(reasoning._scope_audit_cache_key(self.d,self.pack,changed),key)
        self.assertIsNone(reasoning.get_scope_audit_cache(reasoning._scope_audit_cache_key(self.d,self.pack,changed)))

    def test_cache_key_includes_source_hash_and_logic(self):
        key=reasoning._scope_audit_cache_key(self.d,self.pack,self.st)
        other_pack=dict(self.pack,source_hash='hash-other')
        self.assertNotEqual(reasoning._scope_audit_cache_key(self.d,other_pack,self.st),key)
        other_d=copy.deepcopy(self.d)
        other_d['conflict_scope']['test']['value']=70
        self.assertNotEqual(reasoning._scope_audit_cache_key(other_d,self.pack,self.st),key)

    def test_only_successful_review_is_cached(self):
        key=reasoning._scope_audit_cache_key(self.d,self.pack,self.st)
        bad=review(scope_status='interpretation_error',issues=['原文到逻辑有误'])
        self.assertFalse(reasoning.add_scope_audit_cache(key,bad))
        self.assertIsNone(reasoning.get_scope_audit_cache(key))

    def test_evidence_cache_zero_bypasses(self):
        key=reasoning._scope_audit_cache_key(self.d,self.pack,self.st)
        with mock.patch.dict(os.environ,{'EVIDENCE_CACHE':'0'}):
            self.assertFalse(reasoning.add_scope_audit_cache(key,self.expected))
            self.assertIsNone(reasoning.get_scope_audit_cache(key))

    def test_lru_capacity_limit(self):
        for index in range(260):
            reasoning.add_scope_audit_cache('k'+str(index),self.expected)
        self.assertEqual(len(reasoning._SCOPE_AUDIT_CACHE),256)
        self.assertIsNone(reasoning.get_scope_audit_cache('k0'))
        self.assertIsNotNone(reasoning.get_scope_audit_cache('k259'))

    def test_audit_scopes_uses_cache_on_second_call(self):
        decisions=[copy.deepcopy(self.d)]
        packs=[self.pack]
        with mock.patch.object(reasoning,'_audit_scopes_llm',return_value={'p1:张三':self.expected}) as llm:
            with mock.patch.object(reasoning,'event') as emit:
                first=reasoning._audit_scopes(decisions,packs,FIELDS,object(),self.st)
                second=reasoning._audit_scopes(decisions,packs,FIELDS,object(),self.st)
        self.assertEqual(first,{'p1:张三':self.expected})
        self.assertEqual(second,{'p1:张三':self.expected})
        self.assertEqual(llm.call_count,1)
        names=[call.args[0] for call in emit.call_args_list]
        self.assertIn('conflict.scope.audit_cached',names)


if __name__=='__main__':
    unittest.main()
