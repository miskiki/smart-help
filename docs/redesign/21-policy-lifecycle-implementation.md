# 21 政策生命周期与全国化后端实施记录（P1）

状态：已实现、默认关闭、待灰度  日期：2026-11-08  基线：217b7fc

## 1. 本轮范围

按设计文档 20 落地 L2/L3/L4 的后端骨架，并顺手修掉审计发现的 P0 向量链路断裂：

1. **P0 修复**：后台向量同步写旧表 policy_vector、生产检索读 policy_vector_qwen3 → 导入/停用政策检索不生效。
2. **outbox 消费者**（新增）：生命周期/导入只写业务表 + outbox，向量/规则/图谱/缓存侧效应由 worker 幂等重放。
3. **生命周期后端**（新增）：状态机、版本发布/回滚、软删/恢复、影响检查、8 条管理 API。
4. **导入管线**（新增）：staging→校验→dry-run→commit 幂等 upsert + 批次回滚 + CLI。
5. **自动管理**（新增）：过期扫描/outbox 消费/健康巡检/覆盖报告/规则补队/滞留草稿，默认 dry-run。
6. **读侧与运行期接线**：规则链路按 policy.status 过滤（T21）；POLICY_SCOPE 结构化范围过滤（默认关）；SERVICE_SCOPE=jiangxi/national 全国化分流（默认 jiangxi，零行为变化）。

所有新能力默认关闭；开关关闭时与 217b7fc 行为一致。

## 2. P0 向量链路修复

### 问题
- 后台 sync/remove/backup 硬编码 `policy_vector`（src/kb_rag.py:274/278、:293；src/vector_safety.py:91）。
- 生产 `POLICY_VECTOR_TABLE=policy_vector_qwen3`（deploy/web.env:7），读取走 `_VECTOR_TABLE`（src/kb_rag.py:142/:322）。
- 唯一写 qwen3 的是人工 CLI `reindex_vectors_2560.py` → 导入、更新、停用、图谱发布后的向量同步在生产全部无效。

### 改法
- `src/kb_rag.py`：sync_policy 的 DELETE/INSERT、remove_policy 的 DELETE 全部改用 `_VECTOR_TABLE`；备份传入同名表。
- `src/vector_safety.py`：`backup_vectors(connection, policy_code, table=None)`，表名限定 `policy_vector`/`policy_vector_qwen3`（`_vector_table_name` 白名单，防注入）；备份 payload 记录 `table`。
- `src/restore_vector_backup.py`：按备份 payload 的 `table` 恢复（旧备份缺 table 时回落 policy_vector），列集合仍按当前表校验，不执行备份中的任意 SQL。
- `tests/test_vector_table.py`（5 例）：sqlite 假库验证 sync 写 qwen3、旧表 0 行；remove 只删 qwen3；备份 payload 表名正确；表名白名单。

### 运行前提
- 目标表必须存在（qwen3 表由 `reindex_vectors_2560.py` 创建）；向量服务（8095）可用；维度不一致时 sync 会拒绝写入（既有 `check_index_compat`）。
- 回退：`POLICY_VECTOR_TABLE=policy_vector` 并重启（回到旧表读写一致，但索引是 768 维旧数据）。

## 3. outbox 消费者 policy_worker（新增）

文件：`src/policy_core/policy_worker.py`（约 300 行）

| 事件 | handler 行为 |
|---|---|
| policy.vector_rebuild | 政策在用 → `kb_rag.sync_policy` 重建 qwen3；suspend/archive/delete/hard_delete 或政策不存在 → `remove_policy` 清索引；非在用状态一律不重建 |
| policy.rule_invalidate | 查该政策已编译规则的 `region_scope` 去重，逐县 `rule_store.enqueue(force=True)` 重排编译；`*` 全局规则如实计入 global_rules |
| policy.kg_sync | `kg_update_queue` upsert pending（表缺失→skipped）；由图谱 worker 消费 |
| policy.cache_invalidate | 依次清 kb_rag / smart_matcher_kb / kg_retrieval 缓存，单个失败只告警 |

- 消费语义：`process_once` 打开短事务 → `outbox.ensure_schema` → `outbox.drain`（claim 失败重试、达到 OUTBOX_MAX_ATTEMPTS 进 dead、无 handler 直接 dead）。handler 自己用短连接，事件之间互不影响。
- 启动：`app.py` 末尾 `policy_worker.start()`，仅 `POLICY_WORKER=1/on/true/yes` 时起守护线程；与 full_model/showcase 无关（修掉「生产 full_model 下 KG/publisher worker 都不启动」的同类问题）。
- CLI：`python -m policy_core.policy_worker --once [--json]`；开关关闭时 rc=2 且不连库。
- 开关：`POLICY_WORKER`（默认 0）、`POLICY_WORKER_INTERVAL`（默认 15s）、`POLICY_WORKER_BATCH`（默认 10）、`OUTBOX_MAX_ATTEMPTS`（默认 3）。
- 测试：`tests/test_policy_worker.py` 17 例（开关/消费/重试/dead/幂等/四类 handler）。
- 回退：保持默认 0，或进程内停止线程；outbox 事件保留可稍后重放。

## 4. 生命周期后端（T18）

- `src/policy_core/outbox.py`：8 列表、幂等 enqueue、（PG）FOR UPDATE SKIP LOCKED claim、finish/fail/drain/counts；事件类型四项，状态 pending/done/dead。
- `src/policy_core/lifecycle.py`：状态机 draft/active/suspended/archived/deleted；`_WRITE_STATUS` 保持现网中文「在用/停用」，归档/删除写 archived/deleted；所有变更写 audit_log + 4 类 outbox 事件；suspend/archive/restore/delete/publish/rollback 均支持 dry_run；删除默认软删，硬删只允许 draft 且无版本、qa_log 可确认 0；`impact()` 返回 details/scope_rows/rules/versions/vectors/qa_log；`versions()` 返回版本链。
- `src/api.py:1031-1219`：8 条路由（POST suspend/archive/restore/delete/publish/rollback，GET impact/versions），`@admin_required`，`POLICY_LIFECYCLE=0`（默认）统一 404 `policy_lifecycle_disabled`。
- 测试：test_outbox 15 例 + test_lifecycle 32 例。

## 5. 导入管线（T19 + T22）

- `src/policy_core/policy_import.py`：import_batch/import_row staging 表；preview→create_batch→validate_batch→commit→rollback→batch_report；commit 单事务、PG advisory lock 2026101204 + FOR UPDATE、二次 commit 幂等（changed=0）；create 写 policy+details+非空 v1 快照+policy_scope+outbox；update 写旧版本快照并 cur_version+1；rollback 仅 committed，批次后出现新版本则 skipped 不强行还原。
- CLI：`deploy/import_policies.py --file ... [--dry-run|--commit] [--batch-id|--rollback|--report] [--json]`，默认 dry-run；`POLICY_IMPORT` 关闭时 rc=2。
- API（T22，`src/api.py:1221-1490`）：`POST /api/policies/import/preview|import|import/<batch>/commit|rollback`、`GET /api/policies/import/<batch>`；`POLICY_IMPORT=0`（默认）全部 404 `policy_import_disabled`。
- 测试：test_policy_import 42 例 + test_api_policy_admin 16 例。

## 6. 自动管理（T20）

- `src/policy_core/autopilot.py`：expire_scan / outbox_drain / health_check / coverage_report / rule_requeue / stale_draft；结构化报告（before/after 计数 + 明细 [:20]），兼容 PG/sqlite。
- CLI：`deploy/policy_autopilot.py {expire-scan|outbox-drain|health-check|coverage-report|rule-requeue|stale-draft|all} [--apply] [--json]`，默认 dry-run；写库需 `POLICY_AUTOPILOT=1` 且 `--apply`。
- 只读实测（生产库）：health issue_total=2597（空壳 40 / 无 scope 40 / 重复组 917 冗余 2513 / 停用残留 qwen3 向量 2）；覆盖 95/100 县、13 功能区、省条 13；过期 108 + 临期 16；规则缺 89/95 县 + global 39；滞留草稿 29。
- 开关：`POLICY_AUTOPILOT`（默认 0）、`POLICY_AUTOPILOT_STALE_HOURS`（72）、`POLICY_AUTOPILOT_EXPIRING_DAYS`（30）；未接 app.py 定时器，由 cron/systemd 调 CLI。

## 7. 读侧与运行期接线

### 7.1 规则链路状态过滤（T21，默认开）
- `rule_store.py:131-173`：ACTIVE_POLICY_STATUSES=('在用','active','启用')、`policy_status_map`（表/列缺失或异常返回 None=无法确认）、`status_filter_enabled`（`POLICY_STATUS_FILTER` 默认 1）。
- `rule_eval.load_rules` 仅在金仓 store 可用时过滤；compiler 离线回落/无 policy 表跳过不崩；过滤事件 `rule.status_filtered`。
- 口径：停用/archived/deleted/draft/NULL/无政策行一律不出卡（只少卡不多卡，FP=0）。
- 测试：test_rule_status_filter 14 例。

### 7.2 地区全国化（T17，默认江西口径不变）
- `region.py`：`service_scope_mode()`（`SERVICE_SCOPE=jiangxi` 默认 / national）、`nationwide_enabled()`（`REGION_NATIONWIDE` 默认 0，national 强制开）、全国区划补齐/关系合并、同市功能区×普通县区→待核。
- `kg_retrieval.py`：national 下外省不再 blocked，能确定性解析即 ok；无法确认统一 ask；旧层「不适」但程序层 None 时降级「地区待细化」。
- `full_flow.region_needs_ask`：地区有内容但解析不出市/县（区）→ 追问（M1：治「只写江西省就等于默认按江西检索」）。
- **config.DEFAULT_POLICY_REGIONS 保持 {'C':'全国','S':'江西省全省'}**：旧链路（api/policy_ingest/policy_preview/recognize/smart_matcher_kb）默认零变化；全国化分流放在 `kg_retrieval._policy_default_regions()` 运行期判断。
- 测试：test_region_national 29 例。

### 7.3 结构化范围过滤（POLICY_SCOPE，默认关）
- `scope_filter.py`：filter_codes/filter_packs 三态（True 保留、False 丢弃、None 保留待核、无 scope 行保留），任何异常退回原集合并记 error，绝不丢候选。
- `full_flow._apply_scope_filter`（候选截断后、reason 前）与 `rule_flow._apply_scope_rows`（dedupe 后、出卡前）接入；事件 `scope.filter`。
- 测试：test_scope_filter 7 例 + test_scope_wiring 10 例。

## 8. 开关与回退矩阵

| 开关 | 默认 | 作用 | 回退 |
|---|---|---|---|
| POLICY_WORKER | 0 | outbox 消费线程 | 保持 0 / 重启 |
| POLICY_LIFECYCLE | 0 | 8 条生命周期 API | 404，无副作用 |
| POLICY_IMPORT | 0 | 导入 CLI/API | rc=2 / 404 |
| POLICY_AUTOPILOT | 0 | 自动管理写库 | dry-run 只读 |
| POLICY_STATUS_FILTER | 1 | 规则读侧状态过滤 | =0 回旧行为 |
| POLICY_SCOPE | 0 | 结构化地区范围过滤 | =0 回旧行为 |
| SERVICE_SCOPE / REGION_NATIONWIDE | jiangxi / 0 | 全国化分流 | 改回默认 |
| ANSWER_STYLE / AGENT_LOOP 等 | 既有 | 回答与调查循环 | 既有开关不变 |

## 9. 验证

- 全量单测：**592 tests OK**（`source deploy/web.env` + `PYTHONPATH=src:.` + `unittest discover -s tests`；含 worker 启动钩子 2 例）。
- 只读生产探测：policy_scope 回填完成（15493 行、unresolved 0、ambiguous 0）；self-match 654 次全部 applicable；policy_status_map 正常；autopilot 六任务报告见 §6。
- 生产尚未启用的原因：`POLICY_WORKER/POLICY_LIFECYCLE/POLICY_IMPORT/POLICY_AUTOPILOT/POLICY_SCOPE` 默认关，需单独灰度；outbox 表尚未在生产创建（首次启用时 ensure_schema 幂等建）。
- **生产 HUP 回归（master 361754 → worker 883011）**：
  - 默认开关零行为变化实测：`policy_worker.enabled()=False`、生产库 `outbox` 表不存在、`policy.deleted_at` 列不存在（关闭开关时 start() 不连库、不建表）；`policy` 状态分布 在用 3333 / 停用 4。
  - `POLICY_STATUS_FILTER=1`（唯一默认开启项）生效后，莲花县规则路径仍 **7 卡**（S-ZB-090/091、S-ZF-008、X-CY-301、X-ZB-300/301/462）、rule=used、1148 字、23 个用户事件（与改动前逐项一致，qa 681）。
  - 青山湖 consultation：1 卡 S-QJ-043、578 字、21 个用户事件（qa 682）。
  - 株洲市（不带省）：region_gate=true、0 卡、1 个地区追问、156 字（qa 683）。
  - 三例 HTTP 200、delta 拼合一致、无 error 帧。
- worker 启动钩子隔离验证（sqlite 临时库 + POLICY_WORKER=1 导入 app）：线程 `policy-worker` 启动、outbox 表幂等创建；POLICY_WORKER=0 时不建表。

## 10. 已知缺口（下一步）

1. **KG worker 在 full_model 下不启动**（app.py:75 条件含 not full_enabled）；outbox 的 kg_sync 只会排队。需与 publisher/rule_worker 一起给生产真正启动的策略。
2. **rule_invalidate 只入队不编译**：生产 rule_worker 无守护进程，重编译要靠 cron/autopilot；入队后规则旧文本仍被读侧使用（状态过滤只解决停用，不解决条件更新）。
3. **national 端到端未闭环**：grader 对 explicit_other 仍判 D 硬拦（national 下会误拦外省），且全国库尚无数据；全国化需要先补数据再开放 SERVICE_SCOPE=national。
4. **5 城区无数据**（东湖/西湖/青云谱/青山湖/浔阳）+ 40 条 S 级空壳 + 26.7% 重复行：属数据治理，需导入/清理批次。
5. 生命周期/导入 API 已接前端管理页（`/admin`，见 docs/27，提交 83f96de；默认开关关时区块置灰）；
   qa_log 版本快照已由 T29 补上（`src/result_snapshot.py` 在 `freeze` 结果追加 `policy_snapshot`，默认开、
   纯附加，生产库只读验证通过），历史问答可定位到当时的 name/level/status/cur_version。
6. outbox handler 的向量重建依赖本地嵌入服务（8095），worker 与检索服务共享资源，灰度时需限速。
