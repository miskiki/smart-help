# P1 输入理解层（intake v1）契约

> 状态：已上线（2026-11-07，commit bf6fb02）｜负责人 Lead｜前置：P0 事件流 v3 已上线（37fb699/021676b/60bc9cb/d60e75a）
> 本文件是 P1 的实现契约与验收标准；代码合并前不得偏离本契约的字段、路由语义与开关。

## 0. 一句话结论

每轮输入先被"读懂"再决定去哪：用**确定性快路径 + 一次低温度模型兜底**产出结构化 intake 决策（意图/主体/主题/已给判据/缺失判据/地区提示），路由到闲聊、政策咨询、资格预判或"只问一句"；不确定时宁可不空跑 20+ 次模型调用，也不把政策问答误当资格预判。

## 1. 现状与问题（改动前）

- 路由只有三桶，且散落在对话层：
  - `src/conversations.py:496` `_is_chat_only` → 闲聊；
  - `src/conversations.py:503` `_reuse_household is not None or not is_consultation(...)` → 匹配，否则咨询；
  - `src/conversations.py:399` `is_consultation` 内部再发一次模型调用（`purpose='core_intent'`，`src/conversations.py:424-427`）。
- `src/smart_matcher_kb.py:138` `is_household_text` 只回答"像不像家庭信息"，不产出主体/主题/已有判据/缺失判据。
- 结果：资格预判即使一句"我能领什么补贴"（无地区、无成员、无年龄）也会进入完整匹配流水线，跑完 20+ 次模型调用才在末尾追问；用户看不到"系统如何理解我"；咨询类问题与资格类问题的边界只靠一段 200 字提示词。
- 事件流没有 intake 事件，P0 的时间线第一条是"开始分析您的问题"，看不到理解动作。

## 2. 决策对象（唯一数据结构）

新模块 `src/intake.py`，纯函数 + 单次模型调用，不依赖 Flask。

```python
classify(question, history=None, has_household=False, household=None, mode=None) -> dict
```

返回（永不抛异常，失败走 fallback）：

```json
{
  "route": "chat | consultation | eligibility | clarify",
  "subject": "household | self | third_party | policy_general | unknown",
  "topic": "低保|残疾|医保|教育|住房|就业|产业|养老|临时救助|其他 中的一项或空字符串",
  "has_household_facts": true,
  "region_hint": {"province": "", "city": "", "county": "", "raw": ""},
  "provided": ["region", "members", "age", "health", "labor", "income", "insurance", "education", "housing", "land"],
  "missing": ["region", "income", "insurance"],
  "confidence": 0.0,
  "source": "rule | model | fallback",
  "reason": "≤40 字中文，仅用于日志/事件 detail"
}
```

约束：
- `route=eligibility` 仅当存在**具体个人/家庭**（"我/我家/该户/这户/父亲/孩子…"）且问题是"是否符合/能领什么/帮我匹配/解释本户结果/补充或更正家庭事实"；纯政策原文、条件、标准、材料、流程、文号、有效期 → `consultation`。
- `route=clarify` 仅当 `eligibility` 且**本轮无任何可用家庭事实、无历史家庭档案、无地区/成员/年龄等提示**（例："我能申请吗""有什么补贴"）——只问一句，不检索、不调匹配模型。
- `region_hint` 只是提示，**不是路由门槛**；地区 unknown 允许进入匹配（P0 地区软过滤），由追问补齐。
- `provided/missing` 是展示与追问依据，v1 **不**用于阻断匹配；P2 匹配核心才用它做证据绑定。
- 决策里不得出现用户原文以外的推断家庭事实；不得输出资格结论。

## 3. 判定顺序（必须按序，先规则后模型）

1. **闲聊确定性守卫**（迁入 `intake.chat_guard`，保留原 `_CHAT_POLICY_WORDS` 硬豁免）：命中 → `chat`，0 次模型调用。
2. **家庭文本守卫**：`smart_matcher_kb.is_household_text` 命中，或 `has_household=True` 且有家庭词 → `eligibility`，`has_household_facts=true`（地区/人数/成员等由规则抽取进 `provided`）。
3. **显式资格短语**：`我(?:家|们)?.{0,16}(?:符合|能否|能不能|可以|能领|能申请)|是否符合|该户|这户|帮我匹配|重新分析|为什么没|为何没` → `eligibility`。
4. **显式咨询短语**（无家庭/资格词）：`申请流程|办理流程|申请材料|补助标准|补贴标准|政策内容|政策原文|政策依据|政策文号|有效期|申请条件` → `consultation`。
5. **模型分类**（唯一新增模型调用，替换现有 `core_intent` 调用，总调用数不增）：
   - `kb_rag._llm_answer(..., temperature=0, purpose='intake_classify')`
   - system 提示只输出 JSON：`{"route","subject","topic","confidence"}`，并给 12 条 few-shot（闲聊/政策原文/条件/材料/资格/第三方家庭/补家庭事实/指代追问/主题不明）。
   - 带 `history[-6:]` 摘要（同现有 `is_consultation` 做法），支持"那需要什么材料"这类指代追问。
6. **fallback**（模型异常/JSON 非法/置信度<0.5 且规则无命中）：有家庭词 → `eligibility`；否则 → `consultation`。禁止 fallback 到 `clarify`（避免无谓打断）。
7. `route=eligibility` 后若满足 §2 的 clarify 条件 → 改判 `clarify`。

## 4. 路由行为（与现有流水线的映射）

| route | 行为 | 调用链 | 用户可见 |
|---|---|---|---|
| chat | `kb_rag.qa_stream(chat_only=True)`（沿用） | 1 次模型 | 自然对话 |
| consultation | `kb_rag.qa_stream(chat_only=False)`（沿用） | RAG 问答 | 条文/材料/流程回答，不出卡片 |
| eligibility | `kb_rag.qa_match_stream`（沿用，规则优先或全模型） | 现有匹配 | 卡片+追问+依据 |
| clarify | 新：模板追问（0 模型、0 检索、<1s） | 无 | 一句自然追问 + 说明可粘贴家庭情况 |

clarify 文案（模板，允许 1 个变量）：
> 可以帮您判断。为了不误判，请先告诉我：家庭在哪个省/市/县（或区），家里几口人，主要成员的年龄、健康和劳动情况（如残疾、患病、上学、缺劳动力），以及家庭主要收入来源。您也可以把家庭情况整段发我。

clarify 轮落库：`qa_log.status='complete'`，`answer=文案`，`match_json=freeze({'match_json': {'kind':'clarify','route':'clarify','intake': decision}}, answer)`（`match_json.kind='clarify'`，导出为空行），`matched_policies=''`；不发 `cards/questions` 帧。

## 5. 事件与前端

新增两个用户可见事件（envelope v=evt-2 不变，phase=understand）：

- `intake.parsed`（顶层事件名，`chain_trace.event('intake.parsed', route=..., subject=..., topic=..., provided=..., missing=..., region_status=...)`）
  - `text`：`理解为：资格预判 · 家庭/本人 · 低保 · 已提供 3 类信息（地区/成员/年龄）`（≤80 字；超出截断）
- `intake.clarify`（仅 clarify 轮）：`text="需要补充家庭信息才能判断（地区/成员/年龄/收入）"`

narrative 只用白名单标量：route/subject/topic 的**枚举中文映射**、`len(provided)`、`len(missing)`、`region_status`；禁止把用户原文或自由文本塞进 text。

事件顺序（规则快速路径示例）：`turn.started` → `intake.parsed` → `region.normalized` → … → `turn.complete`。

## 6. 开关与回退

- `INTAKE_ENGINE=1`（默认在代码里开启；`deploy/web.env` 显式写 1）时走本契约。
- `INTAKE_ENGINE=0`：`run_turn` 完全走旧分支（`_is_chat_only`/`is_consultation`），不产生 intake 事件，行为与 P0 完全一致。
- 模型分类失败不降级成错误：走 §3.6 fallback，用户无感。
- 无需重启数据库；无 schema 迁移。

## 7. 实施清单（文件）

1. 新增 `src/intake.py`：`enabled()/chat_guard()/extract_signals()/classify()/clarify_text()`；白名单常量；纯函数可单测。
2. `src/conversations.py`：`run_turn` 计算一次 decision（refine 不计算）；分支选择改为按 decision.route；`turn.started` 后 `event('intake.parsed', ...)`；clarify 的 `_gen_maker`；旧逻辑保留在 flag 关闭分支。
3. `src/events.py`：`TOP_EVENTS` 增加 `intake.parsed/intake.clarify`；`narrative` 增加两段文案；无需改 classify 结构。
4. `tests/test_intake.py`（新）：路由矩阵、provided/missing 抽取、fallback、clarify 条件、永不抛异常、不产生资格结论。
5. `tests/e2e_p1_intake.py`（新）：真实 HTTP/Flask test_client 跑 4 条（闲聊 0 卡、政策原文咨询 0 卡、具体家庭资格 有卡、模糊资格 clarify 0 卡且 <2s/0 次匹配调用），断言 `intake.parsed` 事件存在且 text≤80 字、无"阶段"字样。
6. `deploy/web.env`：`INTAKE_ENGINE=1`。
7. `docs/redesign/09-p1-intake-contract.md`：本文件，实施后补 §8 实施记录。

## 8. 验收口径

- 路由矩阵（≥12 输入）全部符合 §3 期望：闲聊不检索；政策原文/条件/材料/流程→consultation；具体家庭描述/资格问题→eligibility；模糊资格→clarify。
- 闲聊轮 0 次检索、0 张卡片；clarify 轮 **0 次匹配/检索调用**、耗时 <2s、无卡片、无追问弹窗。
  （路由规则高置信时 0 次模型调用；模糊输入会兜底 1 次 `intake_classify` 意图分类，约 0.7–1.0s，不触发任何匹配/检索。）
- 现有 116 个 unittest + 新增单测全绿；P0 e2e（`tests/e2e_m0_events.py`）继续 PASS（规则路径仍出卡、FP=0 口径未变）。
- 真实一轮资格匹配事件流含 `intake.parsed`，且时间线文本人话、无阶段字样。
- `INTAKE_ENGINE=0` 回退后 4 类输入行为与 P0 一致（开关验证一次即可）。
- 模型调用数：分类轮相比旧实现 **不增加**（旧 `core_intent` 1 次 → 新 `intake_classify` 1 次；闲聊/规则命中路径 0 次）。

## 9. 非目标（留给 P2/P3）

- 不在此层做事实抽取/单位归一（年龄区间、时间口径、收入单位）——P2 条件原子化时统一处理。
- 不在此层改匹配算法、卡片口径、证据门。
- 不引入多轮槽位表单；clarify 只问一句，用户可自由文本回答。


## 10. 实施记录（2026-11-07）

**提交**：`bf6fb02`（P1 输入理解层 v1 + P3-1 实时收尾事件修复）；生产已平滑重载（gunicorn master 361754，HUP 后 worker 更新）。

### 10.1 落地内容

| 文件 | 变更 |
| --- | --- |
| `src/intake.py`（新，342 行） | 确定性规则优先 + 一次低温度模型兜底；输出 route/subject/topic/has_household_facts/region_hint/provided/missing/confidence/source/reason；永不抛异常 |
| `src/conversations.py` | `_old_choice()` 保留旧逻辑；`_choose()` 在 worker 线程内先理解再选路；clarify 走零匹配模板；理解结论写入本轮快照（`meta.intake/route`，并写入 `match_json` 基座） |
| `src/events.py` | `intake.parsed`/`intake.clarify` 顶层事件 + narrative + SAFE_KEYS；`intake_classify` 进入 PURPOSE_PHASE=understand |
| `deploy/web.env` | `INTAKE_ENGINE=1`（代码默认即开启；=0 完全回退旧路由） |
| `tests/test_intake.py`（新） | 20 例：路由矩阵/provided/missing/fallback/永不抛异常/不产出资格结论 |
| `tests/e2e_p1_intake.py`（新） | 真实链路四路由（chat/consultation/eligibility/clarify）+ 0 卡/有卡/事件/耗时断言 |

### 10.2 验收数据（2026-11-07）

- **单元/离线**：136 unittest 全绿（含新增 20 例）；前端 `node tests/ask_stream_offline.mjs` 19/19。
- **P1 e2e（in-process 真实模型）四路由 PASS**：

| 输入 | route | 耗时 | 用户事件 | 卡片 | 备注 |
| --- | --- | --- | --- | --- | --- |
| 你好，你是谁？ | chat | 0.9s | 3 | 0 | 规则命中，0 模型调用 |
| 低收入家庭认定条件是什么？ | consultation | 2.5s | 9 | 0 | 只检索原文，不做资格判定 |
| 莲花县完整家庭描述 | eligibility | 3.4s | 22 | 7 | 规则路径出卡，模型调用 2 次 |
| 我想申请补贴 | clarify | 0.9s | 4 | 0 | 兜底 1 次意图分类，无匹配 |

- **P0 e2e 回归 PASS**：实时用户事件 22 == 回放 22；`turn.complete` 实时/回放都在（P3-1 修复生效）。
- **生产 HTTP 实测（gunicorn :5000）**：资格预判 3.0s/22 用户事件/7 卡/回放 22 与实时一致；clarify 0.6s/4 用户事件/0 卡，时间线文本：
  `开始分析您的问题` → `理解为：需要补充信息 · 本人` → `需要补充家庭信息才能判断（地区/成员/年龄/健康）` → `分析完成`。
- `/ask` 200，时间线/逐段函数均在页面内。

### 10.3 回退与已知边界

- `INTAKE_ENGINE=0`：完全回到 `_is_chat_only`/`is_consultation` 旧路由，不产生 intake 事件。实测回退：闲聊 0 卡、莲花县完整家庭 7 卡、两轮均 0 条 intake 事件。
- 注意：`deploy/web.env` 只在 gunicorn master 启动时被 sourcing，`kill -HUP` 不会重新读取；显式关闭需改 0 后重启（代码默认开启，故本次 HUP 即生效）。
- 边界（留给 intake v2）：provided 目前只做"是否提到"的存在性识别，不做单位/时间/数值语义；多轮指代与主体延续仍靠 history 粗粒度提示；地区提示只做文本抽取，不在此层归一（P2 region 维表统一处理）。

