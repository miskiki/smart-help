# 政策预览抽屉 + 视觉重构 + 智能追问 实施计划

> **For agentic workers:** 本计划按任务拆给不同执行者。每个任务只改自己的文件清单，跨任务接口以本文 Interfaces 为准。

**Goal:** 政策卡片可点开右侧预览抽屉；对话页视觉换成智枢文渊语言；追问由模型决策+确定性护栏；回答四段式去套话。

**Architecture:** 后端新增只读预览接口（`src/policy_preview.py` + `src/api.py` 路由）；追问决策进入 `policy_core/full_questions.py` 的 canonical 分支，复用现有事实字典与 asked 台账；回答文案在 `full_advice.py`/`result_model.py`/`rule_flow.py` 收敛。前端只改 `src/static/ask.html` 一个文件（原生 CSS + Vue3 CDN，无构建步骤）。

**Tech Stack:** Python 3.10 (Flask + SQLAlchemy + KingbaseES)、Vue3 CDN、原生 CSS。

**Spec:** `docs/superpowers/specs/2026-11-06-policy-preview-and-smart-followup-design.md`

## Global Constraints

- Python 解释器：`/home/adminroot/miniconda3/envs/smart-help-web/bin/python`。
- 策略库：KingbaseES `127.0.0.1:54321`，库 `policy_db_mig20261021`；auth 库 `smart_help_auth_mig20261021`。**账号与口令只写在部署机 `deploy/web.env`（`ADMIN_DB_URL` / `SMART_HELP_AUTH_DATABASE_URL`），不写入任何文档或代码。**
- 独立脚本取证一律用 `psycopg2` 直连（SQLAlchemy 在脚本里会因 `KingbaseES V008...` 版本串抛 `AssertionError`）；Flask 应用内用 ORM / `db.session`。
- **本仓库不是 git 仓库**：没有 commit 步骤，替代为“验证步骤”。
- 生效方式：只允许 `kill -HUP <gunicorn master pid>`（当前 315937）；**禁止** `kill` master，会拖垮 `deploy/foreground.py` 整栈。全栈重启才用 `nohup bash deploy/start.sh >> runtime/logs/xxx.log 2>&1 &`。
- 前端单文件无构建：改完必须把内联 `<script>` 抽出来 `node --check` 通过，且 `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:5000/ask` 为 200。
- 文案规则：中文；不出现内部字段名（`member_enrolled` 这类）；不承诺资格（“符合/可领取/保证”）；不要求上传证明材料。
- 事实字段白名单：`policy_core/fields.json` 字典键（`filtered=...` 里的 `fields`）。

## 文件结构

| 文件 | 责任 | 任务 |
| --- | --- | --- |
| `src/policy_preview.py`（新建） | 组装单条政策预览数据（主表+明细+原文摘录） | 1 |
| `src/api.py`（修改） | 暴露 `GET /api/policy/preview` | 1 |
| `src/policy_core/full_questions.py`（修改） | 追问决策 `decide_questions()` + canonical 分支接入 | 2 |
| `src/policy_core/full_settings.py`（修改） | `max_rounds` 可由 `FULL_MODEL_MAX_ROUNDS` 覆盖，默认 4 | 2 |
| `src/policy_core/full_advice.py`（修改） | 四段式提示词、免责句只出现一次、400 字上限 | 3 |
| `src/policy_core/result_model.py`（修改） | 正文去固定小节、不再原样重述已答“不清楚”的口径 | 3 |
| `src/policy_core/rule_flow.py`（修改） | 规则路径同步去套话 | 3 |
| `src/static/ask.html`（修改） | 预览抽屉、智枢文渊视觉 tokens、对话样式 | 4 |

## Review Focus（最容易被真实输入打穿的五处）

1. 预览接口收到**已下线政策编号**或**明细为空的国家/省级政策**（`policy_detail` 可能 0 行，如 S-ZB-088）——必须返回主表信息而不是 500。
2. 追问决策模型返回**空 questions 或非法 field**（模型抖动/幻觉）——必须回落确定性候选，不能整轮无追问也不能崩。
3. 用户在追问里答**“不清楚/暂时无法提供”**——同一字段后续不得再问、正文不得原样重词。
4. 视觉改造后**窄屏（<960px）与历史长消息**——抽屉不能盖住输入框，消息不能横向溢出。
5. 全文检索/历史消息卡片点开预览时后端**未登录**（401）与**会话过期**——前端要给出可读提示，不能白屏。

---

### Task 1: 政策预览数据层 + 只读接口

**Files:**
- Create: `src/policy_preview.py`
- Modify: `src/api.py`（在文件末尾添加路由，复用已有 `bp`、`login_required`、`jsonify`）
- Test: `/tmp/test_preview_api.py`

**Interfaces:**
- Produces: `policy_preview.build_preview(policy_code: str, detail_id: int | None = None) -> dict`

```python
{
  "policy": {"code","name","level","regions","category","target","conditions",
             "benefit","process","contact","effect_date","status","cur_version","source"},
  "detail": {"detail_id","region","benefit","conditions","process","contact","phone","extra"} | None,
  "details": [{"detail_id","region"}],
  "excerpt": str,               # 原文摘录，≤3000 字
  "excerpt_truncated": bool
}
```

- 明细选择：`detail_id` 给定且属于该政策则用它；否则优先 `region` 含“全省/省级”的明细，其次第一条；`details` 最多返回 20 条。
- `excerpt`：`policy_vector_qwen3` 中该 `policy_code` 的 `doc_text` 按 `id` 升序拼接，超 3000 字截断并置 `excerpt_truncated=true`；无切片给空串。
- 找不到编码：抛 `LookupError('policy_not_found')`；路由捕获后返回 `404 {"error": "policy_not_found"}`。

- [ ] **Step 1: 写失败测试 `/tmp/test_preview_api.py`**

用 Flask test client（`from app import app`，`app.config['TESTING']=True`）验证：
1. 未登录 `GET /api/policy/preview?code=S-ZB-090` → 401；
2. 登录态（直接 `with client.session_transaction() as s: s['user_id']=1`，若鉴权走 JWT 则改为 mock `authentication.current_user`）→ 200 且 `data['policy']['name']=='困难残疾人生活补贴'`、`data['detail']['detail_id']==9401`、`excerpt` 非空；
3. `code=S-ZB-088`（明细 0 行）→ 200 且 `detail is None`、`details==[]`；
4. `code=NOT-EXIST` → 404。

- [ ] **Step 2: 跑测试，确认失败（404/500）**
Run: `/home/adminroot/miniconda3/envs/smart-help-web/bin/python /tmp/test_preview_api.py`

- [ ] **Step 3: 实现 `src/policy_preview.py`**（ORM 查 `models.Policy`；`policy_detail`、`policy_vector_qwen3` 用 `db.session.execute(text(...))`）

- [ ] **Step 4: 在 `src/api.py` 末尾注册路由** `@bp.get('/policy/preview')`，参数 `code`（必填）、`detail_id`（可选 int），`@login_required`。

- [ ] **Step 5: 跑测试确认通过**
Run: 同 Step 2，期望 `ALL PASS`。

- [ ] **Step 6: 验证步骤（替代 commit）**
`/home/adminroot/miniconda3/envs/smart-help-web/bin/python -m py_compile src/policy_preview.py src/api.py` 通过。

---

### Task 2: 追问决策引擎（模型决策 + 确定性护栏）

**Files:**
- Modify: `src/policy_core/full_questions.py`
- Modify: `src/policy_core/full_settings.py`（`limits()` 里 `max_rounds=int(os.environ.get('FULL_MODEL_MAX_ROUNDS','4'))`）
- Test: `/tmp/test_question_decide.py`

**Interfaces:**
- Produces: `full_questions.decide_questions(st, assessments, fields, model, config) -> dict`

```python
{"need_more": bool, "reason": str,
 "questions": [{"fact_key": "1:member_enrolled", "why": str, "gain": str}]}
# 已过护栏；fact_key 形如 "<entity>:<field>"，entity 为 'household' 或成员序号
```

- 护栏（顺序执行，逐条丢弃不合法项）：
  1. `fact_key` 必须在 `fields` 白名单且 entity 合法（`household` 或 `st['members']` 内序号）；
  2. `st['asked']` 已有的 `fact_key` 不再问（除 `conflict` 字段）；
  3. `gain` 必须提到 ≥1 条具体政策名（来自 `assessments` 的 `name`），否则丢弃；
  4. 最多 3 条，按 gain 中政策条数降序；
  5. `st['round'] >= config['max_rounds']` → 直接返回 `need_more=False`；
  6. 连续两轮问题集合相同 → `need_more=False`、`st['question_plan_status']['status']='stalled'`；
  7. 模型异常/JSON 不合法/字段全被丢 → 返回 `{'need_more': None, ...}` 表示“回落”，由调用方走现有确定性排序。

- 接入点：`questions.plan()` 的 `canonical_questions` 分支，在 `options.sort(...)` 之前调用 `decide_questions`；`need_more is True` 时按返回的 `fact_key` 顺序构造问题（问法仍用 `definition['question']`，`reason` 用返回的 `why`），`need_more is False` 时返回 `[]`，`None` 时保留现有确定性排序。

- [ ] **Step 1: 写失败测试 `/tmp/test_question_decide.py`**（用假 `model` 函数返回预设 JSON，覆盖：正常 2 条、越权字段被丢、重复字段被拦、gain 无政策名被丢、round 到顶、模型抛异常回落、连续两轮相同 stalled）
- [ ] **Step 2: 跑测试确认失败**
- [ ] **Step 3: 实现 `decide_questions()` 与 `limits()` 改动**
- [ ] **Step 4: 跑测试确认通过**（期望 `ALL PASS`）
- [ ] **Step 5: 回归**：`python -m py_compile src/policy_core/full_questions.py src/policy_core/full_settings.py`，并跑 `/tmp/test_fixes_unit.py`（应仍 43 passed）。

---

### Task 3: 回答去模板、去重复确认

**Files:**
- Modify: `src/policy_core/full_advice.py`、`src/policy_core/result_model.py`、`src/policy_core/rule_flow.py`
- Test: `/tmp/test_advice_detemplate.py`

**Interfaces:**
- `full_advice.generate(...)` 返回体新增 `disclaimer`（bool，表示本轮是否已展示免责句）；会话状态 `st['disclaimer_shown']` 首次置 True。
- 正文 ≤400 字；超限重写一次，仍超按句号截断。
- 已答“不清楚/暂时无法提供”的字段（`st['asked'][k]['answer']` 为 `None` 或值含“不清楚”）在正文中不得以“需确认 X”原样出现，统一改为“你此前表示 X 暂不清楚，这条暂不能判定”。
- `result_model.render()`：删除固定小节标题“核验到的政策线索”“本次检索情况”等，只在有内容时输出政策名行。
- `rule_flow.render_rule_mode()`：固定开头套话只在首轮出现（`coverage['round']` 判定）。

- [ ] **Step 1: 写失败测试 `/tmp/test_advice_detemplate.py`**（构造 rows/coverage/claims，断言：不含“全库模型分析”“以上依据原政策库原文与您的自述核验”；免责句第二轮不出现；已答不清楚的字段不再出现在“需确认”句式；正文 ≤400 字）
- [ ] **Step 2: 跑测试确认失败**
- [ ] **Step 3: 改三个文件的文案与提示词**
- [ ] **Step 4: 跑测试确认通过**
- [ ] **Step 5: 回归**：`python -m py_compile` 三文件 + 跑 `/tmp/test_fixes_unit.py`。

---

### Task 4: 前端预览抽屉 + 智枢文渊视觉

**Files:**
- Modify: `src/static/ask.html`（单文件；CSS 在 `<style>`，Vue3 在 `<script>`）
- Test: `/tmp/check_ask_frontend.sh`

**视觉 tokens（替换现有 `:root` 里的配色，保留变量名或新增）**

| 角色 | 值 | 用途 |
| --- | --- | --- |
| 页面基底 | `#f7f9f7` | body 背景 |
| 卡片/面板 | `#ffffff` | 卡片、抽屉、输入区 |
| 侧栏 | `#f1f5f2` | 左会话栏 |
| 主文字 | `#23362b` | 正文 |
| 次级文字 | `#63756a` | 说明 |
| 辅助文字 | `#7d8b81` | 占位 |
| 操作色 | `#168b63` | 主按钮、强调 |
| 选中/弱强调 | `#e7f4ec` | 选中项、chip |
| 轻边框 | `#e5ebe6` | 所有 1px 边框 |

圆角：卡片/抽屉 12px、按钮/输入 8px；阴影只保留 `0 1px 2px rgba(23,43,77,.04)`；对话正文容器 `max-width: 860px; margin: 0 auto`。

**抽屉行为**
- 点击 `.pc` 卡片任意位置（排除内部 `a`/`button`）→ `openPreview(c)`。
- `openPreview(c)`：`fetch('/api/policy/preview?code=' + encodeURIComponent(c.code) + (c.detail_id ? '&detail_id=' + c.detail_id : ''), {credentials:'same-origin'})`；`res.status===401` → 提示“登录已过期，请刷新页面”；其它非 200 → 提示“该政策预览暂时不可用”。
- 抽屉结构：遮罩 `.pv-mask` + 右滑面板 `.pv`（默认 520px，可拖 420–860px）+ 头部（政策名/编号/层级/状态/关闭按钮）+ 正文分段（基本信息 / 适用对象 / 申请条件 / 补贴标准 / 申领流程 / 联系方式 / 本次核验依据 / 原文摘录）+ 底部“复制编号”按钮。
- “本次核验依据”用传入的卡片对象渲染（`decision`/`satisfied`/`missing`/`source_gaps`），不重新求值；缺失时显示“本次无核验明细”。
- 关闭：Esc、点遮罩、关闭按钮；关闭后焦点回到触发卡片（`ref` 记录）。

- [ ] **Step 1: 写 `/tmp/check_ask_frontend.sh`**：抽 `<script>` 到 `/tmp/ask_inline.js` → `node --check`；`curl` `/` 与 `/ask` 断言 200；`grep` 断言文件含 `pv-mask`、`openPreview`、`#168b63`。
- [ ] **Step 2: 跑检查确认失败**
- [ ] **Step 3: 实现 CSS tokens 与对话样式（第一批）**；跑 Step 1。
- [ ] **Step 4: 实现预览抽屉（第二批，含 openPreview 与模板）**；跑 Step 1。
- [ ] **Step 5: 验证**：`curl GET /ask` 200；`node --check` 通过；默认宽度抽屉不遮挡输入区（人工或截图）。

---

### Task 5: 集成验证与生效（Lead）

- [ ] 跑全部测试：`/tmp/test_preview_api.py`、`/tmp/test_question_decide.py`、`/tmp/test_advice_detemplate.py`、`/tmp/test_fixes_unit.py`、`/tmp/check_ask_frontend.sh`。
- [ ] 真实 e2e：同会话四轮（家庭描述 → 追问回答 → 再问 → 换户），断言无重复追问、无“模型未返回完整有效的政策判定”、卡片可预览。
- [ ] `kill -HUP 315937`，确认新 worker 与 `GET /`、`GET /ask` 200。
- [ ] 追加 `audit_20261106.md` 5.8 节 + 向用户汇报（含证据）。
