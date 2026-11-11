# 模型管理前端整页重做 + ② 否定语义全链路统一 — 设计文档

- 日期：2026-11-10
- 上游：`audit_20261110_模型管理前端与政策问答链路缺陷审计_修订版.md`（同仓库根目录）
- 状态：设计待批准，尚未动手

---

## 0. 一句话

做两件事：① 把 `/admin` 里的模型管理区，从"一条写死的人机表单+内联卡片"，重写成**智枢 `ModelSettings.vue` 的四件交互**（类型 Tab 卡片格、抽屉式编辑器、凭证三态卡、保存前连通性测试），并在原生 JS 栈内完成；② 统一"无残疾"这类**明确否定**在家庭事实层的表示，让它在抽取、追问调度、出卡三层都不再触发"残疾等级"。

---

## 1. 范围

### 1.1 做

| Track | 内容 | 关联缺陷 |
|---|---|---|
| **A** | 模型管理前端整页重做（模仿智枢四件） | ①‑P1‑1、①‑P1‑2、①‑P1‑3、①‑P1‑4、①‑P1‑5 |
| **B** | "无残疾"否定语义全链路统一 | ② |

### 1.2 明确不做

- 不把智枢的 Vue/Element Plus/t-design 组件真正搬进 smart-help（不做 npm/vite、不改 `app.py` 服务指向、不引第三方依赖）。
- 不做智枢的 **模型调试抽屉**（`ModelDebugDrawer.vue`，707 行）。
- 不做智枢的 **本地模型浏览/下载**（本地是 OpenAI 兼容的 `127.0.0.1:8096`，不支持列模型/拉模型，抄不动）。
- 不做政策侧 ③④⑤（锚定口径、互斥透传、去重键强化、事实×条件硬校验）。
- 不做 ② 之外的政策链路问题，不与 ③④⑤ 合并排期。

---

## 2. 现状

### 2.1 A：模型管理前端现状

文件：`src/static/admin.html`（332 行）、`src/static/admin.js`（模型段约 860–1105 行）、`src/model_admin.py`（279 行）、`deploy/seed_models.py`。

- `admin.html` 的模型区是一个 `<section class="card" id="models-card">`：顶部一行工具（类型筛选 `#model-type-filter` + 刷新），下面一个 `#models-list` 容器；"添加/编辑"都走同一个弹窗 `#model-mask`。
- 弹窗表单 `#model-form-body` 字段：`mf-name` / `mf-type` / `mf-provider` / `mf-base-url` / `mf-model-name` / `#mf-api-key-row` / `#mf-dim-row`（仅 embed 显）/ `mf-query-instruction` / `mf-json-mode` / `#model-form-error`。
- **编辑态把凭证行整行隐藏**：`admin.js:1026` `$('mf-api-key-row').hidden = !!model`。于是前端**没有任何入口**改 `api_key`——后端 `PUT /api/admin/models/<id>/credentials` 能力存在，前端没接。
- current 卡片格是"表格行+徽标"，不是智枢式的"卡片网格 + 悬浮操作"。

后端契约（已落地，前端只调用）：

| 方法 | 路由 | 说明 |
|---|---|---|
| GET | `/api/admin/models?type=` | 列表；返回每行的凭证**配置状态**（不含明文 key） |
| POST | `/api/admin/models` | 新建；`{type,name,provider,base_url,model_name,dim,query_instruction,json_mode,api_key,is_default?}` |
| PUT | `/api/admin/models/<id>` | 更新；**`type` 在更新时被强制取旧值**（`model_admin.py:82‑82`），`api_key` 永不在此覆盖 |
| PUT | `/api/admin/models/<id>/credentials` | `{api_key}` 设置/更换，返回 `{fields:{api_key:{configured}}}`；不带 `api_key` 时只返元数据 |
| DELETE | `/api/admin/models/<id>/credentials` | 清空凭证 |
| DELETE | `/api/admin/models/<id>` | 删除；默认模型拒删 |
| POST | `/api/admin/models/<id>/test` | 连通性测试（chat/embed/rerank 已支持），返回 `{ok,latency_ms,detail}` |
| GET | `/api/admin/models/index-status` | 各 embed 模型的索引状态（轮询进度） |
| POST | `/api/admin/models/<id>/reindex` | 重建向量索引（后台任务） |
| POST | `/api/admin/models/<id>/default` | 设为该类型默认 |

**①‑P1‑1（阻断，线上已发生）**：`src/model_admin.py:24` `_ID_RE = re.compile(r'[A-Za-z0-9_-]{1,64}')` 拒绝了 seed 脚本生成的中文/点号 id。`deploy/seed_models.py:39‑41` 的 `_slug` 在 Unicode 下保留了中文和点号。生产 `models` 表 7 行中 **_3 条**被拒（含两个本地对话模型）。规则会被 PUT/DELETE/credentials/test/reindex/default 全部复用，所以这些模型的"改、删、测、设默认"**一律 400**；列表接口不经 `_row_by_id`，所以模型照样显示、弹窗照样能开——这就是用户看到的"找不到修改功能"。

### 2.2 B：否定语义现状

三层口径不一致：

- **字段表示**：`household_facts.member_facts()` / `reconcile_member()` 把"无残疾"解析成 **字符串** `fields['disability']='无残疾'`，同时把 `names['negative_identity_tags'].add('残疾')` 并从 `identity_tags` 里剔除。下游存在**两套表示**：健康类布尔门用 `value is False`（`src/policy_core/full_facts.py:96`、`src/policy_core/reasoning.py:1441‑1453`），字符串 `'无残疾'` 不参与。
- **追问调度**：`src/policy_core/questions.py:40‑46` `can_explore()` 对 `exploration_requires` 成员做 `facts[...]['value'] not in (False, None, '无', '未参保')` 判断。**否定值若是字符串 `'无残疾'`，会继续判为 True → 追问被排进计划。** 这是 ② 的实际通过路径。
- **追问题干**：`src/household_flow.py:384‑386` 的等级追问门，用 `re.search(r'重度残疾|[一二三四1234]级', policy_text) and '残疾' in smart_matcher_kb.member_identity_tags(f)` 决定是否追问等级。该门本身**已能识别 `'无残疾'`**（`smart_matcher_kb.py:378‑393` 的 `member_identity_tags` 显式排除 `'无残疾'/'不残疾'/'未残疾'/'没有'` 及 `非残疾|不是残疾|没有残疾`）——所以这一层**不是**根因，但仍需在重构后保持等价，且要补等价单测。

关键事实（已有实证）：`household_facts.member_facts('残疾情况：无残疾')` →
- `identity_tags=[]`、`negative_identity_tags=['残疾']`
- `fields['disability']='无残疾'`（**字符串**，在下游 `==`/`is False` 判定里是"真值"）

---

## 3. 目标（行为契约）

### 3.1 A — 模型管理前端

**A‑1 类型 Tab 卡片格**
- 页首一个 Tab 行：`全部 / 对话 / 嵌入 / 重排`，Tab 上带数量。
- 职责：只切视图，不请求后端（后端 `?type=` 本来就是可选路由；前端只 过滤本地数组，避免重复调用）。
- 网格每张卡片必须显性暴露：`默认` 徽标、类型徽标、`本地/云端` 徽标（**新维度，销 P1‑4**）、`无密钥` 徽标（仅 embed 需要时）、建果状态（embed 显示索引行数/维度/重建中）。
- 悬浮（或常显）操作：`编辑 / 测试连通 / 重建索引(仅 embed) / 设为默认 / 删除`。
- 删除默认模型：弹原生 confirm"先把该类型默认切到其他模型"。
- 新增入口是网格末尾一张"+ 添加模型"ADD 卡，点击打开抽屉，**只对 'chat' 之外的类型也开放**。

**A‑2 抽屉式编辑器**
- 右侧抽屉（z‑index 高于主内容；点击遮罩/ESC/取消关闭）。
- 字段与后端 `_parse_payload` 一一对应；`dim` 仅 `type=embed` 显、`query_instruction`/`json_mode` 仅 `type=chat` 显（保持现有规则）。
- 边框/文案上"新建"与"编辑"的区别：
  - **新建**：`type` 可选（Tab 已限定当前类型则默认选中）。
  - **编辑**：`type` **置灰不可改**（与后端 `is_update` 的强制忽略行为一致，消除"给了可编辑假象"）。
- 保存按钮：`保存`（不定期） + `保存前测试`（见 A‑3）。

**A‑3 保存前连通性测试（第 4 件）**
- 触发：抽屉内"保存前测试"按钮，编辑/新建都可用。
- 语义：**在不落库的情况下**，用当前表单里填的 provider/base_url/model_name/dim（+ 新建态下表单里的 api_key；编辑态下**未传 key 则用库里已存的**）打一次真实请求，返回 `{ok, latency_ms, detail}`。
- 后端需要**新增**一个 draft 测试接口（见 §4.6）：`POST /api/admin/models/test-draft`，入参 `{type, provider, base_url, model_name, dim?, api_key?}`，实现内部复用 `model_providers` 的 `test_chat/test_embed/test_rerank` 逻辑，**不写数据库、不触发缓存失效**。
- 结果就地展示：✓/✕ + 耗时 + detail；失败时 detail 需要是人类可读的原因（例如"base_url 返回 401，请检查 api_key"）。
- 兜底：若 draft 接口不可用（未实现时），按钮退化为"保存"后由 `POST /:id/test` 提示——但本设计**要求 draft 接口随 A‑3 一起落地**，不允许退化。

**A‑4 凭证三态卡（销 ①‑P1‑2）**
- 抽屉里，把当前"编辑态隐藏整行 + 新建态密码框"替换成**三态卡**：

| 状态 | 展示 | 操作 |
|---|---|---|
| 已配置 | 绿勾 + "已配置" | `更换` / `移除` |
| 未配置 | 锁形 + "未配置" | `配置` |
| 编辑中 | 真实密码框 + 保存/取消 | 保存内联提交 |

- `更换/配置` → `PUT /models/<id>/credentials {api_key }`，**立即/独立提交**，返回 `{fields:{api_key:{configured}}}` 后本地刷新状态。**不与"保存模型表单"合并提交。**
- `移除` → 内联二次确认 → `DELETE /models/<id>/credentials`，立即提交，不撤销。
- 编辑态的"已配置"状态**源自列表接口已返回的凭证状态**（不新增 GET 子资源）。
- 语义上与智枢 `CredentialResource.vue` 一致：保留/替换/清除三种意图分开，避免误删"钥匙"。

**A‑5 顺手销掉的次要缺陷**
- ①‑P1‑3：编辑态 `type` 置灰（A‑2 已含）。
- ①‑P1‑4：卡片增加 `本地/云端` 维度（A‑1 已含）；本地判据用现有 `src/model_providers.py:36` `LOCAL_HOSTS`。
- ①‑P1‑5：`重建索引` 按钮按后端前提（`provider=='openai'` && 非 `LOCAL_HOSTS` && `api_key` 已配置）灰显 + tooltip 显示原因（A‑1 已含）。

### 3.2 B — 否定语义全链路统一

> ⚠️ **本节前提已被真链路实证推翻（2026-11-10）**：按生产入口连续复现四轮，「无残疾」不产生任何残疾/等级追问；B‑0 的「投影成布尔 False 就能堵住追问」与 B‑1 的「`build_rule_questions` 会曝出 `disability_grade`」都缺乏证据。**改动目标以 §8 实证修订为准，本节保留作为历史推理与被证伪的候选机制清单。** 保留价值：记录「哪些路径已经核过、不必再查」，避免 Track B 重复劳动。

**B‑0 统一抹平两套表示**
- 在**事实层**（`household_facts.py`）引入唯一的显式否定谓词，形如 `is_explicit_negation(value)`，识别且仅识别：`False / None / '' / '否' / '无' / '没有' / '未' / '非' / '不' / '0' / '无残疾' / '没有残疾' / '非残疾' / '不是残疾' / '未残疾' / '不知道'/'未知'/'不清楚'/'暂时无法提供'`。
- 显式否定不能再靠字符串透传：归一化必须在**事实投影点**落地，而这一点已经核过，不在 `reconcile_member()` 里。
  - `src/household_facts.py:156` 现在的动作是：`str(fields.get('disability') or '').strip() in ('无','否','0','无残疾','非残疾','不是残疾人','没有残疾')` → `negatives.add('残疾')`。**它只往 `negative_identity_tags` 写，从不写布尔** — 这正是"无残疾仍被当残疾人"的落点，也是 B‑0 的补丁必须补的位置。
  - `src/policy_core/reasoning.py:1441‑1453` `_member_field()` 读的是 `hh['policy_core']['facts']`（键是 `member:disabled`，**布尔字段 `disabled`，不是 `household_facts` 里的字符串字段 `disability`**）：`isinstance(v,bool)` → 原值；否则 `'是'|'有'|'在读'|'已在校'|'true'|'True'`→`True`，`'否'|'无'|'未在读'|'不在校'|'false'|'False'|'没有'`→`False`。**字符串 `'无残疾'` 两边都不命中 → 返回 `('无残疾', True)` → 被 `_CATEGORY_RULES` 的 `check=lambda v:v is False` 判为真值**。归一化要消除的正是这条透传链。
  - 因此 B‑0 必须新增**显式否定归一函数**（如 `is_explicit_negation(v)`），并在事实写入路径里把显式否定投影成**布尔 `False`**（注意：投影目标是 `disabled` 布尔事实；`household_facts` 的字符串字段 `disability` 保留人类可读原文或另存 `disability_note`，避免丢文案）。正面表述保持字符串（`'一级'`、`'残疾（等级待确认）'` 等）。
- 原 `negative_identity_tags` 流派继续存在，与新的布尔表示**保持一致**：它们是同一事实的两种投影，不允许一处写字符串、另一处写布尔。`reconcile_member()` 的既有否定分支（`household_facts.py:156`）**保留**，只是在此之上再补一投影。
- 破坏性检查：`src/policy_core/full_facts.py:96` 的 `health_not_disability_denial` 已经在 `value is False and name=='disabled'` 下要求原文含 `无残疾|没有残疾|非残疾|不是残疾`——B‑0 的归一化必须保证句子恰好走到这个合法分支（即投影成 `False` 时，`quote` 要带上用户原文里那句含"无残疾/没有残疾/非残疾/不是残疾"的逐字片段），而不是再抛 `health_not_disability_denial`。同时不得把 `reasoning.py:951`「已验证的明确肯定否定必须保留为 conflicts」改回未知。

**B‑1 追问调度层（② 的实际通过路径，两个生产路径都要堵）**

经核对，生产路径有两条，**目前只有一条被 `can_explore` 守着**：

| 路径 | 追问从哪来 | 是否有 `can_explore` 守门 |
|---|---|---|
| `full_model`（`full_flow.py:16` → `full_questions.plan`） | `_candidates()`（`full_questions.py:37‑…`）取 `facts_missing`/`direction_missing` 与 `profile_discovery` 字段 | **有**：`full_questions.py:57 / 73 / 337` 都先调 `can_explore` |
| `rule_engine`（**生产当前打开**，`RULE_ENGINE=on`；`rule_flow.try_rule_run` → `build_rule_questions`） | `cards[].missing` 与 `details.missing`（`status in no_satisfied_atom/substantive_condition_missing`） | **没有**：`rule_flow.py:242‑315` 只检查 `key in asked` 与 `state=='known'`，**从未调用 `can_explore`** |

所以：
- 只改 `can_explore`（或 B‑0 的字段归一化）**不足以堵住生产规则路径**——`build_rule_questions` 仍会把 `disabled` 判为 `not True` 的缺项变成 `disability_grade` 追问。
- 必须**同时也**把 `can_explore` 注入 `build_rule_questions` 的候选筛选（与 `full_questions._candidates` 同口径，复用同一函数，不要复制筛选逻辑）：在 `for key in keys` 循环前用 `can_explore(key, st['facts'], fields)` 过滤掉不必要的空白事实，防止相同的问题从规则路径重复曝出。

具体改动：
- `src/policy_core/questions.py:40‑46` `can_explore()`：对布尔语义字段，`value in (False, None, '无', '未参保')` 之外再加入归一化后的显式否定（实际上 B‑0 已归一，布尔 `False` 已覆盖；按 `is_explicit_negation` 实现更稳）。
- `discovery_proof()`（同文件 35‑39）的 `exploration_conditions` 求值用同一个归一化查询，保证 `disabled=true` 条件不会因为值是字符串 `'无残疾'` 而误判为真出条件没触发的异常。
- `src/policy_core/rule_flow.py:242‑315` `build_rule_questions`：在 `keys` 生成后加一次 `can_explore` 过滤（在 `key in asked` 检查之外，紧邻 `line 291` 的 `for key in keys:` 循环）。
- 规则：**"已答清楚的字段不再追问"变成一条跨层一致谓词**，而不是各层各写一套字符串清单。

**B‑2 出卡层（保险丝）**
- `src/policy_core/reasoning.py:1426‑1435` `_CATEGORY_RULES` 的 `disabled` `check=lambda v: v is False`，以及 `_member_field()`（1441‑1453）里的布尔归一化——B‑0 之后 `v is False` 正好命中；**这些代码不动**，只作为不回退的见证。若 B‑0 没做归一化，`v is False` 不会命中 —— 这正是复现 ② 的路径。
- 新增 `tests/test_household_facts_negative.py` 类对 `household_facts` + `questions.can_explore` + `reasoning._member_field` 三层做**等价回归**：显式否定后不允许再排进 `disability_grade` 追问。

**B‑3 不回退条件**
- 正面表述不能被归一化吞掉：
  - `'一级'`、`'二级'` 等保留字符串；
  - `'残疾（等级待确认）'` 必须继续触发等级追问；
  - 用户我不知道级但给了类型（如"肢体残疾"）时，`disability_type` 字段仍可追问；
  - 不破坏"原有明确否定必须保留为 conflicts"（`reasoning.py:951` 的绑定修复规则）——改动只影响**是否追问**，不影响**已给结论**。

---

## 4. 设计（怎么改）

### 4.1 A 的文件与尺寸

| 文件 | 改 | 量级 |
|---|---|---|
| `src/model_admin.py` | 扩宽 `_ID_RE`；新增 `POST /api/admin/models/test-draft`；可选：把"编辑无凭证也可见"的元数据返回纳入列表（`row_meta` 已有，无需新增） | ~20 行 |
| `src/static/admin.html` | 模型区 `<section id="models-card">` 换成类型 Tab + 卡片网格 + 抽屉骨架；保留司机弹窗结构，改字段/加三态卡，且 `#model-mask` 复用为抽屉容器 | 模型区 ~180 行 |
| `src/static/admin.js` | 重写模型段 860–1105：卡片渲染、Tab 切换、新增抽屉逻辑、保存前测试、三态卡、凭证即时提交、二次确认删除；`$('admin.js')` 的 `:root` 缓存指纹 `?v=` 一起升 | 模型段 ~350 行 |
| `deploy/seed_models.py` | 不改（保留中文 id 口径）；如选择"改 `_slug`"，则同步改 _slug | 0 或小 |
| `src/model_providers.py` | 只被 draft 接口借用，不改 | 0 |
| `tests/test_model_admin_api.py` | 增补：CJK/点号 id 走编辑→保存→删除端到端；draft 测试接口契约 | ~60 行 |

**id 白名单决策**：**扩宽 `_ID_RE`**（推荐），因为把 `_slug` 改成 ASCII 会改变生产库里已有 id，代价更大；`_ID_RE` 修改后必须保持长度上限与**不能引入新的不安全字符**。

> 注意（builder 需复核）：id 会进入 `src/vector_reindex.py:37` 的 `reindex_cursor_<mid>` 配置键、audit_log、`models` 表上的部分唯一索引。扩宽前需确认这些下游是否假定 ASCII；若假定（如 JSON 键合法性无关，但日志/审计渲染需确认），必须同步加固。

### 4.2 A 的接口契约（新增/复用）

新增（A‑3）：

```
POST /api/admin/models/test-draft
Body: {"type": "chat|embed|rerank", "provider": "...", "base_url": "...",
       "model_name": "...", "dim": 1536?, "api_key": "..."?}
Resp: {"ok": true|false, "latency_ms": 123, "detail": "人类可读原因"}
副作用：无（不写库、不写缓存、不触发 reindex）
```

复用（A‑4 凭证三态卡）：

```
PUT   /api/admin/models/<id>/credentials  Body:{"api_key":"..."}  → {"ok":true,"fields":{"api_key":{"configured":true}}}
DELETE /api/admin/models/<id>/credentials                       → {"ok":true}
```

复用（A‑2/A‑3/A‑1）：`PUT /api/admin/models/<id>`、`POST /api/admin/models`、`POST /api/admin/models/<id>/test`、`POST /api/admin/models/<id>/reindex`、`POST /api/admin/models/<id>/default`、`GET /api/admin/models/index-status`、`GET /api/admin/models?type=`。

### 4.3 A‑3 draft 接口的复用边界

`src/model_admin.py:197‑209` 的 `test_model(mid)` 走的是 `model_providers` 的 `test_chat/test_embed/test_rerank`，但这些函数**取的是已入库行**。draft 接口需要"按传入 cfg 构造一个临时行"跑同一套函数，**不能复制测试逻辑到 `model_admin`**。

Builder 决策点（写进计划）：
- 方案甲：在 `model_providers` 里加 `test_with_cfg(cfg, model_name, kind, dim?)`，`test_chat/theme_embed/test_rerank` 内部改调它，`test_model(mid)` 与 `test-draft` 共用。
- 方案乙：`model_admin` 自己调 `model_providers` 的最新 category 函数、传入 cfg  path=embed/rerank 的真实入参。
- 推荐甲（复用、可操作性好），除非 `model_providers` 的签名被证明无法抽出。

```python
# 伪代码（kind=chat/embed/rerank）
def _run_draft(kind, provider, base_url, model_name, api_key, dim):
    import from model_providers import CFG_CHAT, CFG_EMBED, CFG_RERANK
    cfg = CFG_KIND[kind]; cfg['provider']=provider; cfg['base_url']=base_url
    cfg['model_name']=model_name; cfg['api_key']=api_key or ''
    if dim: cfg['dim']=dim
    return test_with_cfg(cfg, model_name, kind)
```

### 4.4 B 的健康（改动点要去做）

| 文件 | 位置 | 改动 |
|---|---|---|
| `src/household_facts.py` | `member_facts()`、`reconcile_member()`、`canonical_fact()` | 引入 `is_explicit_negation(value)`；显式否定的 `disability` 归一化 `False`；原文措辞进 `disability_note` |
| `src/policy_core/questions.py` | `can_explore()`（40‑46）、`discovery_proof()`（35‑39） | 用统一谓词判定"是否还值得追问" |
| `src/household_flow.py` | 384‑386 | 换成 `is_explicit_negation(f['disability'])` 直接判定（勿再依赖 `member_identity_tags` 的实现细节） |
| `src/smart_matcher_kb.py` | `member_identity_tags()`（378‑393） | 不做行为改动，但把排除清单**抽成** `is_explicit_negation` 复用（消除重复字符串清单） |
| `src/policy_core/full_facts.py` | 96 | 无行为改动；作为 `False + 原文有否定限定` 的见证 |
| `src/policy_core/reasoning.py` | 1426‑1435、1441‑1453 | 无行为改动；作为 `v is False` 见证 |
| `tests/test_houseload_facts_negation.py`（新增） | — | 三层等价回归 |

### 4.5 B 的验收

1. `member_facts('残疾情况：无残疾')` → `disabled` 语义归一化后 `value is False`，且 `disability_note='无残疾'`。
2. `questions.plan(...)` 在 `disabled` 为显式否定时**不再**把 `disability_grade` 排进 `pending`。
3. 正面对照：`member_facts('残疾情况：一级')` → `disability_grade` 追问**仍然**被排。
4. 既有 `src/policy_core/tests/`（或并存位置）里涉及 `disability` 的用例不回归。
5. 回归链：`PYTHONPATH=src:. /home/adminroot/miniconda3/envs/smart-help-web/bin/python -m unittest discover -s tests -p "test_*.py"` 全绿（基线 994 + 新增）。

### 4.6 顺序（两类改  ᠣᠯᠭᠣᠬᠤ

| 顺序 | Track | 理由 | 验收手段 |
|---|---|---|---|
| 1 | **B‑0/B‑1/B‑2** 否定语义统一 | 改动小、影响面集中、能用单测快速锁住；不依赖前端 | `tests/` 单测 + 既有全量回归 |
| 2 | **A‑1 id 白名单修复** | 阻断性缺陷，且是后续编辑画像能保存的前提 | 端到端：中文 id 模型编辑→保存→删除一只跑通 |
| 3 | **A‑2/A‑3/A‑4 前端整页重做** | 最大，但依赖 #2 先通，否则编辑照样 400 | 浏览器人工 + seed 后实际打开 `/admin` 走一遍四件 |

> 说明：B 与 A 互不阻塞，可并行；但 A‑3 依赖 A‑2 的抽屉骨架，A‑4 依赖 A‑3 之外的凭证子资源（后端本来就有）。

---

## 5. 风险

| 风险 | 影响 | 应对 |
|---|---|---|
| 扩宽 `_ID_RE` 触发下游 ASCII 假定（`vector_reindex` 键、audit_log、唯一索引） | 重建/调试路径静默失效 | 实现时核这些消费点的实际约束；在计划中列为逐条为改前检查 |
| 归一化 `disability=False` 改变字段语义，波及正面情形 | 残疾类正确问被吞 | 用 §4.5 的对照组回归 ×"同等同字段"保证；回归含三级联动 |
| 凭证三态卡解除"编辑态隐藏"后用户误删 key | 模型调用中断 | 删除后显示回"未配置"状态卡而不是空密码框；删除动作保留内联二次确认 |
| 抽屉重写牵连 `admin.js` 既有锚点/事件绑定失效 | 其他管理功能坏 | 只改模型段作用域；用 `/admin` 冒烟验其他功能不含有值 |

---

## 6. 未决问题（交 builder）

1. ~~`id` 里允许的字符集的确切上限~~ —— **已核**：`src/models.py:139` `id = db.Column(db.String(64), primary_key=True)`，上限是 **64**（不是 190），`deploy/migrate_models.py` 另有 `uq_models_default` 部分唯一索引。因此 `_ID_RE` 放宽时长度上限应与 64 对齐；`_slug` 保留中文/点号（`deploy/seed_models.py:41`）会生成 >64 字符的 id 需截断/转写（**该项仍需 builder 实现时定策略**——不影响设计，但实现计划里必须逐条给出）。
2. ~~`model_providers` 的 `test_chat/test_embed/test_rank` 签名能否抽出 `test_with_cfg`~~ —— **待实现期定**：§4.3 方案甲（复用现有三个函数）与方案乙（新增 draft 端点）都可行，实现计划必须选一个并写明，避免计划里出现"两套都用"。实施顺序表 1 是 B，A 在后，所以这个选择不阻塞 Track B。
3. `can_explore` 里是否还有其他字段用字符串否定表示（额度类 `out_of_pocket=0`、`med_insured='0'` 等）需要一并纳入统一谓词。
4. 前端 `#` 内联 `?v=` 缓存指纹升级后，是否有发布流程外的 CDN/代理缓存需要同步。
5. 新增 `test-dræft` 接口是否需要 `@admin_required` 之外的额外防护（预计：内网工具，风险低）。

---

## 7. 不做（再次明确写，避免范围蔓延）

- 不做 ③④⑤ 任何一项。
- 不做模型调试抽屉、本地模型浏览/下载。
- 不多加任何运行时开关（保持 `POLICY_MATCH_MODE=full_model / RULE_ENGINE=on / ANSWER_CLAIMS=1`）。

---

## 8. 实证修订（2026-11-10 真链路复现后，B 的目标层重定）

### 8.1 复现方法与可复跑脚本

生产入口 = `POLICY_MATCH_MODE=full_model` → `kb_rag.qa_match_stream` → `policy_core.full_flow.run`（真实政策库 + 本机 Qwen3.6-35B 作答）。复跑脚本：

- `docs/superpowers/evidence/2026-11-10-negation/repro_negation.py`（两轮，`hh={}` 起步）
- `docs/superpowers/evidence/2026-11-10-negation/repro_negation3.py`（每轮 `facts.resolve` 载入会话态，需 `app.app_context()`）
- `docs/superpowers/evidence/2026-11-10-negation/repro_negation4.py`（首轮建家庭，第二轮在同一内部 `hh` 上说「无残疾」——最接近生产的多轮语义）

跑法：`cd <repo> && set -a && . deploy/web.env && set +a && PYTHONPATH=src <venv>/bin/python docs/superpowers/evidence/2026-11-10-negation/repro_negation4.py`

### 8.2 四轮观测结果

| 轮次 | 输入 | 追问键 |
|---|---|---|
| 1 | 两口人 / 莲花县 / 农村户口 / 母亲58岁 / 有低保 | `core\|household\|disaster_loss`、`core\|household\|enrolled`、`core\|household\|medical_cost_event` |
| 2 | **我母亲无残疾，也没有残疾证，不用问残疾等级** | `core\|household\|monitoring`、`core\|household\|poverty_exit`、`core\|1\|member_poverty_exit` |
| 3 | 先问「莲花县残疾人两项补贴怎么申请？」再说无残疾 | 同上，仍无残疾类追问 |
| 4 | 首轮建家庭（母亲张兰58岁 + 我本人30岁），次轮「张兰无残疾，也没有残疾证」 | `monitoring` / `poverty_exit` / `member_poverty_exit`，**无 `disability_*`** |

**结论：按当前代码，「无残疾」推不出「问残疾等级」。** 另三条静态核验同向：

- `smart_matcher_kb.member_identity_tags({'disability':'无残疾'})` → `set()`（`{'health':'残疾','disability':'无残疾'}` 也是空集）
- `questions.can_explore('1:disability_grade', …)` 对「无残疾」和「一级」**均**返回 `False`（`discovery_proof` 先判 `exploration_conditions`，line‑44 的 `exploration_requires` 回退不可达）
- `rule_eval.load_rules()` 39 条规则中引用 `disability_grade` 的为 **0** 条
- `household_facts.py:87‑93` 反向守卫：文本含「无残疾」时连真等级都不写

⇒ §3.2 的 B‑0/B‑1 改动**不能**声称修复 ②，因为 ② 在目标代码上不可复现。

### 8.3 真链路暴露的两个真缺陷（Track B 的新靶子）

**缺陷 A：显式否定被整条丢弃（你说过的「无」系统一个字没记）**

- 观测：第 2 轮说「无残疾、没有残疾证」后，`members[].fields` 里只有 `core_age`，**既无 `disability` 字符串字段，也无 `negative_identity_tags`**，更无布尔 `disabled=false`。
- 落点：`src/policy_core/full_facts.py:145` 抽取提示词末尾写死「**健康绝不提disabled=false**」。这句本意是禁止「身体健康 ⟹ 无残疾」的推断，但措辞把**用户显式否认残疾**一并压掉；而 `src/policy_core/full_facts.py:96` 的守卫 `if value is False and name=='disabled' and not re.search(r'无残疾\|没有残疾\|非残疾\|不是残疾',quote): raise ValueError('health_not_disability_denial')` 本来是**允许**「原文含否定词时就输出 `False`」的——提示词比守卫更严，等于把合法通道关了。
- 修法方向：提示词必须区分两件事——「**健康** 不等于「无残疾」（不得据此输出 `disabled=false`）」与「**用户明说无残疾/没有残疾证 ⇒ 必须输出 `disabled=false` 并附逐字原话**」。守卫不动。

**缺陷 B：成员称谓/姓名从未绑定到成员编号（追问只能点「成员1」）**

- 观测：输入「母亲张兰58岁」「我本人30岁」，两轮跑完 `members[].name` 全是 `null`，但年龄都正确落到两个成员上。
- 落点：`src/policy_core/full_facts.py:9` `MEMBER=re.compile(r'(?:家庭成员|成员)\s*([1-9]\d?)(?!\d|人)')` 与 `src/policy_core/member_labels.py:79` `_MEMBER_HEAD=re.compile(r"(?:家庭)?成员\s*([1-9]\d?|[一二三四五六七八九十])")` —— **两者都只认字面量「成员N」**。自然说法（母亲/父亲/我本人/张兰）既不产生姓名发现，也不产生成员编号绑定。
- 后果（与用户观感直接相关）：系统只能问「**成员1**：该成员本人是否属于已认定的脱贫人口？」。用户刚说完「母亲无残疾」，看到针对「成员1」的追问，感知就是「说了也没听」。`member_labels.member_display()` 本已支持「成员1（张三）」渲染，但 `extract_member_names()` 拿不到名字，能力空转。
- 修法方向：把称谓词（母亲/父亲/儿子/女儿/配偶/本人/我…）与相邻中文姓名纳入成员身份发现，产出 `{member_no: name}` 与称谓映射；`member_display` 复用既有渲染。

### 8.4 目标重述（B 的新验收）

1. 用户明说「无残疾 / 没有残疾证 / 不是残疾人」⇒ 该成员事实层出现 `disabled=false`（带逐字 quote），且后续**不再**追问 `disability_grade` / `disability_certificate`；不得触发 `health_not_disability_denial`。
2. 用户用称谓或姓名指代成员（「母亲张兰」「我本人」）⇒ 该成员 `name` 非空，追问文案为「成员N（张兰）」而非「成员N」。
3. 正面表述不回退：`'一级'`、`'残疾（等级待确认）'`、`'肢体残疾'` 仍触发等级/类型追问。
4. 已有明确否定仍保留为 `conflicts`（`reasoning.py:951`），不因本次改动退回未知。

### 8.5 明确作废

- ~~在 `build_rule_questions` 注入 `can_explore`~~ —— 该路径不产生 `disability_grade`，无证据支持改动，**不做**。
- ~~`household_facts.py:156` 的否定分支补投影~~ —— 该分支不在生产 `full_model` 路径的事实来源上（生产事实来自 `full_facts` 的 LLM 抽取），补了也改不了 ② 的现象；**留作低优先一致性收尾，不列入 Track B 必做**。
- ~~B‑2「不改代码只做见证」~~ —— 前提（`_member_field` 收到 `'无残疾'` 导致误标）未在生产路径复现，见证测试改为 §8.4 的 1–4 条。

---

*本文档为设计阶段产物，未经用户批准不得动手。批准后进入 writing-plans。*

---

## 9. 实施后修订（Track A / Track B 都落地后补记）

### 9.1 字段显隐按语义，不按原文

原文 §2 写「`query_instruction` / `json_mode` 仅 chat」。实施时按代码语义改正：

- `query_instruction` → **仅 embed**（`model_providers.row_to_embed_cfg` 用它做查询前缀）；
- `json_mode` → **仅 chat**；
- rerank 两者都不显示。

抽屉里由 `syncDrawerRows()` 同时管 `#mf-dim-row` / `#mf-query-row` / `#mf-json-row`，`tests/test_admin_js_contract.py` 有断言。

### 9.2 `audit_log.target_code` 的真实宽度

`src/models.py` 定义的是 `String(64)`（实施前 32），但**生产 KingbaseES 上该列实为 `TEXT`（无界）**，本来就不会截断；只有旧 sqlite 本地库还停在 32。因此 `deploy/migrate_models.py` 的宽度判定改为三态：

- 正整数 = 定长，`<64` 才算 `missing`；
- `-1` = 无界（TEXT），永不报缺；
- `None` = 表/列缺失，只在 postgresql 方言下计入 `missing`。

### 9.3 最终评审的必改项

整支分支评审（两轴：spec 符合 + 代码质量/风险）给出 CHANGES REQUIRED，必改项已全部落地（commit `5a5c43a`）：

| # | 问题 | 处置 |
|---|---|---|
| 1 | `request()` 只读 `msg/reason`，后端可读 `detail` 到不了 UI | 改读 `detail`，并新增 `#save-test-result` 就地结果 + 404/500 兜底 |
| 2 | `/test-draft` 的 `id` 兜底可把已存密钥发到任意 `base_url` | 锁死 type/provider/base_url 与库中该行一致才兜底，并写 `test_draft_stored_key` 审计；4 条负向测试 |
| 3 | 默认模型没有删除按钮 | 始终渲染，点击给出"先切默认"的明确文案 |
| 4 | 抽屉无遮罩、点外部不关闭 | 外层改遮罩容器，点遮罩关闭 |
| 5 | 明文密钥在关闭后仍留在 DOM | `clearCredDraft()` 在关闭/取消/保存成功三处清 value 与卡片内容 |
| 6 | migrate 的 `ok` 不含 audit 宽度 | 见 §9.2 |
| 7 | 字段显隐与原 spec 冲突 | 见 §9.1 |
| 8 | Task 6 浏览器冒烟未跑 | 本机无 playwright；已降级为 **HTTP 级 ALL PASS** + 人工后验，风险由负责人接受 |

延后项（三态 CSS 已顺手补掉）：ADD 卡入网格、初始 active Tab、索引徽标维度、新建类型随 Tab、内联移除确认、`_ID_RE` 与 `_slug` 对齐、cursor key 宽度、`models.py` 的 CRLF、admin-only SSRF 文档化。
