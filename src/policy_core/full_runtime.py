import json
import os
import time
import threading
from contextvars import ContextVar

_active_call=ContextVar("policy_model_call",default=None)

def record_transport_metrics(**metrics):
    record=_active_call.get()
    if record is not None:record.update(metrics)
from chain_trace import _emit,_current
from .publishing import parse_json

def event(name,**data):
    _emit(_current.get(),'full_model.'+name,**data)

class Budget:
    def __init__(self,llm,config):
        self.llm=llm;self.config=config;self.started=time.monotonic();self.calls=[];self._lock=threading.Lock()
    @property
    def remaining(self):return self.config['deadline']-(time.monotonic()-self.started)
    def __call__(self,*args,**kwargs):
        with self._lock:
            if self.remaining<3 or len(self.calls)>=self.config['max_calls']:raise TimeoutError('本轮模型预算已用完')
            record=dict(purpose=kwargs.get('purpose'));self.calls.append(record)
            item_ids=kwargs.pop('diagnostic_item_ids',None)
            if item_ids:record['item_ids']=item_ids
        from kb_rag import _check_cancel
        _check_cancel(kwargs.pop('session_id',None) or getattr(self,'session_id',None))
        # 单次调用超时：默认 40 秒（生产基线不变），可用 FULL_MODEL_CALL_TIMEOUT 调整。
        # 本地大模型 prompt+长输出可能超过 40 秒时，通过环境变量放宽，避免误伤正常调用。
        try:call_timeout=int(os.environ.get('FULL_MODEL_CALL_TIMEOUT','40'))
        except ValueError:call_timeout=40
        kwargs['timeout']=min(call_timeout,kwargs.get('timeout',call_timeout),self.remaining)
        tick=time.monotonic()
        record['started_ms']=round((tick-self.started)*1000,1)
        # 模型调用开始即上报，前端在长推理期间能看到“正在调用模型：<用途>”；
        # 结束事件同 kind+status=ok/error，前端按 running 行原位替换，不重复两行。
        try:event('model_call_start',purpose=record.get('purpose'))
        except Exception:pass
        active_token=_active_call.set(record)
        try:
            try:return self.llm(*args,**kwargs)
            except Exception as exc:
                import requests
                cause=exc.__cause__
                if not isinstance(cause,requests.ConnectionError) or self.remaining<8:raise
                record['connection_retries']=1
                kwargs['timeout']=min(kwargs['timeout'],self.remaining)
                return self.llm(*args,**kwargs)
        except Exception as exc:record['error']=type(exc).__name__;raise
        finally:
            record['seconds']=round(time.monotonic()-tick,3)
            record['finished_ms']=round((time.monotonic()-self.started)*1000,1)
            try:event('model_call',**record)
            finally:_active_call.reset(active_token)

def ask(model,purpose,task,payload,system,schema=None):
    compact_fact=purpose=='core_full_reason_audit'
    if compact_fact:
        from .llm_contract import fact_audit_schema,decode_fact_audit
        schema=fact_audit_schema([d['id'] for d in payload.get('decisions',[])])
        system+='\n实际返回紧凑JSON {"items":[{"id":"决策id","o":true,"m":true,"c":true,"q":true,"v":true,"g":true,"e":[],"b":[]}]}。字段一一对应：o=object_supported，m=matches_supported，c=conflicts_supported，q=questions_supported，v=coverage_supported，g=source_gaps_supported，e=issues，b=blocking_conflicts。布尔值必须依据前述全部独立审核规则判断，不能默认true。e与b保留实际错误与遗漏的硬冲突，b内部仍用原协议的完整证据、fact_keys、test、meaning。只缩短字段名，不减少审核内容。'
    if purpose in ('core_full_scope_audit','core_full_reason_audit'):
        system+='\n只输出单行JSON；不要在JSON结构中添加排版空格和换行，字符串里的原文空白必须保留。'
    tags={}
    if isinstance(model,Budget):
        items=payload.get('decisions') or payload.get('items') or payload.get('sources') or []
        if isinstance(items,list):tags['diagnostic_item_ids']=list(dict.fromkeys(item.get('source_id') or item.get('id') for item in items if isinstance(item,dict) and (item.get('source_id') or item.get('id'))))
    result=parse_json(model(task,json.dumps(payload,ensure_ascii=False,default=str),system=system,
                            purpose=purpose,temperature=0,**tags,**(dict(response_schema=schema) if schema is not None else {})))
    return decode_fact_audit(result) if compact_fact else result
