# 11 · P3 调查式 agent 与逐字引文回答（契约）

状态：已上线（2026-11-07，commit f4fdb3b；独立实例真 HTTP 验收 PASS）。依据：`docs/redesign/00-overall.md` §6.5（L3 智能体循环）、
`docs/redesign/10-p2-match-core-contract.md`（P2 条件原子与分档）。

## 1. 一句话结论

规则前置仍然负责"快"，但**"看过却没有出卡"的政策不再是一行黑盒统计**：系统对它们跑一个
**只读、有界、可回退**的调查循环（最多 3 步 / 3 次模型调用 / 30 秒，工具只能读政策原文与
已抽取事实），把每张未出卡政策"卡在哪一道机制"写成用户能核对的一句话，并附上**逐字回链的
政策原文引文**；卡片集合、档位与资格口径完全不变（FP=0 红线）。

## 2. 为什么改

P2 之后仍有两类体验问题（03/08 文档已量化）：

1. **80–190s 的全模型链路里，超过一半模型调用花在"逐条核验"上**；被拒来源只留下
   `rejected_policies` 的 90 字错误码和 `interpretation_failed` 计数，用户看不到
   "到底是地区不符、对象不符、还是原文没写口径"。
2. **模型判定与原文引文脱钩**：P2 的 A 档要求引文逐字回链，但 B/C 档卡片仍只有
   "已确认/仍需核对"的转述，用户无法自己核对依据；规则路径的 satisfied 原子没有 clause 引文，
   回答里的"已确认"其实是规则编译后的二手描述。

调查循环解决第 1 点；`ANSWER_CLAIMS` 开关解决第 2 点（回答末尾追加逐字引文依据）。

## 3. 工具层（`src/policy_core/tools.py`）

只读注册表，`TOOL_SPECS` 是给模型看的白名单；所有工具只接收 JSON 标量参数，返回结构化
observation，**禁止任何写操作、禁止生成卡片、禁止改档位**。

| 工具 | 参数 | 返回 | 用途 |
| --- | --- | --- | --- |
| `read_clause` | `code`, `field?` | `title/target/conditions/benefit` 等原文段（≤420 字） | 读政策原文，给引文 |
| `region_check` | `code` | `applicable/unknown/explicit_other` + 条目地区 | 判断属地是否可核验 |
| `member_facts` | `member_no` | 仅 `known` 状态的成员事实（字段/值） | 核对家庭已知信息 |
| `evaluate_condition` | `code`, `field`, `value`, `op?`, `member_no?`, `subject?` | 三值 `state` + `actual/missing/reason` | 用 P2 求值器逐条件求值 |
| `search_policy` | `query` | 候选原文命中数 + top3 `code` | 在已加载候选内按关键词定位 |
| `prior_judgement` | `code` | 原决策 `status` + `matches(quote_ok)` + `unknowns` | 复核上一轮判定 |

`verbatim_ok(quote, source)` 折叠空白后做**子串**匹配；`clause_of(pack)` 拼接
`title/target/conditions/parent_target/parent_conditions/benefit/process/name/detail_*`，
与 `grader.clause_text` 同一口径。

## 4. 调查循环（`src/policy_core/agent.py`）

入口：

```python
investigate(hh, packs, decisions, reason_audit, fields, config, model,
            session_id=None, text='') -> {
  'state': 'ok'|'skipped'|'disabled'|'budget_skipped'|'model_error'|'error',
  'focus': int, 'steps': int, 'calls': int, 'tools': [str],
  'findings': [{'code','name','member','status','reason','quote','quote_ok',
                'region','field_checks'}],
  'quotes_verified': int, 'version': 'agent-1'}
```

- **focus**：`decisions` 中 `status != 'related'` 的来源，按 `policy_code` 去重，最多 4 条；
  没有 focus 直接 `skipped`（0 次调用）。
- **预算**：每轮开始检查 `model.remaining >= 6` 且未超 `AGENT_DEADLINE`；模型调用经过
  `full_runtime.Budget`，与主体链路共享同一份 `max_calls` 预算。
- **计划协议**：`ask(model,'agent_plan',...,payload,PLAN_PROMPT)`，模型只输出
  `{"thought","actions":[{"tool","args"}],"findings":[{"code","note"}],"done"}`；
  每步最多 2 个动作，未知工具返回 `{"ok":false,"error":"unknown_tool"}`，不会崩溃。
- **上限**：`AGENT_MAX_STEPS`（默认 3，1–6）、`AGENT_MAX_CALLS`（默认 3，1–6）、
  `AGENT_DEADLINE`（默认 30s）、`FOCUS_LIMIT=4`、`MAX_TOOLS_PER_STEP=2`。
- **失败隔离**：任何异常只在返回值里写 `state=model_error/error`；`AnswerCancelled` 原样抛出；
  绝不修改 `hh/packs/decisions/rows`，不新增卡片。
- **确定性收尾**：模型只提供 `thought/note`，最终 `findings` 由 `_assemble` 用 P2 求值器、
  `region_check`、`prior_judgement` 和来源原文重建；`reason` 出现任何资格结论措辞
  （符合资格/可以领取/保证/肯定符合/必然通过/已经符合）时整段丢弃；`quote` 必须
  `verbatim_ok` 才算 `quote_ok=true`，渲染层只展示 `quote_ok` 的引文。

## 5. 接入点与渲染

- `full_flow.py`：在 `coverage['rejected_policies']` 汇总之后、`question_plan` 之前调用；
  条件 `coverage.get('rejected_policies') and not coverage.get('scope_blocked')`。
  结果写入 `coverage['agent']`；异常写 `{'state':'error','error':...}`，主链路继续。
- `result_model.py`：
  - `agent_lines(coverage)` 在"看过但未出卡"清单之后输出核验说明；无 findings 时不出现。
  - `claims_block(rows)` 仅在 `ANSWER_CLAIMS=1` 时输出"原文依据（逐字引用政策原文）"，
    引文只取 `row.claims` 中 `kind=satisfied` 的 `evidence[].quote`，每张卡最多 1 条。
  - 两者都是**追加段落**，不改 `render` 的既有结构、不改 400 字建议正文。

## 6. 事件（用户可见，SSE v3 自动转发）

| 事件 | kind | phase | exposure | 人话 |
| --- | --- | --- | --- | --- |
| `full_model.agent_thought` | `agent.thought` | decide | user | 调查第 N 步：thought |
| `full_model.agent_tool_call` | `agent.tool.call` | decide | user | 调用只读工具 tool（政策） |
| `full_model.agent_tool_result` | `agent.tool.result` | decide | user | 工具 tool 返回条件成立/不符/待定 |
| `full_model.agent_finding` | `agent.finding` | decide | user | 核验说明：政策（共 N 条） |

`PURPOSES` 新增 `agent_plan='调查计划'`、`agent_summarize='核验总结'`；
`PURPOSE_PHASE['decide']` 已包含这两个 purpose，所以 `model.call` 事件会显示"调查计划"，
不会混进"生成回答"。

## 7. 开关与回退

| 环境变量 | 默认 | 作用 | 回退 |
| --- | --- | --- | --- |
| `AGENT_LOOP` | `1` | 是否运行调查循环 | `0` 直接返回 `disabled`，不建工具上下文、不调模型 |
| `AGENT_MAX_STEPS` / `AGENT_MAX_CALLS` / `AGENT_DEADLINE` | 3 / 3 / 30 | 循环上限 | 调小或 `AGENT_LOOP=0` |
| `ANSWER_CLAIMS` | `0` | 是否输出逐字引文依据 | `0` 保持 P2 版式；先开灰度再默认开 |

回退验证要求：`AGENT_LOOP=0` 与 `ANSWER_CLAIMS=0` 时，9 案例卡片集合、档位分布、
`render` 文本结构必须与 P2 基线逐项一致（仅允许耗时噪声）。

## 8. 验收（本轮）

### 8.1 单测（全绿）

- `tests/test_tools.py` 12 例、`tests/test_agent.py` 8 例、`tests/test_result_agent.py` 7 例、
  `tests/test_events_v3.py` 追加 4 例（agent 事件是否用户可见）。
- 全量：`PYTHONPATH=src:. python -m unittest discover -s tests -p 'test_*.py'` → **243 tests OK**。

### 8.2 真实链路（规则路径，确定性）

`eval_run.py --cases c1,c7,c8`，同进程先后跑 `AGENT_LOOP=1 ANSWER_CLAIMS=1` 与 `AGENT_LOOP=0`：

| 用例 | 卡片集合（两次完全相同） |
| --- | --- |
| c1 | S-ZB-090、S-ZB-091（2 卡） |
| c7 | S-ZB-090、S-ZB-091（2 卡） |
| c8 | S-ZB-006、008、034、087、088、X-JK-018、X-ZB-157（7 卡） |

合计 11 卡、集合逐项相同；平均耗时 12.1s vs 11.0s（模型噪声）。规则路径在 `full_flow` 早期
return，调查循环不参与——这正是"快路径不变"的证明。

### 8.3 全模型路径（AGENT_LOOP=1 确实执行）

条件：`RULE_ENGINE=off`、单跑（不并发）、`FULL_MODEL_DEADLINE=600`、`AGENT_MAX_STEPS=1`、
`AGENT_MAX_CALLS=1`，c1 家庭文本。结果：

- 主体链路 169.5s 结束，调查循环 **state=ok**、1 步 / 1 次调用，工具
  `read_clause`+`region_check`，**4 条 findings 全部 quote_ok=true**（如 X-JK-410
  引文"脱贫人口，防止返贫致贫对象"、S-ZB-091 引文"属于残疾人'两项补贴'中的护理补贴…"）；
- 正文出现"核验说明如下"与"原文依据"两段，长度 1266 字（仅开引文时 1080 字，P2 版 847 字）。

**必须知道的现实**：生产 `FULL_MODEL_DEADLINE=300`、`FULL_MODEL_MAX_CALLS=60` 下，主体链路会把
预算用尽，调查循环只能得到 `state=budget_skipped`（首检 remaining<8 即退出，0 次调用、0 秒）。
这是有意的失败隔离：宁可不解释，也不拖慢主链路或抢占核验预算。要让调查循环在生产真正跑起来，
应把 `FULL_MODEL_DEADLINE` 抬到 420–600（或降低 `max_sources_per_turn` 让主体更快结束），
而不是削弱主体预算。

### 8.4 卡片不变性（诚实结论）

- 规则路径：A/B 两次卡片集合逐项相同（见 8.2）。
- 全模型路径：模型输出本身不确定，A/B 两次卡片集合不同（ON: S-ZB-090/X-ZB-301；
  OFF: S-ZB-090/S-ZB-091），差异来自模型判定而非调查循环。调查循环在 `rows` 已定稿、
  分档完成后才运行，只读 `packs/decisions`，返回值仅写入 `coverage['agent']`；
  `tests/test_agent.py` 有"输入对象在调用前后不发生改变"的断言。因此卡片集合的稳定性
  只能按规则路径/同模型输出的口径验收，不能用跨轮全模型对比来证明。

### 8.5 生产（独立实例真 HTTP 验收）

为不动生产 5000，另起一个**同代码、独立端口**的 gunicorn（127.0.0.1:5001，
`RULE_ENGINE=off`、`FULL_MODEL_DEADLINE=600`、`AGENT_LOOP=1`、`ANSWER_CLAIMS=1`），
用真实登录态 + `POST /api/qa/stream` 跑 c1 家庭文本，验收脚本 `/tmp/live_check_p3.py`：

| 指标 | 结果 |
| --- | --- |
| HTTP / 耗时 | 200 / 207.6s（全模型链路） |
| SSE 帧 / 事件 / 用户可见 | 190 / 189 / 115 |
| agent 事件 | thought 3、tool.call 5、tool.result 5、finding 3（回放 16，与实时一致） |
| 正文 | 1306 字，含"核验说明如下"与"原文依据" |
| 逐段答案 | 16 段 delta，拼合与整段逐字节相等 |
| 卡片 | 3（S-ZB-090 等） |
| 快照 | `coverage.agent.state=ok`、calls=3、3 条 findings 全部 quote_ok |
| 回放 | user 115 == 实时 115、含 turn.complete、trace 一致 `bd5f9f6c…` |
| /ask | 200 |

生产 5000 在 HUP 重载新代码后复跑 P1 live 回归（规则路径）：eligibility 7 卡 / rule_used /
23 用户事件 / 回放 23 / trace 一致，clarify 0 卡 / 4 用户事件 / 回放 4，failures 为空。

**上线注意**：生产 master 的环境变量是启动时继承的，`kill -HUP` 只重载代码、不重读
`deploy/web.env`。因此 P3 开关在新代码里默认生效（`AGENT_LOOP=1`），而
`ANSWER_CLAIMS` 代码默认 0，需要重启一次应用进程才会按 web.env 的 1 打开逐字引文段。

### 8.6 FP=0

调查循环不写卡片、不写事实、不改档位；`findings` 只做解释；`quote_ok` 必须逐字回链，
回链失败的引文不进入正文。

## 9. 后续（P3-B 完整版）

- 把"结论边界 → 逐条依据引文 → 待补齐"三段式做成独立渲染器，替换 `full_advice` 的首段，
  而不是追加段落；需要先让 `answer_segments` 按 segment 流式并保留现有免责句。
- 工具层接入 `search_policy` 的向量召回（当前只做候选内词面命中），以及跨轮调查缓存。
