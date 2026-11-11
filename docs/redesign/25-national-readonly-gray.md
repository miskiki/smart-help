# 25 只读全国模式与单独灰度（实施记录）

状态：已实现并已在 127.0.0.1:5001 灰度验证；生产 5000 保持江西口径（开关默认关）。
提交：77f5d3e（只读短路）、fab82a7（全国模式属地判定）、ffd70b9（灰度脚本与方案）。

## 1. 目标

用户要求（m05065）：全国数据从同事/共享获取，**开启只读模式**，先接入、再单独灰度、再补剩余项。

本轮把"只读"落成可验证的运行期能力：

- 家庭所在地区在政策库中**没有任何收录条目**时，系统如实说明"政策库还没有收录该地区政策"，
  **不生成政策卡片、不作资格判断、不默认套用江西省政策**。
- 收录判断是数据驱动的（读 `policy_scope` 结构化范围表），不是按省名写死。
- 探测失败/地区解析不明一律**放行**（绝不因探测问题误判"未收录"）。

## 2. 实现

| 文件 | 作用 |
|---|---|
| `src/policy_core/coverage_probe.py` | `coverage(region_text)`：`admin_division.canonical` 解析 → 构造祖先键（`CN` / `P:省` / `C:省/市` / `X:省/市/县` / `Z:功能区`）→ 只读统计 `policy_scope` 行数与政策数；表缺失/查询异常 → `covered=None`；`summary()` 输出中文边界说明 |
| `src/policy_core/full_flow.py` | `national_readonly_enabled()`（`NATIONAL_READONLY`，默认 0）、`_region_coverage()`（异常→None 放行）、`_readonly_no_data_advice()`（确定性文案，只陈述收录事实）、`run()` 中 `region_ask` 之后短路：`covered is False` → 0 卡 0 问并记录 `coverage.readonly_national/no_policy_data/region_coverage` |
| `src/events.py` | 新事件 `region.coverage`（标题"地区政策收录检查"，用户可见），文案：`政策库暂无该地区的政策条目`；`region.gate` 文案按 `SERVICE_SCOPE` 区分"江西省服务范围"/"服务范围" |
| `src/policy_core/result_model.py` | `no_policy_data` 时不再列空检索事实（避免正文自相矛盾） |
| `src/policy_core/region.py` | 全国模式下 `explicit_other()` 一律 False：外省家庭不再预判"明确不适配"，改由政策自身适用范围判定（政策只覆盖别处仍 D 档不出卡） |

## 3. 开关与回退

| 开关 | 默认 | 作用 | 回退 |
|---|---|---|---|
| `NATIONAL_READONLY` | 0 | 只读全国模式短路 | 置 0 / 删除 |
| `SERVICE_SCOPE` | jiangxi | `national` 时地区口径全国化（service_scope_check 外省不再 blocked） | 置 `jiangxi` |
| `REGION_NATIONWIDE` | 0 | 江西词表解析不出时回落全国行政区划层 | 置 0 |
| `POLICY_SCOPE` | 0 | 用 `policy_scope` 结构化范围三态过滤候选政策 | 置 0 |

生产 `deploy/web.env` 未改动：以上全部默认关 → 生产行为零变化。

## 4. 单独灰度（5001）

- 启动：`cp deploy/web.env deploy/web.gray.env && cat deploy/web.gray.env.example >> deploy/web.gray.env && bash deploy/gray_up.sh`
- 状态：`bash deploy/gray_status.sh`；停止：`bash deploy/gray_down.sh`
- 隔离：bind 5001，脚本拒绝 bind 5000、拒绝操作生产 master；`deploy/web.gray.env` 已加入 `.gitignore`。
- 只读守卫：`NATIONAL_READONLY=1` 且 `POLICY_LIFECYCLE/IMPORT/AUTOPILOT/WORKER=0`，否则拒绝启动。

## 5. 灰度实测（真 HTTP `/api/qa/stream`，admin JWT）

| 案例 | 结果 |
|---|---|
| 莲花县（江西，有县级政策） | 8.8s / 200 / 7 卡 `S-ZB-090,S-ZB-091,S-ZF-008,X-CY-301,X-ZB-300,X-ZB-301,X-ZB-462` / 1148 字 / 24 用户事件（qa 684）；`scope_filter`：input 7 / kept 6 / dropped 0 / no_scope 1 |
| 青山湖区（江西，无县级政策） | 38.7s / 200 / 1 卡 `S-QJ-043` / 578 字 / 22 事件（qa 685） |
| 株洲市不带省（湖南，库中无收录） | 4.9s / 200 / **0 卡 0 追问**/ 115 字（qa 686）：`coverage.readonly_national=true`、`no_policy_data=true`，正文为收录说明并明确"不作资格判断" |

灰度实例加载当前工作树代码；与生产同一 policy 库（只读 SELECT）。已确认灰度未在生产库新建 `outbox`/`import_batch` 表，`policy.deleted_at` 未新增。

## 6. 已知边界

- 灰度提真实问题仍会写生产认证库 `qa_log/conversation/qa_trace`（与开关无关，属既有副作用）。
- 功能区（开发区）家庭若只有 host 县区政策、无精确功能区收录 → `covered=False`（保守语义：功能区只覆盖自己）。
- `policy_scope` 未回填的库 → `covered=None` 放行，界面上显示"暂不能判断"而不是"未收录"。
- 真接入全国数据仍需数据源：本机未找到同事/共享的全国政策文件，等待提供路径或放入 `data/incoming/`。

## 7. 下一步（按优先级）

1. 全国数据接入（`deploy/national_intake.py`，见 docs 22）→ dry-run 报告 → 灰度 commit。
2. KG 图谱 worker 在生产 full_model 下启动（`KG_WORKER_IN_FULL_MODEL`，见 docs 26 任务）。
3. 数据治理 dry-run（5 城区缺口 / 40 空壳 / 917 重复组，docs 24）。
4. 前端政策管理页（生命周期/导入 API 已就绪，默认关）。

## 8. 复测（2026-11-08 15:00，604074c 后重新部署）

重新拉起灰度（pid 954787）并做生产 HUP（master 361754 → 新 worker 955446），把 T26/T27/T28/T29 与导入
效期修复一并上线（除 `POLICY_QA_SNAPSHOT` 默认开外，其余新开关仍默认关）。

| 环境 | 案例 | 结果 |
|---|---|---|
| 灰度 5001 | 莲花县 | 9.0s / 200 / 7 卡 / 1148 字 / 24 用户事件（qa 687） |
| 灰度 5001 | 青山湖区 | 40.0s / 200 / 1 卡 `S-QJ-043` / 578 字 / 22 事件（qa 688） |
| 灰度 5001 | 株洲市不带省 | 5.0s / 200 / 0 卡 0 追问 / 115 字（qa 689），`no_policy_data=true` |
| 生产 5000 | 莲花县 | 8.7s / 200 / 7 卡 / 1148 字 / 23 用户事件（qa 690） |
| 生产 5000 | 青山湖区 | 39.6s / 200 / 1 卡 `S-QJ-043` / 578 字 / 21 事件（qa 691） |
| 生产 5000 | 株洲市不带省 | 5.6s / 200 / `region_gate=true` / 1 追问 / 156 字（qa 692） |

行为与改动前逐项一致；唯一新增是回答快照：qa 690 的 `policy_snapshot.items` 7 条、qa 691 1 条，
`schema_version` 仍为 2；qa 692 无引用编号 → 不写该键（设计如此）。生产库仍无
`outbox`/`import_batch` 表、`policy.deleted_at` 未新增。