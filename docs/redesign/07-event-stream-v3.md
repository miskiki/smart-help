# 事件流 v3：去阶段化的实时实况（P0 契约）

> 目标：前端时间线不再是"请求→理解→检索→评估…"的固定阶段叙事，而是系统真实动作的时间序列表；
> 后端每发生一件事就推一条带人话句子的事件；答案按段流出；刷新后回放同一数据模型。

## 1. 后端 envelope 变更（src/events.py）

- VERSION 升为 `evt-2`。
- envelope 增加字段 `text`：**给人看的一句话**（≤80 字，只由安全标量拼装，禁止用户原文/家庭值/证据原文）。
- 旧字段（v/seq/trace_id/turn_id/kind/phase/exposure/title/status/ts/detail/metrics）全部保留，
  `phase` 降级为普通属性，**前端不得再按 phase 分组**。
- text 示例：
  - span_start：`正在加载政策库…`；span_end：`已加载政策库（0.3s）`
  - retrieval.candidates：`检索到 35 条候选政策，保留 12 条`
  - evidence.load：`从政策库筛出 10 条可用证据（排除 25 条）`
  - model.call：`调用模型：逐条核验（2.1s）`（purpose 已映射中文，禁止英文原码）
  - gate.name_rejected：`被名称门拦截：<原因>`
  - rule.evaluated：`逐条求值 453 条规则，生成 6 张卡片`
  - questions.planned：`规划了 3 个待补充问题`
  - facts.extracted：`家庭事实抽取完成`
  - answer.compose：`组织回答（512 字）`
  - turn.complete：`分析完成，匹配 6 项政策`
  - 未知 kind：回退 title，不丢事件。

## 2. 答案分段事件（src/conversations.py）

- 保留最终 `{'type':'answer','content': 全文}` 帧（旧前端兼容）。
- 在其前面新增若干帧：
  `{'type':'answer.delta','event_id':'<trace_id>:a','segment_id':i,'content':'本段文本','done':bool}`
- 分段规则（src/events.py:answer_segments）：优先按空行分段；单段 >160 字按句号/分号切；
  单段 <8 字合并；最后一段 done=true。所有段拼起来必须**逐字节等于**最终 answer。
- 段与段之间不人为 sleep；前端用打字机效果呈现。

## 3. SSE 帧

- progress 帧维持现有 `id/event/data` 格式，envelope 内新增 text。
- answer.delta 帧为普通 data 帧即可（type 已在 data 里）。
- 心跳继续 `: ping`。

## 4. 前端改造（src/static/ask.html）

1. **删除按阶段分组渲染**：`displayPhaseGroups` 不再用于渲染；改为 `timelineItems(m)`：
   - 按 seq 排序的 user-exposure 事件平铺；
   - audit/internal 事件收进时间线底部一个 `<details>内部过程 N 步</details>`，内部同样平铺；
   - 每行：状态图标（running 用 spinner）、`ev.text || ev.title`、detail 计数 chips、右侧耗时；
   - 不得出现"请求/理解/检索/评估/裁决/作答/落库"等阶段名，也不得出现"阶段"字样。
2. 流式中：面板自动展开；最新一条 running 事件高亮；用户手动收起后不再抢开。
   结束后：折叠摘要改为 `思考 N 步 · 模型调用 M 次 · 用时 Xs`（M 为 kind=model.call 事件数；
   耗时优先 done 事件/后端 duration，没有则前端计时）。
3. 回放（loadTrace）与实时共用同一套 flat 渲染；旧数据没有 text 时回退 title。
4. 答案：`answer.delta` 追加到 `last.content`；`answer` 帧直接**覆盖**为全文（不再 +=，避免重复）。
5. JS 纯函数必须可离线测试：`timelineItems`、`evText`、`mergeEnvelope`、`decodeSSEFrame`、
   `applyAnswerDelta` 不依赖 Vue/DOM。新增 `tests/ask_stream_offline.mjs`：从 ask.html 提取
   `// ==================== SSE v2 帧解析 + 思考时间线纯函数 ====================` 到
   `// ==================== （纯函数区结束） ====================` 之间的脚本，node 直接执行断言。

## 5. 验收

- 真实 SSE 抓包：一轮问答 ≥15 条 progress 事件，每条 envelope 有可读 text；无阶段分组字段参与渲染。
- 长回答（>300 字）answer.delta ≥3 段，拼接等于最终 answer。
- 刷新历史后回放：同一轮显示 flat 时间线，条数与实时一致。
- 旧浏览器缓存/旧数据缺 text 时不报错。
- 96 个既有 unittest 全绿；新增后端单测覆盖 text 与 answer_segments。

## 6. 实施记录（2026-11-07）

- 后端（task-10）：
  - `src/events.py`：`VERSION='evt-2'`；新增 `narrative()`（把 stage/model/domain/rule/gate 事件翻成 ≤80 字人话，只用计数/耗时/编号等白名单标量）、`answer_segments()`（空行优先、>160 字按句切、<8 字合并，拼接逐字节等于原文）；`build()` 的 envelope 新增 `text`。
  - `src/conversations.py`：final answer 之前按段发 `{'type':'answer.delta', event_id, segment_id, content, done}`，随后仍发全文 `{'type':'answer'}`（覆盖渲染，幂等）。
  - 单测 `tests/test_events_v3.py`（20 例）；`tests/test_m0_events.py` 版本断言升至 evt-2；全套 116 个 unittest 全绿。
  - `src/policy_core/full_runtime.py`：每次模型调用开始即发 `model_call_start`（kind=model.call、status=running），前端原位替换为完成行，长推理期间可见真实动作。
  - 包裹层 `model.answer/model.health/consultation.pipeline/answer.pipeline` 的 exposure 置 internal，避免时间线同一动作出现两行。
- 真实链路验收 `tests/e2e_m0_events.py`（规则快速路径，莲花县 2 人户）：20 条用户可见事件全部有 text、无"阶段"字样、≤80 字；12 段 answer.delta 拼接逐字节等于最终 answer；回放 32 事件、21 条用户可见事件 text 齐全。
- 前端（task-9，impl-frontend）：平铺时间线 `timelineItems`、`answer.delta` 追加、`answer` 全文覆盖、`tests/ask_stream_offline.mjs` 19 项离线断言；阶段分组代码 0 残留。
- 生产 HTTP 验收（真实 gunicorn 127.0.0.1:5000，2026-11-07 平滑重载后）：42 帧 / 23 progress / 20 条用户可见事件 / 7.1s；12 段 answer.delta 拼接一致；回放 200、32 事件、text 无缺失；`/ask` 页面已含 v3 纯函数。
- 提交：`37fb699`（后端 envelope text+answer.delta）、`021676b`（开始事件+内部化+叙述精修）、`60bc9cb`（前端平铺时间线+离线测试）。
