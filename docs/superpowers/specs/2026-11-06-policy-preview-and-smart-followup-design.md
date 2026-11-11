# 政策预览抽屉 + 视觉重构 + 智能追问 设计

日期：2026-11-06
状态：待确认

## 意图

用户三点诉求：

1. 点匹配到的政策卡片，能看到这条政策的预览——体验对齐 `/home/adminroot/projectfiles/caichang/project/ZhuSuRag-maiin/ZhiSuRag-main` 里“在线编辑 Markdown 知识”的右侧辅助面板：抽屉式、可拖宽、分段渲染、阅读舒适，而不是弹窗塞一段文字。
2. 整个系统“交互过于丑陋”，要借鉴智枢文渊的交互逻辑、前端样式与对话样式。
3. 智能体回答太模板化，追问机械化：
   - 该补的信息要补，但不是每轮都补；
   - “未定”不做固定分类，由智能判断“这一答是否值得问”；
   - 不设业务轮数限制，只设一个防空转的最大轮数兜底；
   - 匹配政策的回答要科学、少套话。

## 目标

1. 政策预览抽屉（右侧滑出、可拖宽、分段、含本次核验依据、原文摘录）。
2. 视觉语言换成智枢文渊 tokens，对话页与登录页统一；对话区居中 780–900px，卡片/消息轻量化。
3. 追问改为“模型决策 + 确定性护栏”：模型判断该不该补、补什么；护栏防重复、防越界、防空转。
4. 回答四段式、少套话：结论 → 依据 → 不确定点及原因 → 下一步；免责句同一会话只出现一次。

## 非目标

- 不重建业务数据、不改数据库结构（预览读现有 `policy`/`policy_detail`）。
- 不引入前端构建工具：继续单文件 `src/static/ask.html`（原生 CSS + Vue3 CDN），避免部署链路变化。
- 不做多租户/权限改造，不改登录鉴权流程。
- 预览首版不保证逐字完整原文（原文正文目前只在向量切片里）。

## 约束

- 前端：`src/static/ask.html`（1259 行，原生 CSS + Vue3），无打包步骤；改动后内联 JS 必须 `node --check` 通过，`GET /ask` 必须 200。
- 后端：Flask + gunicorn（`kill -HUP <master>` 生效，直接杀 master 会拖垮 `deploy/foreground.py` 整栈）；库为 KingbaseES（SQLAlchemy 直连会因版本串 AssertionError，脚本取证一律 psycopg2 直连）。
- 政策数据：`policy`（policy_code / policy_name / level / regions / target / conditions / benefit / process / contact / effect_date / status / cur_version / source），`policy_detail` 9403 行（region / benefit / conditions / process / contact / phone / extra …）。
- 现有全模型链路：`full_flow.run()` → `question_plan`（`policy_core/full_questions.py`）→ `questions.plan()`（candidates 来自 `assessments` 的 missing/direction_missing，`max_questions=4`、`max_rounds=3`）。
- 现有规则链路：`rule_flow.try_rule_run()` → `build_rule_questions()`（锚定缺项 → 卡缺项 → 阻塞数排序）。

## 方案与取舍

### 一、政策预览

**A（采用）只读接口 `GET /api/policy/preview?code=<policy_code>&detail_id=<可选>`**，`login_required`：

```json
{
  "policy": {"code","name","level","regions","target","conditions","benefit","process","contact","effect_date","status","cur_version","source"},
  "detail": {"detail_id","region","benefit","conditions","process","contact","phone","extra"},
  "details": [{"detail_id","region"}],
  "excerpt": "原文摘录（≤3000 字，来自 policy_vector_qwen3 该 code 的切片，按序拼接）",
  "excerpt_truncated": true
}
```

- `detail_id` 缺省时按“与本次会话县区匹配的明细优先，其次省级/全省条目”选一条。
- “本次核验依据”（已满足/待确认/结论）由前端用卡片行数据渲染，不落库、不重新求值——避免预览与回答不一致。
- 抽屉：宽 520px 默认、可拖 420–860px；分段折叠（基本信息 / 对象 / 条件 / 待遇 / 流程 / 材料 / 联系方式 / 本次核验依据 / 原文摘录）；Esc、遮罩点击、关闭按钮都能关；打开后焦点进抽屉，关闭后焦点回卡片。

**B（弃）只靠对话 payload 里的卡片字段渲染**：字段不全（无 process/contact/明细），规则路径与全模型路径字段还不一致，做不出“预览”。

**C（弃）直接复用智枢文渊的预览组件**：技术栈不同（TDesign + Vue SFC + 构建链），迁移成本高于自建，收益只是外观相似。

### 二、视觉与对话样式

**A（采用）换皮不换骨架**：保留现有 DOM 结构（左会话栏 + 对话区 + 输入区），重写 CSS 变量与组件样式为智枢文渊 tokens：

| 角色 | 值 |
| --- | --- |
| 页面基底 | #f7f9f7 |
| 卡片/面板 | #ffffff |
| 侧栏 | #f1f5f2 |
| 主文字 | #23362b |
| 次级文字 | #63756a |
| 辅助文字 | #7d8b81 |
| 操作色 | #168b63 |
| 选中/弱强调 | #e7f4ec |
| 轻边框 | #e5ebe6 |

圆角 12px（卡片/抽屉）/ 8px（按钮/输入）；无重阴影，只用 1px 边框与极浅投影；对话正文 780–900px 居中；消息行去掉“整块灰底”，改为“浅色卡片 + 左侧细强调线”；卡片头加奖牌式编号；思考链保留但收成一行。

**B（弃）整体重排页面结构**（顶部路径栏、多级导航、工作台式布局）：周期长、与本轮三诉求无关，且现有单页应用没有对应业务页面。

### 三、智能追问

**A（采用）模型决策 + 确定性护栏**：

> 先修正一个事实：生产环境 `canonical_questions=True`，现有 `questions.plan()` 走到字典冻结问法分支时**完全不调用模型**（`model_calls=0`），追问是由“缺项计数排序”确定性挑出来的——所以“机械、每轮都补、反复确认”的观感主要来自这里，加上正文每轮把同一个未定口径原样重述一遍。本次改两件事：决策层加一次轻量模型调用；正文层不再原样重述已答“不清楚”的口径。

决策层（`policy_core/full_questions.py` 的 `plan()` 内新增 `decide()`）：

- 输入：当前命中政策及各自卡点（`missing` / `direction_missing` / 原文缺口径）、已确认事实、已问列表（字段 + 轮次 + 用户回答原话）、本轮新增信息、当前轮次。
- 输出：

```json
{"need_more": true,
 "reason": "还有 2 条政策的资格取决于是否在校",
 "questions": [{"field": "member_enrolled", "member": "1", "why": "雨露计划要求中高职在校", "gain": "答'是'可判定 2 条政策"}]}
```

- `questions` 允许 0–3 个；`need_more=false` 时不出追问区，正文直接给结论与“可继续核验的条件”。

护栏层（同一函数内，模型输出后立即执行）：

1. **白名单**：`field` 必须存在于事实字典 `fields`；`member` 必须存在；不合法直接丢弃。
2. **不重复**：`st['asked']` 里已有的字段（含答“不清楚/暂时无法提供”）不得再问，除非本轮新增信息与该字段直接相关且用户主动提及（此时允许一次“确认式”追问）。
3. **不问用户无法回答的**：政策口径缺口（`source_gaps` / `interpretation_pending`）不生成问题，改为正文一句“这条政策的原文没写清 X，建议向经办机构确认”。
4. **价值排序**：按 `gain` 里声称能改变的政策条数排序，取前 3；声称 0 条的丢弃（防“问了也白问”）。
5. **防空转硬上限**：总轮数上限 `FULL_MODEL_MAX_ROUNDS`（默认 4，环境变量可调），到顶不再问；连续两轮问题集合相同则强制停止（`question_plan_status='stalled'`）。
6. **可回落**：模型失败/超时/输出不合 schema → 回落现有确定性 candidates 逻辑（`fallback='verified_field_questions'`），不退化、不中断回答。

“不设业务轮数限制”的落实：只要模型判断还有高价值信息且护栏不拦，就可以继续问，最多到硬上限 4 轮；不存在“必须问满 3 轮”的机械行为。

**问法仍用字典冻结措辞**（不改写、不新增审计成本）：模型只决定“问哪个字段、为什么问、答了能得到什么”，问题文本取该字段 `definition['question']`，从而避免落入现有 `audit_questions` 的原文回查链路。新增的决策调用与原规划调用二选一（`canonical_questions` 分支内替换为决策调用），**不叠加**。

**正文层同步去重复**：已答“不清楚/暂时无法提供”的字段，后续不再以“需确认 X”原样出现在结论里，改为“你此前表示 X 暂不清楚，这一条暂不能判定”。这是“反复确认”的主要来源。

### 四、回答去模板

- `policy_core/full_advice.py` 提示词改四段式：一句结论 → 依据（原文引用或已确认事实，逐条对应政策）→ 不确定点及原因（家庭事实缺 / 原文口径缺，分开写）→ 下一步（补什么、去哪问）。
- 固定小节标题只在有内容时出现（`核验到的政策线索` 之类的通用标题删掉，换成政策名加粗行）。
- 免责句（“部分满足不等于完整资格”）只在会话第一轮出现：会话状态记 `disclaimer_shown`。
- 正文上限 400 字；超限重写一次，仍超按句子边界截断。
- 规则路径 `rule_flow.render_rule_mode()` 同步：去掉固定开头套话，只在首轮出现一次。

## 验收

1. 单测（新增 `/tmp/test_preview_api.py`、`/tmp/test_question_decide.py`）：接口字段完整、未登录 401、选明细逻辑；护栏 6 条各一例（越权字段丢弃、重复字段拦截、口径问题不生成、gain=0 丢弃、轮数到顶停止、模型失败回落）。
2. e2e（真实 run_turn，同会话 4 轮）：第 2 轮问题不与第 1 轮重复；答“不清楚”后该字段不再出现；第 4 轮后强制停止追问；回答正文含四要素、免责句只出现一次、≤400 字。
3. 视觉：`node --check` 内联 JS 通过；`GET /ask` 200；抽屉在 1280/1440 宽度下不遮挡输入区。
4. 人工：浏览器点卡片出抽屉、拖宽、Esc 关闭、窄屏可用；对话与登录页视觉一致。

## 风险

- **原文不是全文**：抽屉里明确标注“以下为原文摘录”，并给出政策编号，后续可补原文入库。
- **模型决策给低价值问题**：用“gain 必须对应具体政策条数”校验，失败即回落确定性逻辑。
- **单文件 CSS 重写回归**：分两批改（先 tokens + 布局，再组件），每批 HUP 后实测一次。
- **追问变多导致单轮变慢**：决策层复用已有一次模型调用（与 question_plan 合并），不新增调用次数。

## 留给实施者

- 抽屉动画时长/缓动、图标细节；窄屏断点（<960px 抽屉改为全屏浮层）。
- 预览字段为空时的占位文案（“原文未提供”而非空白）。
- 视觉稿的具体间距在实现时对照智枢文渊现有页面取证（截图或源码），不自创数值。
