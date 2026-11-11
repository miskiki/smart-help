# 政策数据治理 dry-run 报告（T27）

> 状态：**只读干跑（2026-11-08）**，未修改任何数据、代码或环境，未执行归档/合并/回填动作；临时产物只写 `/tmp`。
> 工具：`deploy/policy_data_audit.py`（v `policy-data-audit-1`，只读审计 + 建议动作，33 例单测）。
> 数据源：Kingbase ES `postgresql+psycopg2://smart_help_policy:***@127.0.0.1:54321/policy_db_mig20261021`（实例 `/proc/361754/environ` 与 `deploy/web.env` 一致）。
> 证据：JSON `/tmp/policy_data_audit_20261108.json`、可读报告 `/tmp/audit_report_verify.txt`；复现命令见 §8。
> 关联：`docs/redesign/17-policy-coverage-audit.md`（覆盖与重复审计）、`docs/redesign/18-policy-lifecycle-code-audit.md`（生命周期现状）、`docs/redesign/13-rule-readiness-province-only.md`、`docs/redesign/14-m3-data-layer-step1.md`。

## 1 一句话结论

**当前生产数据可以确认存在四类可治理问题：40 条空壳、917 组 3430 行重复（冗余 2513 行）、961 行跨级同名、5 个县区覆盖为零；时效缺口已比旧库基线小得多（缺 53/13/54 行），但 `updated_at` 仍只有 18/3337 行有值。** 本报告只给处置建议，不执行写操作；所有计数来自一次只读全库扫描，可重复复跑。

## 2 只读保证

| 层 | 保证 |
| --- | --- |
| 连接 | PostgreSQL/Kingbase 连上后执行 `SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY`；SQLite 执行 `PRAGMA query_only = ON`。只读会话下即使代码误写也会被数据库拒绝。 |
| 语句 | 全模块只有 `SELECT`/`COUNT`/`SUM`；不建表、不 `ensure_schema`、不写 `policy`/`policy_detail`/`policy_version`/`policy_scope`、不改 `status`。单测 `test_no_write_statement_in_suggestions` 对输出 JSON 做写语句关键字断言。 |
| 范围 | 只读四张主表：`policy`、`policy_detail`、`policy_version`、`policy_scope`；不触碰文件、部署配置或生产进程。 |
| 退出码 | 0=成功；2=连接失败（stderr 明确写"只读审计未执行"，绝不误报空结果）。 |

**Kingbase 空串语义**：KingbaseES（Oracle 兼容）把 `''` 当 `NULL`，`TRIM(COALESCE(col,''))=''` 恒为 NULL（既不为真也不为假）。因此判空按方言分支：postgresql 用 `BTRIM(col) IS NULL`（同时覆盖 NULL/空串/纯空白），SQLite 用 `TRIM(COALESCE(col,''))=''`。不分支会把"空串"全部漏算（旧脚本在本库上曾因此报 0 缺口）。

## 3 干跑结果摘要（2026-11-08）

### 3.1 总览

| 表 | 行数 |
| --- | ---: |
| policy | 3337 |
| policy_detail | 9403 |
| policy_version | 3339 |
| policy_scope | 15493 |

### 3.2 (a) 覆盖画像

- 县区：**95/100 有明细**，0 明细 5 个——东湖区、西湖区、青云谱区、青山湖区、浔阳区。
- 功能区：**13/13 有明细**；明细合计 9403 = 县区 8412 + 功能区 978 + 省级条目 13。
- 省域：唯一政策名 **1801** 个，对应 **3297** 个 `policy_code`；无法归一地区 **0**（明细 region 全部能落入标准行政区或"江西省全省"）。
- 设区市明细（县区明细 / 县区名数 / 功能区个数 / 功能区明细）：

| 市 | 县区覆盖 | 县区明细 | 县区名数 | 功能区 | 功能区明细 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 南昌市 | 5/9 | 364 | 242 | 2 | 180 |
| 景德镇市 | 4/4 | 330 | 181 | 0 | 0 |
| 萍乡市 | 5/5 | 401 | 200 | 2 | 123 |
| 九江市 | 12/13 | 1034 | 209 | 0 | 0 |
| 新余市 | 2/2 | 154 | 122 | 1 | 64 |
| 鹰潭市 | 3/3 | 234 | 185 | 3 | 203 |
| 赣州市 | 18/18 | 1749 | 658 | 2 | 207 |
| 吉安市 | 13/13 | 1371 | 315 | 0 | 0 |
| 宜春市 | 10/10 | 873 | 252 | 0 | 0 |
| 抚州市 | 11/11 | 946 | 342 | 2 | 122 |
| 上饶市 | 12/12 | 956 | 452 | 1 | 79 |

- 功能区明细（前几名）：赣州经济技术开发区 112、湾里管理局 97、蓉江新区 95、信江新区管委会 90、南昌经济技术开发区 83、三清山管委会 79、萍乡经济技术开发区 77、东临新区 70、仙女湖区 64。

### 3.3 (b) 空壳清单

- **40 条**政策无任何明细：在用 39 + 停用 1；全部 `level=S`；`regions` 为 NULL 40 条；**无 `policy_scope` 行 40 条**；版本行存在（0 版本数=0）。
- 样例：`S-CY-006`（到户产业奖补）、`S-JK-024`～`031`（资助参保/医疗救助系列）、`S-JR-009`（帮扶小额信贷贴息）——这批是 2026 汇总批次里有策略级文本、缺明细的条目。
- 影响：仍占 `policy` 行与策略级向量，可能进入召回后被 `evidence_loader` 以 `missing_detail` 丢弃，浪费召回预算并制造同名重复候选。

### 3.4 (c) 重复组

- **917 组 / 覆盖 3430 行 / 冗余 2513 行**（占 9403 明细的 26.7%）；按 `(region, conditions, target)` 完全同文分组，组内保留最早 `detail_id`。
- 头部：都昌县 43 行（keep detail_id=9343，涉及 X-CY-564/X-CY-566）、贵溪市 33、吉水县 32、龙南市 28、庐山市 26、靖安县 23、铅山县 21、新干县 20。
- 影响：同一家庭同一条件下会命中多条同文明细，重复占 `max_candidates` 预算，且跨 `policy_code` 时会让卡片证据数量虚高。

### 3.5 (d) 跨级同名

- **145 个 policy_name 同时出现在 X 与 S 两级**，覆盖 **961 行**（占 3337 行 policy 的 28.8%）。
- 样例：先诊疗后付费（X-JK-010/017/026 vs S-JK-007/009/015，45 行）、稻谷补贴（X-CY-036/065 vs S-CY-005，39 行）、高龄津贴（X-ZB-054/148 vs S-ZB-010，29 行）、签约服务 28 行、危房改造补助 25 行、残疾儿童康复救助 25 行。
- 影响：省/县两级各有一份实现，检索可能同时召回；无法从数据上判断是"同一制度的省县分级"还是"重复收录"，**必须人工确认后才能合并**（对应 `policy_family` 模型，见 17 号文档 §6.1）。

### 3.6 (e) 时效缺口

| 表 | 总行 | effect 缺失 | expire 缺失 | 缺失率 |
| --- | ---: | ---: | ---: | ---: |
| policy | 3337 | 53 | 53 | 1.59% |
| policy_detail | 9403 | 13 | 13 | 0.14% |
| policy_version | 3339 | 54 | 54 | 1.62% |

- 在用政策按 level：**S 279 行里缺 effect/expire 各 52**；**X 3054 行全部有值**。缺的 53 行 = 40 条空壳 + 13 条省级条目（多为 2026 汇总批次，日期是 `2026-01-01`/`2030-01-01` 这类粗粒度口径）。
- `updated_at`：仅 **18/3337** 行有值；`policy_version.version_no` 全为 v1（3339 行）；`cur_version`：v1 3335 行、v2 2 行。

### 3.7 (f) 建议动作清单

| 动作 | 数量 | 风险 | 说明 |
| --- | ---: | --- | --- |
| archive | 40 | low | 空壳政策归档/软删并隔离策略级向量；不物理删除 |
| merge | 917 | medium | 重复明细组合并，保留最早 detail_id，合并前留版本快照 |
| manual | 145 | high | 跨级同名先人工确认同一制度性，再决定合并或保留 |
| backfill | 8 | medium | 回填/显式 unknown；= 有缺口的在用 level 数 1（S）+ 零覆盖县区 5 + detail 表缺口 1 + version 表缺口 1 |

合计 **1110** 条建议；风险分布 high 145 / medium 925 / low 40（多行样本按 `--limit` 截断，控制台只展示前 N 条）。

## 4 治理原则

1. **真源是导入管线，不是数据库手工改**：任何数据修复都应回写"可重复执行的导入/迁移脚本 + 批次记录"，数据库只做软状态变更；禁止在生产库直接 `UPDATE`/`DELETE` 人工修数。
2. **删除一律走归档/软删**：空壳与重复行先打标（如 `status=归档` 或独立归档表）并保留 `policy_id`/`detail_id`、原文与向量隔离，保证可追溯、可回滚；不物理删除。
3. **合并必须留版本快照**：`policy_version` 先落合并前快照（`version_no` 递增、保留来源），再做 `policy_family` 归一；同一制度的多级实现用"族-实现"模型表达，不用删行表达。
4. **跨级重名先人工确认**：同名 X/S 不一定是重复（可能县细则 vs 省制度），数据层无法自动裁决；先人工打标"同一制度/不同制度"，再进入合并或保留流程。
5. **未知不静默**：时效缺失显式写 `unknown` 并进入审计报表，不以空值冒充"长期有效"；覆盖为零的县区显式标注"暂不可用"，不默认回落到省/全国。

## 5 四类问题处置路径

### 5.1 空壳（40）

- 现状：无明细、无 scope、仍有版本行与策略级向量。
- 动作：先隔离（不参与召回、不加候选预算）→ 人工确认是否有可补明细；有则补录并生成明细/scope，无则归档软删。
- 验收：空壳数降为 0 或被显式归档；召回 trace 中 `missing_detail` 丢弃数归零。
- 禁止：直接 `DELETE FROM policy`；保留向量继续参与召回。

### 5.2 重复明细（917 组 / 3430 行）

- 现状：同 `(region, conditions, target)` 完全同文，跨 `policy_code` 的情况存在。
- 动作：按组保留最早 `detail_id`（或条件最全者），其余归档；合并前写 `policy_version` 快照；合并后更新 `policy_scope` 明细映射。
- 验收：重复率 < 5%（当前冗余 26.7%）；被归档行的 `detail_id` 在归档表可查；同一家庭条件不再重复出卡。
- 禁止：删除行后不做版本快照；跨 `policy_code` 直接合并而不确认制度关系。

### 5.3 跨级同名（145 名 / 961 行）

- 现状：X/S 两级各有实现，无法从文本判定是否同一制度。
- 动作：人工确认 → 同一制度则建 `policy_family`（族）并把各级实现挂为子项；不同制度则保留并在 `policy_name`/别名上区分；短中期检索侧先做去重展示（同族只出最优一条）。
- 验收：145 名 100% 有确认结论（族/非族）；族内展示去重生效；不因同名而重复出卡。
- 禁止：机器自动合并跨级同名；改用户可见 `policy_name` 原值（只加族/别名映射）。

### 5.4 覆盖缺口与时效缺口

- 覆盖：5 个零明细县区（东湖/西湖/青云谱/青山湖/浔阳）要么补录上游文件，要么显式标注"暂未收录"并在结果卡边界文案里说明，**不得默认按江西省/全国政策放行**。
- 时效：53/13/54 行缺失回填或显式 `unknown`；粗粒度日期（`2026-01-01`/`2030-01-01`）标记为"汇总批次近似值"，避免被当作精确生效日；`updated_at` 只有 18 行，需在导入管线写入批次时间。
- 验收：在用政策 `effect_date` 非空或 unknown 100%；`updated_at` 覆盖率随导入批 100%；审计工具可重复跑且计数稳定。

## 6 与 docs/17 旧基线的差异（必须知晓）

doc 17 的审计针对旧库 `policy_db`（该库现已不存在，配置指向 `policy_db_mig20261021`）。本次对比：

| 项目 | doc 17（旧库） | 本次（当前生产库） | 说明 |
| --- | --- | --- | --- |
| policy / detail / version | 3337 / 9403 / 3339 | 同 | 行数稳定 |
| 空壳 | 40 | 40 | 一致 |
| 重复组 / 覆盖行 / 冗余 | 917 / 3430 / 2513 | 同 | 一致 |
| 跨级同名 / 行 | 145 / 961 | 同 | 一致 |
| 5 个零覆盖县区 | 有 | 有 | 一致，尚待补录 |
| effect/expire 非空 | **全 0** | policy 3284/3337、detail 9390/9403、version 3285/3339 | 迁移库已大量回填，旧文档时效结论已过期 |
| updated_at 非空 | 18 | 18 | 仍只有 0.54% |
| version_no | 全 v1 | 全 v1 | 版本模型仍未用起来 |

结论：**覆盖/重复/空壳/跨级四类计数稳定、可继续作为治理基线；时效类验收必须以本报告（当前配置库）为准，不能用 doc 17 的"全空"结论。**

## 7 验收标准

本轮（dry-run 交付）：

- 工具 `deploy/policy_data_audit.py` 可在任意 PG/SQLite URL 上只读运行，输出七节报告与 JSON（audit-2 起含 `scope_consistency`）；连接失败退出码 2 且不产生误导性空报告。
- `tests/test_policy_data_audit.py` 43 例全绿；全量 `unittest discover` 门禁全绿。
- 真实生产库跑一次，计数与本报告一致；JSON 落 `/tmp`，不写库。

治理落地（后续批次）：

- 空壳：40 → 0（归档或补明细），召回 `missing_detail` 丢弃归零。
- 重复：冗余率 26.7% → < 5%，每组保留规则可审计、版本快照齐备。
- 跨级同名：145 名 100% 人工确认，族模型可检索去重。
- 覆盖：5 个零覆盖县区补录或显式"暂未收录"边界文案；无默认回落。
- 时效：在用政策 effect/expire 非空或 unknown 100%，`updated_at` 随导入批 100%。
- `policy_scope` 与 `policy_detail` 映射一致性审计（15493 行）已纳入 audit-2（§10）：当前 0 缺行、0 孤儿、1 政策级不一致。

## 8 复现与用法

```bash
cd /home/adminroot/smart-help
set -a && . deploy/web.env && set +a
# 人类可读报告
/home/adminroot/miniconda3/envs/smart-help-web/bin/python deploy/policy_data_audit.py --report
# JSON 落盘（只读；--limit 控制样本条数）
/home/adminroot/miniconda3/envs/smart-help-web/bin/python deploy/policy_data_audit.py \
  --report --limit 20 --json /tmp/policy_data_audit.json
# 指定其它库（仍只读）
/home/adminroot/miniconda3/envs/smart-help-web/bin/python deploy/policy_data_audit.py \
  --url 'postgresql+psycopg2://user:pass@host:port/db' --json -
```

参数：`--report`（控制台报表）、`--json PATH|-`（JSON 输出，`-` 为 stdout）、`--limit N`（样本/清单截断，默认 50）、`--env-file PATH`（默认 `deploy/web.env`，不存在则忽略）、`--url URL`（优先于 env/config）。

## 9 风险与未做项

- **只建议不执行**：本报告没有任何写操作；归档/合并/回填需另立变更单与审批。
- **单次扫描**：真实库只跑一次（2026-11-08），计数会随上游导入变化；建议每次导入批次后复跑并对比。
- **`policy_scope` 一致性只报不修**：15493 行已只读核对（§10），唯一不一致为 `X-JY-038` 的 `regions='全省'` 写法，需数据侧统一为 `江西省全省`；工具不改任何行。
- **跨级同名的人工结论不在数据里**：145 名需要业务确认，机器无法替代。
- **临时证据文件**：`/tmp/policy_data_audit_20261108.json` 与 `/tmp/audit_report_verify.txt` 为本次运行产物，重启机器会丢失；需要长期留存应归档到文档仓库或对象存储。

## 10 policy_scope 一致性只读报告（policy-data-audit-2 新增）

本报告只读比对 `policy_scope`（15493 行；主键 `policy_code`+`detail_id`+`region_key`）与 `policy` / `policy_detail` 的映射，不做任何修复。政策级 scope 行以 `detail_id='-'` 表示（Kingbase 中空串等于 NULL，导入侧用 '-' 占位）。四项口径与真实计数（2026-11-08 只读库 `policy_db_mig20261021`）：

| 项 | 定义 | 真实计数 |
|---|---|---|
| 明细无 scope 行 | `policy_detail.region` 非空但无对应 detail scope 行，按 (policy_code, detail_id) 匹配 | 0 行 / 0 政策 |
| 孤儿 scope 行 | scope 行的 detail_id 在 `policy_detail` 不存在；detail_id 全局唯一时按 detail_id 匹配，否则按 (policy_code, detail_id)，并单列"政策码不一致" | 0 行（detail_id 全局唯一=True、重复 0）；政策码不一致 0 |
| 政策级 scope 与 regions 不一致 | `policy.regions` 拆分 token 归一后的键集合 vs 该政策 policy 级 scope 行的 region_key 集合，差集非空即不一致 | 1 政策（缺 1 / 多 1；未归一 token 1、未归一 scope 键 0） |
| 归一失败残留 | scope 行 `scope_level` 为空/NULL，或 `region_key` 以 `U:` 开头 | 0 行（level 空 0 / U: 键 0） |

真实唯一不一致：`X-JY-038`（`regions='全省'`）→ regions 侧归一为 `U:全省`（无省限定，无法落到 P:/C:/X:），scope 侧为 `P:江西省`。属数据写法问题：建议把该政策 regions 改为 `江西省全省`（或在导入归一规则中把裸 `全省` 落为 `P:江西省`），二者取其一并保持一致；关联 docs/19 审计 M2（裸"全省"/"江西省"写法）。

JSON 键 `sections.scope_consistency` 含四块：`detail_without_scope` / `orphan_scope_rows` / `policy_region_mismatch` / `unresolved_scope_rows`，每块均有 `samples` 与 `truncated`；控制台对应 `[g] scope 一致性`。样本受 `--limit` 截断（上限 `SCOPE_SAMPLE_CAP=20`），计数永不截断。audit-2 的建议动作计数因本项从 1110 变为 1111（补 1 条 manual：`X-JY-038` 数据写法修正）。

复现：

```bash
cd /home/adminroot/smart-help
set -a && . deploy/web.env && set +a
/home/adminroot/miniconda3/envs/smart-help-web/bin/python deploy/policy_data_audit.py \
  --report --limit 20 --json /tmp/policy_data_audit_scope_20261108.json
```

风险与边界：

- **只报不修**：不一致行只进建议动作（backfill/manual），不自动改写 `policy_scope` 或 `policy.regions`。
- **单次快照**：计数为 2026-11-08 一次只读扫描；上游导入后应复跑对比。
- **全表载入**：`policy_scope` 15493 行一次性读入内存分区；规模到百万级前需改为 SQL 聚合。
- **政策级不一致只比 policy 级行**：detail 级 scope 的键差异由"明细无 scope 行"与"孤儿"覆盖，不在此项重复计。
- 历史证据：`/tmp/policy_data_audit_scope_20261108.json`（本次运行产物，易丢）。
