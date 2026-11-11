# 30 · 模型注册表：解除模型硬耦合 + 前端自定义连接对话/嵌入/重排模型

> **参考仓库**：`/home/adminroot/projectfiles/caichang/project/ZhuSuRag-maiin/ZhiSuRag-main`（智枢文渊，Go + GORM + Vue3，腾讯 WeKnora 改造）
> **对照基线**：`/home/adminroot/smart-help`（Python/Flask + Kingbase，HEAD=ec89699，2026-11-08 政策全国化收尾后）
> **本文性质**：设计/方案文档。不写代码、不改资产。回答四个问题：参考项目的自定义模型机制长什么样？smart-help 现在耦合在哪？目标设计是什么样？分几步落地、怎么验收、风险在哪？

---

## 0. 一句话结论

1. **smart-help 的模型是"yaml 平台捆绑"，不是一等资源**：对话/嵌入/重排三类模型全部写死在 `config/model_settings.yaml` 的 `MODEL_PLATFORMS` 里，前端只能"选已有的"，不能"接新的"。
2. **最深的耦合是嵌入模型被向量索引锁死**：`index_dim()` 全局锁 + 单一 `_VEC_CACHE` 内存矩阵 + 表名白名单 `('policy_vector_qwen3','policy_vector')`，换嵌入模型必须跑一次性脚本重建。
3. **但向量表已有 `embed_model` 列**（`src/kb_rag.py:322` 查询时按 `v.embed_model=:model` 过滤），行级模型标记的基础已在库里——方案利用这一点做到**多嵌入模型共存、切换即回滚**，不需要表名扩散。
4. **参考项目（智枢）的做法是"模型=数据库行+类型+厂商构造器+前端可增删改测"**：`models` 表按 (类型) 定默认、api_key AES-GCM 加密落库、凭证走独立子资源、`/models/:id/debug` 用生产同款构造器实测连通性、运行时按 ID 现场构造客户端无池化。本方案按 smart-help 的 Python/Flask 体量裁剪移植。
5. **落地分三步**：P1 对话模型注册表（最浅耦合，先打通"前端自定义+连通性测试"全链路）→ P2 嵌入模型+向量索引多模型共存（最难，含 reindex 泛化）→ P3 重排模型+全量切换。每步独立开关、独立回退，默认行为不变。

---

## 1. 背景与目标

**用户请求**（2026-11-09）：对 smart-help 接入的模型解除耦合绑定，丰富自定义模型功能，使用户能**在前端自定义连接**的模型进行使用，包括**对话模型、嵌入模型、重排模型**三类。先研读参考项目（智枢文渊）的自定义模型功能，理解后再出方案。

**范围内**：
- 三类模型（chat/embed/rerank）从 yaml 平台捆绑迁移到数据库注册表。
- 前端"模型管理"页：增删改、默认切换、连通性测试。
- 问答页对话模型下拉从"读 yaml"改为"读注册表"。
- 嵌入模型切换时的向量索引兼容策略。
- 凭证加密、SSRF 防护、管理权限收口。

**范围外**（明确不做，避免扩散）：
- `deploy/foreground.py` 本地 llama 栈的编排方式（8081/8095/8096/8097 仍由 supervisor 管理，只是"本地平台"变成注册表里的一行）。
- ASR / VLLM / 文生图等智枢支持的其它模型类型（smart-help 无此需求）。
- 多租户（smart-help 是单租户+admin 角色制，不引入 tenant_id）。
- Ollama 本地模型拉取状态机（智枢的 local 源走 Ollama 自动下载；smart-help 本地模型是 llama-server 常驻，不需要下载态）。

---

## 2. 参考架构：智枢文渊的自定义模型机制

（源码依据：`internal/types/model.go`、`internal/handler/model.go`、`internal/handler/model_credentials.go`、`internal/application/service/model.go`、`frontend/src/views/settings/ModelSettings.vue`、`frontend/src/components/ModelEditorDialog.vue`）

### 2.1 数据模型：模型是一等资源

`models` 表（GORM）关键字段：

| 字段 | 说明 |
|---|---|
| `type` | `KnowledgeQA`(对话) / `Embedding` / `Rerank` / `VLLM` / `ASR` |
| `source` | `local`(Ollama) / `remote` |
| `provider` | `openai / aliyun / zhipu / gemini / deepseek / openrouter / siliconflow / jina / generic`(自定义 OpenAI 兼容) 等 30 种，决定构造器+默认 URL |
| `parameters`(JSONB) | `base_url`、`api_key`、`embedding_parameters{dimension}`、`extra_config`、`custom_headers`、`max_concurrency` |
| `is_default` | 按 (租户, 类型) 唯一——每类一个默认模型 |
| `is_builtin` / `managed_by` | 内置模型由 `config/builtin_models.yaml` 声明，启动时 UPSERT 同步（`managed_by='yaml'` 行被覆盖，用户改过的行保留），删条目才软删，`${ENV}` 插值 |

### 2.2 凭证安全

- `api_key`/`app_secret` **AES-GCM 加密落库**（`SYSTEM_AES_KEY`），GORM Value/Scan 钩子自动加解密，解密失败降级空串不报错。
- 列表接口 `apiKey` 永远返回空串，前端用 `credentials` 元数据渲染"已配置/未配置"状态。
- 凭证走**独立子资源** `PUT/DELETE /models/:id/credentials`：`PUT /models` 更新模型时 api_key 永不覆盖已存凭证，避免"改个名字把 key 冲掉"。

### 2.3 前端：设置→模型页

- 按类型分 Tab 的卡片列表：类型徽章 + 名称 + 默认标签 + 内置锁标 + 厂商副标题（嵌入显示维度、对话显示 vision 图标）；操作 = 编辑 / 复制 / 删除（popconfirm）。
- 编辑弹窗流程：**选类型 → 选厂商（下拉带默认 URL 预填，优先 API `/models/providers`，后备硬编码）→ 填 base_url + api_key + 模型名 + 嵌入维度 → 保存前"连通性测试"**。
- 连通性测试 = `POST /models/:id/debug`：用**生产同款构造器**对已存模型实测——对话走 ChatStream、嵌入走 Embed 并回传实际维度、重排走 Rerank。
- 删除前做**引用检查**：被知识库/Agent/长期记忆引用则 400 拒绝（"model is used by N knowledge base(s)"）。
- 增删改收归 SystemAdmin，非管理员只读+可选用。

### 2.4 运行时：现场构造，无池化

`GetChatModel/GetEmbeddingModel/GetRerankModel(modelId)` 按 ID 取行 → 用最新 Parameters **现场构造客户端**，无连接池——改完凭证、换默认，下次调用自动生效，不用重启、不用缓存失效。这是"解除耦合"的关键：**调用点只认 modelId，不认 yaml 平台名**。

### 2.5 可移植要点（对 smart-help 的裁剪）

| 智枢机制 | smart-help 移植 | 裁剪理由 |
|---|---|---|
| models 表 | 同构移植（去 tenant_id、去 VLLM/ASR） | 单租户 |
| AES-GCM 凭证加密 | 移植（Fernet/AES-GCM，密钥走 env） | 直接复用 |
| 凭证独立子资源 | 移植 | 直接复用 |
| `/models/:id/debug` 实测 | 移植（生产同款 `_llm_answer`/`_embed`/`rerank_scores`） | 直接复用 |
| 30+ 厂商构造器 | **只留 2 个 provider：`openai`(OpenAI 兼容 generic) + `dashscope_rerank`(DashScope 原生重排)** | smart-help 现状只有 OpenAI 兼容 + DashScope 原生 rerank 两种调用形态，不铺厂商 |
| Ollama 本地源+下载态 | 不做（local 作为 base_url 指向 127.0.0.1 的 remote 处理） | 本地栈由 deploy 编排，不走 Ollama |
| builtin YAML 调和 | **不做**，改为一次性 seed 脚本（yaml 平台→models 表） | smart-help 无"内置模型随版本更新"需求 |
| KB/Agent 绑定+引用检查 | 简化为"默认模型不可删"（会话 selected_model 按名称失效重选，已有机制） | 无 KB/Agent 概念 |

---

## 3. smart-help 现状耦合盘点

### 3.1 配置源与调用链全景

```
config/model_settings.yaml
  MODEL_PLATFORMS: [bailian, local-qwen3-emb, 本地Qwen3.6-35B@8081, 本地Qwen3.6-MoE@8082]
  DEFAULT_EMBED_PLATFORM: bailian
        │
        ├── 对话链：
        │   app_config.qa_llm_platform（Kingbase，30s 缓存）
        │     → kb_rag.get_current_llm_platform() (src/kb_rag.py:95)
        │     → recognize.load_model_cfg(platform) (src/recognize.py:96)  # 读 yaml 平台 profile
        │     → conversations.model_options() (src/conversations.py:30)  # 前端下拉选项=直接读 yaml llm_models
        │     → POST /api/model/switch → Conversation.selected_model（会话级）
        │     → conversations.selected_config() → MODEL_CONFIG ContextVar (src/conversations.py:15,460)
        │     → kb_rag._cfg() (src/kb_rag.py:121) → _llm_answer/_llm_stream (src/kb_rag.py:429,488)
        │         → base_url + '/chat/completions'，Bearer api_key，OpenAI 兼容
        │   另：recognize.llm_extract (src/recognize.py:166) 政策字段抽取，同 cfg，json_object 模式
        │
        ├── 嵌入链：
        │   app_config.qa_embed_platform（默认 yaml DEFAULT_EMBED_PLATFORM，30s 缓存）
        │     → model_registry.embed_cfg() (src/model_registry.py)
        │     → kb_rag._embed() (src/kb_rag.py:157) → base_url + '/embeddings'，带 dimensions 参数
        │     → kb_rag._embed_queries() (src/kb_rag.py:347) 查询侧加 Instruct 前缀（Qwen3 协议）
        │     → 向量落 Kingbase policy_vector_qwen3 表（BYTEA float32），行带 embed_model 列
        │
        └── 重排链：
            model_registry.rerank_cfg() → 平台 profile.rerank_endpoint + rerank_model
              → recognize.rerank_scores() (src/recognize.py:218)
                  → 端点空则走 DashScope 原生 /api/v1/services/rerank/text-rerank/text-rerank
                  → 请求体 {model, input:{query, documents}}，响应 output.results[].relevance_score（DashScope 格式）
                  → 本地栈靠 serve_qwen3_rerank.py 兼容层伪装成同一格式（8096 /v1/rerank）
              → kb_rag.preflight_local_models() (src/kb_rag.py:191) 问答前本地 rerank 健康自检
```

### 3.2 耦合点清单（按深度排序）

| # | 耦合点 | 位置 | 深度 | 说明 |
|---|---|---|---|---|
| C1 | 嵌入模型被向量索引锁死 | `src/kb_rag.py:142-144`（表名白名单）、`model_registry.index_dim/set_index_dim`（app_config 全局锁）、`kb_rag._load_vectors():304-337`（单一 `_VEC_CACHE` 矩阵 + 维度不符即 ValueError） | **最深** | 换嵌入模型维度不符 → 索引锁死，必须跑 `src/reindex_vectors_2560.py` 一次性全量重建（硬编码 EMBED_URL/MODEL/DIM 三个常量） |
| C2 | 三类模型全部 yaml 平台捆绑 | `config/model_settings.yaml` + `recognize.load_model_cfg():96-120`（平台名→profile dict） | 深 | 加一个模型要改文件+重启；"平台"是端点+模型的捆绑，无法单换模型 |
| C3 | 前端下拉直读 yaml | `conversations.model_options():30-37` | 中 | 用户只能选 yaml 里预置的 llm_models，无自定义入口 |
| C4 | 重排响应格式绑死 DashScope | `recognize.rerank_scores():218-246` | 中 | 只认 `output.results[].relevance_score`；接 Jina/Cohere 等通用 rerank 要改调用代码 |
| C5 | 调用点散落、无统一构造入口 | `kb_rag._embed/_llm_answer/_llm_stream`、`recognize.llm_extract/rerank_scores`、`reindex_vectors_2560._embed_batch` | 中 | 五处各自拼 base_url+key+model，无"按模型行构造客户端"的统一层 |
| C6 | 无连通性测试 | 全局 | 中 | 接错 key/端点要到问答时才报错（503 ApiError），无保存前验证 |
| C7 | api_key 明文落 yaml/可进 git | `config/model_settings.yaml:52`（sk-ws-… 明文） | 安全 | 无加密、无凭证子资源 |
| C8 | 平台切换 30s 缓存 | `model_registry.py`（qa_llm_platform/qa_embed_platform 各 30s 进程内缓存） | 浅 | 多 worker 下切换不即时，可接受但需在方案中说明 |

### 3.3 现状中**可保留**的资产（方案不推倒重来）

- **`embed_model` 行级标记**：`policy_vector_qwen3` 表每行带 `embed_model` 列，`_load_vectors()` 查询时已按 `v.embed_model=:model` 过滤（`kb_rag.py:322`）——多模型共存的行级基础已在库里，缺的只是运行时单缓存锁。
- **会话级 selected_model 失效机制**：`conversations.py:57-60` 已处理"原会话模型已移除，请重新选择"——注册表删除模型时直接复用。
- **`preflight_local_models` 问答前健康自检**（不降级原则）——保留，改为对注册表默认嵌入模型做。
- **`_robust_cutoff` 分布鲁棒截断**（`kb_rag.py:355-365`）：阈值按相似度分布自适应，不绑死模型——换嵌入模型不用调阈值。
- **`deploy/foreground.py` 本地栈编排**：不动，本地模型只是注册表里 base_url 指向 127.0.0.1 的普通行。
- **`app_config` 键值表**：默认模型指针、索引元数据继续用它，不新建元数据表。

---

## 4. 目标设计

### 4.1 数据模型：`models` 表（Kingbase DDL）

```sql
CREATE TABLE IF NOT EXISTS models (
    id            VARCHAR(64) PRIMARY KEY,              -- 短 id，如 'm-chat-001'，前端引用
    type          VARCHAR(16) NOT NULL,                 -- chat | embed | rerank
    name          VARCHAR(100) NOT NULL,                -- 展示名（前端下拉显示）
    source        VARCHAR(16) NOT NULL DEFAULT 'remote',-- remote（含本地 llama，base_url 指 127.0.0.1）
    provider      VARCHAR(32) NOT NULL DEFAULT 'openai',-- openai（OpenAI 兼容）| dashscope_rerank（DashScope 原生重排）
    base_url      TEXT NOT NULL,                        -- 端点根（openai: .../v1；dashscope_rerank: 完整 rerank URL）
    api_key_enc   TEXT,                                 -- Fernet 加密后的凭证（nullable，本地 EMPTY 不存）
    model_name    TEXT NOT NULL,                        -- 模型标识（如 qwen-plus / qwen3-embedding-4b）
    dim           INTEGER,                              -- 嵌入维度（type=embed 必填，128–4096）
    query_instruction TEXT,                             -- 查询侧 Instruct 前缀（Qwen3 协议，可空）
    json_mode     BOOLEAN,                              -- 对话 json_object 支持标记（本地 llama 用）
    is_default    BOOLEAN NOT NULL DEFAULT FALSE,       -- 按 type 唯一默认
    created_at    TIMESTAMP DEFAULT now(),
    updated_at    TIMESTAMP DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_models_default ON models(type) WHERE is_default;
```

**语义**：
- **默认模型 = 每类型一行 `is_default=TRUE`**（部分唯一索引保证）。问答链不再读 `qa_llm_platform`/`qa_embed_platform` 平台名，改读 `models` 表按 type 取默认行（保留 30s 缓存，说明见 §4.8-C8）。
- **会话级选择**：`Conversation.selected_model` 继续存，但值从"平台名/模型名拼接"改为 **models.id**；模型被删后走既有"已移除请重选"路径。
- **凭证**：`api_key_enc` 只写不读回显（列表/详情接口永远不回传，前端显示"已配置/未配置"）。

### 4.2 凭证加密

- 采用 **Fernet**（cryptography 库，AES-128-CBC+HMAC，smart-help 已装——`deploy` 依赖链含 cryptography；若未装则降级为 AES-GCM 手写，密钥同样来源）。
- 密钥来源：环境变量 `MODEL_CRED_KEY`（base64 Fernet key），`deploy/web.env` 注入；未设置时从 `Kingbase 实例级 app_config` 派生一个稳定 key 并告警（保证重启不丢凭证，同时提示运维显式配置）。
- 加解密封装在 `src/model_creds.py`（新文件，~60 行）：`encrypt(plain) -> str`、`decrypt(enc) -> str`、`is_configured(enc) -> bool`；解密失败降级空串+日志告警（对齐智枢行为）。

### 4.3 Provider 抽象层：`src/model_providers.py`（新文件）

**核心原则（移植智枢 2.4）：调用点只认 model 行，现场构造，无池化。**

```python
def build_chat_model(row)      -> ChatEndpoint   # .complete(messages, temperature, response_schema, timeout) / .stream(...)
def build_embed_model(row)     -> EmbedEndpoint  # .embed(texts, timeout) -> list[list[float]]（含 dim 校验）
def build_rerank_model(row)    -> RerankEndpoint # .rerank(query, documents, timeout) -> list[float] | None
```

两个 provider 实现：

| provider | chat | embed | rerank | 说明 |
|---|---|---|---|---|
| `openai` | `base_url/chat/completions`，Bearer，支持 `response_format:{json_object}` + 本地 json_mode 兜底（`kb_rag.py:448` 现有逻辑内移） | `base_url/embeddings`，带 `dimensions` 参数（现有 `_embed` 逻辑内移） | `base_url/rerank`（OpenAI/Jina 兼容格式 `{query, documents}` → `results[].relevance_score`） | smart-help 现有一切远端/本地 OpenAI 兼容端点都归此类 |
| `dashscope_rerank` | — | — | DashScope 原生 URL，请求体 `{model, input:{query, documents}}`，响应 `output.results[].relevance_score`（现有 `recognize.rerank_scores` 逻辑内移） | 现状 DashScope 重排走的形态，保留 |

**现有五处散落调用点全部改为经由本层**：
- `kb_rag._embed()` → `build_embed_model(embed_row).embed()`
- `kb_rag._llm_answer/_llm_stream()` → `build_chat_model(chat_row)`
- `recognize.llm_extract()` → `build_chat_model(chat_row)`（json_mode）
- `recognize.rerank_scores()` → `build_rerank_model(rerank_row)`
- `reindex_vectors_2560._embed_batch()` → `build_embed_model(row)`（reindex 脚本参数化，见 §4.5）

**模型行→调用 cfg 的映射**：`kb_rag._cfg()`/`_embed_cfg()`/`rerank_cfg()` 保留函数名（调用方不动），内部实现改为从注册表取默认行并构造 endpoint。`MODEL_CONFIG` ContextVar 语义不变，值从"yaml cfg dict"变为"chat model row"。

### 4.4 运行时切换与缓存

- 默认模型指针不再用 `qa_llm_platform`/`qa_embed_platform` 平台名，改由 `models.is_default` 表达。
- 进程内 30s 缓存保留（对齐 C8 现状，多 worker 下切换延迟 ≤30s，与今天一致）；缓存键改为 type。
- **凭证更新即时生效**：凭证子资源 PUT 后主动失效本进程缓存（进程内事件，跨 worker 仍走 30s，与现状同级别）。
- `preflight_local_models` 保留，改为对**默认 embed 行**（若 base_url 是本机）做健康自检。

### 4.5 嵌入侧深度耦合：多模型共存策略（本方案核心）

**现状**：行级 `embed_model` 标记已存在（`kb_rag.py:322` 已按模型过滤），但运行时是单一 `_VEC_CACHE` 矩阵 + `index_dim()` 全局锁 → 换模型即锁死。

**目标**：**一张向量表、多个嵌入模型共存，切换默认即回滚**。

改动点：
1. **`_VEC_CACHE` 改为按模型 id 分键的缓存字典**：`{_CACHE[model_id]: {mat, codes, texts, metas, dim}}`。`_load_vectors(model_row)` 按 `v.embed_model=:model_name` 过滤（现有 SQL 已支持），各自建矩阵。LRU 上限 4 个模型（防内存膨胀），超出淘汰最久未用。
2. **废除 `index_dim()` 全局锁**：改为**按模型**的维度校验——`_load_vectors` 内对过滤出的行做 `vecs.shape[1] != row.dim` 即报错"该模型的索引未重建"（现有 `kb_rag.py:330` 逻辑，从全局锁降级为行级自检）。
3. **新嵌入模型接入流程**（替代"锁死+一次性脚本"）：
   - 前端添加 embed 模型行（base_url/key/model_name/dim）→ 连通性测试（实测返回维度，与填的 dim 不符则提示修正）→ 设为默认。
   - 此时新模型的 `embed_model` 行**不存在**，检索会命中 0 行（`_load_vectors` 过滤后为空 → 现有空缓存路径，返回空结果不报错）。
   - 管理页对该模型显示"索引未建"状态 + "重建索引"按钮 → 调 `POST /api/admin/models/:id/reindex`（后台任务，复用 reindex 逻辑）。
   - reindex 完成 → 新模型行级索引就绪，默认已切过去，检索自动走新模型；**旧模型的向量行原样保留**，切回旧模型即刻恢复（这就是共存+回滚）。
4. **reindex 脚本泛化**：`src/reindex_vectors_2560.py` 的三个硬编码常量（EMBED_URL/EMBED_MODEL/EMBED_DIM）改为**从 models 表目标行读取**，表名不再硬编码（去掉 `policy_vector_qwen3` 白名单，统一写当前表，靠 `embed_model` 行级标记区分）。断点续跑游标 `app_config.reindex_cursor_2560` 改为按模型 id 命名。
5. **写入侧**：`sync_policy` 入库新政策时，只对**当前默认 embed 模型**生成向量（行标记 `embed_model=默认模型名`）——与现状一致，不扩散写放大。

**为什么不做"一模型一表"**：表名扩散（`policy_vector_<model>`）会把 C1 的表名白名单耦合变成 N 份，`_load_vectors`/`api.py:875-880` 的统计 SQL 全部要改；行级标记+按模型缓存是现有 schema 的自然延伸，改动面最小。

### 4.6 API 设计（全部 `@admin_required`，复用 `src/authentication.py:90` 现有装饰器）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/admin/models?type=chat` | 模型列表；`api_key` 恒回 `''`，附 `api_key_configured: bool` |
| GET | `/api/admin/models/providers` | 返回支持的 provider 清单 + 默认 URL 预填（openai/dashscope_rerank） |
| POST | `/api/admin/models` | 创建（name/type/source/provider/base_url/api_key/model_name/dim/query_instruction/json_mode/is_default）；SSRF 校验 base_url；embed 必填 dim 128–4096 |
| PUT | `/api/admin/models/:id` | 更新；**api_key 永不在此覆盖**（对齐智枢，防改名冲掉凭证） |
| PUT | `/api/admin/models/:id/credentials` | 凭证子资源：`{api_key?}` 设置/更换；不带字段则返回 `{api_key:{configured}}` 元数据 |
| DELETE | `/api/admin/models/:id/credentials` | 清空凭证 |
| DELETE | `/api/admin/models/:id` | 删除；**默认模型拒删**（先切默认）；被 `Conversation.selected_model` 引用的走既有"已移除请重选" |
| POST | `/api/admin/models/:id/test` | **连通性测试**（对齐智枢 `/models/:id/debug`）：chat→发一句实测对话（含 json_mode 校验）；embed→实测 1 条文本回传实际维度；rerank→实测 1 组 query+documents。返回 `{ok, latency_ms, detail}` |
| POST | `/api/admin/models/:id/default` | 设为该类型默认（部分唯一索引原子切换） |
| POST | `/api/admin/models/:id/reindex` | 嵌入模型索引重建（后台任务，返回任务 id；进度走 `app_config` 游标，管理页轮询） |
| GET | `/api/admin/models/index-status` | 各 embed 模型的索引状态（行数/维度/是否已建/重建进度） |

**问答页对话下拉**（`/api/model/current`、`/api/model/switch`，`conversations.py:55-79`）：选项来源从 `model_options()` 读 yaml 改为读 `models WHERE type='chat'`，值改为 models.id；`selected_config()` 改为按 id 取行构造。接口形状不变，ask.html 改动极小。

### 4.7 前端设计

**A. 管理页新增"模型管理"卡片**（`src/static/admin.html`，现有 search-card/lifecycle-card/import-card 同款卡片，Vue CDN 单页）：
- 三个 Tab：对话 / 嵌入 / 重排（各带计数）。
- 模型卡片：类型徽章 + 名称 + 默认标签 + provider 副标题（embed 显示维度、索引状态"已建/未建/重建中 N%"）；操作 = 编辑 / 测试连通性 / 设默认 / 删除（popconfirm）。
- 编辑弹窗：选类型 → 选 provider（下拉，openai 带默认 URL 预填）→ base_url + api_key + 模型名 + dim（embed）+ Instruct 前缀（embed，可选）→ **保存前"测试连通"按钮**（调 `/test`，成功才允许保存）→ 保存。
- api_key 字段：创建时可填；编辑时显示"已配置（点击更换）"，走 `/credentials` 子资源。
- embed 卡片额外显示索引状态与"重建索引"按钮（调 `/reindex`，轮询 `/index-status`）。

**B. 问答页**（`src/static/ask.html:488` 模型下拉）：无结构改动，数据源换后自动显示注册表的对话模型列表。

### 4.8 开关矩阵与迁移（对齐项目"默认行为不变"铁律）

**功能开关**（`app_config` 或 env，默认全 0，灰度期行为与现状完全一致）：

| 开关 | 默认 | 作用 |
|---|---|---|
| `MODEL_REGISTRY` | 0 | 总开关。0 = 问答链全走 yaml 平台（现状路径原样保留）；1 = 走注册表 |
| （无 P1/P2/P3 独立开关，按 type 灰度：先只 seed chat 行，embed/rerank 行不 seed 时该类型自动回落 yaml） | | |

**迁移步骤（一次性 seed）**：
1. `deploy/seed_models.py`（新脚本）：读 `config/model_settings.yaml`，把 4 个平台映射成 models 行——bailian→1 chat(qwen-plus)+1 embed(text-embedding-v4,1536)+1 rerank(dashscope_rerank)；local-qwen3-emb→1 embed(qwen3-embedding-4b,2560)+1 rerank(openai,8096)；本地 8081/8082→2 chat。默认标记对齐现状：chat 默认=bailian 的 qwen-plus（对齐 `qa_llm_platform` 默认）、embed 默认=local-qwen3-emb（对齐 `DEFAULT_EMBED_PLATFORM` 当前 web.env 覆盖值）。
2. 现有向量行的 `embed_model` 列已是 `qwen3-embedding-4b`，seed 后与新行 model_name 一致，**无需数据迁移**。
3. `MODEL_REGISTRY=1` 灰度验证（对照问答结果+检索召回）→ 默认置 1 → 下个版本移除 yaml 回落路径。

**C8 缓存说明**：注册表默认行查询保留 30s 进程缓存（与现状平台切换延迟一致，不劣化）；凭证子资源更新主动失效本进程缓存。

---

## 5. 安全设计

| 项 | 措施 | 依据 |
|---|---|---|
| 凭证存储 | Fernet 加密落库，密钥 `MODEL_CRED_KEY`（env），解密失败降级空串+告警 | 智枢 AES-GCM 同构；现状 yaml 明文 key 是已知隐患（C7） |
| 凭证回显 | 所有 GET 接口 `api_key` 恒 `''`，只回 `configured` 布尔 | 智枢 DTO 剥离 |
| 凭证覆盖 | `PUT /models/:id` 永不覆盖已存凭证，只走 `/credentials` 子资源 | 智枢同构 |
| SSRF | `base_url` 创建/更新时校验：仅 http/https、拒绝 `file://` 等、主机名非空；**不封私网**（本地 llama 127.0.0.1 是合法用途），但日志记录所有指向私网的新增 | 智枢 ValidateURLForSSRF 裁剪（smart-help 本地栈合法） |
| 权限 | 全部管理端点 `@admin_required`；问答页普通用户只能"选"对话模型，不能增删改 | 现状 `authentication.py:90` |
| 审计 | 模型增删改/凭证变更/设默认/重建索引 → 写 `app_config` 审计键或新表 `model_audit_log`（id/actor/action/model_id/at），管理页可查 | 对齐项目生命周期审计惯例 |

---

## 6. 验收标准与测试

**单元测试**（对齐项目 904 例全绿的惯例，新增进 `tests/`）：
- `model_creds`：加密/解密往返、错误密钥降级空串、`is_configured` 判定。
- `model_providers`：两个 provider 的 endpoint 构造（mock requests）——openai chat/embed/rerank 请求体与响应解析、dashscope_rerank 请求体格式、dim 不符报错、rerank 失败返回 None（降级 difflib 路径）。
- `models` API：默认唯一约束（同类型第二默认被拒）、删除默认被拒、凭证子资源不覆盖、SSRF 拒绝 file://、embed 缺 dim 被拒、列表不回显 key。
- `_VEC_CACHE` 多模型分键：两个 embed 模型交替检索互不污染、LRU 淘汰、行级维度自检报错文案。
- 回落路径：`MODEL_REGISTRY=0` 时所有调用走 yaml（现有 904 例回归全绿，零行为变化）。

**集成/手工验收**：
- 前端加一个 OpenAI 兼容对话模型（用现有 bailian key 换个模型名）→ 测试连通成功 → 设默认 → 问答页下拉出现新模型 → 提问走新模型（trace 里 model.meta 事件验证，`kb_rag.py:478`）。
- 前端加一个**不同维度**的嵌入模型 → 测试连通回传真实维度 → 设默认 → 管理页显示"索引未建" → 点重建 → 轮询进度 → 完成 → 检索召回对照（同一批问题，新旧模型 top-k 重合度报告）→ 切回旧模型立即恢复（共存回滚验证）。
- 删默认模型被拒；删非默认模型后旧会话提示"已移除请重选"。

**性能基线**：注册表查询 30s 缓存命中时零额外 DB 往返；向量缓存按模型分键后内存 = Σ(活跃模型向量矩阵)，LRU=4 封顶，对照现状单矩阵无退化。

---

## 7. 风险与开放问题

| # | 风险/问题 | 处置 |
|---|---|---|
| R1 | **换嵌入模型=换检索语义**：不同模型相似度分布不同，`_robust_cutoff` 自适应（median+δ）能吸收大部分，但 top-k 召回集合会变 | 重建索引后必须跑召回对照（验收标准已列）；管理页保留旧模型行=随时可回滚 |
| R2 | 多模型向量共存 → 表行数增长（每模型一份全量向量） | 当前政策切片量级（数千行）下 BYTEA 2560 维约 10KB/行，单模型全量 <100MB，可接受；提供"归档旧模型向量"管理操作（P3 后按需） |
| R3 | Fernet 密钥丢失=全部凭证不可解 | 密钥未配置时从 app_config 派生稳定 key（§4.2），并显式告警；seed 脚本 dry-run 打印将加密的 key 数 |
| R4 | `reindex` 后台任务与在线问答并发写同一表 | 现状 reindex 脚本已是"新行插入+状态位"非破坏式（`reindex_vectors_2560.py` 设计），沿用；重建期间该模型检索走已有行（可能不全），管理页提示 |
| R5 | 30s 缓存多 worker 不即时（现状同款，C8） | 不劣化即接受；凭证子资源已做本进程主动失效 |
| R6 | yaml 回落路径长期存在=双轨维护 | 灰度验证后一个版本内移除 `MODEL_REGISTRY=0` 路径，yaml 降级为纯 seed 源 |
| 开放 | 是否需要"每会话选嵌入模型"？ | 不需要——嵌入模型绑定向量索引，会话级切换无意义（切了索引不跟）。只对话模型是会话级的，与现状一致 |
| 开放 | 重排模型是否也要"索引未建"概念？ | 不要——rerank 无状态，连通性测试通过即可用 |

---

## 8. 工作分解（实施顺序，每步独立回退）

| 阶段 | 内容 | 新增/改动 | 回退 |
|---|---|---|---|
| **P1 对话模型注册表** | models 表 DDL + `model_creds.py` + `model_providers.py`(chat) + 管理端 API(chat) + admin.html 模型卡片(chat Tab) + ask.html 下拉换源 + seed 脚本(chat 行) + `MODEL_REGISTRY` 开关 | 新 3 文件 + admin.html 加卡片 + conversations.py 下拉换源；kb_rag 对话链加 registry 分支 | `MODEL_REGISTRY=0` 全回落 yaml |
| **P2 嵌入模型+索引共存** | providers(embed) + `_VEC_CACHE` 按模型分键 + 废除 index_dim 全局锁 + reindex 泛化（参数化+API 化+后台任务）+ 管理页 embed Tab（索引状态/重建）+ seed(embed 行) | kb_rag.py 向量链改造（核心）+ reindex 脚本参数化 | 开关回落 + 向量行不动（只增不改） |
| **P3 重排模型+收口** | providers(rerank, dashscope_rerank) + 管理页 rerank Tab + seed(rerank 行) + 移除 yaml 回落路径 + 审计日志页 | recognize.py 重排链换源 | 重排失败本就降级 difflib（现状兜底保留） |

**预估**：P1 约 1.5–2 天（表+API+前端+seed+回归），P2 约 2–3 天（向量链改造是主体，含召回对照验收），P3 约 1 天。合计 4.5–6 个工作日，每阶段结束 904 例回归全绿为门禁。

---

## 9. 与既有设计文档的关系

- `05-zhishu-reference.md`：已分析智枢的**智能体运行时+事件流**可移植点；本文补充其**模型管理**机制的可移植点（两者同源不同面）。
- `27-admin-console.md`：admin.html 卡片式管理页的既有约定，本文"模型管理"卡片按同款实现。
- `26-switch-rollout-runbook.md`：开关灰度 runbook 惯例，本文 `MODEL_REGISTRY` 开关按同款管理。
