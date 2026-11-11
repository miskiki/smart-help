# 模型管理前端整页重做（Track A）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `/admin` 的模型管理区重做成智枢 `ModelSettings.vue` 的四件交互（类型 Tab + 卡片网格、抽屉式编辑器、凭证三态卡、保存前连通性测试），在原生 JS 栈内完成，并顺带销掉 ①‑P1‑1～P1‑5 五个缺陷。

**Architecture:** 前端无框架，`src/static/admin.html` 内联样式 + `src/static/admin.js` 原生 JS（end2end 一个 IIFE，`request()` / `toast()` / `esc()` 是既有工具）。后端只做两处增量：放宽 `_ID_RE` 让 seed 生成的中文点号 id 可编辑，以及新增"保存前测试"的 draft 接口。凭证改动全部走既有 `/credentials` 子资源，前�套表单保存绝不带 `api_key`。

**Tech Stack:** Flask（Blueprint `model_admin`）、SQLAlchemy、原生 JS + 原生 CSS、KingbaseES（生产，Varchar 长度受约束）。测试：Python `unittest` + Flask `test_client`。

**Spec:** `docs/superpowers/specs/2026-11-10-model-admin-ui-and-negation-design.md` §1.1（Track A）、§2.1、§3.1、§4.1–4.3、§4.6 顺序 #2/#3。本计划只实现 Track A；Track B（否定语义）**不在**本计划内，另立计划。

## Global Constraints

- 不引入任何前端框架/npm 依赖：不做 vue、不做 vite、不改 `app.py` 静态目录、不加 `static/` 之外的资源。唯一允许改动的静态文件是 `src/static/admin.html` 与 `src/static/admin.js`。
- 只改模型段作用域：`admin.js` 除模型段（约 888–1105）与 `bindEvents()` 里挂模型按钮的代码之外，其他功能的 handler/锚点一律不碰。
- `src/model_admin.py` 的 `_ID_RE` 放宽后长度上限仍为 **64**（与 `models.id` 列一致，见 `src/models.py:142` `id = db.Column(db.String(64), primary_key=True)`），且不得引入路径分隔符、引号等不安全字符。
- 目录外不改：`src/model_providers.py` **只**被 draft 接口借用（抽取一个复用函数），不得改其对外行为。
- 凭证三态卡的"模型表单保存"与"凭证改动"是**两次独立提交**；编辑态表单绝不在 body 里带 `api_key`。
- `部署`（seed/迁移脚本）若要动，必须改 `deploy/migrate_models.py` 的 DDL 与生产库迁移登记，不能只改 Python 定义。
- 所有 `?v=` 缓存指纹（见 `src/static/admin.html` 末尾 `/admin.js?v=20261109-p2embed`）随 `admin.js` 改动一并升级。
- 全量回归命令（基线 994 通过）：`PYTHONPATH=src:. /home/adminroot/miniconda3/envs/smart-help-web/bin/python -m unittest discover -s tests -p "test_*.py"`。
- 本会话审批关闭，重试不得请求 sandbox escalation。

## Review Focus

以下输入/失败模式规格没有逐个列测试，是最可能伤到真实使用者的五类——给每行点名具体测试/冒烟项，并放进对应 task 的步骤里：

1. **中文/点号 id 的模型做任何编辑/删除**——审核者必须能凭 `pytest` 用例确认**没有**再出现 `模型编号无效`。→ Task 1 的 `test_cjk_dotted_id_roundtrip`。
2. **token 值被用于 key 生成/缓存键**（`reindex_cursor_<mid>`、audit log、配置键）——审核者可能担心放宽字符集后这些键名复用或断连；必须逐条说明下游约束。→ Task 1 的核对步骤 + `src/vector_reindex.py` 核读。
3. **匿名或非管理员访问 draft/凭证接口**——审核者可能假设新增 `/test-draft` 无鉴权；必须逐字确认仍是 `@admin_required`。→ Task 1/2 各回读路由装饰器。
4. **编辑态把 type/name 改掉**——`_parse_payload(is_update=True)` 会强制 `type_` 取旧行值，但 `name` 仍可改；UI 编辑态若仍可改 type，用户会觉得"改了没生效"但更危险的是误以为生效。→ Task 5 的 UI 用例 + Task 1 的 `test_update_ignores_type`。
5. **抽屉里粘贴形如 RFC3986 的 base_url（带 userinfo/空格）或非 http 协议**——审核者可能在 draft 测试里亲手刻意这样做；必须返回人类可读 detail 而不是 500。→ Task 2 的 `test_test_draft_rejects_bad_base_url`。

---

## 文件结构

| 文件 | 职责 | 状态 |
|---|---|---|
| `src/model_admin.py` | `_ID_RE` 放宽；新增 `POST /api/admin/models/test-draft`；`test_model` 内部改调新抽取的函数 | 改 |
| `src/model_providers.py` | 新增 `test_with_cfg(cfg, timeout=60)`；`test_chat/test_embed/test_rerank` 改为调用它 | 改（只加一个函数 + 改内部实现） |
| `src/static/admin.html` | 模型区换成 Tab 行 + 卡片网格 + 抽屉骨架 + 凭证三态卡；样式内联 | 改 |
| `src/static/admin.js` | 重写模型段：卡片渲染、Tab 切换、抽屉表单、三态卡、保存前测试、二次确认删除；升级 `?v=` | 改 |
| `deploy/migrate_models.py` | `audit_log.target_code` 列宽从 32 扩到 64（若 seed id 会写该列） | 改（按需） |
| `tests/test_model_admin_api.py` | 追加 id 往返 + draft 契约用例 | 改 |
| — | `config/model_settings.yaml` 的 seed id 现状**不改** | 不碰 |

> 为什么 `admin.html`/`admin.js` 分开只动模型段：其他 section（政策详情抽屉、生命周期弹窗）与模型编辑弹窗共用 `modal-mask` 样式与 `esc()`/`toast()`；完整重写两个文件会让审核者无法小步回归。按现有二件职责拆分即可。

---

## Task 1: 放宽 `_ID_RE` + `audit_log.target_code` 列宽 + 下游键核对

**Files:**
- Modify: `src/model_admin.py:24`
- Modify: `src/models.py:160`（`AuditLog.target_code`）
- Check: `src/vector_reindex.py`（读 `reindex_cursor_<mid>` 是否假定 ASCII）
- Modify: `deploy/migrate_models.py:42` 起的 DDL
- Test: `tests/test_model_admin_api.py`

**Interfaces:**
- Consumes: 无（先做，其他 task 按此口径调 `_row_by_id` 时能过 64 字符中文 id）
- Produces: `_ID_RE` 的新正则（写进本 task 的代码步骤里）；要求「放行 seed id 全集」与「长度 ≤64」。

- [ ] **Step 1: 先写失败测试**。在 `tests/test_model_admin_api.py` 新增 `TestIdNamespace`，含：

```python
class TestIdNamespace(ModelAdminApiTestBase):
    def test_cjk_dotted_id_roundtrip(self):
        seed_like = 'seed-本地Qwen3.6-35B-qwen3.6-35b-a3b'   # 实际 seed id（见 config/model_settings.yaml）
        resp = self.client.put('/api/admin/models/' + seed_like,
                               json={'name': '改名测试'})
        self.assertNotEqual(resp.status_code, 400)     # 关键：不能再被 _ID_RE 拒
        self.assertEqual(resp.status_code, 404, resp.get_data(as_text=True))  # 库中没有 → 404 而非 400

    def test_id_length_capped_at_64(self):
        bad = 'a' * 65
        resp = self.client.put('/api/admin/models/' + bad, json={'name': 'x'})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('模型编号无效', resp.get_json()['msg'])

    def test_rejects_unsafe_chars(self):
        for bad in ('/', '../../etc', 'q"x', "q'x"):
            resp = self.client.put('/api/admin/models/' + bad, json={'name': 'x'})
            self.assertEqual(resp.status_code, 400, bad)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=src:. /home/adminroot/miniconda3/envs/smart-help-web/bin/python -m unittest tests.test_model_admin_api.TestIdNamespace -v`
Expected: `test_cjk_dotted_id_roundtrip` FAIL（`模型编号无效` 400，而非 404）。

- [ ] **Step 3: 放宽正则**。`src/model_admin.py:24` 改为：

```python
_ID_RE = re.compile(r'[\w.一-鿿]{1,64}', re.UNICODE)
```

一行说明：`\w` 在 `re.UNICODE` 下已含中文/数字/字母/下划线，显式加上 `[\u4e00-\u9fff]` 是为了把中文范围显式化便于 review。**不包含** `/`、引号、`;`、控制字符、空格。若实现者更倾向不写显式区间、直接 `[^\/\\:*?"<>|]+`，也可，但必须守住"不含路径分隔符/引号"，并把它写在该行上方注释里。

- [ ] **Step 4: 把 `AuditLog.target_code` 从 32 扩到 64**。`src/models.py` 的 `target_code = db.Column(db.String(32))` → `db.Column(db.String(64))`；`deploy/migrate_models.py` 里 `audit_log` 的 `target_code VARCHAR(32)` → `VARCHAR(64)`。**理由（写进注释）**：`model_admin._audit` 在每次 create/update/credentials/clear/delete/reindex/default 都写 `target_code=row.id`，而 seed 的 7 条 id 里 6 条 ≥34 字符（最长 `seed-local-qwen3-emb-emb-qwen3-embedding-4b`）。生产库（KingbaseES）会因变长串溢出报错；本地 sqlite 不拦。此步必须和迁移脚本同步改，不能只改定义。

- [ ] **Step 5: 读下游消费点并记录结论**。`grep -n "mid\|\.id" src/vector_reindex.py | head -30`，确认 `reindex_cursor_<mid>`、config 键、日志里是否真的要求 ASCII。把结论写成一行注释挂在 `_ID_RE` 上方；若发现任何真假定，追加子步骤修复，否则本步只是记录。

- [ ] **Step 6: 跑测试确认通过**

Run: `PYTHONPATH=src:. /home/adminroot/miniconda3/envs/smart-help-web/bin/python -m unittest tests.test_model_admin_api.TestIdNamespace -v`
Expected: 3 个测试 PASS。

- [ ] **Step 7: 全量回归**

Run: `PYTHONPATH=src:. /home/adminroot/miniconda3/envs/smart-help-web/bin/python -m unittest discover -s tests -p "test_*.py"`
Expected: 全绿（994 + 新增）。

- [ ] **Step 8: Commit**

```bash
git add src/model_admin.py src/models.py deploy/migrate_models.py tests/test_model_admin_api.py
git commit -m "fix(models): 放宽 _ID_RE 放行 seed 中文点号 id，audit_log.target_code 扩宽到 64"
```

---

## Task 2: 抽取 `test_with_cfg` + 新增 `POST /api/admin/models/test-draft`

**Files:**
- Modify: `src/model_providers.py`（新增 `test_with_cfg`；`test_chat/test_embed/test_rerank` 内部改调）
- Modify: `src/model_admin.py`（新增路由）
- Test: `tests/test_model_admin_api.py`

**Interfaces:**
- Consumes: `row_to_chat_cfg/row_to_embed_cfg/row_to_rerank_cfg`、`validate_base_url`
- Produces: `test_with_cfg(cfg: dict, timeout: int = 60) -> dict`（返回 `{'ok': bool, 'latency_ms': int, 'detail': str}`）；`POST /api/admin/models/test-draft`，body `{type, provider, base_url, model_name, dim?, api_key?}`，返回 `{ok, latency_ms, detail}`，**不写库、不缓存、不触发 reindex**。

- [ ] **Step 1: 先写失败测试**（`TestTestDraft`）：

```python
class TestTestDraft(ModelAdminApiTestBase):
    def _post(self, **overrides):
        payload = {'type': 'chat', 'provider': 'openai',
                   'base_url': 'http://127.0.0.1:8081/v1',
                   'model_name': 'qwen-test'}
        payload.update(overrides)
        return self.client.post('/api/admin/models/test-draft', json=payload)

    def test_shape_and_no_db_write(self):
        before = self._list()['total']
        r = self._post()
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        body = r.get_json()
        self.assertTrue(body['ok'] in (True, False))      # 可达性未知，但契约固定
        for k in ('latency_ms', 'detail'):
            self.assertIn(k, body)
        self.assertEqual(self._list()['total'], before, 'draft 不得写库')

    def test_rejects_bad_base_url(self):
        r = self._post(base_url='not-a-url')
        self.assertEqual(r.status_code, 400)
        self.assertIn('detail', r.get_json())   # 或 err.message 里的人类可读原因

    def test_embed_requires_dim(self):
        r = self._post(type='embed')
        self.assertIn(r.status_code, (400,))
```

`test_rejects_bad_base_url` 与 `test_embed_requires_dim` 的具体协议（400 还是 `{ok:false,detail:...}`）实现者先定：路由**不应**抛 `ApiError` 让 body 缺 detail，而是捕获并返回 `{"ok":False,"latency_ms":0,"detail":"<原因>"}`。两种都可，但必须自洽——**写成注释决定**，不要让前端为了猜而写。

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=src:. /home/adminroot/miniconda3/envs/smart-help-web/bin/python -m unittest tests.test_model_admin_api.TestTestDraft -v`
Expected: FAIL（`4 Not Found`）。

- [ ] **Step 3: 抽取 `test_with_cfg`**。在 `src/model_providers.py` 里加：

```python
def test_with_cfg(cfg, timeout=60):
    """按传入 cfg 跑 chat/embed/rerank 之一；不落库。cfg 键同 row_to_chat_cfg/row_to_embed_cfg/row_to_rerank_cfg 的字段集合。"""
    kind = cfg.get('kind')          # 由调用方填充；chat/embed/rerank
    ...
    # 内部按 kind 复用 test_embed/test_rerank 的 HTTP 调用主体：
    # - chat  → POST {base_url}/chat/completions
    # - embed → POST {base_url}/embeddings
    # - rerank→ POST {base_url}/...　(或见 row_to_rerank_cfg.api_url)
    ...
    return {'ok': ok, 'latency_ms': ms, 'detail': human_readable}
```

一行说明：`test_chat/test_embed/test_rerank` 现有签名是 `(row, timeout=60)`，先把 `row` 转 `cfg` 的逻辑（`row_to_*_cfg`）已经存在，所以新函数只要吃 `cfg` 并复用同一段 HTTP 调用体；`test_chat(row)` 洗成 `return test_with_cfg(row_to_chat_cfg(row))`。**不要复制**测试逻辑到 `model_admin.py`。

- [ ] **Step 4: 新增路由**。`src/model_admin.py` 加：

```python
@model_bp.post('/test-draft')
@admin_required
def test_draft():
    """保存前连通性测试：按传入字段构造临时 cfg 跑一次，不写库、不缓存、不触发 reindex。"""
    from model_providers import CFG_CHAT, CFG_EMBED, CFG_RERANK, test_with_cfg   # 或 CFG_* 的实际名
    d = request.get_json(silent=True) or {}
    ...
    return jsonify(ok=result['ok'], latency_ms=result['latency_ms'], detail=result['detail'])
```

具体 `cfg` 构造：取 `CFG_CHAT/CFG_EMBED/CFG_RERANK`（或等价默认 cfg 工厂），把传入 `provider/base_url/model_name/api_key/dim` 覆盖进去，`kind` 按 `type` 填。**在实现者自洽 Write 注释**：哪些字段必需（`base_url` 必填，`model_name` 必填，`embed` 必填 `dim`）。

- [ ] **Step 5: 确保 base_url 校验先于实网调用**：`validate_base_url(base_url, type_)` 失败时**不要**打进网络，直接返回 `{"ok":False,"latency_ms":0,"detail": reason}`。这正是 Review Focus #5。

- [ ] **Step 6: 跑测试确认通过**

Run: `PYTHONPATH=src:. /home/adminroot/miniconda3/envs/smart-help-web/bin/python -m unittest tests.test_model_admin_api.TestTestDraft -v`
Expected: 全部 PASS。

- [ ] **Step 7: 全量回归**

Run: `PYTHONPATH=src:. /home/adminroot/miniconda3/envs/smart-help-web/bin/python -m unittest discover -s tests -p "test_*.py"`
Expected: 全绿。

- [ ] **Step 8: Commit**

```bash
git add src/model_providers.py src/model_admin.py tests/test_model_admin_api.py
git commit -m "feat(models): 抽取 test_with_cfg 并新增 POST /test-draft 保存前连通性测试"
```

---

## Task 3: 卡片网格 + 类型 Tab + 抽屉骨架（`admin.html`）

**Files:**
- Modify: `src/static/admin.html`（模型区 `#models-card` 与 `#model-mask` 弹窗，约 230–331 行；内联 CSS）

**Interfaces:**
- Consumes: Task 1 的 `_ID_RE` 放宽；后端现有 `GET /api/admin/models?type=` / `POST /:id/test` / `POST /:id/credentials`。
- Produces: 一个 tab 容器（id `model-tabs`，每项 `data-mtype` + 计数徽标 `#model-tab-count-<type>`）、卡片网格容器 `#model-grid`、ADD 卡片 `#model-grid-add`、抽屉容器复用 `#model-mask` 但改为 `#model-drawer`；字段 id 保持既有（`mf-name/mf-type/mf-provider/mf-base-url/mf-model-name/mf-dim/mf-query-instruction/mf-json-mode`）以外的新增见下。

- [ ] **Step 1: 写一个 HTML 结构核对脚本**（无 JS 框架，所以"测试"是静态结构断言）。在 `tests/` 下加 `test_admin_model_dom.py`：

```python
import pathlib, unittest
ROOT = pathlib.Path(__file__).resolve().parents[1]
HTML = (ROOT / 'src' / 'static' / 'admin.html').read_text(encoding='utf8')
JS = (ROOT / 'src' / 'static' / 'admin.js').read_text(encoding='utf8')

class TestModelDom(unittest.TestCase):
    def test_tabs_and_grid_and_drawer_present(self):
        for id in ('model-tabs', 'model-grid', 'model-grid-add', 'model-drawer',
                   'mf-api-key-tag', 'credential-card', 'save-test-btn'):
            self.assertIn(id, HTML)
    def test_js_refs_new_ids(self):
        for id in ('model-tabs', 'model-grid', 'credential-card', 'save-test-btn'):
            self.assertIn(id, JS)
            self.assertIn('#' + id if False else "'" + id + "'", JS)
    def test_cache_fingerprint_upgraded(self):
        ver = HTML.split('/admin.js?v=')[1].split('"')[0]
        self.assertRegex(ver, r'^20261110')
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=src:. /home/adminroot/miniconda3/envs/smart-help-web/bin/python -m unittest tests.test_admin_model_dom -v`
Expected: FAIL（`id='model-tabs' not in HTML`）。

- [ ] **Step 3: 改 `admin.html` 模型区结构**。把现有 `#models-card` 里的 `btn-row` 与 `#models-list` 替换为：

```html
<div class="tabs" id="model-tabs">
  <button class="tab" type="button" data-mtype="">全部 <b id="model-tab-count-">0</b></button>
  <button class="tab" type="button" data-mtype="chat">对话 <b id="model-tab-count-chat">0</b></button>
  <button class="tab" type="button" data-mtype="embed">嵌入 <b id="model-tab-count-embed">0</b></button>
  <button class="tab" type="button" data-mtype="rerank">重排 <b id="model-tab-count-rerank">0</b></button>
</div>
<div class="model-grid" id="model-grid"></div>
<div class="model-card model-card--add" id="model-grid-add"><span>+ 添加模型</span></div>
```

一行说明：`#model-type-filter`/`#model-refresh-btn` 这两样旧入口的 id **不再使用**（保留也行，但 JS 不再监听它们；改此句即可）。
抽屉骨架（`#model-mask` → `#model-drawer`，或保留 `#model-mask` 外壳另加脚注行）改为右侧抽屉，必须有一席之地：

```html
<div class="model-drawer-card" id="model-drawer" hidden>
  <div class="drawer-head">
    <h3 id="model-modal-title">添加模型</h3>
    <button class="close" type="button" data-close="model-drawer">×</button>
  </div>
  <div id="model-form-body">
    <!-- 字段留空；Task 4 填 -->
  </div>
  <div class="drawer-foot">
    <button class="btn btn-gray" type="button" id="save-test-btn">保存前测试</button>
    <button class="btn btn-blue" type="button" id="model-save-btn">保存</button>
    <button class="btn btn-gray" type="button" data-close="model-drawer">取消</button>
  </div>
</div>
```

- [ ] **Step 4: Task 3 只到"骨架"；字段留空是有意**。Task 4 填字段。本步结束供应用编辑器能打开抽屉并看到标题为"添加模型""编辑模型：X"。

- [ ] **Step 5: 加一页级 CSS**。在 `<style>` 内追加 `.tabs/.tab/.model-grid/.model-card/.model-card--add/.drawer-head/.drawer-foot/.cred-card/.cred-state` 等类，保持现有「卡片 + 徽章 + 按钮」视觉语言，不引第三方库。

- [ ] **Step 6: 跑 DOM 测试**

Run: `PYTHONPATH=src:. /home/adminroot/miniconda3/envs/smart-help-web/bin/python -m unittest tests.test_admin_model_dom -v`
Expected: 全 PASS（若 step 1 的 `#mf-api-key-tag/#credential-card` 是 Task 4 才加的，把 `test_js_refs_new_ids` 里的对应断言挪到 Task 4 后跑）。目录没写死，实现者自行调整。

- [ ] **Step 7: Commit**

```bash
git add src/static/admin.html tests/test_admin_model_dom.py
git commit -m "refactor(admin): 模型区改为类型 Tab + 卡片网格 + 抽屉骨架，模型编辑改抽屉"
```

---

## Task 4: `admin.js` 重写模型段（渲染 + 抽屉字段 + 三态卡）

**Files:**
- Modify: `src/static/admin.js`（模型段 888–1105；`bindEvents()` 里的模型按钮绑定更新）

**Interfaces:**
- Consumes: Task 3 的 DOM id；后端 `GET /api/admin/models`、`POST /:id/test`、`PUT /:id/credentials`、`DELETE /:id/credentials`、`POST /:id`、`POST ""`、`POST /:id/delete`（删除 404→改既有 404 语义）。
- Produces: `renderModels(items, status)`、`renderTabCounts(items)`、`openModelDrawer(model)`、`saveModel()`、`credentialCard(model)`、`testDraft()`、`pollReindex()`。

- [ ] **Step 1: 写 JS 侧测试**。纯 DOM 无框架 → 用一段 Node 断言 JS 里的函数体/字符串常量（不真跑浏览器）。在 `tests/test_admin_js_contract.py`：

```python
import pathlib, unittest
ROOT = pathlib.Path(__file__).resolve().parents[1]
JS = (ROOT / 'src' / 'static' / 'admin.js').read_text(encoding='utf8')

class TestAdminJsContract(unittest.TestCase):
    def test_tab_switch_does_not_request(self):
        # 正则匹配 Tab 切换函数体：不出现 request(
        import re
        m = re.search(r'function \[^(]*?modelTab[^\n]*\n(.*?)\n  }', JS)
        self.assertIsNotNone(m)
        self.assertNotIn('request(', m.group(1))
    def test_credential_save_is_not_in_model_form(self):
        # saveModel 的 body 不得含 api_key
        m = re.search(r'function saveModel\(\) \{.*?\n  \}', JS, re.S)
        self.assertIsNotNone(m)
        body = m.group(0)
        self.assertNotIn('api_key', body.replace('api_key_configured', ''))
    ...
```

> 注：这种"字符串断言"测试不是行为测试，是**契约/回归护栏**；真实行为靠 Task 7 的浏览器冒烟。实现者据此写，注意别把未来 Task 6 才实现的另一函数一并断言。

- [ ] **Step 2: 跑确认失败**

Run: `PYTHONPATH=src:. /home/adminroot/miniconda3/envs/smart-help-web/bin/python -m unittest tests.test_admin_js_contract -v`
Expected: FAIL。

- [ ] **Step 3: 重写 `renderModels(items, status)`**。保持 `esc()` 转义、`embedIndexBadge()` 逻辑；卡片要显示：类型徽标、`m.kind`（本地/云端，在 Task 1 已有）、**默认**徽标、**无密钥**徽标（仅 embed）、embed 索引徽标。卡片操作按钮：`编辑` `复制`（仅非内置；smart-help 没有 `is_builtin`，所以**不复制**这项）、`测试连通`、`重建索引`（仅 embed，且按 `provider=="openai" && kind!='本地' && api_key_configured` 灰显 + tooltip 理由）、`设为默认`、`删除`（`is_default` 时显示原生 confirm 先切默认）。按钮仍以 `data-mop` + `data-mid` 驱动，`onModelOp` 不变。

- [ ] **Step 4: 改 Tab 切换**：`modelTabs` 上 `click` → 先在本地过滤 `adminModels` 数组（本地缓存，不再 `loadModels()` 做事后过滤），更新 `#model-tab-count-*`、`#model-grid`；`#model-grid-add` 点击 → 打开抽屉。

- [ ] **Step 5: `openModelDrawer(model)`**。字段回填；**编辑态把 `mf-type` 置灰、`mf-type` 的 `value` 用 `model.type`；**新建态 `mf-type` 可改；`dim` 行仅 embed 显；`query_instruction`/`json_mode` 仅 chat 显；`mf-api-key-row` 的位置留空（Task 6 盖凭证卡）。

- [ ] **Step 6: 凭证三态卡**。抽屉内 `mf-api-key-row` 位置渲染 `credentialCard(model)`：

- 有 `model.api_key_configured===true` → 显示「已配置」+ `更换` `移除`
- `false|undefined` → 显示「未配置」+ `配置`
- `编辑中` → 真实 `password` input + `保存` `取消`，空值时"保存"禁用；`Enter` 直接保存
- 保存 → `PUT /api/admin/models/<id>/credentials {"api_key":...}` → 返回 `{fields:{api_key:{configured}}}` 刷新本地 state
- 移除 → 二次确认 → `DELETE /api/admin/models/<id>/credentials`
- **不新加** GET 子资源；状态元数据来自列表的 `api_key_configured`

- [ ] **Step 7: `saveModel()`**：payload = 既有字段；编辑态**绝不**带 `api_key`（新键映射备注：请求体里 `api_key_configured` 是列表字段，不要误拷进 payload）；新建态若用户填了 `mf-api-key` 才带 `api_key`（若我们改成三态卡，新建态也走"未配置→配置"对话框，则新建态 `api_key` 从不在表单 payload 里——需在 Task 4 里**选一个**并写注释）。

- [ ] **Step 8: 跑契约测试**

Run: `PYTHONPATH=src:. /home/adminroot/miniconda3/envs/smart-help-web/bin/python -m unittest tests.test_admin_js_contract -v`
Expected: PASS。

- [ ] **Step 9: 跑全量回归**（JS 改动不应影响 Python 测试，但确保没有因 import 顺序/字符串误伤）

Run: `PYTHONPATH=src:. /home/adminroot/miniconda3/envs/smart-help-web/bin/python -m unittest discover -s tests -p "test_*.py"`
Expected: 全绿。

- [ ] **Step 10: Commit**

```bash
git add src/static/admin.js src/static/admin.html tests/test_admin_js_contract.py
git commit -m "feat(admin): 模型卡片网格、Tab、抽屉、凭证三态卡

- 卡片显示 类型/本地云端/默认/无密钥/索引 徽标
- 火柴 credential card：configured/unconfigured/editing 三态
- 凭证改动独立提交，表单保存不带 api_key
- 编辑态 type 置灰
```
```

---

## Task 5: 保存前测试按钮（前端接 Task 2 的 `/test-draft`）

**Files:**
- Modify: `src/static/admin.js`（`save-test-btn` 绑定 + `testDraft()`）
- Test: `tests/test_admin_js_contract.py`

**Interfaces:**
- Consumes: `POST /api/admin/models/test-draft`（Task 2）
- Produces: `launchDraftTest(model|null)`；按钮在抽屉底部 `v-if` 语义等价（新建/编辑都可用）。

- [ ] **Step 1: 写契约断言**：

```python
    def test_save_test_btn_calls_draft(self):
        self.assertIn('test-draft', JS)
        self.assertIn('save-test-btn', JS)
        # save-test 只能与字段无关——按钮文本不含"保存后"
        m = re.search(r'id="save-test-btn"[^>]*>\s*([^<]+)\s*<', JS)  # 若按钮在 HTML 则检查 HTML
```

- [ ] **Step 2: 跑，确认失败**

Run: `... -m unittest tests.test_admin_js_contract -v`
Expected: FAIL。

- [ ] **Step 3: 实现 `launchDraftTest(model)`**：从抽屉字段读 `provider/base_url/model_name/dim`；新建态若用户填了 api_key 则带上，编辑态不传 key（后端按需回落到库里配置）；`request('/api/admin/models/test-draft', {method:'POST', body})`。

- [ ] **Step 4: 就地展示结果**：`✓/✕` + `latency_ms` + `detail`；失败时把 `detail` 加粗放大展示（`#save-test-result`）。

- [ ] **Step 5: 兜底文案**：如果 draft 接口因为某种原因 404/500， toast 错误并提示"保存后由连通性测试提示"，但**不允许**工具隐藏失败。

- [ ] **Step 6: 跑契约测试**

Run: `... -m unittest tests.test_admin_js_contract -v`
Expected: PASS。

- [ ] **Step 7: 提交**

```bash
git add src/static/admin.js tests/test_admin_js_contract.py
git commit -m "feat(admin): 模型抽屉「保存前测试」按钮接入 /test-draft"
```

---

## Task 6: 浏览器冒烟 + 集成验收

**Files:** 无代码改动；`src/static/admin.html` 的 `?v=` 指纹已在 Task 3 升级（或 Task 4 若未升级，本步补升）。

- [ ] **Step 1: 冒烟清单**（一次 `/admin` 会话，seed 后）：

| 检查 | 期望 |
|---|---|
| Tab 切换 全部/对话/嵌入/重排 | 只切本地缓存视图，**不**出现对后端的 `GET /api/admin/models` |
| 每张卡片 | 至少出现 类型/本地云端/默认/无密钥 徽标；embed 卡片出现索引徽标 |
| 点击卡片"编辑" | 抽屉从右侧滑出；`类型`  كوكونىس灰；`API Key` 显示三态卡 |
| 三态卡已配置 → 点"移除" | 二次确认 → `DELETE /credentials` → 状态回到「未配置」 |
| 三态卡未配置 → 点"配置" | 出现密码框 + 保存 → `PUT /credentials` → 该卡回「已配置」 |
| 保存前测试 | ✓/✕ + 毫秒 + detail；失败时 detail 是人类可读原因 |
| 编辑一个中文 id 模型（seed 的本地模型） | 不出现 `模型编号无效`；保存成功 |
| 删掉"对话"类型的默认模型 | 原生确认要求先切默认 |
| verify/冒烟 | 其他 section（政策详情抽屉、生命周期弹窗）仍正常 |

- [ ] **Step 2: 冒烟结果写入 `docs/superpowers/plans/` 后** commit 或留在会话；**以 Task 1 的 CJK 往返测试为行为基准**，二者都要绿。

- [ ] **Step 3: 回归**

Run: `PYTHONPATH=src:. /home/adminroot/miniconda3/envs/smart-help-web/bin/python -m unittest discover -s tests -p "test_*.py"`
Expected: 全绿。

- [ ] **Step 4: Commit（若 Task 4 未升级指纹则本步补）**

```bash
git commit -am "chore(admin): 冒烟通过；admin.js ?v= 指纹升级

验证：Tab 切换不请求后端；中文 id 编辑不再 400；三态卡配置/移除往返通。
```

---

## 附：Task 我在写时的自检（自省）

- **规格覆盖**：§3.1 的 A‑1（Tab/卡片/徽标/操作/删除默认确认/ADD 卡）→ Task 3+4；A‑2（抽屉 + 字段集合/编辑态 type 置灰）→ Task 3+4；A‑3（保存前测试）→ Task 2+5；A‑4（凭证三态卡）→ Task 4；A‑5（P1‑3/4/5）→ Task 1（放宽）+ Task 3（本地云端/依赖/无密钥徽标）+ Task 4（灰显 reindex 条件）。全部有对应。
- **步骤粒度**：每步只写一种东西（正则、函数体、DOM 结构、测试断言）。
- **类型一致性**：Task 2 的返回契约 `{ok,latency_ms,detail}` 与原 `test_model` 完全一致；Task 4 的 payload 键与 `_parse_payload`（`type/name/provider/base_url/model_name/dim/query_instruction/json_mode/source`）一致；Task 5 复用 Task 2 契约。
- **Review Focus**：五条输入类各有 Task 1/2/4/5 的用例或冒烟项覆盖。
- **比例**：本计划约 20KB，而规格 283 行，属同一量级；前端无行为测试框架，故以 DOM/JS 契约断言 + 浏览器冒烟代替，这是本次计划的正当替代。
