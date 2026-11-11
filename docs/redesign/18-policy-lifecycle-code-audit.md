# 18 政策生命周期管理后端能力与耦合审计（2026-11-08）

> 范围：政策入库（单条/批量/文件）、更新、停用、版本、草稿、发布、回滚、向量重建、规则编译触发、图谱同步，以及"删除旧政策 + 导入新政策 + 内部智能自动管理"三项需求的现状差距。
> 方法：只读代码（read/grep）+ Kingbase 只读 SELECT + 生产进程/日志检查。不写库、不启停服务、不 commit。数据快照时间 2026-11-08 09:00–09:20。
> 基线：git tag baseline-20261106（commit 0d20ef1）；生产库 policy_db_mig20261021、认证库 smart_help_auth_mig20261021。

## 1. 一句话结论

1. **系统没有"删除政策"能力，只有"停用"**：src/api.py 全部路由只有 GET/POST，无 DELETE/PUT/PATCH（grep "methods=.*(DELETE|PUT|PATCH)" 为空）；全仓 DELETE 语句只有 policy_detail（policy_ingest.py:91）、旧向量表 policy_vector（kb_rag.py:274/293；restore_vector_backup.py:38）、kg_edge（kg_rebuild.py:277）；政策主表、policy_vector_qwen3、policy_rule 均无删除/级联逻辑。
2. **生产向量链路与管理后台断裂（最致命）**：管理后台的同步/删除/备份全部硬编码旧表 policy_vector（kb_rag.py:274/277、:293、vector_safety.py:91），生产检索却读 policy_vector_qwen3（kb_rag.py:142/147，_load_vectors:322）；唯一写 qwen3 的 reindex_vectors_2560.py 只能人工 CLI 跑（:22-27、:69-115）。因此"导入新政策/更新/停用"在生产检索里不生效，直到人工重建。
3. **KG 图谱发布 worker 在生产配置下永不启动**：src/app.py:75 的条件是 KG_GRAPH_ENABLED and not showcase_enabled() and not full_enabled()；生产 POLICY_MATCH_MODE=full_model（deploy/web.env:1）使 full_enabled()=True，条件恒 False。mark_pending 只写队列，队列里 2 条 ready 记录自 2026-10-12 起无人消费；图谱基线（kg_node.POLICY=3333）停在 10-12 的台账状态。
4. **规则编译与生命周期脱钩**：rule_worker 只在"某县本地规则 < 20"时补编（rule_worker.py:25-41），没有管理操作触发的重编入口；rule_store.load_rules（rule_store.py:131-137）不 join policy.status/cur_version，政策停用后旧规则仍可装载（当前恰好无残留，属数据巧合而非约束）。
5. **版本机制只写不读的半成品**：更新时写 PolicyVersion 快照（api.py:289-293），但全仓无读取/回滚/发布代码（grep PolicyVersion 仅 api.py:10/289/328 引用）；新增政策 v1 快照为空字符串（api.py:328-331），历史不可恢复；无 publish/rollback 路由。
6. **没有智能自动管理**：无定时过期扫描、无替代关系（superseded_by）、无变更影响分析、无入库质量门/去重；29 条待确认草稿从 2026-10-08 滞留至今（policy_draft 待确认 29），10-12 之后 audit_log 无任何管理动作。

## 2. 现状架构

```mermaid
flowchart TD
    subgraph 写入侧（管理后台 /api）
      A[上传 pdf/xlsx/csv/docx<br/>api.py:345-407] --> B{识别}
      B -->|xlsx/csv| C[table_import.recognize_many<br/>table_import.py:56-70]
      B -->|pdf/docx| D[recognize.recognize_pdf<br/>recognize.py:355-400]
      C --> E[PolicyDraft 草稿<br/>models.py:76-99]
      D --> E
      E -->|待人工确认| F[人工在 /drafts 确认<br/>api.py:474-519]
      E -->|文号命中/新增/相似度命中| G[_apply_draft_to_ledger<br/>api.py:228-342]
      F --> G
      G -->|新增| H[(policy + policy_version v1<br/>policy_detail 全删重插)]
      G -->|更新| I[(policy 版本+1<br/>PolicyVersion 旧版快照)]
      G -->|停用| J[(policy.status=停用)]
      H --> K[commit 后 _kb_sync_upsert<br/>api.py:523-545]
      I --> K
      J --> L[commit 后 remove_policy<br/>api.py:265-275]
      K --> M[kb_rag.sync_policy<br/>只写旧表 policy_vector]
      L --> M
      K --> N[kg_update_queue pending<br/>graph_update_state.mark_pending]
      L --> N
      N -.->|worker 生产不启动| O[kg_source_update.process_pending<br/>永不消费]
      H --> P[rule_worker 懒编译<br/>仅当该县规则<20]
    end
    subgraph 读取侧（问答/预览）
      Q[full_model 检索<br/>_load_vectors] -->|读| R[(policy_vector_qwen3)]
      S[规则引擎 rule_eval] -->|读| T[(policy_rule, 不过滤政策状态)]
      U[政策预览 preview] -->|读| R
      V[policy-health] -->|读| W[(旧表 policy_vector)]
    end
    M -. 生产检索看不到 .-> R
```

关键代码位置：

| 环节 | 位置 | 行为 |
|---|---|---|
| 上传入口 | src/api.py:345-407 | 接收 pdf/xlsx/csv/docx，逐文件识别、逐条建草稿、可立即自动落库 |
| 草稿确认 | src/api.py:474-519 | 人工选 update/new，approve 调 _apply_draft_to_ledger |
| 落库核心 | src/api.py:228-342 | 新增/更新/停用三分支；更新写版本快照；停用只改 status |
| 明细写入 | src/policy_ingest.py:82-103 | 先 mark_pending，再 DELETE policy_detail 全量重插 |
| 知识库同步 | src/api.py:523-545 → src/kb_rag.py:225-284 | 写旧表 policy_vector，版本/状态二次校验后硬删重插 |
| 向量重建 | src/reindex_vectors_2560.py:69-115 | 人工 CLI，源旧表→目标 qwen3，按 id 覆盖 |
| 规则编译 | src/compile_rules.py:181-205 | ThreadPool 编译后 save 到 SQLite + 金仓 policy_rule |
| 规则装载 | src/policy_core/rule_store.py:131-137 | 只按 region_scope 过滤，不 join 政策状态/版本 |
| 图谱队列 | src/graph_update_state.py:5-18 | 写 kg_update_queue(pending)，ON CONFLICT 覆盖 |
| 图谱发布 | src/kg_source_update.py:319-387 | 单发布者锁 + 逐 job 重建；worker 由 app.py:75 条件控制 |
| 缓存签名 | src/smart_matcher_kb.py:157-182 | 只覆盖 policy/policy_detail/rule_mapping/rule_check，不含向量/规则/图谱表 |

## 3. 现状能力矩阵

图例：有 = 可用且有后端入口；半 = 有代码但链路不完整/仅部分生效；无 = 无入口/无实现。

| 能力 | 结论 | 代码位置 | 行为与限制 |
|---|---|---|---|
| 导入-单条（文件） | 有 | api.py:345-407；recognize.py:355-400 | pdf/docx 走 recognize_pdf；必须存在显式"政策编号："行，否则按新增处理 |
| 导入-批量（表格） | 有 | api.py:365-367；table_import.py:6-70 | xlsx/csv 一次 ≤500 明细、≤50 政策；同编号不同名直接报错 |
| 导入-新增 | 有 | api.py:320-342 | gen_policy_code 生成编号，cur_version=v1，PolicyVersion 快照为空 |
| 导入-更新（同编号） | 有 | api.py:281-318 | 必须与 match_policy_id 一致；版本+1；旧版写 PolicyVersion 快照 |
| 导入-幂等/去重 | 无 | — | 无唯一约束（information_schema 无 FK/UNIQUE）；重复上传同名/同文号无拒绝，仅靠识别 match_type 提示 |
| 更新 | 有 | api.py:281-318；policy_ingest.py:82-103 | 明细全删重插；字段来自草稿可编辑集 api.py:455-456 |
| 删除-硬删除 | 无 | — | 无 DELETE 路由、无 purge 脚本、无回收站；policy 表 3333 在用/4 停用，无删除记录（audit_log 无删除动作） |
| 删除-软删除（停用） | 半 | api.py:251-279 | 只有草稿 status=停用 + 命中已有编号时才走；停用不清理明细/向量/规则/图谱；无恢复接口 |
| 停用后恢复 | 无 | — | 无 restore 路由；只能再走一次更新把 status 改回"在用" |
| 版本留档 | 半 | api.py:289-293；models.py:62-73 | 更新时快照旧 policy 列+全明细 JSON；新增 v1 快照为空；全仓无读取入口 |
| 版本回滚 | 无 | — | 无 API/UI；content_snapshot 只写不读 |
| 发布/审核流 | 无 | — | 确认即落库（api.py:392-396 自动确认），无多级审核、无定时生效 |
| 草稿 | 有 | models.py:76-99；api.py:429-519 | 状态 待确认/已确认/已驳回；29 条待确认自 10-08 滞留；无超时清理/提醒 |
| 向量重建（qwen3） | 半 | reindex_vectors_2560.py:22-115 | 仅人工 CLI；按源表 id 覆盖，不清理源已删除行；无 API/定时/增量队列 |
| 向量同步（自动） | 半 | api.py:523-545；kb_rag.py:225-284 | 新增/更新/停用确实会调用，但写的是旧表 policy_vector，生产检索读 qwen3 → 生产不生效 |
| 向量删除 | 半 | kb_rag.py:287-297 | 只删旧表；qwen3 中停用政策仍留 2 行（X-YS-040 detail 8736、X-JR-074 detail 9390） |
| 向量备份 | 半 | vector_safety.py:88-112 | 备份 SELECT * FROM policy_vector（旧表硬编码）；qwen3 无备份 |
| 规则编译触发 | 半 | rule_worker.py:25-71；compile_rules.py:181-205 | 仅"县本地规则<20"懒触发；无管理操作触发；无编译质量门/时延遥测 |
| 规则失效/清理 | 无 | rule_store.py:131-137 | load_rules 不过滤 policy.status/cur_version；无 DELETE policy_rule；停用/删除后旧规则仍可能装载 |
| KG 同步（请求） | 半 | graph_update_state.py:5-18；api.py:256-257/267/271/525-530 | 写 kg_update_queue pending；仅在有 worker 时消费 |
| KG 发布 worker | 无（生产） | app.py:73-77；foreground.py:94-102/:171/:174 | 生产 full_model 下 start_worker 不执行；foreground 也不启动；队列 2 条 ready 未消费 |
| 缓存失效 | 半 | api.py:265-275/523-530；smart_matcher_kb.py:241-244 | 失效在 commit 后 try 内，异常被吞成 kb_sync 失败；签名不含 policy_vector/policy_rule/kg_* |
| 审计日志 | 有 | api.py 多处；audit_log | 动作仅上传文档/新增/更新/停用/占位隔离/演示/初始导入；无删除、无发布、无回滚 |
| 导出 | 有 | api.py:186-224 | 全量导出含停用；导出前备份旧文件；无删除标记列 |
| 预览 | 有 | api.py:1003-1030；policy_preview.py:151-179 | 只按 code 查，不检查 status；停用政策仍可预览原文 |
| 健康检查 | 半 | api.py:870-902 | 统计读旧表 policy_vector；生产 qwen3 未索引状态不可见 |
| 问答历史生命周期联动 | 无 | qa_log 在认证库；qa_log 510 行 | 历史回答引用 384 个不同 policy_code、635 个 detail_id；停用/删除无失效标记、无版本快照 |

## 4. 关键流程与代码细节

### 4.1 上传→草稿→落库：确认即生产，无审核缓冲

- 文件先落盘 src/uploads（api.py:360-363），xlsx/csv 走 table_import.recognize_many，其余走 recognize.recognize_pdf（api.py:365-369）。
- 每个识别结果建 PolicyDraft（confirm_status=待确认），match_type ∈ {文号命中、替代关系命中、相似度命中、新增、待人工确认}；来源/置信度/候选 policy_id 一并记录（api.py:377-388）。
- **非"待人工确认"的草稿会被立即自动落库并标已确认**（api.py:389-396）；只有"待人工确认"留在草稿箱。即上传即生产，人只处理模型不确定的 28/43（DB 快照：待人工确认 28、新增 12、文号命中 2、相似度命中 1）。
- 逐文件、逐条 commit（api.py:397-401），无整批事务：一批 50 个政策时若第 30 条失败，前 29 条已落库且无批次号可回滚。
- 识别质量门：prepare 只校验占位名/明细非空/每条 conditions 或 target/地区兜底/日期顺序（policy_ingest.py:48-79）；不校验重复政策、不校验文号唯一、不比对已有同名家族。

### 4.2 更新分支：版本号只增、快照只写

- SELECT ... FOR UPDATE 锁政策行（api.py:283）；取旧 policy 列 + SELECT * FROM policy_detail 作为 old（api.py:283-285）。
- 写 PolicyVersion(version_no=旧版本号, content_snapshot=json(old), note=更新 vN→vN+1)（api.py:289-293）；policy.cur_version 改为 vN+1（api.py:286-288）。
- AuditLog('更新政策') + write_details（policy_ingest.py:82-103）：mark_pending → DELETE policy_detail WHERE policy_id → 逐行 INSERT（:91-103）。
- commit（api.py:315）之后才 _kb_sync_upsert（api.py:316）：知识库同步失败不影响台账，但也没有重试队列，只写 draft_data.index_status=failed（policy_ingest.py:106-113）。
- 版本快照只在"更新"发生；新增政策 v1 快照为空串（api.py:328-331），且无任何读取 API/UI，故该表目前是只写归档。

### 4.3 停用分支：不是删除，且提交早于外部同步

- 触发条件：草稿 status=停用 且 match_policy_id 非空（api.py:237）；否则按普通更新处理。
- 流程：FOR UPDATE → p.status='停用'、updated_by/updated_at → mark_pending → AuditLog('停用政策') → **先 commit（api.py:264）** → try 内做 4 件外部同步：invalidate_cache、invalidate_db_cache、request_update、kb_rag.remove_policy（api.py:265-275）→ save_sync。
- 问题：commit 已生效，任何一步外部同步异常都只被 except 吞掉并记为 kb_sync 失败；没有 outbox、没有重试、没有补偿任务。停用后明细仍留（X-CY-344/X-JR-074/X-YS-040 各 1 条），qwen3 向量仍留 2 行，KG/规则无人清理。
- 没有恢复入口；也没有"删除草稿/撤销停用"接口。

### 4.4 向量链路断裂（核心发现）

| 角色 | 读/写表 | 证据 |
|---|---|---|
| 生产检索 _load_vectors | 读 env POLICY_VECTOR_TABLE（生产 policy_vector_qwen3），并 join policy 过滤版本/状态/日期 | kb_rag.py:142-148、:304-338；deploy/web.env:7 |
| 后台同步 sync_policy | 删/插 policy_vector（旧 768 维表），SQL 字符串硬编码 | kb_rag.py:274、:277-281 |
| 后台删除 remove_policy | DELETE FROM policy_vector（硬编码） | kb_rag.py:293 |
| 备份 backup_vectors | SELECT * FROM policy_vector（硬编码） | vector_safety.py:91 |
| 健康检查 policy_health | 统计 policy_vector（旧表） | api.py:870-890 |
| 预览 excerpt | 读 policy_vector_qwen3（正确） | policy_preview.py:108-121 |
| 唯一写 qwen3 的入口 | reindex_vectors_2560.py 人工 CLI，源=policy_vector、目标=qwen3，按 id 覆盖 | reindex_vectors_2560.py:22-27、:69-115 |
| 旧表实际维度 | 部分 768 维（bge），部分 NULL；与 qwen3 同 id、同 doc_text | 前序分析（task-2 文档）与向量表实测 |

推论：后台"导入新政策/更新/停用"后，生产检索仍用旧快照；要等人工跑 reindex_vectors_2560（全量 10424 条逐条重新 embedding），新政策才可被召回。retry-index 返回 ok 也无法改变 qwen3（api.py:855-867 → _kb_sync_upsert → sync_policy 写旧表）。同时 qwen3 的删除/备份/健康检查都没有对应实现。

### 4.5 规则编译：懒触发 + 无生命周期清理

- 触发链：full_flow 在规则引擎 on 且 region 已知时先 rule_flow.try_rule_run（full_flow.py:146-157）→ rule_eval.load_rules(county) → 本地规则 <20 条则 rule_store.enqueue(county) 并回退完整模型（rule_flow.py:306-322；rule_worker.py:25-41）→ rule_worker --watch 每 60s 认领任务，compile_regions 全量编译该县（rule_worker.py:56-71；compile_rules.py:181-205）。
- 编译候选来自全部"在用"政策+明细（compile_rules.py:27-58），用 evidence_loader.load 过滤 region_status，按 text_hash 去重跨县复用（:124-135），region_scope 归属 '*' 或县名（:148-149）。
- 每次编译最多 6 分钟/县（audit_20261106 背景），无质量门（不抽检 rule_json 与原文明细是否一致）、无编译时延遥测、无失败重试上限之外的告警。
- **删除/停用不清理规则**：rule_store.load_rules 只按 region_scope 过滤（rule_store.py:131-137），不 join policy.status/cur_version；全仓无 DELETE FROM policy_rule。当前 policy_rule 541 行中 orphan/停用为 0 是数据巧合（停用的 4 个政策未覆盖规则）；一旦停用已编译县的政策，旧规则会继续参与推理。

### 4.6 KG 图谱同步：请求已写、发布不跑

- 管理操作写 kg_update_queue：mark_pending 用 INSERT ... ON CONFLICT(policy_code) 置 pending（graph_update_state.py:5-18）；上传/更新/停用/重试索引都会调 request_update（api.py:256-257、:267、:271、:525-530；kg_source_update.py:390-391）。
- 消费端 process_pending：schema_ready → pg_try_advisory_xact_lock(2026101201) 单发布者 → 取 pending → lock_tables → rebuild(apply=True) → 逐 job AuditLog → 提交后 sync_job_vectors（kg_source_update.py:319-362）。
- **生产不启动**：app.py:75 条件排除 full_model；foreground.py 只起 llama/qwen/gunicorn/rule_worker（:94-102、:171、:174），无 kg worker。实测无 kg_source_update 进程；kg_update_queue 仅 2 条 ready（S-QT-001、X-YS-040，attempts=0，requested 2026-10-12）——不是 pending 也无人处理，图谱基线停在 10-12。
- 结果：kg_node 的 POLICY=3333（与 10-12 台账一致），新增/停用的政策不会进图；kg_policy_edge 6198 亦是旧基线。

### 4.7 缓存与签名：管理操作后的可观测性缺口

- smart_matcher_kb 的 DB 签名包含 today + policy/policy_detail/rule_mapping/rule_check 的行级 md5 聚合（smart_matcher_kb.py:157-182；REPEATABLE READ）；**不含 policy_vector、policy_rule、kg_***。因此向量/规则/图谱变化不会触发该缓存重载。
- 写操作的 invalidate 调用发生在 commit 之后、try 块内（api.py:265-275、:523-530），异常被吞；若进程多实例，其他实例的 _VEC_CACHE 依赖 max(updated_at) 轮询（kb_rag.py:322），旧表更新不会影响 qwen3 的 max_ts，故 qwen3 变化也不会被感知（除非重启）。
- 生产实测 gunicorn 有两个 master/worker 痕迹（361754 与 751616），缓存失效语义在多实例下更不可控。

### 4.8 识别与"自动管理"基础

- recognize.match_policy（recognize.py:293-351）：明确编号冲突/名称相似但层级分类不一致/多命中且名称相似<0.8 → 待人工确认；文号/替代关系命中要求原文真实出现（:311-318 反幻觉）；名称相似≥0.72 或（≥0.35 且内容≥0.85）→ 待人工确认；否则新增 conf=1.0。
- 识别不做入库前查重（不去 policy 表找同名同文号同地区家族），不做批次内去重；table_import 只在"同一政策编号对应不同名称"时拒绝（table_import.py:41-42），不检查跨批重复。
- PolicyDraft 有 confirm_status 但没有超时/提醒/批量清理；43 条草稿中 29 条待确认自 2026-10-08 14:51 起滞留。

### 4.9 事务、并发与权限

- 事务边界：政策主表+明细在单事务（write_details 后统一 commit）；但知识库/图谱/缓存都是 commit 后的外部副作用，无 outbox，失败即漂移。
- 编号生成：gen_policy_code（api.py:79-90）在 PostgreSQL 下用 pg_advisory_xact_lock(2026101202)+LIKE 前缀 max+1；SQLite 分支无锁（本地/后台 fallback）。DB 无 UNIQUE(policy_code)，并发上传理论上可生成重号。
- 路由权限：所有管理路由 @admin_required（如 api.py:104-105、:345-346）；预览 @login_required（api.py:1003 附近）；无二次确认/CSRF 相关证据（本轮未审计前端安全）。

### 4.10 前端管理页面与 JS：只有"上传/草稿/确认"，没有编辑/删除/版本

- templates/ 目录为空；管理界面是单页外壳 src/static/index.html（约 6.9KB）+ src/static/app.js（836 行）。
- 路由入口：/upload、/drafts（app.js:813-814）；上传 POST /api/upload（app.js:288）；重试索引 /api/drafts/{id}/retry-index（app.js:293、:409）；policy-health 页（app.js:300）。
- 草稿页可选"入库方式"update/new（app.js:343-344），展示候选政策；保存草稿 → /drafts/{id}/update（app.js:385）；确认落库 → /drafts/{id}/confirm（app.js:396）；驳回（app.js:405）；图谱状态/重试（app.js:411-412）。
- 全站没有任何"删除政策/编辑政策/版本列表/回滚/发布"按钮或 fetch 调用；唯一的删除类 UI 是会话删除（app.js:459）。因此前端不是缺按钮，而是后端根本没有对应路由。

### 4.11 定时任务与后台进程：只有懒编译 worker

- 无 APScheduler、无 before_first_request、无系统 crontab（deploy 下仅 autostart.sh 开机自启）；后台线程/进程仅有：
  - kg_source_update.start_worker 的 daemon 线程 policy-graph-update（kg_source_update.py:365-387）——生产不启动（见 4.6）；
  - rule_worker --watch --interval 60（rule_worker.py:81、:127；deploy/foreground.py:94-102）；
  - reindex_vectors_2560.py 的 while 循环（reindex_vectors_2560.py:87）——仅人工运行；
  - conversations 的后台 worker（与政策生命周期无关）。
- 结论：没有"过期政策扫描、队列重试、数据巡检、草稿提醒"等定时任务的基础设施；管理动作全部依赖 HTTP 请求同步完成。

### 4.12 导出、预览、健康检查

- 导出（api.py:186-224）：全量政策→xlsx 台账；旧文件先备份为 *_备份_时间戳.xlsx；含停用政策与管理字段，无"删除/替代"列。
- 预览（api.py:1003-1030；policy_preview.py:151-179）：只按 policy_code 查，不判 status；停用政策仍返回 policy/detail/excerpt（excerpt 从 qwen3 取，按 detail_id 严格过滤，:124-148）。删除后若行被硬删，预览 404。
- 健康检查（api.py:870-902）：只查 status=在用 的政策；details 数 + 旧表 policy_vector 在用数；issues 仅地区待补充/明细待确认/当前版本索引未就绪/图谱待构建。生产 qwen3 是否就绪不可见。

## 5. 数据快照（2026-11-08 只读实测）

### 5.1 政策库 policy_db_mig20261021

| 表 | 行数/分布 | 与生命周期的关系 |
|---|---|---|
| policy | 3337（在用 3333、停用 4）；cur_version v1=3335、v2=2（X-JY-038、S-ZB-089） | 无删除态；max(updated_at)=2026-10-12 12:55:08 |
| policy_version | 3339；无空快照；maxlen 68723；version_no 全 v1（插入旧版本号） | 只写归档，无读取/回滚 |
| policy_detail | 9403；停用政策仍有明细 3 条 | 更新时全删重插 |
| policy_draft | 43（待确认 29、已确认 14）；待人工确认 28、新增 12、文号命中 2、相似度命中 1 | 29 条自 10-08 滞留 |
| audit_log | 72；上传文档 47、新增 12、停用 7、更新 2、占位隔离 2、演示 1、初始导入 1 | 无删除/发布/回滚动作；10-12 后无管理操作 |
| policy_rule | 541 行/282 个 code；partial 457、complete 82、failed 2；region_scope 覆盖 7 个县+* | 无清理逻辑；rule.version 与 cur_version 一致 540/541 |
| policy_rule_job | 4（万安 done、于都 done、新建 done、青山湖空） | 懒编译任务表 |
| kg_update_queue | 2（S-QT-001、X-YS-040，status=ready，attempts=0，requested 10-12） | 无人消费 |
| kg_node | 13397（POLICY 3333、SUBSIDY 4164、CONDITION 3867、DOC 1624…） | 图谱基线停在 10-12 |
| kg_policy_edge | 6198 | 同上 |
| policy_vector_qwen3 | 10424（在用 10423/停用 1）；全 qwen3-embedding-4b；max(updated_at)=2026-11-05 09:40；53 行 detail 悬空；停用政策仍留 2 行 | 生产读表；删除/备份/健康检查都不覆盖 |
| policy_vector（旧） | 10424；policy_version 全非空；停用政策 2 行 | 后台写表、重建源 |
| app_config | reindex_cursor=0、reindex_cursor_2560=0、vector_index_dim=2560 | 重建游标均为 0（无进行中任务） |

约束检查：information_schema 查 policy/policy_detail/policy_version/policy_rule/policy_vector*/policy_draft 的 FK/UNIQUE 返回空集；当前 detail/rule/vector orphan 均为 0（无 DB 级保护）。

### 5.2 认证库 smart_help_auth_mig20261021

- qa_log 510 行（最新 qa677 2026-11-08 03:10）；match_json 非 null 470、matched_policies 非 null 228；conversation 282。
- 历史引用：384 个不同 policy_code、635 个不同 detail_id（match_json 中 detail_id 字段共 54475 处，含候选列表）；当前缺失/停用引用为 0（因为从未删除），但数据结构中没有版本/状态快照字段，删除后历史回答无法判定失效。
- 样本：qa675 莲花县引用 7 个政策（S-ZB-090、S-ZB-091、S-ZF-008、X-CY-301、X-ZB-300、X-ZB-301、X-ZB-462）；qa676 青山湖区 S-QJ-043；qa677 株洲市（省外）被拒。

### 5.3 本地 SQLite fallback（data/policy_db/smart_policy.db）

policy 3325、policy_detail 9390、policy_version 3325、qa_log 3、rule_mapping 34、rule_check 12；与 Kingbase 相差 policy 12 行、detail 13 行——两库已漂移。src/config.py:62-73 的解析顺序决定：只有没有 ADMIN_DB_URL 时才回落 SQLite；生产有 env，故 SQLite 是本地/后台 fallback，不是生产库。

## 6. 缺口清单（对应三项需求）

### 6.1 需求 A：删除旧政策

| 缺口 | 证据 | 影响 |
|---|---|---|
| 无硬删除入口 | api.py 路由仅 GET/POST；grep DELETE 无 policy 表 | 错录/重复政策只能"停用"，永久占编号 |
| 无回收站/墓碑 | models.py 无 deleted_at/deleted_by；无 tombstone 表 | 误删不可恢复，历史无法审计 |
| 停用不级联 | policy_detail 仍留 3 条；qwen3 仍留 2 行；rule/kg 无清理 | 停用后仍可能被预览/规则/图谱引用 |
| 无删除前影响分析 | 无 qa_log 引用检查、无替代政策校验 | 删除会让历史回答/收藏/外链静默失效 |
| 无删除审计动作 | audit_log 动作枚举无删除 | 合规审计缺环 |
| 无批量下线 | 只有逐条草稿停用 | 一次更新一批政策时人工成本高 |

### 6.2 需求 B：导入新政策（后端功能）

| 缺口 | 证据 | 影响 |
|---|---|---|
| 导入不更新生产向量 | sync_policy 写旧表；reindex 仅人工 | 新政策导入后检索不到，直到人工全量重建 |
| 无入库查重 | recognize 不查同名同文号家族；DB 无 UNIQUE | 重名重灾（产业奖补 52、先诊疗后付费 45、稻谷补贴 39）会继续增长 |
| 批次无事务 | 逐条 commit（api.py:397-401） | 批量导入部分成功难回滚 |
| 停用/更新不触发规则重编 | 无管理操作→rule_worker 通道；rule_worker 仅按县规则<20 补 | 政策内容变更后旧规则继续用 |
| 图谱不刷新 | worker 不启动 | 新政策不进图，图谱证据与台账脱节 |
| 草稿滞留无治理 | 29 条待确认>1 个月 | 未完成政策无法自动提醒/批量处置 |
| 无 dry-run/差异预览 | 无版本 diff API | 更新前无法看"哪些明细被删/改" |

### 6.3 需求 C：内部政策智能自动管理

| 缺口 | 证据 | 影响 |
|---|---|---|
| 无生命周期状态机 | 只有 status 在用/停用 + cur_version | 缺 draft/active/superseded/expired/retired 语义 |
| 无过期自动处理 | 检索层按 expire_date 过滤（kb_rag.py:322；evidence_loader valid_dates）但无定时任务把政策置过期 | 台账状态与检索事实长期不一致 |
| 无替代关系 | 无 superseded_by 字段/表；识别只提示"替代关系命中" | 新旧政策共存，用户看到废止政策 |
| 无变更影响分析 | 无 qa 历史引用/向量/规则/KG 待刷新清单 | 每次更新靠人工记忆补下游 |
| 无定时体检 | audit_log 10-12 后无动作；无 cron/worker 做数据巡检 | 漂移（SQLite vs Kingbase、qwen3 过期、队列停摆）无人发现 |
| 无质量门/抽检 | compile_rules 无 rule 抽检；ingest 无重复检查 | 规则/入库质量无度量 |
| 无审批/定时发布 | 确认即生效 | 无法在指定日期批量生效 |

## 7. 耦合点与风险（R1–R10）

| 编号 | 耦合/风险 | 证据 | 等级 |
|---|---|---|---|
| R1 | 生产向量表与管理写入表不一致 | kb_rag.py:274/277/293 vs :142/147/:322；vector_safety.py:91；api.py:870-890 | P0 |
| R2 | 停用/删除无级联（明细/向量/规则/图谱/qa） | policy_detail 3 条残留；qwen3 停用政策 2 行；rule_store 不过滤状态；kg worker 不跑 | P0 |
| R3 | KG 发布 worker 条件性不启动 | app.py:73-77；foreground.py:94-102/:171/:174；队列 2 条 ready | P0 |
| R4 | 规则与政策版本/状态脱钩 | rule_store.py:131-137；无 DELETE policy_rule；rule_worker 触发面窄 | P1 |
| R5 | 外部副作用在 commit 后执行，异常被吞 | api.py:264-275、:315-316、:523-544 | P1 |
| R6 | 无唯一约束/FK，编号生成依赖进程内锁 | information_schema 空集；api.py:79-90 | P1 |
| R7 | 版本快照只写不读、新增快照为空 | api.py:289-293、:328-331；grep PolicyVersion 仅 3 处 | P1 |
| R8 | 健康检查/预览与真实生产索引不一致 | api.py:870-902 读旧表；preview 不判 status | P1 |
| R9 | 缓存签名不含向量/规则/图谱，多实例下失效不可控 | smart_matcher_kb.py:157-182、:241-244；实测两个 gunicorn 痕迹 | P2 |
| R10 | 导入识别缺查重/幂等，批量逐条提交 | recognize.py:293-351；api.py:397-401；DB 无 UNIQUE | P1 |

## 8. 改造边界建议（只给建议，不写实现）

1. **先修向量链路（P0，最小改动）**：把 kb_rag 的写路径参数化为 _vec_table()（或统一 vector_store 抽象），使 sync_policy/remove_policy/backup/健康检查全部作用于 POLICY_VECTOR_TABLE；qwen3 删除按 policy_code（或 detail_id）执行；补 qwen3 备份。改造完成前，任何"导入新政策"都不可信。
2. **补生命周期状态机（P1）**：policy 增加 4 个状态与替代字段：draft/review → active → superseded/expired/retired；新增 policy_supersedes(policy_code, superseded_by, effective_date, reason)。停用=retired，删除=purge（仅管理员+二级确认+tombstone 记录）；恢复=active（校验版本）。
3. **删除/停用级联编排（P1）**：引入 outbox 表（policy_code, action, target=vector/rule/kg/cache, status, attempts），台账事务内写 outbox，后台 worker 消费；杜绝 commit 后 try/except 吞错。删除前做影响分析：qa_log 引用数、向量/规则/KG 待清理清单、替代政策是否已生效。
4. **导入管线（P1）**：统一 import_job（批次号、dry-run、diff、回滚）；入库前查重（政策编号/文号/名称+地区家族/正文相似度），重复给出"更新 or 停用旧版 or 新建"三选一；批量导入单事务或按批次可回滚；草稿 TTL=7 天提醒/自动归档。
5. **规则生命周期（P1）**：rule 增加 policy_version；rule_store.load_rules 只取 status=active 且 version=cur_version；停用/删除/版本升级写 outbox 触发重编或标记 stale；编译队列暴露 job 状态与时延指标。
6. **KG 发布无条件化（P1）**：full_model 下也启动 kg worker（或改成独立进程/队列消费者）；kg_update_queue 增加 attempts/next_retry/最后错误，提供 /api/kg/queue 可观测；图谱节点带 policy_version。
7. **智能自动管理（P2）**：每日扫描（过期→expired、队列积压、qwen3 与台账 diff、SQLite/Kingbase diff、草稿滞留、规则 stale），产出巡检报告与工单；变更影响分析（qa 历史引用/热门政策）自动生成下游待刷新清单；发布窗口（指定日期批量生效）。
8. **API 面（P1/P2）**：DELETE /api/policies/<code>（软删，可选 purge）、POST /api/policies/<code>/restore、GET/POST /api/policies/<code>/versions（列表/diff/rollback）、POST /api/import/jobs（批量、状态、回滚）、POST /api/policies/<code>/publish。所有写操作记录 operator+reason+影响范围。

**建议实施顺序**：R1 向量链路 → R2/R5 outbox 与级联 → R3 KG worker → R7 版本发布/回滚 → 生命周期状态机 → 导入管线与智能巡检。原因是：当前"导入新政策"在生产不可见，任何上层管理功能都建立在此之上。

## 9. 附录：证据索引与方法限制

关键证据索引：

| 主题 | 文件:行号 |
|---|---|
| 路由全集（无 DELETE/PUT/PATCH） | src/api.py:104、:116、:157、:186、:345、:429、:440、:447、:474、:855、:870、:912、:918、:931、:948、:959、:967、:991、:1003 |
| 新增/更新/停用三分支 | src/api.py:228-342（停用 :251-279、更新 :281-318、新增 :320-342） |
| 自动落库（非待人工确认） | src/api.py:389-396 |
| 上传批次无事务 | src/api.py:397-401 |
| 版本快照 | src/api.py:289-293、:328-331；src/models.py:62-73 |
| 明细全删重插 | src/policy_ingest.py:82-103（DELETE :91） |
| 向量同步硬编码旧表 | src/kb_rag.py:225-284（:274、:277-281）、:287-297（:293） |
| 生产向量读表 | src/kb_rag.py:142-148、:304-338（SQL :322） |
| 向量重建 CLI | src/reindex_vectors_2560.py:22-27、:69-115 |
| 向量备份旧表 | src/vector_safety.py:88-112（:91） |
| 健康检查旧表 | src/api.py:870-902 |
| 预览不判状态/读 qwen3 | src/api.py:1003-1030；src/policy_preview.py:108-121、:124-148、:151-179 |
| KG 队列标记 | src/graph_update_state.py:5-18 |
| KG worker 条件不启动 | src/app.py:73-77；deploy/foreground.py:94-102、:171、:174 |
| KG 发布逻辑 | src/kg_source_update.py:300-316、:319-362、:365-387、:390-391 |
| 规则装载不过滤状态 | src/policy_core/rule_store.py:131-137 |
| 规则懒编译 | src/policy_core/rule_flow.py:306-322；src/rule_worker.py:25-41、:56-71；src/compile_rules.py:181-205 |
| 缓存签名范围 | src/smart_matcher_kb.py:157-182、:241-244 |
| 识别匹配阈值 | src/recognize.py:293-351、:355-400 |
| 表格导入限制 | src/table_import.py:41-42、:47-52、:63-66 |
| 环境开关 | deploy/web.env:1-2、:7、:37-42；src/config.py:39、:62-73、:74-77、:102 |

数据来源与限制：

- 代码行为来自当前工作区（基线 baseline-20261106 + 未提交改动；git status 显示另有 data/regions/、deploy/build_china_divisions.py、src/eval/ 等未跟踪文件，非本次任务产物，未纳入审计）。
- 数据库数字为 2026-11-08 09:00–09:20 的只读快照；ksql 当前用户无权限，使用项目 Python 环境 + psycopg2 只读连接（deploy/web.env 的 ADMIN_DB_URL / SMART_HELP_AUTH_DATABASE_URL）。
- 运行期未做任何写操作、未启停服务；进程信息来自 ps 只读。
- "无删除"结论基于路由与全仓 SQL 静态检查；若有外部脚本绕过应用直接删库，不在本审计范围。
