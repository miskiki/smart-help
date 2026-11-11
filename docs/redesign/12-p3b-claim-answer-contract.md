# P3-B：claim 驱动三段式回答（契约与验收）

状态：已上线（2026-11-07，开关 `ANSWER_STYLE`，默认 claims，可回退 classic）
代码：`src/policy_core/claim_answer.py`（新增）、`src/policy_core/rule_flow.py`（clause_quote + 规则路径接入）、`src/policy_core/result_model.py`（全模型路径接入）
测试：`tests/test_claim_answer.py`（16 例，含开关/结构/整合）

## 1. 一句话结论

回答正文从“模型自由散文 + 卡片行列表”改成**确定性三段式**：先给结论边界，再逐条列已核实条件与已绑定依据，最后列待补齐项。所有依据只来自已核验行与已绑定证据；不调用模型、不新增卡片、不改档位、不产生资格结论。

## 2. 三段结构（段间空行 = SSE 流式分段点）

### 第一段：结论边界
- 本次逐条核对了 N 项与家庭情况相关的政策，其中 S 项已有已核实条件支持；
- 若存在 C 档：另有 P 项需要补充家庭信息后才能判断；
- 固定口径：“已满足部分条件、政策依据待补齐”，只是已核实条件的汇总，不等于资格认定。
- 计数优先取展示行自身的档位（A/B 支持、C 待补充），保证与第二段逐条内容一致；行上无档位时才回退 `coverage.match_core.counts`。

### 第二段：逐条依据
每张卡片一行：`- **政策名**（成员）：已核实条件：…`，其下按可得性给依据：
- 全模型路径：已绑定 claim 上的**逐字引文**，标 `原文：“…”`（只取 `kind=satisfied` 的 `evidence[].quote`，引用不到就整行不出现）；
- 规则路径：`clause_quote`（catalog 的 target/conditions 条件文字），标 `政策条目载明：“…”`——**不冒充原文逐字**；
- `source_gaps` 以“政策原文未明确的口径”列出。
- 最多展示 6 行，其余提示“见页面卡片列表”。

### 第三段：待补充
- 聚合去重后的缺项（过滤“等待补充家庭事实后继续核验”这类占位语）；
- 追问标签（最多 4 条，提示可直接选“不清楚”）；
- 已答“不清楚”的说明（`unclear_note`）；
- 看过但没出卡的原因（`why_not` / `rejected_policies` / P3 agent findings）；
- 未完成分析、候选截断提示；
- 首轮免责句（`coverage.disclaimer` 为真且非 scope_blocked）。
- 没有待补充项时保留第三段，给固定的一句“没有新的待补充项”。

## 3. 模块接口（deep module）

```python
claim_answer.enabled()                    # ANSWER_STYLE 未显式关闭
claim_answer.answer(rows, questions, coverage, intro=None)  # -> str；rows 为空/关闭返回 ''
```

内部只用行上已有字段：`name/member/satisfied/missing/source_gaps/tier/claims/clause_quote`，以及 `coverage` 的 `match_core/why_not/rejected_policies/agent/unclear_facts/interpretation_pending/interpretation_failed/disclaimer/candidate_budget_truncated`。不读写数据库、不调用模型。

## 4. 接入点

- `result_model.render(advice, rows, questions, coverage)`：`rows` 非空且开关开启时直接返回三段式（模型生成的 `advice` 本段不再使用；成本已在生成阶段发生，后续阶段可省）。`render_repeat` 保持原样（重复轮次只回结论摘要）。
- `rule_flow.render_rule_mode(...)`：开关开启且有卡片时，把首轮开场、点名核验提示、地区未确认提示放进 `intro`（仍是一段），正文交给三段式；否则走原版式。
- `rule_flow.build_rule_rows / _anchored_rows`：新增 `clause_quote`，由 `_clause_text(rule)` 从 `detail_target/target` + `conditions/detail_conditions` 组装并截 80 字。
- 流式：`conversations.py` 仍按 `events.answer_segments` 切段发送 `answer.delta`；三段之间是空行，因此至少形成三段逐段流式；拼合逐字节等于 `answer`。

## 5. 开关与回退

- `ANSWER_STYLE=claims`（默认，代码内置）→ 三段式；
- `ANSWER_STYLE=classic` 或 `0` → 完全回退旧版式（模型散文 + 卡片行 + 原 claims_block/why_not 等）；
- 只影响正文渲染：卡片、档位、match_core、why_not、事件层均不变。

## 6. 验收记录

### 单元/整合
- `tests/test_claim_answer.py` 16 例：开关默认开/classic/0 关闭、三段空行分隔、边界计数、禁用词（完全符合/可以领取/保证符合/保证能/已符合资格）、已绑定引文才出现“原文：”、规则行只出“政策条目载明”、空 rows 返回空串、why_not/agent 段保留、免责句仅在标记时出现、intro 开头、`result_model.render` 与 `rule_flow.render_rule_mode` 的 claims/classic 双路径、行档位计数覆盖 coverage 计数。
- 全量单测：263 tests OK。

### 规则路径生产 live（5000，莲花家庭 7 卡）
- `/tmp/live_p3b.py`：PASS；elapsed 7.3s，43 帧，23 用户事件，answer 1148 字/10 段 delta 拼合一致，三段结构 true、边界 true、依据 true、无禁用词；
- 卡片数 7、rule_engine=used、codes 与 P2 验收一致（S-ZB-091/X-ZB-300/S-ZB-090/X-ZB-301/X-ZB-462/S-ZF-008/X-CY-301）；
- trace_id 实时=回放，回放用户事件 23==实时，含 turn.complete；
- 实际正文：5 项支持 + 2 项待补充，逐条“政策条目载明”，缺项/追问/免责句齐备。

### 全模型路径（独立 gunicorn 5001，RULE_ENGINE=off，c6 无地区家庭）
- `/tmp/live_p3b_full.py`：PASS；elapsed 189.8s，164 帧，99 用户事件，answer 1621 字/13 段 delta 拼合一致；
- 三段结构 true、边界 true、**逐字原文引文 true**，无禁用词；
- 卡片 2 张（X-ZB-399、X-ZB-248，均为 B 档，与 c6 既有基线一致）；
- trace_id 实时=回放，回放用户事件 99==实时，回放事件 218；model.calls 70（含 agent 1 步）。

## 7. 回退实测

- `ANSWER_STYLE=classic`：`tests/test_claim_answer.py` 的 `test_result_model_render_claims_vs_classic`、`test_rule_render_claims_vs_classic` 断言旧版式恢复（散文/已满足行），三段式不出现；
- 生产未设置 `ANSWER_STYLE`，代码默认 claims；若要回退，设置 `ANSWER_STYLE=classic` 后重启应用进程生效（gunicorn 启动时继承环境变量，HUP 不重读 web.env）。

## 8. 已知边界

- 规则路径没有来源文档逐字引文，只能用 catalog 条件文字，标注为“政策条目载明”；
- “支持/待补充”计数来自展示行档位，若将来新增未分档行会回退 coverage 计数；
- 点名核验提示只列前 4 项（超出显示“等 N 项”）；
- `advice` 在全模型路径仍会先生成一次（P3-B 未去掉该调用，属后续优化）。
