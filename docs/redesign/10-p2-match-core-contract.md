# 10 · P2 匹配核心 v2 契约（condition atom + 分档）

状态：实施中（M1 第一切片）。依据：`docs/redesign/00-overall.md` §6.4（L2 匹配核心）。

## 1. 一句话结论

把"政策要求"与"家庭事实"的比对，从**字符串规则 + 多道后置否决门**改成
**条件原子（condition atom）+ 三值求值器 + A/B/C/D 分档**：判定只由求值器写状态，
模型只能"提议原子 + 引文"，证据必须逐字回链来源原文；"未知"不再等于"不适用"，
除四类硬拦截外不再有"一票否决"。

## 2. 为什么改

现状三类结构性缺陷（01/03 文档已量化）：

1. **条件表示三套并存**：`policy_rule.rule.eligibility` 的 AST、规则库的 82 字段词表、
   全模型链路的自然语言判定。同一件事在三处用不同方式判断，规则库覆盖不到的地方就只能
   退回 80–170s 的全模型链路，且两者结论不可比。
2. **否决门只增不减**：`structural_gate`/`completeness_gate`/`audience_gate`/
   `domain_gate`/`name_gate`/`_member_category_gate` 六类，全部是"命中即 0 卡"，
   误杀后既不解释也不降档（62 岁被"残疾儿童"政策命中的根因就是这里）。
3. **判定与证据脱钩**：模型可以给出"已满足"而引文对不上原文；规则路径的 satisfied
   原子没有 clause 引文，A 档"已核验"无从核验。

## 3. 原子 schema

```python
atom = {
  'condition_id': 'c1',          # 稳定标识（同一候选内唯一）
  'subject': 'member'|'household'|'policy',
  'field': 'disability_grade',   # 字段字典（fields.json）里的键
  'op': 'eq'|'ne'|'in'|'nin'|'gte'|'gt'|'lte'|'lt'|'between'|'exists'|'contains'|'implies'|'count_gte',
  'value': True|False|2|['一级','二级']|{'min':..,'max':..},
  'unit': '周岁', 'period': None,
  'source': 'rule'|'structured'|'llm_proposal'|'user',
  'evidence': {'clause_id': '...', 'quote': '...'},   # llm_proposal 必填
  'state': 'satisfied'|'violated'|'unknown'|'not_applicable',  # 仅求值器可写
  'evaluated_by': 'conditions.cond-1',
  'reason': '...', 'actual': ..., 'missing': ['2:age'],
}
```

只有 `conditions.evaluate_atom` / `conditions.aggregate` 会写 `state`；
在此之前的任何层（规则编译、模型输出、检索）都只能产出 `source='rule'|'llm_proposal'` 的**未求值原子**。

## 4. 三值求值语义

| 场景 | state |
| --- | --- |
| 事实已知且满足 | `satisfied` |
| 事实已知且不满足 | `violated` |
| 事实未知（含字段无定义、值不合法、成员无该事实） | `unknown` |
| 条件本身不适用于该主体（政策未提、主体不匹配） | `not_applicable` |

- `AND`：任一 `violated` → `violated`；全部 `satisfied` → `satisfied`；否则 `unknown`。
- `OR`：任一 `satisfied` → `satisfied`；全部 `violated` → `violated`；否则 `unknown`。
- `NOT`：`satisfied`↔`violated`，`unknown` 保持 `unknown`，`not_applicable` 保持。
- **未知不是违反**：`unknown` 永不被当作 `violated` 去拦候选；它只影响档位（A→B/C）与追问。
- 标准算子的比较语义复用 `policy_core/engine.py`（同一套类型校验、成员作用域绑定），
  `contains`/`implies`/`count_gte`/`nin` 由 `conditions.py` 补齐并保持三值。

## 5. 证据绑定（A 档的必要条件）

- `source='llm_proposal'` 的原子必须带 `evidence.quote`。
- 绑定校验：`normalize(quote)` 必须是 `normalize(clause_text)` 的**逐字子串**
  （只折叠空白，不做同义改写、不做标点替换），且 clause 必须来自本轮候选来源。
- 校验失败：`evidence_bound=False`，原子降为 `unknown`，并写
  `binding='rejected'`；该候选最高只能到 B 档，且进"为什么没有"清单（原因 `evidence_binding`）。
- 规则原子（`source='rule'`）来自离线编译的原文条款，允许 `clause_id` 缺省，
  但 A 档仍要求 `evidence.quote` 存在且能回链；缺失时降 B 并标注 `binding_gap`。

## 6. 地区适用性（维度，不是条件）

`region_status` 取值与判定（复用 `policy_core/region.py`）：

| 取值 | 判定 | 对档位的影响 |
| --- | --- | --- |
| `applicable` | 政策地区 = 家庭地区，或政策是上级覆盖 | 可进 A/B |
| `parent_applicable` | 政策为省级/全国，家庭县在其内 | 可进 A/B |
| `unknown` | 家庭地区未确认/无法解析 | 卡片最高 B，追问地区 |
| `reference` | 其他县同级，可参照但非本县适用 | 可进 C，不进 A/B |
| `explicit_other` | 明确是其他区县/外省且政策为专属 | **硬拦截 D**（不出卡，进为什么没有） |

地区值变体（"江西省"/"江西省新建区"）由 `region_code` 等价层消解，**不再产生 conflict**。

## 7. 四类硬拦截（其余门一律改为扣分/标注）

> 其中**政策无效**与**属地明确不符**直接判死（D，不出卡、进"为什么没有"）；
> **证据绑定失败**与**口径越界**是"封顶"拦截：绑定失败时已验证条件不再算数，
> 最高 B（无已证条件时 C），不判"不适用"。

| 拦截 | 触发 | 处理 |
| --- | --- | --- |
| `evidence_binding` | 已满足/已核验条件无逐字引文 | 降 unknown / 最高 B；进 why-not |
| `safety_wording` | 文本出现资格结论词（"符合条件/可以领取/确定享受"）且无免责 | 重写或降档 |
| `policy_invalid` | 政策停用/过期/未生效 | 不出卡，进 why-not |
| `region_explicit_other` | 地区明确为其他区县 | 不出卡，进 why-not |

原 `domain_gate`/`completeness_gate`/`structural_gate`/`audience_gate`/`name_gate`/
`_member_category_gate` 的结果**保留为判定信号**，但改为：A 档的必要条件 / B 档的扣分项 /
D 档的可解释原因，不再直接 0 卡。

## 8. 档位与排序

| 档 | 条件 | 用户看到的话 |
| --- | --- | --- |
| **A** 已核验 | region applicable/parent；无 violated；≥1 实质条件 satisfied 且有逐字引文；无未确认关键条件；四类硬拦截全过 | "已满足 M 项条件，政策依据待补齐" + 逐条依据 |
| **B** 可能符合 | region 可适用；无 violated；≥1 satisfied；仍有 unknown **关键**条件 | "可能符合（非资格结论），需核实 N 项" |
| **C** 需补充信息 | 无 satisfied 或仅身份类缺失；无 violated | "补齐这 N 项即可判断" |
| **D** 不适用 | audience/region 明确违反，或政策无效 | 不出卡，进"为什么没有"清单 + 原因 |

排序：`tier A > B > C` → `fit_score`（地区贴合 + 已证条件数 + 语义分）→ 证据完整度 → 名称去重。
用户点名政策（anchor）仍置顶，组内再按档位。

## 9. claim（卡片与回答的唯一事实来源）

```python
claim = {
  'claim_id', 'text', 'kind': 'satisfied'|'missing'|'possible'|'not_applicable',
  'policy_code', 'condition_ids': [...],
  'evidence': [{'clause_id','article_no','quote','source'}],
  'verified_by', 'verifier_version',
}
```
卡片字段（satisfied / missing / decision / basis）与回答句子都从 claim 派生；
A 档 claim 必须有 `evidence` 且 quote 通过逐字子串校验，否则 `gate.blocked` 并降档。

## 10. 两路接入点

1. **规则快速路径**（`policy_core/rule_flow.py`）：`evaluate_family` 的 card/detail
   → `grader.grade_rule_card` → 行上写 `tier`/`fit_score`/`claims` → 排序 →
   `coverage['match_core']` 计数 → `match.graded` 事件。卡片口径文案不变。
2. **全模型路径**（`policy_core/full_flow.py` + `reasoning.py`）：`build_rows` 产出的
   rows 已带 satisfied/missing_keys/reasoning → `grader.grade_row` 用 `pack['source']`
   的原文做逐字引文校验 → 行上写 tier → 排序 → why-not 清单补 tier/reason。

## 11. 开关与回退

- `MATCH_CORE_V2=1`（默认）：启用分档、排序、why-not 清单。
- `MATCH_CORE_V2=0`：完全回到旧行为（不改排序、不写 tier 以外的判定）。
- 卡片固定口径"已满足部分条件，政策依据待补齐"、禁止资格结论、FP=0 三条红线不变。

## 12. 验收

- 单测：`tests/test_conditions.py`、`tests/test_grader.py` 全绿（三值真值表、绑定拒绝、
  档位映射、地区 explicit_other、排序稳定）。
- 9 案例门禁：卡片集合与基线（52 卡）一致或更少误放行；62 岁成员不得出现"残疾儿童"类卡片；
  tier 分布可解释（每张卡都有 tier+claim）。
- 生产：`kill -HUP` 重载后 /ask 正常，`match.graded` 事件可见，P50 不退化。


## 13. 实施记录（2026-11-07）

### 13.1 落地文件

- 新增 `src/policy_core/conditions.py`：条件原子 DSL（`parse` / `evaluate` 三值 /
  `ask_question`），把"条件"收敛成一类可求值对象。
- 新增 `src/policy_core/grader.py`：`grade_rule_card` / `grade_row` / `claims_for` /
  `why_not` / `enabled`；A/B/C/D 档映射与证据绑定门；`MATCH_CORE_V2` 开关。
- 改 `src/policy_core/rule_flow.py`：规则卡片过 grader 后写 `tier`/`fit_score`/`claims`，
  按档位排序，`hidden_d` 记 D 档，`coverage['match_core']` 计数；点名政策的合成行
  （`_anchored_rows`）也参与分档（`grades_out`）。
- 改 `src/policy_core/full_flow.py`：全模型 rows 过 `grade_row`，why-not 从被隐藏行生成。
- 改 `src/policy_core/rule_eval.py`：户级身份事实下发给成员视角（脱贫户/监测对象是整户认定），
  修真实误放行；并在金仓查询异常时保证 `load_rules` 不抛 `UnboundLocalError`。
- 改 `src/policy_core/result_model.py`、`src/events.py`：卡片带档位与 why-not，
  `match.graded` 用户事件（"条件匹配分档：A x · B y · C z · D w"）。

### 13.2 单测

`tests/test_conditions.py`、`tests/test_grader.py`、`tests/test_rule_eval_identity.py`
（户级身份下发 5 例）、`tests/test_events_v3.py` 新增 `match.graded` 人话断言。
全量：`PYTHONPATH=src:. python -m unittest discover -s tests -p 'test_*.py'` → 216 tests OK。

### 13.3 9 案例门禁（`tests/e2e_p2_gate.py`，真实链路）

| 案例 | 耗时 | 卡片 | 档位 |
|---|---|---|---|
| c1 c2 c3 c4（莲花 2 人家庭） | 9.7–10.5s | 2（S-ZB-090/091） | 全 B |
| c5（一级残疾+长期照护） | 14.2s | 5 | 全 B |
| c6（无地区，全模型链路） | 191.4s | 2（X-ZB-248/399） | 全 B |
| c7（莲花费） | 13.9s | 2 | 全 B |
| c8（吉安县） | 14.7s | 8（含 C 档） | B/C |
| c9（点名雨露计划，新建区） | 17.2s | 3（点名核验信息行+1 普通卡） | 全 B |

- 结果 **PASS（0 失败）**，产物 `runtime/logs/p2_gate.json`；基线 52 卡 → 本次 26 卡
  （三个重复家庭案例按同一家庭文本计算），无新增误放行。
- 回归断言：62 岁家庭不出"8–15 岁残疾儿童康复救助"（S-ZB-058）；c1 不再出
  X-ZB-300/301（脱贫/监测身份政策）；A 档卡引文逐字校验；hidden 行都有 why-not 与原因。
- c9 修正：原脚本把 c9 当闲聊断言 0 卡，实际是真实政策咨询（规则路径给点名政策锚定
  信息行），改为仅当 `intake.route == 'chat'` 才断言 0 卡。

### 13.4 开关回退

- `MATCH_CORE_V2=0` 实测 c1：13.6s，6 卡，无 `tier`、`coverage['match_core']` 为空、
  无 `match.graded` 事件，行为与 P2 之前一致；=1（默认）时 2 卡、四档计数、why-not 完整。
- `MATCH_CORE_V2` 只控制分档/排序/why-not 与事件，不改检索、规则求值、卡片口径。
- 运行期环境变量在 gunicorn 启动时由 `deploy/web.env` 注入；代码默认开启，
  `kill -HUP` 即可生效；显式 =0 需重启或改环境。

### 13.5 已知边界

- 规则路径多数卡片为 B 档、reason=`evidence_binding_gap`：规则原子证据只到 clause id，
  没有逐字引文，按契约封顶 B；要升 A 需 M3 的 clause 原文层。
- c6（无地区）仍走全模型链路（约 200–250s），P2 未改链路选择；预算收敛与并行化在 M1/M2。
- 全模型路径的 why-not 只覆盖本轮实际核验过的候选，不等于"全库未命中"。



### 13.6 生产验收（2026-11-07，真实 HTTP + gunicorn master 361754）

- 重载：`kill -HUP 361754` → 新 worker 启动（graceful-timeout 15）。
- 请求：莲花县 2 人家庭（62 岁二级残疾 + 60 岁健康），`POST /api/qa/stream`。
- 结果 PASS：7 张卡（tier B×5、C×2），快照 `coverage['match_core'].version=grade-1`、
  counts A0/B7/C2/D0，`match.graded` 用户事件可见且文本计数与快照一致
  （"条件匹配分档：A 0 · B 7 · C 2 · D 0"）；实时 23 条用户事件，回放 23 条且同样
  含 `match.graded`/`turn.complete`；answer 12 段 delta 拼合与整段逐字相等；trace_id
  实时/回放/qa_trace 三者一致；`/ask` 200。
- 修复一项一致性问题：`match.graded` 原在 `rule_flow.try_rule_run` 早期发出，只统计规则卡片、
  未含点名政策合成行，与快照 counts 不一致；改为分档全部完成后、D 档已隐藏时发出，
  计数直接取 `coverage['match_core']['counts']`（commit 8652ca2）。

### 13.7 回退边界补充

- 生产 env 里 `MATCH_CORE_V2` 未显式设置 → 代码默认 `1`；如需回退，在 `deploy/web.env`
  加 `MATCH_CORE_V2=0` 并重启 gunicorn（`kill -HUP` 不重读 env）。

