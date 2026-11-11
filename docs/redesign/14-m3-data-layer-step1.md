# M3 数据层第一步：去无谓模型调用 + 地区功能区归一（契约与验收）

状态：已上线（2026-11-07；开关 `REGION_FUNCTIONAL_ZONES`，默认 1，可回退）
代码：`src/policy_core/full_advice.py`、`src/policy_core/full_flow.py`、`src/policy_core/geo.py`、`src/policy_core/region.py`
测试：`tests/test_advice_claims_skip.py`（8 例）、`tests/test_region_functional_zones.py`（9 例）

## 1. 一句话结论

两件事：① 全模型链路里“先生成一段散文建议、又被 claim 三段式整体替换”的那次模型调用不再发生（c6 实测 32 次调用中无 `core_full_advice`，答案仍为三段式 + 逐字引文 + 免责句）；② 数据层核查确认 5 个城区政策为 0 是**上游从未收录**（不是入库/编译 bug），并把政策库里 13 个功能区/开发区写法归一为县区级地区维度——赣州经济技术开发区的 112 条政策明细从“无法裁决”变为“确定适用”。

## 2. 改动一：claims 生效时不再生成用不上的模型 advice

### 2.1 问题

- `src/policy_core/result_model.py:216` `render(advice,rows,questions,coverage)` 开头：`claim_answer.enabled()` 且 `rows` 非空时直接 `return claim_answer.answer(...)`，模型散文 `advice` 根本不参与正文；
- 但 `src/policy_core/full_flow.py:310` 在此之前仍调用 `full_advice.generate(...)`，等于每轮白烧一次模型调用与 3–8 秒；
- 不能直接删：`generate` 开头有副作用 —— `mark_disclaimer(st,coverage)`（首轮免责句 + 轮次 `st['advice_turn']`）与 `coverage['unclear_facts']=unclear_fact_map(st,fields)`；`scope_blocked` 分支还要返回固定的“不在江西省服务范围”句子。

### 2.2 改法

```python
# src/policy_core/full_advice.py:39
def skip_model_advice(rows, coverage):      # rows 空 / scope_blocked / classic 版式 → False
    return bool(rows) and not coverage.get('scope_blocked') and claim_answer.enabled()

# src/policy_core/full_advice.py:50
def prepare_claim_state(st, coverage, fields):   # 与 generate() 开头完全一致的前置
    disclaimer = mark_disclaimer(st, coverage)
    coverage['unclear_facts'] = unclear_fact_map(st, fields)
    return disclaimer
```

`src/policy_core/full_flow.py:319-326`：

```python
if skip_model_advice(rows, coverage):
    _disclaimer = prepare_claim_state(st, coverage, fields)
    advice, advice_audit = '', dict(status='claims_render', model_calls=0, disclaimer=_disclaimer)
else:
    advice, advice_audit = generate(hh, st, rows, questions, coverage, fields, model)
```

无卡片（要靠散文说明“本次没形成线索”）与 `scope_blocked`（安全约束句）照旧走 `generate`；`ANSWER_STYLE=classic` 时也照旧。

### 2.3 验收（真实全模型链路，c6 无地区家庭）

- 配置：`RULE_ENGINE=off FULL_MODEL_DEADLINE=600 FULL_MODEL_MAX_CALLS=60 AGENT_MAX_STEPS=1 AGENT_MAX_CALLS=1`；
- 结果：**76.5s**、2 卡（X-ZB-248 / X-ZB-399）、`advice={'status':'claims_render','model_calls':0,'disclaimer':True}`、**call_count=32 且 purposes 中无 `core_full_advice`**（fact_extract/audit、retrieval_plan、rerank、reason×5、binding_repair×2、conflict_scope×4、reason_audit×8、reason_repair×3、scope_audit、branches_match×2、agent_plan、question_bind、question_decide）；
- 正文 1546 字：三段式齐备（`本次逐条核对了` / `已核实的条件与政策依据` / `还需要补充与核对的事项`）、含逐字引文（`原文：“`）、含首轮免责句。

## 3. 改动二：政策库功能区写法归一（地区维度补全）

### 3.1 现状

- `policy_detail` 有 **109** 个 distinct `region`，其中 14 个不是 `geo.JIANGXI_COUNTY` 的民政口径县区名：武功山风景名胜区、江西省全省、信江新区管委会、仙女湖区、蓉江新区、南昌经济技术开发区、龙虎山管委会、湾里管理局、鹰潭高新技术产业开发区、抚州高新区、东临新区、三清山管委会、萍乡经济技术开发区、赣州经济技术开发区；
- 旧 `canonical_levels` 对这些写法只能解析出省/市（如“江西省赣州市赣州经济技术开发区” → 省=江西省、市=赣州市、县=None），政策地区一侧（“赣州经济技术开发区”）甚至只能到 unknown → `policy_region_verdict` 返回 None，既不判适用也不判不适用，只能靠软过滤保留并标注 unconfirmed，规则路径也无法按县区命中。

### 3.2 改法

- `src/policy_core/geo.py:39` 新增 `FUNCTIONAL_ZONES`（13 项，name→所属设区市）：南昌经济技术开发区/湾里管理局→南昌市；赣州经济技术开发区/蓉江新区→赣州市；萍乡经济技术开发区/武功山风景名胜区→萍乡市；鹰潭高新技术产业开发区/信江新区管委会/龙虎山管委会→鹰潭市；抚州高新区/东临新区→抚州市；三清山管委会→上饶市；仙女湖区→新余市；
- `src/policy_core/region.py:63-65` 构建 `_CITY_OF_COUNTY_ALL`（民政县区 + 功能区），`_COUNTY_CANON` 仍只用民政口径县区，避免“赣州经济技术开发”这类截断词被当成简称；
- `src/policy_core/region.py:174-178`：`江西省全省` 这类写法直接判省级，不再往下猜县区；
- `src/policy_core/region.py:181`：县区检测池按开关在 `_CITY_OF_COUNTY_ALL` / `_CITY_OF_COUNTY` 之间切换；
- 语义口径：**功能区是独立的县区级维度**，与同市其他区县互不覆盖（判 disjoint，只表示不适用，不会误放行）；家庭只写到市时与功能区政策判 descendant（保留候选，不裁决）；
- 开关：`REGION_FUNCTIONAL_ZONES=0` 回到旧行为（功能区写法解析不出县区，回到 unknown）。

### 3.3 验收

- 家庭地区取“江西省赣州市赣州经济技术开发区”，对 `policy_detail` 中 `region='赣州经济技术开发区'` 的 112 行政策明细：开关开 → **112 行 verdict=True**；开关关 → **112 行 verdict=None**（对照实验，证明差异只来自归一）；
- `policy.regions='江西省全省'` 的省级政策对同一家庭 verdict=True；
- 单测：`tests/test_region_functional_zones.py` 9 例 + `tests/test_region.py` 既有 41 例 = 51 tests OK；全量 **292 tests OK**。

## 4. 数据层核查结论（5 个城区缺口）

| 检查项 | 结果 |
| --- | --- |
| `policy` 总行数 | 3337 |
| `policy_detail` 总行数 | 9403 |
| 东湖区 / 西湖区 / 青云谱区 / 青山湖区 / 浔阳区 | `policy`、`policy_detail` **双表均 0 行**（`policy.regions` 顿号清单从未出现） |
| `policy.level` 分布 | X=3057、S=280，**没有市层级** |
| 空值 | target/conditions/benefit/process/regions 空值均为 0 |
| 明细文本长度 | conditions 平均 59.6 字、target 平均 9.4 字 |

结论：五个城区是**上游数据从未收录**，不是入库或编译 bug；且库内没有市层级政策，所以对它们“省级通用政策”就是唯一正确的兜底口径（已由 `RULE_PROVINCE_ONLY` 兑现，见 docs/redesign/13-rule-readiness-province-only.md）。代码层不应造数据；这 5 个城区应进入数据侧待办（确认是否真有县级政策原文可补录）。

## 5. 开关与回退

| 开关 | 默认 | 回退效果 |
| --- | --- | --- |
| `ANSWER_STYLE` | claims | `classic` → 恢复“模型散文 + 卡片行”版式，并恢复生成 advice 调用 |
| `REGION_FUNCTIONAL_ZONES` | 1 | `0` → 功能区写法回到无法解析（unknown，软过滤保留） |

两项改动都不改卡片集合、档位判定与 FP=0 红线：advice 只是正文生成层；地区归一只把“无法裁决”变成“按同一县区维度裁决”，且新判定的 disjoint 一律来自同层级明确不同名（不会把不同地区判成适用）。

## 6. 运维注意与下一步

- 功能区县区第一次被问到时会走 `try_rule_run` → 规则未就绪 → `enqueue` 编译任务 + 回退全模型；编译完成后（`policy_rule_job.done`）后续请求自动切到规则快路径。首个请求慢属于预期，事件层会显示 `rule.not_ready`；
- 下一步（M3 续）：clause/condition 抽取（把 `policy_detail` 的 target/conditions/benefit 切成带位置的条款行，供规则原子与证据绑定引用）；收录事件化（政策变更触发 detail/向量/规则增量更新）；5 城区数据待办跟进。
