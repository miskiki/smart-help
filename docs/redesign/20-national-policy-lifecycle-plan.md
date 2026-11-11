# 20 全国化与政策生命周期管理设计方案

状态：实施中（P1 后端已落地，默认关闭待灰度；实施记录见 21）  日期：2026-11-08  基线：9375d3c
依据：[17 覆盖量审计](17-policy-coverage-audit.md)、[18 生命周期代码审计](18-policy-lifecycle-code-audit.md)、[19 地区模型审计](19-region-model-audit.md)
用户目标（m04114）：系统不得默认江西；政策按地区纵向/横向全国适配；支持删除旧政策、导入新政策的后端；内部政策管理具备智能化自动管理方案，降低高耦合。

## 1. 现状一句话

当前库是「江西省县级政策工具箱」：3337 条政策、9403 条明细全部在江西，0 全国行、0 市层级、0 外省；5 个城区无数据；26.7% 明细重复；时效/版本字段基本不可用；没有删除、没有导入管线、没有自动巡检；向量链路写读两张表，生产导入不生效；KG/规则 worker 在生产不启动。代码侧江西词表硬编码 12 处以上、默认值 6 处（17/18/19 详列）。

## 2. 总体架构（低耦合四层）

| 层 | 职责 | 真源/产物 | 是否同步主链路 |
|---|---|---|---|
| L1 地区归一 | 文本→结构化行政区划；关系判定 | admin_division（全国数据）+ policy_scope 表 | 运行期只读，可开关 |
| L2 政策数据 | 政策/明细/版本/状态/地区范围 | policy、policy_detail、policy_version、policy_scope | 是 |
| L3 生命周期 | 增删改停复、版本发布、批量导入 | lifecycle 服务 + import 管线 + outbox | 写操作，事务化 |
| L4 自动管理 | 过期扫描、outbox 消费、质量巡检、覆盖报告 | policy_autopilot（CLI + worker 可选） | 异步，幂等可重跑 |

解耦原则：**任何写操作只写业务表 + outbox 事件，不直接同步向量/规则/KG**；消费者幂等重放。这样导入/停用/删除都不依赖运行期服务在线。

## 3. 地区模型（全国化 + 纵向深度）

### 3.1 唯一判定真源

- `admin_division.canonical(text)`：返回 {level, province, city, county, ambiguous, zone, matched, national}；等级 national/province/city/county/unknown；功能区（经开区/新区/管委会/风景名胜区…）按县区级、归设区市。
- `admin_division.relation(policy_region, household_region)`：ancestor/same/descendant/disjoint/unknown，applicable=True 仅 ancestor/same。
- `policy_scope`：每条政策/明细的地区范围落一行（PK policy_code+detail_id+region_key），运行期用 `match()` 做并集语义（省政策对县家庭 applicable，县政策对省家庭 unknown=待补充，功能区与同市其他区县 unknown=待补充而非直接排除）。
- 旧 `policy.regions` 文本字段保留（兼容读），写侧双写；`policy_scope` 是查询真源。

### 3.2 纵向层级策略

- 全国→省→市→县区→功能区；乡镇/街道本期不建维度（文本级匹配，命中区县即可）。
- 直辖市：省→「市辖区/县」伪地级→区县；省直辖县级行政区划同理；解析结果 city=None 时不强行编造层级。
- 歧义（如「朝阳区」）：`ambiguous` 非空 → level=unknown，触发追问带省份限定，不猜测。
- 只到省的家庭：服务范围校验通过但政策匹配需要市级/县级 → 由追问门（REGION_ASK_GATE 扩展）要求补全，绝不按省会默认。
- 功能区：独立县区级口径；同设区市的其他区县家庭遇到功能区政策 → 保留待核（不误放行、不误丢）。

### 3.3 运行期接入与开关

- `POLICY_SCOPE=1`：检索/评分阶段用 policy_scope.match 参与地区判定；0 保留旧 region.py 逻辑（默认 0，灰度后切 1）。
- `SERVICE_SCOPE=jiangxi|national`：jiangxi（默认）保持现网外省 blocked；national 下外省不再一刀切 blocked，无数据时如实返回「该地区暂无政策」并可追问。
- `REGION_NATIONWIDE=0|1`：家庭地区解析在江西词表之外回落到 admin_division；SERVICE_SCOPE=national 时强制 1。
- 修复 M1：家庭只到省 → ask 追问市/县区全称；M3：程序层不确定时不得回落旧层硬判「不适」；M10：删除 DEFAULT_POLICY_REGIONS 的江西省默认。
- 回退：POLICY_SCOPE=0、SERVICE_SCOPE=jiangxi、REGION_NATIONWIDE=0 即回旧行为，不改卡片数以外的任何语义。

## 4. 生命周期（L3）

### 4.1 状态机

`draft → active → suspended → archived → deleted(软删)`；允许 `suspended→active`（恢复）、`archived→active`（恢复）、`deleted→active`（管理员恢复，需二次确认）。`draft` 只来自导入/人工创建，未发布不参与检索。所有流转写 audit_log + outbox（policy.activated/suspended/archived/deleted/restored/published）。

### 4.2 版本链

- 每次内容变更（导入更新、人工编辑、批量修正）生成 policy_version 新行：version_no 递增、snapshot(JSON)、status、effective_at、operator、batch_id；cur_version 指向当前版本。
- 发布/回滚：`publish(code, version_no)`、`rollback(code, version_no)` 事务内切换 cur_version 并写 outbox。
- 历史问答不追溯改写；qa_log 现有 match_json 保留，新增可选 policy_snapshot 字段本期不做。

### 4.3 删除/停用/恢复

- 停用（suspend）：status=suspended，保留数据与 scope，检索排除；向量/规则打失效标记（outbox 消费）。
- 归档（archive）：status=archived，从管理列表默认隐藏，检索排除。
- 软删（delete）：deleted_at + status=deleted，policy_scope 保留用于历史解释；级联：detail 标记、vector 删除事件、rule 失效事件、KG 失效事件；提供 restore。
- 硬删只允许「从未发布且无引用」的草稿，防误删。
- 影响检查：删除/停用前返回受影响 qa_log 数、规则数、向量数、明细数（dry-run 报告）。

### 4.4 API（bp url_prefix='/api'）

- `POST /api/policies/<code>/suspend|archive|restore|delete`（body: reason, operator）
- `GET  /api/policies/<code>/impact`（影响预检）
- `GET  /api/policies/<code>/versions`、`POST /api/policies/<code>/publish`、`POST /api/policies/<code>/rollback`
- `POST /api/policies/import`（multipart/JSON → 建批次，返回 batch_id + dry-run 报告）
- `GET  /api/policies/import/<batch_id>`（进度/差异/错误）
- `POST /api/policies/import/<batch_id>/commit`（确认落库）
- `GET  /api/policy-health/coverage`（全国/分省/分市覆盖与缺口）

## 5. 导入管线（L3）

1. **staging**：raw 行落到 import_batch/import_row（保留原始 JSON、行号、来源）。
2. **validate**：必填字段、policy_code 合法、区域可归一（unresolved 列出）、level/regions 一致、日期可解析；错误行不阻塞整批，产出报告。
3. **dry-run diff**：新增/更新/无变化/冲突（同 code 不同来源）/待停用（历史 code 未出现在批次时只提示不自动停用）。
4. **commit**：单事务按 policy_code 幂等 upsert；每 policy 生成新版本；写 policy_scope（replace_policy）；写 outbox（vector.rebuild/rule.invalidate/kg.sync）；更新 batch 状态。
5. **回滚**：批次按 batch_id 撤销（仅未发布批次或管理员指定），复用版本链。

支持的输入：Excel/CSV（沿用现有 /upload 解析器结构）、JSON 行数组。批次表：import_batch(id, source, file_name, total, valid, invalid, status, dry_run, created_by, created_at, committed_at)。

## 6. 自动管理（L4）

`deploy/policy_autopilot.py`（默认 --dry-run，--apply 才写）：

- `expire-scan`：effect_date/expire_date 过期/临近（T-30）扫描，产出建议停用清单。
- `outbox-drain`：消费 outbox（向量重建、规则失效、KG 同步、缓存失效），幂等重试 + 死信表。
- `health-check`：空壳政策（0 明细）、无 scope、重复组、无版本、无规则、孤儿明细。
- `coverage-report`：全国/分省/分市/分县覆盖与缺口，输出 JSON。
- `rule-requeue`：规则缺失县区补队（复用 rule_worker enqueue）。
- `--apply --limit N` 控制批量；每项输出 before/after 计数。

可选常驻 worker：`POLICY_AUTOPILOT_WORKER=1` 时 app.py 启动后台线程，默认关闭。

## 7. 开关与回退矩阵

| 开关 | 默认 | 作用 | 回退 |
|---|---|---|---|
| POLICY_SCOPE | 0 | 运行期使用 policy_scope 判定 | 0=旧逻辑 |
| SERVICE_SCOPE | jiangxi | 服务范围（江西/全国） | jiangxi |
| REGION_NATIONWIDE | 0 | 家庭地区全国解析 | 0 |
| POLICY_LIFECYCLE | 0 | 新生命周期 API/软删生效 | 0=旧行为 |
| POLICY_IMPORT | 0 | 导入管线 API | 0=停用入口 |
| POLICY_AUTOPILOT | 0 | 自动管理任务/worker | 0 |

所有开关读 env，修改需重启进程（HUP 不重读 web.env）。

## 8. 分阶段实施

- **P0（本迭代）**：①policy_scope 回填与查询（done）；②运行期全国化：admin_division 接入 region.py 回落 + service_scope 配置化 + M1/M3/M10 修复；③生命周期服务与 API（停用/归档/软删/恢复/影响预检）；④导入管线（staging→validate→dry-run→commit）；⑤outbox + 自动管理骨架；⑥文档与单测门禁。
- **P1**：全国数据接入试点（1–2 省，真实导入）、过期/替代关系、质量门、coverage 报表进管理台、worker 常驻。
- **P2**：乡镇/街道维度、跨省政策迁移、问答历史版本快照、自动采集与政策变更发现。

## 9. 验收标准

- 单测：新增模块单测 + 全量 discover 全绿（当前基线 370）。
- FP=0：老链路开关关闭时行为与 9375d3c 完全一致（同一批 case 卡片集合/档位不变）。
- 真机：导入批次 dry-run 报告正确、commit 幂等（二次 commit 0 变更）、停用后检索不再出卡、恢复后复现。
- 生产：HUP 后规则路径回归（莲花/青山湖/株洲）卡片集合与事件数一致；新 API 只读端点 200。
- 运维：autopilot --dry-run 输出可读、--apply 幂等、失败可重试。
