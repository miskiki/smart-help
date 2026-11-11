// 离线自测（事件流 v3 前端契约）：从 src/static/ask.html 抽取“纯函数区”脚本，在 Node 中执行并断言。
// 不依赖浏览器与后端：node tests/ask_stream_offline.mjs
// 覆盖：SSE 新旧帧解析、envelope 合并（同 seq 覆盖 / running→done 原位替换）、平铺时间线（排除内部事件、按 seq 排序）、
//       evText 回退 title、answer.delta 多段拼接等于全文且最终 answer 覆盖不重复。
import { readFileSync } from 'node:fs';
import { strict as assert } from 'node:assert';
import vm from 'node:vm';

const html = readFileSync(new URL('../src/static/ask.html', import.meta.url), 'utf8');
const lines = html.split('\n');
const start = lines.findIndex(l => l.includes('SSE v2 帧解析 + 思考时间线纯函数'));
const end = lines.findIndex((l, i) => i > start && l.includes('（纯函数区结束）'));
if (start < 0 || end < 0) {
  console.error('未找到纯函数区标记行，无法离线测试');
  process.exit(1);
}
const pureCode = lines.slice(start + 1, end).join('\n');

const context = vm.createContext({});
const bridge = '\n;globalThis.__ASK_PURE__ = { decodeSSEFrame, parseSSEFrame, mergeEnvelope, normalizeEnvelopes,'
  + ' timelineItems, evText, applyAnswerDelta, evIcon, evIconClass, evMs, evRowClass, evKey,'
  + ' isInternalEvent, isReplay, displayEvents, thinkSummary, latestProgress };';
vm.runInContext(pureCode + bridge, context, { filename: 'ask-pure-zone.js' });
const A = context.__ASK_PURE__;
// vm 里创建的对象/数组原型属于另一个 realm，先转成本域普通数组再深比较。
const plain = (list) => Array.from(list || []);

const done = [];
function check(name, fn) { fn(); done.push(name); console.log('ok ' + done.length + ' - ' + name); }

try {
  // ---------- SSE 帧解析 ----------
  check('新帧解析出 id/event/data', () => {
    const raw = [
      'id: 12',
      'event: retrieval.candidates',
      'data: {"type":"progress","event":{"v":"evt-2","seq":12,"trace_id":"t1","turn_id":1,'
        + '"kind":"retrieval.candidates","phase":"retrieve","exposure":"user","title":"检索候选政策",'
        + '"status":"ok","text":"检索到 35 条候选政策，保留 12 条","ts":"2026-11-07T08:30:00+08:00",'
        + '"detail":{"unique_candidates":35},"metrics":{"ms":812.4}}}',
    ].join('\n');
    const f = A.parseSSEFrame(raw);
    assert.equal(f.id, '12');
    assert.equal(f.event, 'retrieval.candidates');
    const p = A.decodeSSEFrame(raw);
    assert.equal(p.type, 'progress');
    assert.equal(p.event.seq, 12);
    assert.equal(p.event.text, '检索到 35 条候选政策，保留 12 条');
    assert.equal(p.event.kind, 'retrieval.candidates');
  });

  check('新帧 data 无空格仍可解析', () => {
    const p = A.decodeSSEFrame('event: progress\ndata:{"type":"progress","event":{"seq":3,"kind":"x"}}');
    assert.equal(p.type, 'progress');
    assert.equal(p.event.seq, 3);
  });

  check('旧帧只有 data 行行为不变', () => {
    const p = A.decodeSSEFrame('data: {"type":"answer","content":"最终全文"}');
    assert.equal(p.type, 'answer');
    assert.equal(p.content, '最终全文');
  });

  check('心跳与空块返回 null', () => {
    assert.equal(A.decodeSSEFrame(': ping'), null);
    assert.equal(A.decodeSSEFrame(': keep-alive\n'), null);
    assert.equal(A.decodeSSEFrame(''), null);
    const f = A.parseSSEFrame(': ping');
    assert.equal(f.data, null);
  });

  check('坏 JSON 返回 null 且不抛错', () => {
    assert.equal(A.decodeSSEFrame('data: {bad json'), null);
    assert.equal(A.decodeSSEFrame('data: 123'), null);
    assert.equal(A.decodeSSEFrame('data: [1,2]'), null);
  });

  check('无 type 时命中旧事件名才兜底，新事件名不臆测', () => {
    const p = A.decodeSSEFrame('event: done\ndata: {"qa_id":530,"trace_id":"t2","status":"ok"}');
    assert.equal(p.type, 'done');
    assert.equal(p.qa_id, 530);
    assert.equal(A.decodeSSEFrame('event: answer.delta\ndata: {"content":"片段"}'), null);
    assert.equal(A.decodeSSEFrame('event: foo\ndata: {"x":1}'), null);
  });

  check('CRLF 帧解析不带 \\r', () => {
    const p = A.decodeSSEFrame('id: 3\r\nevent: progress\r\ndata: {"type":"progress","event":{"seq":3,"kind":"x"}}\r');
    assert.equal(p.event.seq, 3);
  });

  // ---------- envelope 合并 ----------
  check('mergeEnvelope 同 seq 原位覆盖', () => {
    const list = [{ seq: 5, kind: 'evidence.load', status: 'running' }];
    A.mergeEnvelope(list, { seq: 5, kind: 'evidence.load', status: 'ok', text: '已加载' });
    assert.equal(list.length, 1);
    assert.equal(list[0].status, 'ok');
    assert.equal(list[0].text, '已加载');
  });

  check('mergeEnvelope running→done 原位替换同一 kind', () => {
    const list = [
      { seq: 7, kind: 'model.call', status: 'running' },
      { seq: 8, kind: 'stage.load', status: 'ok' },
    ];
    A.mergeEnvelope(list, { seq: 9, kind: 'model.call', status: 'ok', text: '调用模型' });
    assert.equal(list.length, 2);
    assert.equal(list[0].seq, 9);
    assert.equal(list[0].status, 'ok');
    assert.equal(list[1].seq, 8);
  });

  check('mergeEnvelope 不同 kind 的 running 行不被误替换', () => {
    const list = [{ seq: 2, kind: 'stage.load', status: 'running' }];
    A.mergeEnvelope(list, { seq: 3, kind: 'model.call', status: 'ok' });
    assert.equal(list.length, 2);
    assert.equal(list[0].kind, 'stage.load');
  });

  check('normalizeEnvelopes 合并并按 seq 升序', () => {
    const out = A.normalizeEnvelopes([
      { seq: 3, kind: 'a', phase: 'retrieve', status: 'ok', title: 't3' },
      { seq: 1, kind: 'b', phase: 'retrieve', status: 'ok', title: 't1' },
      { seq: 3, kind: 'a', phase: 'retrieve', status: 'running', title: 't3-running' },
    ]);
    assert.deepEqual(plain(out.map(x => x.seq)), [1, 3]);
    assert.equal(out.length, 2);
  });

  // ---------- 平铺时间线 ----------
  check('timelineItems 排除 audit/internal 且按 seq 排序', () => {
    const m = { events: [
      { seq: 4, kind: 'a', exposure: 'user' },
      { seq: 3, kind: 'b', exposure: 'audit' },
      { seq: 1, kind: 'c', exposure: 'user' },
      { seq: 2, kind: 'd', exposure: 'internal' },
    ] };
    const it = A.timelineItems(m);
    assert.deepEqual(plain(it.rows.map(x => x.seq)), [1, 4]);
    assert.deepEqual(plain(it.internal.map(x => x.seq)), [2, 3]);
  });

  check('timelineItems 回放优先且空数据安全', () => {
    const live = { events: [{ seq: 1, kind: 'live' }] };
    const m = {
      events: live.events,
      replay: { events: [{ seq: 9, kind: 'replay.user' }, { seq: 10, kind: 'replay.audit', exposure: 'audit' }] },
    };
    const it = A.timelineItems(m);
    assert.deepEqual(plain(it.rows.map(x => x.kind)), ['replay.user']);
    assert.deepEqual(plain(it.internal.map(x => x.kind)), ['replay.audit']);
    assert.deepEqual(plain(A.timelineItems({}).rows), []);
    assert.deepEqual(plain(A.timelineItems({}).internal), []);
  });

  // ---------- 文本与状态 ----------
  check('evText 优先 text，旧数据回退 title', () => {
    assert.equal(A.evText({ text: '检索到 12 条', title: '检索' }), '检索到 12 条');
    assert.equal(A.evText({ title: '检索候选政策' }), '检索候选政策');
    assert.equal(A.evText({ kind: 'retrieval.candidates' }), '');
    assert.equal(A.evText(null), '');
  });

  check('状态图标/行样式/耗时', () => {
    assert.equal(A.evIcon({ status: 'error' }), '✗');
    assert.equal(A.evIcon({ status: 'ok' }), '✓');
    assert.equal(A.evIcon({ status: 'skipped' }), '⏭');
    assert.equal(A.evIcon({ status: 'running' }), '▶');
    assert.equal(A.evRowClass({ status: 'error' }), 'tl-err');
    assert.equal(A.evRowClass({ status: 'running' }), 'tl-run');
    assert.equal(A.evMs({ metrics: { ms: 812.4 } }), '0.8s');
    assert.equal(A.evMs({ metrics: { ms: 0 } }), '');
  });

  // ---------- answer.delta 分段拼接 ----------
  check('applyAnswerDelta 多段拼接逐字节等于最终 answer', () => {
    const full = '根据政策，您家符合部分条件，建议补充材料。';
    const segs = [
      { type: 'answer.delta', segment_id: 0, content: '根据政策，', done: false },
      { type: 'answer.delta', segment_id: 1, content: '您家符合部分条件，', done: false },
      { type: 'answer.delta', segment_id: 2, content: '建议补充材料。', done: true },
    ];
    let content = '';
    for (const s of segs) content = A.applyAnswerDelta(content, s);
    assert.equal(content, full);
  });

  check('最终 answer 帧整段覆盖不重复', () => {
    const full = '根据政策，您家符合部分条件，建议补充材料。';
    let content = '根据政策，您家符合部分条件，建议补充材料。';
    content = full; // 等价 handleEvent 的 last.content = ev.content || ''
    assert.equal(content, full);
    assert.equal(content.length, full.length);
    assert.notEqual(content, full + full);
    // 源码级护栏：answer 分支必须是覆盖，且 answer.delta 分支存在
    assert.ok(html.includes("ev.type === 'answer.delta'"), 'ask.html 缺少 answer.delta 分支');
    assert.ok(html.includes("last.content = ev.content || ''"), 'answer 分支未改为覆盖');
    assert.ok(!/last\.content\s*\+=/.test(html), 'answer 仍存在 += 追加');
  });

  check('applyAnswerDelta 容错：空值/非字符串片段不改变已有内容', () => {
    assert.equal(A.applyAnswerDelta(null, { content: 'a' }), 'a');
    assert.equal(A.applyAnswerDelta('已有', null), '已有');
    assert.equal(A.applyAnswerDelta('已有', {}), '已有');
    assert.equal(A.applyAnswerDelta('已有', { content: 123 }), '已有');
  });

  // ---------- 结构护栏：阶段分组必须彻底移除 ----------
  check('渲染路径无阶段分组残留', () => {
    const banned = /displayPhaseGroups|PHASE_ZH|PHASE_ORDER|tl-phase|isPhaseOpen|togglePhase|lastPhaseOf/;
    const hit = html.match(banned);
    assert.equal(hit, null, 'ask.html 仍残留阶段分组代码：' + (hit && hit[0]));
  });
} catch (e) {
  console.error('FAILED after ' + done.length + ' passing checks');
  console.error(e && e.stack ? e.stack : e);
  process.exitCode = 1;
}

if (!process.exitCode) console.log('ALL PASSED: ' + done.length + ' checks');
