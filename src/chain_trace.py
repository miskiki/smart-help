"""Privacy-minimized local request tracing; never changes matching decisions."""
import functools
import importlib
import json
import logging
from collections import OrderedDict
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import queue as _queue
import threading
import time
from contextvars import ContextVar
from uuid import uuid4

import events as _events

_current = ContextVar('chain_trace', default=None)
_parent = ContextVar('chain_span', default=None)
_loggers = {}
_lock = threading.Lock()
# 回放：内存保留最近若干轮事件（每轮上限 4000 条），超出后回退扫描 JSONL 文件。
_RING = OrderedDict()
_RING_MAX_TURNS = 50
_RING_MAX_EVENTS = 4000
# 第五阶段：日志分级。summary 下高频噪声阶段（缓存命中 graph.load、逐条 eligibility.verify）
# 只聚合计数，不逐次写事件；debug 保持逐条明细。install() 时从 config 读取。
_LEVEL = 'summary'
# 噪声阶段：graph.load 多为缓存命中（<50ms），eligibility.verify 单请求可达数百次。
NOISY_STAGES = ('graph.load', 'eligibility.verify')


def _agg(state, name, **fields):
    """summary 模式下对高频阶段做聚合计数（不写逐条 span）。"""
    a = state.setdefault('agg', {})
    key = a.setdefault(name, {'count': 0, 'total_ms': 0.0})
    key['count'] += 1
    key['total_ms'] = round(key['total_ms'] + float(fields.get('duration_ms', 0)), 3)
    if name == 'graph.load':
        if float(fields.get('duration_ms', 0)) >= 50:
            key['real_loads'] = key.get('real_loads', 0) + 1
    elif name == 'eligibility.verify':
        verdict = fields.get('verdict')
        vc = key.setdefault('verdict_counts', {})
        if verdict in ('通过', '待核实', '不适用'):
            vc[verdict] = vc.get(verdict, 0) + 1
        if fields.get('region_failed'):
            key['region_failed'] = key.get('region_failed', 0) + 1
        if fields.get('unparsed_count'):
            key['unparsed_count'] = key.get('unparsed_count', 0) + fields['unparsed_count']


def _progress_view(event, fields, envelope=None):
    """把链路事件压缩成前端思维链条目（key 用于同阶段 start→end 原位更新）。

    旧字段由 events.legacy_fields 保持兼容；envelope 存在时一并下发新协议事件。
    返回 None 表示该事件不进思维链。
    """
    return _events.progress_view(event, fields, envelope)


def _emit(state, event, **fields):
    if state is None:
        return
    try:
        with _lock:
            seq = state.get('seq', 0) + 1
            state['seq'] = seq
        envelope = _events.build(state, event, fields, seq)
        row = dict(time=time.strftime('%Y-%m-%dT%H:%M:%S%z'),
                   trace_id=state.get('id'), event=event)
        if state.get('turn_id') is not None:
            row['turn_id'] = state['turn_id']
        row.update(fields)
        row['seq'] = seq
        try:
            logger = state.get('logger')
            if logger is not None:
                logger.info(json.dumps(row, ensure_ascii=False))
        except Exception:
            pass  # 磁盘/序列化异常不影响主流程
        _remember(state, row)
        # 思维链：有订阅者时把用户可读/可审计的事件实时推给 SSE 流（不抛错、不阻塞流水线）。
        subs = state.get('subs')
        if subs and envelope and envelope.get('exposure') in ('user', 'audit'):
            view = _events.progress_view(event, fields, envelope)
            if view:
                with _lock:
                    for q in list(subs):
                        try:
                            q.put_nowait(view)
                        except Exception:
                            pass  # 队列满/订阅者消失：进度可丢，主流程不能停
    except Exception:
        # Observability must not break the application, including on full disks.
        pass


def event(name, **fields):
    """在当前请求 trace 上记录一条事件（供业务代码显式标注轮次边界等）。"""
    _emit(_current.get(), name, **fields)


def _remember(state, row):
    """把事件放入内存回放环；trace_id 维度保留最近 _RING_MAX_TURNS 轮。"""
    trace_id = state.get('id')
    if not trace_id:
        return
    with _lock:
        rows = _RING.get(trace_id)
        if rows is None:
            rows = []
            _RING[trace_id] = rows
        rows.append(row)
        if len(rows) > _RING_MAX_EVENTS:
            del rows[:len(rows) - _RING_MAX_EVENTS]
        while len(_RING) > _RING_MAX_TURNS:
            _RING.popitem(last=False)


def set_turn(turn_id):
    """把 qa_id 绑定到当前请求的 trace 上（落库与回放按轮定位）。"""
    state = _current.get()
    if isinstance(state, dict):
        state['turn_id'] = turn_id


def current_trace_id():
    state = _current.get()
    return state.get('id') if isinstance(state, dict) else None


def current_turn_id():
    state = _current.get()
    return state.get('turn_id') if isinstance(state, dict) else None


def _trace_dir():
    return Path(os.environ.get('CHAIN_TRACE_DIR', str(Path(__file__).parent / 'logs')))


def _scan_trace_files(trace_id):
    """扫描当前进程可读的 JSONL（含轮转文件），按 seq 还原一轮的完整事件。"""
    rows = []
    try:
        directory = _trace_dir()
        if not directory.is_dir():
            return rows
        for file in sorted(directory.glob('chain_trace-*.jsonl*')):
            try:
                with file.open(encoding='utf-8') as handle:
                    for line in handle:
                        if trace_id not in line:
                            continue
                        try:
                            row = json.loads(line)
                        except ValueError:
                            continue
                        if row.get('trace_id') == trace_id:
                            rows.append(row)
            except OSError:
                continue
    except Exception:
        return rows
    rows.sort(key=lambda row: row.get('seq') if isinstance(row.get('seq'), int) else 0)
    return rows


def replay(trace_id, since=0):
    """按 trace_id 回放一轮事件（先内存环，未命中再扫 JSONL）；返回 envelope 列表。"""
    if not trace_id:
        return []
    try:
        since = int(since or 0)
    except (TypeError, ValueError):
        since = 0
    with _lock:
        rows = list(_RING.get(trace_id) or [])
    if not rows:
        rows = _scan_trace_files(trace_id)
    out = []
    for index, row in enumerate(rows):
        seq = row.get('seq')
        if not isinstance(seq, int):
            seq = index + 1
        if seq < since:
            continue
        envelope = _events.from_row(row)
        if envelope is None:
            envelope = {'v': _events.VERSION, 'seq': seq, 'trace_id': trace_id,
                        'turn_id': row.get('turn_id'), 'kind': row.get('event'),
                        'phase': 'evaluate', 'exposure': 'audit',
                        'title': row.get('event'), 'status': 'ok',
                        'ts': row.get('time'), 'detail': {}, 'metrics': {}}
        envelope['seq'] = seq
        out.append(envelope)
    out.sort(key=lambda item: item.get('seq') or 0)
    return out


def subscribe(state=None):
    """当前请求内订阅思维链进度事件；返回队列，无请求态（脚本/定时任务）返回 None。"""
    state = _current.get() if state is None else state
    if not isinstance(state, dict):
        return None
    q = _queue.Queue(maxsize=256)
    with _lock:
        state.setdefault('subs', []).append(q)
    return q


def unsubscribe(q, state=None):
    if q is None:
        return
    state = _current.get() if state is None else state
    if isinstance(state, dict):
        with _lock:
            subs = state.get('subs')
            if subs and q in subs:
                subs.remove(q)


def _summary(value):
    if isinstance(value, (list, tuple)):
        return {'items': len(value)}
    if not isinstance(value, dict):
        return {}
    out = {}
    for key in ('rows', 'excluded', 'dropped_kg', 'candidates', 'members', 'policies', 'details'):
        if isinstance(value.get(key), (list, dict)):
            out[key + '_count'] = len(value[key])
    if isinstance(value.get('rows'), list):
        counts = {}
        for row in value['rows']:
            verdict = ((row.get('forbid_v2') or {}).get('evidence') or {}).get('verdict')
            if verdict in ('通过', '待核实', '不适用'):
                counts[verdict] = counts.get(verdict, 0) + 1
        out['verdict_counts'] = counts
    return out


def traced(name):
    def decorate(fn):
        if getattr(fn, '_chain_traced', False):
            return fn
        @functools.wraps(fn)
        def call(*args, **kwargs):
            state = _current.get()
            if state is None:
                return fn(*args, **kwargs)
            span = uuid4().hex[:16]
            parent = _parent.get()
            token = _parent.set(span)
            start = time.perf_counter()
            noisy = _LEVEL == 'summary' and name in NOISY_STAGES
            if not noisy:
                _emit(state, 'span_start', span_id=span, parent_id=parent, stage=name)
            status, extra = 'ok', {}
            try:
                value = fn(*args, **kwargs)
                extra = _summary(value)
                if name == 'eligibility.verify' and isinstance(value, dict):
                    checks = value.get('checks') or {}
                    extra = {'verdict': value.get('verdict'),
                             'failed_dimensions': [k for k, v in checks.items() if v is False],
                             'unknown_dimensions': [k for k, v in checks.items() if v in ('unknown', 'conflict')],
                             'unparsed_count': len(checks.get('unparsed_conditions') or [])}
                    # Request-local ordinal, never member names or household identifiers.
                    state['decisions'] += 1
                    extra['decision_no'] = state['decisions']
                    if args and isinstance(args[0], dict):
                        extra['policy_code'] = args[0].get('code')
                    if len(args) > 1 and isinstance(args[1], dict):
                        extra['detail_id'] = args[1].get('detail_id')
                return value
            except BaseException as exc:
                status = 'error'
                extra = {'error_type': type(exc).__name__}
                raise
            finally:
                duration_ms = round((time.perf_counter()-start)*1000, 3)
                if noisy:
                    # 噪声阶段：只聚合，不写逐条事件
                    _agg(state, name, duration_ms=duration_ms,
                         verdict=extra.get('verdict'),
                         region_failed=('region' in (extra.get('failed_dimensions') or [])),
                         unparsed_count=extra.get('unparsed_count', 0))
                else:
                    _emit(state, 'span_end', span_id=span, parent_id=parent, stage=name,
                          status=status, duration_ms=duration_ms, **extra)
                    # 汇总阶段耗时（供 request_end 的 trace_summary 使用）
                    spans = state.setdefault('spans', {})
                    key = spans.setdefault(name, {'count': 0, 'total_ms': 0.0, 'errors': 0})
                    key['count'] += 1
                    key['total_ms'] = round(key['total_ms'] + duration_ms, 3)
                    if status == 'error':
                        key['errors'] += 1
                _parent.reset(token)
        call._chain_traced = True
        return call
    return decorate


def mark_turn_error():
    state = _current.get()
    if state:
        state['turn_error'] = True


class TraceMiddleware:
    def __init__(self, app, logger):
        self.app, self.logger = app, logger

    def __call__(self, environ, start_response):
        # Static files are omitted. No query strings, headers, bodies or user IDs logged.
        if not environ.get('PATH_INFO', '').startswith('/api/'):
            return self.app(environ, start_response)
        state = {'id': uuid4().hex, 'logger': self.logger, 'decisions': 0, 'turn_error': False,
                 'agg': {}, 'spans': {}, 'subs': [], 'seq': 0, 'turn_id': None}
        start = time.perf_counter()
        token = _current.set(state)
        parent_token = _parent.set(None)
        code = [500]
        def respond(status, headers, exc_info=None):
            code[0] = int(status.split()[0])
            headers = [(k, v) for k, v in headers if k.lower() != 'x-trace-id']
            return start_response(status, headers + [('X-Trace-ID', state['id'])], exc_info)
        _emit(state, 'request_start', method=environ.get('REQUEST_METHOD'))
        try:
            iterable = self.app(environ, respond)
        except BaseException as exc:
            _emit(state, 'request_end', status='error', error_type=type(exc).__name__,
                  duration_ms=round((time.perf_counter()-start)*1000, 3))
            raise
        finally:
            _parent.reset(parent_token)
            _current.reset(token)
        def stream():
            token = _current.set(state)
            parent_token = _parent.set(None)
            status = 'ok'
            try:
                yield from iterable
            except GeneratorExit:
                status = 'disconnected'
                raise
            except BaseException as exc:
                status = 'error'
                _emit(state, 'stream_error', error_type=type(exc).__name__)
                raise
            finally:
                try:
                    if hasattr(iterable, 'close'):
                        iterable.close()
                finally:
                    if state['turn_error'] or code[0] >= 400:
                        status = 'error'
                    # 第五阶段：请求级汇总（阶段耗时、噪声聚合、判定与原因分布），一次输出
                    _emit(state, 'trace_summary',
                          spans=state.get('spans') or {},
                          noisy_agg=state.get('agg') or {},
                          decisions=state['decisions'],
                          http_status=code[0], stream_status=status)
                    _emit(state, 'request_end', status=status, http_status=code[0],
                          duration_ms=round((time.perf_counter()-start)*1000, 3))
                    _parent.reset(parent_token)
                    _current.reset(token)
        return stream()


def install(app):
    global _LEVEL
    if os.environ.get('CHAIN_TRACE_ENABLED', '1').lower() in ('0', 'false', 'no'):
        return
    if app.extensions.get('chain_trace'):
        return
    try:
        import config as _cfg
        _LEVEL = _cfg.TRACE_LOG_LEVEL
    except Exception:
        _LEVEL = 'summary'
    directory = Path(os.environ.get('CHAIN_TRACE_DIR', str(Path(__file__).parent / 'logs')))
    try:
        directory.mkdir(parents=True, exist_ok=True)
        # Per-process files avoid unsafe shared rotation with multiple workers.
        path = str(directory.resolve() / ('chain_trace-%s.jsonl' % os.getpid()))
        with _lock:
            logger = _loggers.get(path)
            if logger is None:
                logger = logging.Logger('chain_trace.' + str(os.getpid()))
                logger.addHandler(RotatingFileHandler(path, maxBytes=10*1024*1024,
                                                     backupCount=5, encoding='utf-8'))
                if os.environ.get('CHAIN_TRACE_STDOUT') == '1':
                    logger.addHandler(logging.StreamHandler())
                logger.setLevel(logging.INFO)
                _loggers[path] = logger
    except OSError:
        app.logger.warning('Chain tracing unavailable: cannot open trace directory')
        return
    targets = {
        'smart_matcher_kb': {'parse_household': 'household.parse', 'load_db': 'policy.load',
                             'match_profile': 'rules.match', 'parse_judgment': 'model.parse_judgment'},
        'kg_retrieval': {'_load': 'graph.load', 'kg_search': 'graph.recall'},
        'kg_forbid_v2': {'verify_member_detail': 'eligibility.verify'},
        'kg_batch5_merge': {'merge_candidates': 'candidates.merge'},
        'kb_rag': {'_load_vectors': 'vector.load', '_embed': 'model.embed',
                   'search': 'vector.search', '_llm_answer': 'model.answer',
                   'preflight_local_models': 'model.health', 'profile_stream': 'household.pipeline',
                   'qa_stream': 'consultation.pipeline', '_run_match_pipeline': 'answer.pipeline'},
    }
    for module_name, funcs in targets.items():
        module = importlib.import_module(module_name)
        for attribute, name in funcs.items():
            setattr(module, attribute, traced(name)(getattr(module, attribute)))
    app.wsgi_app = TraceMiddleware(app.wsgi_app, logger)
    app.extensions['chain_trace'] = {'path': path}
    @app.before_request
    def trace_route():
        from flask import request
        _emit(_current.get(), 'route', endpoint=request.endpoint)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Read local trace events by X-Trace-ID')
    parser.add_argument('trace_id')
    parser.add_argument('--directory', default=str(Path(__file__).parent / 'logs'))
    args = parser.parse_args()
    if len(args.trace_id) != 32 or any(c not in '0123456789abcdef' for c in args.trace_id):
        parser.error('Expected a 32-character lowercase hex trace ID')
    for file in sorted(Path(args.directory).glob('chain_trace-*.jsonl*')):
        with file.open(encoding='utf-8') as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get('trace_id') == args.trace_id:
                    print(json.dumps(row, ensure_ascii=False))
