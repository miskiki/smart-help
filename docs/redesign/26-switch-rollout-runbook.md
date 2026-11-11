# 26 政策生命周期/全国化开关启用顺序与回滚手册

状态：实施手册（2026-11-08）。配套实现见 docs/20（设计）、docs/21（后端实施记录）、docs/25（只读全国模式灰度）。
所有写能力默认关闭；本手册给出"按什么顺序打开、每步怎么验证、怎么一键回退"。

## 1. 当前生产状态（事实）

| 项 | 状态 |
|---|---|
| 生产进程 | gunicorn master 361754（--bind 127.0.0.1:5000 --workers 1 --worker-class gthread --threads 2 app:app） |
| 灰度实例 | 127.0.0.1:5001（脚本 `deploy/gray_up.sh|gray_status.sh|gray_down.sh`，env `deploy/web.gray.env`，已 gitignore） |
| `policy_scope` | 已回填 15493 行（2026-11-08），读侧开关 `POLICY_SCOPE=0` |
| `POLICY_STATUS_FILTER` | 1（默认开）：规则读侧按 policy.status 过滤停用/归档/删除 |
| `POLICY_QA_SNAPSHOT` | 默认开：回答快照附政策版本/状态（纯附加，已生产只读验证） |
| `POLICY_WORKER` / `POLICY_LIFECYCLE` / `POLICY_IMPORT` / `POLICY_AUTOPILOT` | 0（关） |
| `POLICY_WORKER_RULE_COMPILE` | 0（关；单县约 6 分钟 GPU） |
| `KG_WORKER_IN_FULL_MODEL` | 0（关；`kg_update_queue` 现存 2 行 status='ready' 是已处理完成的历史记录，不是待办） |
| `SERVICE_SCOPE` / `REGION_NATIONWIDE` / `NATIONAL_READONLY` | jiangxi / 0 / 0（生产江西口径） |

注意：改 `deploy/web.env` 后 HUP **不会**重新读取（master 的环境在启动时固定），需按现有部署方式完整重启进程。

## 2. 启用顺序（每步独立可回退，前一步验证通过再开下一步）

### 阶段 A：读侧零风险（已生效）
1. `POLICY_STATUS_FILTER=1`（默认开）：停用政策不再出卡。验证：`policy_status_map()` 正常、莲花/青山湖案例卡片与基线一致。
2. `POLICY_QA_SNAPSHOT`（默认开）：`qa_log.match_json.policy_snapshot` 出现 items/missing；0 卡回答不写该键。回退：置 0。

### 阶段 B：后台消费者（不改变回答行为）
3. `POLICY_WORKER=1`：outbox 消费者线程（`policy_worker.start()`，app.py 末尾）。首次启动幂等建 `policy_outbox` 表。
   验证：`SELECT status,count(*) FROM policy_outbox GROUP BY status` 只有 done；日志出现 `policy-worker` 线程；无 lifecycle 入口时表应为空。
   回退：置 0 重启（线程不再启动，已有表保留、无副作用）。
4. `KG_WORKER_IN_FULL_MODEL=1`（可选，与 3 独立）：full_model 下启动图谱更新 worker。
   前置：确认 `kg_update_queue` 无 pending/failed 积压；先只读跑 `deploy/kg_source_update` dry-run（`apply=False`）看报告。
   风险：图谱重建长时间持 `policy/policy_detail/kg_*` 表锁并整表重写；必须在低峰、单 worker 窗口执行。
   回退：置 0 重启。

### 阶段 C：管理 API（人可写，先灰度）
5. `POLICY_LIFECYCLE=1`：开放 suspend/archive/restore/delete/publish/rollback + impact/versions。
   前置：阶段 B 的 worker 已开（否则状态变更只入 outbox 不生效）。
   验证（灰度实例 5001）：对一条**已停用**的历史政策试 `GET impact`（只读）；再试 suspend → 观察 outbox 4 事件、policy.status、`policy.deleted_at`；
   worker drain 后确认向量表 qwen3 中该 code 0 行（停用→remove_policy）。
   回退：置 0 重启（API 返回 404 policy_lifecycle_disabled；已改的 status 需人工按 audit_log 反向操作）。
6. `POLICY_IMPORT=1`：开放导入预览/提交/回滚。
   验证顺序：`--offline` 校验 → 连库 dry-run（对照 `deploy/import_policies.py --dry-run`）→ 小样本 `--apply`（1–3 条）→ 检查 import_batch/import_row 与 outbox → 全量分批。
   回退：`POST /api/policies/import/<batch_id>/rollback`（仅 committed 且批次后无新版本）；开关置 0 重启。
7. `POLICY_WORKER_RULE_COMPILE=1`：常驻规则编译器（消费 `policy_rule_job`）。
   前置：GPU 低峰、确认无在线推理排队；每轮 1 个县（`POLICY_WORKER_RULE_COMPILE_BATCH=1`）、间隔 60s。
   验证：`SELECT region,status,count(*) FROM policy_rule_job GROUP BY 1,2`；编译后该县 `policy_rule` 行数与版本变化。
   回退：置 0 重启（job 保持 pending/failed，不丢）。
8. `POLICY_AUTOPILOT=1` + 定时（cron/systemd，call `deploy/policy_autopilot.py all --apply`；或 `POLICY_AUTOPILOT_WORKER` 常驻）。
   先 `--dry-run` 看报告；apply 只做 status 变更（expire-scan）与入队，不级联向量/规则（需 worker 配合）。
   回退：不跑即可；已改 status 按 audit_log 反向。

### 阶段 D：全国化（需要数据先到）
9. `POLICY_SCOPE=1`：结构化范围三态过滤（drop 明确不适用的候选、unknown 保留待核）。
   前置：`policy_scope` 回填完成且自洽（docs/25 §5 已验证 654 次自 match 全 True）。
   灰度观察：`coverage.scope_filter={input,output,kept,dropped,no_scope}`；dropped 非 0 时抽查被丢政策（应为外地/功能区专属）。
   回退：置 0（不过滤）。
10. `SERVICE_SCOPE=national` + `REGION_NATIONWIDE=1`（可选再开 `NATIONAL_READONLY=1`）：地区口径全国化 / 未收录如实说明。
    前置：真实全国数据已导入（`deploy/national_intake.py`），且 grader 外省判定已在 national 下验证。
    回退：改回 jiangxi / 0。

## 3. 一次会话内的最小验证清单（任何阶段）

```bash
# 1) 开关与线程
python -c "from policy_core import policy_worker as w; print(w.enabled(), w.rule_compile_enabled())"
# 2) outbox 健康
psql ... -c "SELECT status,count(*) FROM policy_outbox GROUP BY 1"   # 无 psql 时用 deploy/policy_autopilot.py outbox-drain --dry-run
# 3) 回答链路（真 HTTP，admin JWT）
/tmp/live_check_gate.py       # 生产 5000
/tmp/live_check_gray.py       # 灰度 5001
# 4) 库结构未被意外改写
python -c "from sqlalchemy import inspect; from kb_rag import _db; print(sorted(t for t in inspect(_db()).get_table_names() if 'outbox' in t or 'import_batch' in t))"
```

## 4. 回退矩阵（按风险从低到高）

| 开关 | 回退动作 | 是否需要重启 | 已写数据是否需要修 |
|---|---|---|---|
| POLICY_QA_SNAPSHOT | 置 0 | 是 | 否（历史快照保留，无害） |
| POLICY_STATUS_FILTER | 置 0 | 是 | 否 |
| POLICY_WORKER | 置 0 | 是 | 否（outbox 表留着） |
| KG_WORKER_IN_FULL_MODEL | 置 0 | 是 | 否（图谱已发布则保留） |
| POLICY_LIFECYCLE | 置 0 | 是 | 是（人工按 audit_log 反向 status） |
| POLICY_IMPORT | 置 0 / 批次 rollback | 是 | 是（用 rollback 或按 import_row 反向） |
| POLICY_WORKER_RULE_COMPILE | 置 0 | 是 | 否（job 留着） |
| POLICY_SCOPE | 置 0 | 是 | 否 |
| SERVICE_SCOPE / REGION_NATIONWIDE / NATIONAL_READONLY | 改回默认 | 是 | 否 |

## 5. 已知依赖与阻塞

- 向量重建依赖本地嵌入服务（8095）与 `policy_vector_qwen3` 表（P0 断电已修复，见 docs/21 §2）；worker 与检索共享资源，灰度需限速。
- `handle_rule_invalidate` 只把县 `rule_store.enqueue(force=True)`，真正编译依赖阶段 C-7 的编译器。
- 全国数据源缺失：本机无同事/共享的全国政策文件；接入前阶段 D 无法开始（见 docs/22、docs/25 §6）。
