# 规则路径可用性：按编译状态与省级规则出结果（契约与验收）

状态：已上线（2026-11-07，开关 `RULE_PROVINCE_ONLY`，默认 1，可回退旧门槛）
代码：`src/policy_core/rule_readiness.py`（新增）、`src/policy_core/rule_flow.py`、`src/rule_worker.py`、`src/events.py`
测试：`tests/test_rule_readiness.py`（10 例）

## 1. 一句话结论

规则引擎是否"可以出结果"从**固定阈值**（县级规则 ≥ 20 条）改为**按编译任务状态判定**：编译已完成（`done`）时，即使该县区没有县级政策、只有省级通用规则，也允许走规则路径并明确标注"本轮按省级通用政策核验"。这样没有县级政策数据的县区不再永久回退到 100–240s 的全模型链路。

## 2. 问题与根因

11-07 巡检记录（`src/eval/巡检记录_20261107.md`）里唯一的实质异常：青山湖区 8 次真实提问全部 `rules_not_ready`，回退旧链路，单次 144–238s。

根因不是编译 bug，而是**数据缺口 + 门槛叠加**：

- `policy_detail` 没有任何青山湖政策（`select count(*) where region like '%青山%'` = 0），编译任务 `policy_rule_job` 记录 `done 0/0`；
- `rule_flow.try_rule_run` 旧门槛只统计 `region_scope` 为本县或 `*` 之外的规则，**省级规则 39 条永远不计入 `local`** → `local < 20` 恒成立 → 每轮都 `enqueue` 并回退；
- 回退后的全模型链路要做完整检索 + 逐条核验，同一县区每次 2–4 分钟。

同样的死循环也会发生在任何"政策库暂未覆盖"的县区，属于系统能力问题，不是单县问题。

覆盖范围实测（`policy_detail` 9403 行，对 `policy_core.geo.JIANGXI_COUNTY` 的 100 个县区做县名比对）：95 个县区有县级政策，**5 个没有**——南昌市东湖区、西湖区、青云谱区、青山湖区，九江市浔阳区。这 4 个南昌老城区 + 1 个九江城区在本次改造前都属于"永久回退"集合。

## 3. 可用性状态机（deep module）

```python
# src/policy_core/rule_readiness.py
MIN_LOCAL = 20
enabled()                                  # RULE_PROVINCE_ONLY 未显式关闭
assess(rules, job=None, min_local=MIN_LOCAL) -> dict(ready, state, local, province)
```

| state | 条件 | 行为 |
| --- | --- | --- |
| `ready` | 县级规则 ≥ `min_local` | 正常规则路径（行为不变） |
| `ready_sparse` | 编译 `done` 且 0 < 县级规则 < `min_local` | 走规则路径（新增：不再回退） |
| `ready_province_only` | 编译 `done` 且县级规则 = 0，省级规则 > 0 | 走规则路径 + 正文标注"暂无县级政策，按省级通用政策核验" |
| `no_rules` | 连省级规则都没有 | 回退（与旧行为一致） |
| `pending` / `running` / `failed` | 编译任务未完成或失败 | 回退 + 入编译队列（与旧行为一致） |
| `disabled` | `RULE_PROVINCE_ONLY=0` | 旧门槛（`local >= min_local` 才算就绪） |

纯函数、无副作用；`job` 只读 `policy_rule_job` 的一行。

## 4. 接入点

- `rule_flow.try_rule_run`：`job = rule_store.job(county)` → `readiness = rule_readiness.assess(rules, job)`；未就绪时入队 + `coverage['rule_engine'] = {state: rules_not_ready, county, local_rules, readiness}` + 事件 `rule.not_ready`；就绪时 `coverage['rule_engine'] = {state: used, county, rules, cards, eval_seconds, readiness, local_rules, province_rules}`。
- `rule_flow.render_rule_mode`：`readiness == ready_province_only` 时，在正文最前插入固定提示"该县区在政策库中暂无县级政策，本轮按省级通用政策核验；县级政策补充入库后会自动纳入。"（claims 与 classic 两种版式都插）。
- `rule_worker.ensure_rules`：与主链路同一口径（`assess`），避免"worker 认为没就绪、主链路认为就绪"两套判断。
- `events.py`：事件白名单增加 `readiness`、`province_rules`，`rule.engine.loaded` 的 detail 带 `readiness/local_rules`；新增顶层事件 `rule.not_ready`（phase=evaluate，exposure=user，标题"规则库未就绪（改用全模型核验）"），人话文案按状态映射：pending→等待编译、running→编译中、failed→编译失败、no_rules→无可用的省级规则。回退不再是"静默变慢"，而在时间线上明确可见。

## 5. 开关与回退

- `RULE_PROVINCE_ONLY=1`（代码默认，生产 web.env 未显式设置即生效）；显式 `=0` 恢复旧门槛（`local >= 20` 才算就绪）。
- 回退粒度是"判定"，不是"结果"：回退后青山湖等县区重新回到全模型链路，卡片与档位规则（P2/P3）不受影响。
- 注意：`deploy/web.env` 只在 gunicorn 启动时 source，`kill -HUP` 不重读；默认值写在代码里，所以本次无需改 web.env 也生效。

## 6. 验收记录

### 单元与门禁

- 全量单测：**274 tests OK**（含 `tests/test_rule_readiness.py` 10 例：阈值、四态、job 状态、开关回退、省级计数；`tests/test_events_v3.py::test_rule_not_ready_is_visible` 覆盖可见性）。
- 9 案例 P2 门禁（`tests/e2e_p2_gate.py`，真实链路）：**PASS，0 处失败**（`runtime/logs/p2_gate.json`）。
  - c1 13.5s / 2 卡 B（S-ZB-090/091）；c2 9.2s / 2；c3 9.2s / 2；c4 9.5s / 2；c5 13.9s / 5；c7 14.0s / 2；c8 14.4s / 8（B/C）；c9 17.1s / 3；
  - c6（无地区，全模型链路）191.8s / 2 卡 B（X-ZB-248/X-ZB-399）。
  - 出卡集合与 P2 基线一致，D 档未出卡，A 档引文断言通过。

### 省级-only 县区真机探针（`/tmp/probe_province_only.py`）

| 地区 | 耗时 | rule_engine | 卡片 |
| --- | --- | --- | --- |
| 江西省南昌市青山湖区（改造前永久回退） | **9.3s** | used / ready_province_only / local 0 + province 39 | 0 卡（通用 2 人健康家庭无相关条件） |
| 江西省萍乡市莲花县（对照组） | 9.3s | used / ready / local 79 + province 39 | 0 卡（hidden_d=1 condition_violated） |

### 真实历史提问 A/B（qa 551 原文）

提问："我是一名贫困农民的学生，我需要申请雨露计划，江西省，青山湖区，家里仅靠种田维持生活, 我想申请，应该怎么申请呢？"

| 链路 | 耗时 | 卡片 | 说明 |
| --- | --- | --- | --- |
| 历史生产（11-06，`rules_not_ready` 全模型） | 144–238s | 0 | 旧版式"全库模型分析"，无卡片 |
| 修复后规则路径 | **13.2s** | 2 卡 B（S-QJ-043 雨露计划、S-QJ-042 生源地信用助学贷款） | 正文含省级-only 提示 + 点名核验；卡片口径仍为"已满足部分条件、政策依据待补齐" |
| 全模型对照（`RULE_ENGINE=off`，本轮复跑） | 21.9s（证据缓存命中） | 0 | 明确写"雨露计划 证据校验未通过 / 证据链未闭合" |

结论：该县区从"永远慢链路且 0 卡"变成"13s、按省级规则给出带依据的部分条件卡"，且卡片仍受 P2 分档与四道拦截约束（B 档 = 证据绑定缺口，不作资格结论）。

### 生产 live（5000，HUP 重载后真实 HTTP）

- 青山湖 qa551 原文：**8.3s**、22 条用户事件、2 卡 B、`rule_engine.readiness=ready_province_only`、正文首段即省级-only 提示、7 段 answer delta 拼合与整段一致、回放 22 == 实时 22、trace 一致。
- 莲花回归（`/tmp/live_check_p1.py`）：eligibility **7.6s / 7 卡**、`rule_state=used`、23 用户事件、回放 23 == 实时 23；clarify 0.9s / 0 卡；均 PASS。
- 复测波动说明：同日负载升高（load avg ≈ 4.6，llama-server 有并发客户）时，同一青山湖请求复测为 50.9s（23 用户事件、1 卡 C、回放 23 == 实时 23、delta 拼合一致），莲花 eligibility 28.8s；事件序列与 route/rule_state 完全一致，耗时差异来自共享模型服务排队与抽取非确定性（卡片档位随抽取事实在 B/C 间波动），不是规则路径引入的额外调用（规则路径模型调用数不变）。

## 7. 已知边界

- **数据缺口仍在**：青山湖等县区 `policy_detail` 无县级政策，本轮只是让系统不再被门槛卡死；补齐县级政策（收录/编译）后会自动从 `ready_province_only` 变 `ready`，卡片范围随之扩大。
- **省级规则只覆盖省级政策**：县级-only 政策在这些县区不会出卡（以前也不会，只是以前 0 卡是"超时后放弃"）。属"少卡"而非"错卡"，符合 FP=0 红线。
- **首轮新县区仍回退**：编译任务第一次入队时 `pending/running`，该轮走全模型链路（可接受：后续轮次自动切换）。
- `ready_sparse`（1–19 条县级规则）从"永远回退"改为"走规则路径"，属行为扩大；当前库内无处于该区间的地区（吉安 129、万安 87、莲花 79、新建 59、于都 58、`*` 39）。
- 省级-only 县区的县级政策缺口应进入数据侧待办（收录/编译），不能只靠本轮判定兜底。待办清单：东湖区、西湖区、青云谱区、青山湖区（南昌市）、浔阳区（九江市）——当前 `policy_detail` 无县级政策，说明这些城区的政策主要在本级/省级，需确认是否真有县级政策可收录；若确认没有，则"省级通用"就是正确口径。
- 城区用户的问答历史在 `qa_log` 里很少（5 个县区仅有 1 条历史提问 qa 551），巡检里"青山湖 8 次"来自当日实时流量；上线后新流量会自动进入 `ready_province_only` 快路径，可用 `coverage.rule_engine.readiness` 统计分布。
