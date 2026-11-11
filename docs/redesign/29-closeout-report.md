# 29 政策全国化 + 生命周期 · 收尾报告（2026-11-08）

> 基线：HEAD `db40f73`。本文是这一轮「全国化 + 政策生命周期 + 自动管理」的收尾文档：交付物清单、
> 开关矩阵与回退、启用顺序、验证证据、已知缺口。设计依据见 `docs/redesign/20-national-policy-lifecycle-plan.md`，
> 后端实施记录见 `21-policy-lifecycle-implementation.md`，前后端联调/灰度见 `22、25、26、27`。

## 1. 一句话结论

1. **全国化不是改默认值，而是加一层可切的分流**：地区归一真源换成 `admin_division`（内置全国 31 省/342 地级/3056 县区），
   运行期由 `SERVICE_SCOPE`（默认 `jiangxi` 不变）/ `REGION_NATIONWIDE` / `NATIONAL_READONLY` 控制；
   默认全关时线上行为与改造前一致（已 HUP 回归验证）。
2. **政策生命周期后端已闭环**：状态机（在用/停用/归档/删除）、版本发布/回滚、软删/硬删、影响预检、
   审计 + outbox 级联事件、outbox 消费者（向量 qwen3 / 规则失效 / KG 同步 / 缓存失效），
   并修掉了 P0 向量断裂（此前后台同步写 `policy_vector`、生产读 `policy_vector_qwen3`）。
3. **导入与治理都有 CLI 且默认只读**：`deploy/national_intake.py`（全国数据接入）、`deploy/import_policies.py`（导入管线）、
   `deploy/policy_governance.py`（数据治理）、`deploy/policy_autopilot.py`（自动体检/过期扫描）；
   写库一律要求显式 `--apply` + 开关 + 确认，误操作可回退。
4. **剩余工作已收敛成 6 条明确缺口**（§6），其中最影响落地的是「全国数据源未接入」和「存量数据质量问题需人工决策」，
   代码侧不再有阻塞项；单测 904 例全绿。

## 2. 交付物

### 2.1 代码

| 模块 | 文件 | 说明 |
|---|---|---|
| 地区 | `src/policy_core/admin_division.py`、`data/regions/china_divisions.json` | 全国行政区划纯函数解析：canonical/relation/功能区/直辖市/省直辖县；`deploy/build_china_divisions.py` 可重建 |
| 结构化范围 | `src/policy_core/policy_scope.py`、`deploy/migrate_policy_scope.py` | policy_scope 表（policy_code+detail_id+region_key）与回填工具；三态 match（True/False/None 待核） |
| 范围过滤 | `src/policy_core/scope_filter.py` | 运行期按结构化范围过滤候选（POLICY_SCOPE，默认 0；异常时绝不丢候选） |
| 生命周期 | `src/policy_core/lifecycle.py`、`src/policy_core/outbox.py`、`src/policy_core/policy_worker.py` | 状态机/版本/影响预检/审计/outbox 与消费者；`src/api.py` 8 条生命周期路由 |
| 导入 | `src/policy_core/policy_import.py`、`deploy/import_policies.py`、`deploy/national_intake.py` | 批次校验/dry-run/幂等 commit/rollback；全国字段映射与离线校验 |
| 自动管理 | `src/policy_core/autopilot.py`、`deploy/policy_autopilot.py`、`deploy/policy_autopilot.cron.example` | 六项体检 + 可选定时 worker + cron 模板 |
| 数据治理 | `deploy/policy_governance.py`、`deploy/policy_data_audit.py` | 只读审计 + 受控执行（归档空壳/回填 scope/修复不一致） |
| 只读全国 | `src/policy_core/coverage_probe.py`、`src/policy_core/full_flow.py` | 按地区收录探测，未收录如实说明且不出资格结论 |
| 运维 | `deploy/migrate_policy_admin.py`、`deploy/gray_up.sh` 等 | 结构一次性迁移（outbox/导入/scope/deleted_at）、灰度实例 5001 |
| 前端 | `src/api.py` `/admin` 页面（`83f96de`） | 检索/生命周期操作/批量导入；开关关闭时优雅降级 |

### 2.2 本轮收尾相关提交

| 提交 | 内容 |
|---|---|
| `2c30ddc` | 生命周期/导入/自动管理后端与管理 API（默认关闭） |
| `6be917a` | 地区全国化分流、规则读侧状态过滤、结构化范围过滤接线 |
| `4051a35` | P0 向量修复（写 qwen3 表）+ outbox 消费者 + docs/21 |
| `77f5d3e` `fab82a7` `9b4ff84` | 只读全国模式、national 不再预判外省不适、全国数据接入 CLI |
| `5ed5cad` `d402deb` `604074c` | 灰度实测记录、开关启用顺序手册、接入链路预演 |
| `c7c4139` `faa5cd0` | full_model 下 KG worker 开关（默认关）、规则编译执行器（默认关） |
| `83f96de` `1693c34` | /admin 管理页 + 文档 |
| `385e6dc` | 家庭仅到省级时的 relation 方向修复 + 地区深度测试 |
| `803a751` | autopilot 定时 worker + cron 模板 |
| `f59e956` `2df1bf5` `db40f73` | 生命周期 API/级联端到端测试、结构迁移脚本、数据治理 CLI |

### 2.3 测试

- 全量：**904 tests OK**（命令：`cd /home/adminroot/smart-help && set -a && . deploy/web.env && set +a && PYTHONPATH=src:. /home/adminroot/miniconda3/envs/smart-help-web/bin/python -m unittest discover -s tests -p "test_*.py"`）
- 覆盖：地区解析/范围匹配（含直辖市/省直辖县/功能区）、只读全国短路、规则状态过滤、范围过滤接线、
  生命周期状态机 + API + outbox 级联端到端、导入管线（CSV/XLSX/JSON）、autopilot 六任务与 worker、
  治理 CLI、结构迁移脚本、`/admin` 页面 API。

## 3. 开关矩阵（默认值 = 现网行为不变）

| 开关 | 默认 | 作用 | 回退 |
|---|---|---|---|
| `POLICY_LIFECYCLE` | 0 | 生命周期 8 条 API | 回 0 → 统一 404 |
| `POLICY_IMPORT` | 0 | 导入 5 条 API 与 CLI 写库 | 回 0 → 404 / CLI rc2 |
| `POLICY_WORKER` | 0 | outbox 消费者（向量/规则/KG/缓存级联） | 回 0 停止线程 |
| `POLICY_WORKER_RULE_COMPILE` | 0 | 消费者里的规则编译执行（消费 policy_rule_job） | 回 0 只入队不编译 |
| `POLICY_AUTOPILOT` | 0 | CLI/tick 是否允许写库（=1 才 apply） | 回 0 → 全部 dry-run |
| `POLICY_AUTOPILOT_WORKER` | 0 | app 启动时挂定时体检线程 | 回 0 不建线程不连库 |
| `POLICY_AUTOPILOT_STALE_HOURS` / `EXPIRING_DAYS` | 72 / 30 | 体检阈值 | 改数值 |
| `POLICY_SCOPE` | 0 | 运行期结构化范围过滤（候选出卡前） | 回 0 → 不调用，行为还原 |
| `POLICY_STATUS_FILTER` | **1** | 规则读侧按 `policy.status` 过滤停用/归档/删除 | 回 0 → 旧行为（本项默认开，属本轮唯一默认行为变化，已回归验证） |
| `SERVICE_SCOPE` | `jiangxi` | `national` 时外省不再一刀切 blocked，按数据如实回答 | 回 `jiangxi` |
| `REGION_NATIONWIDE` | 0 | 运行期用全国区划解析（江西词表之外） | 回 0 |
| `NATIONAL_READONLY` | 0 | 未收录地区短路为只读说明（0 卡 0 结论） | 回 0 |
| `REGION_ASK_GATE` | **1** | 地区无法解析到市/县区时追问（不是默认江西） | 回 0 |
| `REGION_FUNCTIONAL_ZONES` / `REGION_SOFT_FILTER` | 1 / 1 | 功能区归一、地区软过滤 | 回 0 |
| `KG_WORKER_IN_FULL_MODEL` | 0 | full_model 下启动图谱更新 worker | 回 0 |
| `POLICY_GOVERNANCE` | 0 | 治理 CLI 写库（另需 --confirm） | 回 0 → 只读 |
| `POLICY_VECTOR_TABLE` | `policy_vector_qwen3` | 向量读写表 | 改表名（白名单两表） |
| `OUTBOX_MAX_ATTEMPTS` | 3 | 事件重试上限，超限进 dead | 改数值 |
| `POLICY_QA_SNAPSHOT` | 1 | qa_log 附加政策版本/状态快照 | 回 0 |

> 改 env 必须重启进程；`kill -HUP` 不会重读 env。

## 4. 启用顺序（从零到可控上线）

1. **结构与回填**（一次性，只读先看）：
   `python deploy/migrate_policy_admin.py`（缺什么）→ `--apply`（建 outbox/import_batch/import_row/policy_scope 与 policy.deleted_at）；
   `python deploy/migrate_policy_scope.py --stats`（已有 15493 行，无需重跑）。
2. **只读能力**（不改问答行为）：`POLICY_SCOPE=1` 观察事件流 `scope.filter` 的保留/排除计数；必要时回 0。
3. **管理 API 与前端**：`POLICY_LIFECYCLE=1` + `POLICY_IMPORT=1`，打开 `/admin`，先 dry-run/预览，再对单条政策试停用→恢复。
4. **级联消费者**：`POLICY_WORKER=1`（先 `--once --json` 看 dead/retry），确认向量重建、规则失效入队、KG 同步、缓存清理。
   向量重建依赖嵌入服务（8095），批量操作前务必限速。
5. **规则编译**：`POLICY_WORKER_RULE_COMPILE=1`（消费 policy_rule_job）；先在灰度或 `--limit` 小批量。
6. **自动管理**：优先 cron 模板（单实例）或单独实例开 `POLICY_AUTOPILOT_WORKER=1`；
   先只用 dry-run，确认报告后再逐任务开 `POLICY_AUTOPILOT=1`。
7. **全国化灰度**：`deploy/gray_up.sh`（127.0.0.1:5001，SERVICE_SCOPE=national/REGION_NATIONWIDE=1/NATIONAL_READONLY=1），
   用 `qa_log` 快照对比江西案例无变化；无全国数据时外省只会得到「未收录」说明，不产生资格结论。
8. **全国数据接入**：把同事/共享数据放入 `data/incoming/` → `python deploy/national_intake.py --file ... --dry-run` → `--apply`（需 POLICY_IMPORT=1）。

## 5. 生产验证证据（2026-11-08）

- 结构现状（只读）：policy 3337（在用 3333/停用 4）、policy_detail 9403、policy_scope 15493；
  outbox 表与 policy.deleted_at 均**不存在**（默认开关下无副作用）；无 policy_worker / autopilot 线程。
- HUP 回归（2026-11-08 16:25 kill -HUP 361754，新 worker 984754，提交 fa9d956）：
  | 案例 | qa_id | 耗时 | 卡片 | 事件 | 结论 |
  |---|---|---|---|---|---|
  | 莲花县 eligibility | 693 | 3.8s | 7（S-ZB-090/091、S-ZF-008、X-CY-301、X-ZB-300/301/462） | 43（用户可见 23） | 与改造前一致，answer 1148 字，delta 拼合 true |
  | 青山湖区 consultation | 694 | 13.6s | 1（S-QJ-043 雨露计划） | 38（21） | 一致，578 字 |
  | 株洲市（不带省） | 695 | 3.1s | 0 | 27（16） | region.gate true、1 条地区追问、156 字，未按江西库匹配 |
  qa_log 已带 `policy_snapshot`（qa-policy-snapshot-1，7/1 条政策版本与状态），无 error 帧。
- 默认开关侧验证：`/admin` 200；生命周期/导入接口在开关默认 0 时返回 404 `policy_lifecycle_disabled`/`policy_import_disabled`；
  policy 仍 3337（在用 3333/停用 4）；`outbox` 表与 `policy.deleted_at` 均未创建（默认关零副作用）。
- 只读全国灰度（5001）：外省未收录地区 0 卡 + 只读说明、地区追问 1 条、江西案例不变（qa 684/685/686，见 docs/25）。
- 数据治理与区域审计只读跑通：空壳 40、重复 917 组/冗余 2513 行、跨级同名 145、scope 不一致 0，
  `--apply` 未在生产执行（等人工决策）。

## 6. 已知缺口（代码侧已无阻塞项）

| 优先级 | 缺口 | 影响 | 建议 |
|---|---|---|---|
| P0 | **全国政策数据源未接入** | `SERVICE_SCOPE=national` 只能如实说「未收录」，无法真正全国匹配 | 拿到数据后放 `data/incoming/`，走 `national_intake` dry-run→apply |
| P1 | national 端到端 grader 仍把「明确外省」判 D 硬拦（`src/policy_core/grader.py` explicit_other） | 只读全国模式已绕过；有全国数据后需放开 | 改为「无数据→只读说明」分支并补测试 |
| P1 | 存量数据质量：5 个城区无明细（东湖/西湖/青云谱/青山湖/浔阳）、40 条 S 级空壳、917 组重复（冗余 26.7%） | 覆盖空洞与重复卡片 | `deploy/policy_governance.py` 归档空壳可干跑；合并需人工；5 城区需补数据 |
| P2 | KG worker 在 full_model 下默认不启动（`KG_WORKER_IN_FULL_MODEL=0`） | 政策变更不会自动同步图谱 | 灰度后开 1；注意 KG 发布链路的嵌入成本 |
| P2 | 规则编译执行器默认关（`POLICY_WORKER_RULE_COMPILE=0`） | 停用/导入后规则只失效不重建 | 先 `--limit` 小批量验证再常开 |
| P2 | 向量重建依赖嵌入服务，批量导入会打满 | 导入/大批停用期间问答可能变慢 | 分批 + 限速；监控 /health |
| P3 | 前端管理页只覆盖检索/生命周期/导入，不含治理与自动管理面板 | 运维仍用 CLI | 后续按需扩展；CLI 已是稳定入口 |

## 7. 运维备忘

- 迁移：`deploy/migrate_policy_admin.py --apply` 建结构；之后再启用 API/worker，避免每个写请求反复 DDL。
- 备份：批量停用/导入前备份 `policy`/`policy_detail`；向量有 `vector_safety` 自动备份（`src/vector_backups/`）。
- 回滚：任何开关回默认值即回到旧行为；`lifecycle.restore` 恢复误归档；导入批次可 `--rollback`（批次后无新版本时）。
- 观测：问答事件流含 `scope.filter`/`region.gate`/`region.coverage`；outbox 计数 `outbox.counts(conn)`（pending/done/dead）。
- 测试门禁：任何改动先跑全量 904 例；开关默认值变更必须同时改测试与本文档。
