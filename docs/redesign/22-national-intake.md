# 22 全国政策数据接入 CLI 与字段模板（T24）

- 状态：已实现（CLI + 离线校验 + dry-run 报告；--apply 复用了 T19 导入引擎）；测试 39 例全绿
- 日期：2026-11-08
- 任务：T24（全国政策数据接入 CLI + 字段模板 + 质量/覆盖报告）
- 相关：docs/redesign/20-national-policy-lifecycle-plan.md（生命周期与开关矩阵）、docs/redesign/21-policy-lifecycle-implementation.md（T19/T22 实施记录）、docs/redesign/09-p1-intake-contract.md（入库契约）、docs/redesign/23-gray-rollout.md（灰度）
- 代码：deploy/national_intake.py（入口）、src/policy_core/policy_import.py（复用，未修改）、data/incoming/README.md（接入提示）

## 1. 目标与边界

同事/共享渠道拿到的全国政策数据（CSV/XLSX/JSON）通常列名不统一、缺少编号、地区跨省。本 CLI 提供：

1. 一键扫描目录或指定文件；
2. 逐行做字段别名映射与只读校验；
3. 输出质量/覆盖报告（行数、错误行、层级/地区分布、覆盖省份、重复编号、日期冲突）；
4. 默认 dry-run **不写库**；只有显式 `--apply`（且 POLICY_IMPORT=1）才复用 T19 的
   `src/policy_core/policy_import.py` 写库。

边界：

- **不修改 policy_import.py**（T19 引擎由别的任务负责）；本 CLI 只调用其既有函数。
- 不新增第三方依赖（openpyxl 已在用）。
- **不改变判定口径**：导入只解决"政策数据有没有"，不做资格结论；卡片口径仍是"已满足部分条件、政策依据待补齐"。
- 默认零行为变化：CLI 是独立进程，不进入 Web 请求路径；不开 `--apply` 就不写任何业务表。
- **不自动生成政策编号**：真实全国源数据常没有"政策编号"列，缺编号会被明确报错（文件名+行号），需要人工补编号后再导入，避免编造唯一键造成误匹配。
- 不递归扫描子目录，不读旧版 .xls，不处理加密/损坏文件（记为 file_errors）。

## 2. 快速开始

```bash
cd <repo> && set -a && . deploy/web.env && set +a
export PYTHONPATH=src:.

# 1) 完全不连库：只做字段/校验/覆盖统计，退出码 0/1
python deploy/national_intake.py --offline

# 2) 默认 dry-run：连库只读 diff（create/update/noop/conflict），不写库
python deploy/national_intake.py

# 3) 机器可读报告（单个 JSON 文档）
python deploy/national_intake.py --json

# 4) 指定文件或目录
python deploy/national_intake.py --file /path/a.csv --file /path/b.xlsx
python deploy/national_intake.py --dir /path/to/incoming

# 5) 写库（必须显式打开开关；默认关）
POLICY_IMPORT=1 python deploy/national_intake.py --apply
```

可选参数/环境变量：

| 参数/变量 | 含义 | 默认 |
| --- | --- | --- |
| `--file`（可重复） | 指定文件；与 `--dir` 互斥 | 无 |
| `--dir` | 扫描目录 | `NATIONAL_INTAKE_DIR` 或 `<repo>/data/incoming` |
| `--source` | 批次来源标识 | `NATIONAL_INTAKE_SOURCE` 或 `national-intake` |
| `--operator` | 操作人（写 import_batch/audit_log） | `POLICY_IMPORT_OPERATOR` 或 `national-intake` |
| `--offline` | 完全不连库 | 关 |
| `--apply` | 写库；需 `POLICY_IMPORT=1` | 关（默认 dry-run） |
| `--json` | 只输出 JSON 文档 | 关 |
| `--env-file` | 读哪个 env 文件 | `POLICY_ENV_FILE` 或 `deploy/web.env` |

## 3. 接入约定

### 3.1 目录、命名、批次

- 默认目录 `data/incoming/`；可用 `NATIONAL_INTAKE_DIR` 或 `--dir` 覆盖。
  目录不存在时自动创建，并写 `README.md`（已存在则不覆盖）。
- 扫描规则：只扫目录第一层，按文件名排序；跳过隐藏文件（`.` 开头）和 Office 临时文件
  （`~$` 开头）；只接受 `.csv / .xlsx / .json`。
- 建议命名：`<来源>-<范围>-<日期>.csv`，例如
  `国务院-全国帮扶政策-20261108.csv`、`江西省-就业帮扶-20261108.xlsx`。
  文件名会作为 `import_batch.file_name` 保留。
- **每个文件一个批次**（`--apply` 时逐文件单事务）：import_batch 记录
  source/file_name/total/valid/invalid/status/operator；行明细写 import_row。
- 批次幂等见 §7：同批次二次 commit 不再变更；重复 `--apply` 会走 update 而非重复建行，
  但会推进版本（已知限制）。

### 3.2 编码与行号

| 格式 | 读取方式 | 行号规则 |
| --- | --- | --- |
| CSV | 先 UTF-8-SIG，失败回落 GB18030 | CSV 记录序号（1 起，含表头前标题行占位）；错误信息带文件名+行号 |
| XLSX | openpyxl read_only + data_only，逐 sheet | 物理行号（1 起）；错误信息带文件名+sheet+行号 |
| JSON | UTF-8（同样回落 GB18030），支持数组、`{"rows":[...]}`、`{"data":[...]}`、单对象 | 数组下标+1 |

- 表格允许表头前有标题行：读取器寻找第一个包含"政策名称"或"政策编号"列的行作为表头，
  其后的空行跳过，物理行号保持不变，便于回原文件定位。
- 同一目标字段出现多列时，先出现的列优先，并产生 warning。
- 缺少表头 → 该文件记 file_errors（不中断其他文件）。

### 3.3 必填与校验口径

校验复用 policy_import._validate_row（内部会调用 normalize_row 和 policy_scope.rows_for），
因此离线报告与 `--apply` 的库内校验口径一致。

| 规则 | 口径 |
| --- | --- |
| 必填 | 政策编号、政策名称、政策层级 |
| 政策编号 | ≤64 字符，须以字母/数字开头，可含 A-Z0-9_.-/:；同批重复=conflict 整组隔离 |
| 政策层级 | C/S/U/X，接受"国家级/省级/市级/县级"等别名（T19 LEVEL_ALIASES） |
| 行政区域 | 必填；仅当层级=国家级(C) 且未填时默认"全国"+warning，其余层级缺失=错误 |
| 适用条件/对象 | 两者都缺 → warning（只写政策主表，不合成明细） |
| 日期 | 可解析为 YYYY-MM-DD；有值且失效 < 生效 = 错误；无值允许 |
| 状态 | 只认"在用/停用"，缺省按 T19 语义 |
| 地区归一 | 无法解析的地区保留 U:<原文> 待核（warning），不静默丢弃 |

## 4. 字段模板

### 4.1 目标字段与别名

| 目标字段 | 常见列名（别名，大小写/空格不敏感） | 说明 |
| --- | --- | --- |
| policy_code | 政策编号、政策编码、编号、代码、policy_code、code | 必填；唯一键 |
| policy_name | 政策名称、政策名、名称、标题、policy_name | 必填 |
| level | 政策层级、层级、级别、level | 必填；国家级/省级/市级/县级 → C/S/U/X |
| regions | 行政区域、政策所属行政区域、所属行政区域、政策适用范围、适用地区、地区、region | 建议与 level 匹配 |
| conditions | 政策适用条件、适用条件、申请条件、条件 | 明细字段 |
| content | 政策内容、政策正文、内容 | **Policy 无正文字段**：仅在缺"适用条件"时兜底为 conditions 并 warning |
| target | 政策适用对象、适用对象、目标对象、对象 | 明细字段 |
| benefit | 补贴标准、补贴标准/元、补助标准、补贴金额、待遇、标准 | 明细字段 |
| unit / cycle | 补贴单位 / 补贴周期（发放周期） | 写明细 extra.benefit_source |
| effect_date / expire_date | 政策生效日期 / 政策失效日期（生效日期、开始日期、实施日期 / 失效日期、截止日期、结束日期） | 写 policy 主表 |
| issuer / implementer | 政策制定主体部门 / 政策实施部门 | 合并进 level_note：制定部门：X；实施部门：Y |
| doc_ref | 政策文件名称及文号、文号、文件文号、发文字号 | policy.doc_ref |
| process | 申领流程、办理流程、流程、申请流程 | 明细 process |
| attr | 政策属性、属性 | policy.attr |
| category1/2/3 | 政策一级/二级/三级分类 | policy.category1/2/3 |
| contact / phone | 政策联系单位/联系人、联系方式、联系人 / 联系电话、电话 | 明细 contact/phone |
| source / status | 来源、文件来源 / 状态 | policy.source/status |
| level_note | 层级说明、备注说明 | policy.level_note |

### 4.2 明确忽略的列

以下列在真实全国数据中出现，但 Policy 表没有对应字段、或已有别处承载，映射时**显式忽略，
不计为未知列**：

年份/年度、帮扶方式、最高限额/元、享受户数、享受人数、受益类别、
适用劳动能力类型、适用健康状况、适用劳动力年龄、适用致贫返贫类型。

如果未来要按最高限额/受益类别做匹配，需要先扩数据模型，再在 IGNORED_HEADERS 里改为映射。

### 4.3 明细合成规则

- 行内有 conditions 或 target 时，CLI 合成一条明细：
  region（=行级 regions）、benefit、unit、cycle、conditions、target、process、effect/expire。
- 行内两者都没有时不合成明细（validator 会给出"未提供政策明细"warning），只写政策主表。
- 行级 regions/benefit/process/contact/effect/expire 会被 T19 的 _effective_details
  回落到每条明细，因此明细可以只写条件/对象。

### 4.4 示例 CSV

```csv
政策编号,政策名称,政策层级,政策所属行政区域,政策内容,政策适用条件,政策适用对象,补贴标准/元,补贴单位,补贴周期,政策生效日期,政策失效日期,政策制定主体部门,政策实施部门,政策文件名称及文号,申领流程
JY-2026-001,莲花县就业帮扶补贴,县级,莲花县,对稳定就业的脱贫人口给予岗位补贴,脱贫人口且连续稳定就业6个月以上,脱贫劳动力,1000,元,月,2026-01-01,2026-12-31,莲花县人力资源和社会保障局,莲花县就业创业服务中心,莲人社发〔2026〕1号,个人申请→村级初审→乡镇复核→县级审批
JY-2026-002,萍乡市创业担保贷款贴息,市级,萍乡市,对符合条件的创业者给予贷款贴息,创业担保贷款在贷且正常还款,创业个人/小微企业,按LPR的50%贴息,%,年,2026-01-01,2026-12-31,萍乡市人力资源和社会保障局,萍乡市创业贷款担保中心,萍人社发〔2026〕2号,银行申报→人社审核→财政贴息
```

说明：第二条的"补贴标准"是文字（按LPR的50%贴息），T19 的 benefit 是文本字段，允许；
但"补贴单位"填 %、"补贴周期"填 年只是示例，实际以源数据为准。真实全国数据通常没有
"政策编号"列，导入前必须补编号，否则整行报错（这是预期行为）。

## 5. 报告字段与退出码

### 5.1 JSON 报告结构（`--json`）

| 键 | 含义 |
| --- | --- |
| version | `national-intake-1` |
| mode | `offline` / `dry_run` / `apply` |
| intake_dir | 扫描目录（--file 模式为空） |
| files / files_total | 每文件摘要 / 文件数（含读取失败） |
| rows_total / rows_ok / rows_error | 总行数 / 校验通过 / 错误 |
| level_distribution | {C,S,U,X,unknown} 行数 |
| region_distribution | {nation,province,city,county,zone,unknown}，**按 scope 行统计**：多地区行各计一次，同一行级/明细重复地区按 region_key+zone 去重，不代表行数 |
| covered_provinces / provinces | 去重省级行政区数 / 名称列表 |
| duplicate_codes / duplicate_code_list | 编号出现 ≥2 次的数量与明细（code/count/files/rows） |
| date_conflicts / date_conflict_list | 行内失效早于生效 + 同编号多行日期不一致的总数与明细 |
| errors / warnings / unresolved | 错误行（file/row_no/sheet?/policy_code/errors）、提醒、待核地区 |
| file_errors | 文件级失败（读取/表头/分析异常） |
| db | {status: offline/ok/unavailable/skipped, reason?} |
| actions / db_preview | dry-run 库内动作计数与 invalid/conflict/changed（仅连库模式） |
| write / applied / note | 是否写库 / 已提交批次 / 末尾提示（默认含"未写库"） |

### 5.2 退出码

| 码 | 含义 |
| --- | --- |
| 0 | 无错误行，且（连库时）库内校验无 invalid/conflict |
| 1 | 存在错误行 / 文件读取失败 / `--apply` 被拒（离线错误或库内 invalid/conflict）/ 提交失败 |
| 2 | 参数错误（--dir+--file、--offline+--apply、文件不存在）或 `--apply` 时 POLICY_IMPORT=0 |
| 4 | 需要连库但数据库不可用：打印降级离线报告并返回 4（`--offline` 不会出现） |

文本报告包含：模式、扫描目录、数据库状态、文件数、行数（成功/错误）、层级分布、地区分布、
覆盖省份、重复编号、日期冲突、文件明细、错误（前 50）、待核地区、提醒（前 20）、
已提交批次（apply 时）和最后一行提示（"未写库"/"已写库"）。

## 6. `--apply` 写库流程

1. `--apply` 必须同时满足 `POLICY_IMPORT=1`（默认 0）→ 否则退出码 2，不连库。
2. 扫描 + 离线校验：只要有错误行或 file_errors，**拒绝写库**，退出 1。
3. 连库（ADMIN_DB_URL，Kingbase/PostgreSQL 或 sqlite）；失败降级离线报告，退出 4。
4. 库内只读 diff（policy_import._plan_rows，带真实文件/JSON 行号）：出现 invalid
   （库内同编号多行等）或 conflict（同批重复编号）→ 拒绝写库，退出 1。
5. 逐文件单事务：
   create_batch(dry_run=False) → validate_batch → commit；该文件任何失败整批回滚，
   已提交的前序文件保留，退出 1 并在报告 note 说明。
6. commit 的副作用由 T19 引擎负责：写 policy/policy_detail/policy_version/policy_scope，
   写 outbox 三类事件（policy.vector_rebuild / policy.rule_invalidate / policy.kg_sync），
   写 audit_log。**向量/规则/图谱真正生效还需 outbox 消费者（POLICY_WORKER=1）**；
   否则只入库，不重建索引。
7. 回滚已提交批次：python deploy/import_policies.py --rollback --batch-id <id>
   （要求 POLICY_IMPORT=1）。

## 7. 幂等与已知限制

- **同批内重复编号**：同文件内同一 policy_code 出现多行 → 整组标记 conflict、拒绝写入，
  不猜测保留哪行；跨文件重复只产生 warning，`--apply` 按文件顺序依次导入（后者 update 前者）。
- **重复 `--apply` 现在可 noop（2026-11-08 修复）**：原先 policy_detail 落库时不写
  effect_date/expire_date，而 _effective_details 用行级效期回填、明细签名又包含效期，
  于是每次重导都被判成 update（created=0, updated=1）并多升一版 policy_version + outbox。
  现已在 policy_import._insert_details 补齐这两列（明细表本来就有），同内容重导变为
  created=0, updated=0, noop=1，不再无谓推进版本；回归见 tests/test_policy_import.py
  的 CommitTest.test_detail_dates_persisted_and_reimport_is_noop。
  注意：修复前导入的历史明细这两列仍为空，首次重导会因效期补齐 update 一次，之后稳定 noop；
  批量重导前先 dry-run 看 changed 数量。
- **回滚**：只能回滚 status=committed 的批次；回滚恢复上一版本/删除本批新建，并写 outbox。
- **地区归一**依赖 policy_scope/区划数据；无法解析的地区保留待核，不阻断整行（warning）。
- **政策内容**不会落库（Policy 无正文字段），只在无适用条件时兜底为 conditions。
- 真实全国源数据无"政策编号"列是常态 → 全部行报错；需先补编号再导入。

## 8. 灰度与回退

- 默认不写库：不带 `--apply` 时即使 POLICY_IMPORT=1 也只读；`--offline` 完全不连库。
- 开关：POLICY_IMPORT 默认 0。关闭时只读能力不受影响，`--apply` 直接退出 2，
  无法写库；这是与生产隔离的第一道门。
- 建议灰度顺序（低峰、记录命令与 batch_id）：
  1. `--offline` 看字段/行级错误，先修数据；
  2. 默认 dry-run 连库看 create/update/conflict；
  3. 小样本文件（10~50 行）`--apply`；
  4. 校验 import_batch / policy / policy_scope / outbox，抽查页面匹配结果；
  5. 再全量分批导入；任一步异常，用 import_policies.py --rollback --batch-id 回滚。
- 回退：停止调用 CLI；POLICY_IMPORT=0 即关闭写库入口；已写数据按批次回滚。
- 生产隔离：CLI 单独进程，不 restart/kill 生产 gunicorn，不修改 deploy/web.env；
  Web 5000 不受影响。写生产库唯一路径就是 `--apply`（+ POLICY_IMPORT=1），执行前应确认
  连接串与操作窗口，并优先在灰度实例（docs/redesign/23-gray-rollout.md）验证。

## 9. 与 policy_import / import_policies 的关系

- 读取：national_intake 自带矩阵/JSON 读取器（保留文件物理行号、支持全国列名、多 sheet），
  逐文件得到 {row_no, sheet, values}；这是相对 policy_import.load_rows 的必要差异
  （后者不保留文件行号，且会丢弃未识别的全国列名）。
- 映射：_map_to_import 把接入字段转成 policy_import 规范行（含 content/部门/明细合成）。
- 校验：逐行复用 policy_import._validate_row（内部 normalize_row + policy_scope.rows_for），
  保证离线报告与 apply 库内校验同一口径。使用私有函数是权衡（T19 未提供带行号的公共入口）；
  升级 policy_import 时需回归本 CLI 测试。
- 写库：复用 create_batch/validate_batch/commit；不复制写库逻辑，不修改 policy_import.py。
- 与 deploy/import_policies.py 的分工：
  - import_policies.py：面向已经规范化好的单文件，提供 --commit/--rollback/--batch-id，
    偏运维执行；
  - national_intake.py：面向未整理的全国数据（别名、多文件、质量报告、默认 dry-run、
    --offline），负责"数据接入前检查"，写库只是最后的显式动作。

## 10. 测试

tests/test_national_intake.py（39 例，标准库 unittest + 临时 sqlite，不连生产）：

- 字段映射：真实全国中文列名、英文别名、忽略列、content 兜底、制定/实施部门合并、
  无 conditions 不合成明细、数字/日期归一；
- 解析：表头前标题行保留物理行号、缺表头 file_errors、空行跳过、GB18030、XLSX、JSON；
- 校验报告：缺必填（带文件名+行号）、层级别名、地区分布六桶、覆盖省份、批内重复隔离、
  跨文件重复 warning、行内日期冲突、同编号日期不一致、空文件；
- 扫描：env 目录、隐藏/临时文件跳过、自动建目录+README、多 --file、参数冲突、
  不支持扩展名、空目录 JSON shape、文本报告含"未写库"；
- 离线/DB：--offline 不连库、--apply 缺开关 exit2、库不可用 exit4 降级、
  dry-run 不写表、apply 有错误行拒绝且 0 行、sqlite 端到端 apply（policy/detail/version/
  scope/outbox/audit/import_batch）与重复 apply 行为。

命令：

```bash
cd <repo> && set -a && . deploy/web.env && set +a
PYTHONPATH=src:. /home/adminroot/miniconda3/envs/smart-help-web/bin/python -m unittest tests.test_national_intake -v
```

未做：真实生产库 `--apply`（仅临时 sqlite 端到端）、真实全国源文件全量导入、
Web 页面匹配效果验收（依赖 T19/T23 的灰度）。

## 11. 链路预演（2026-11-08，本机真实数据，只读）

本机没有同事/共享的全国数据，先用现有江西省入库格式文件做整链路预演，证明"数据一到即可接入"：

```bash
# 1) 完全离线（不连库）：确认列名映射、层级/地区分布、行级错误
PYTHONPATH=src:. python deploy/national_intake.py --offline \
  --file "data/2026年帮扶政策汇总_入库格式.xlsx"
# 2) 连库只读 dry-run：确认与库内 diff（create/update/noop/conflict）
PYTHONPATH=src:. python deploy/national_intake.py \
  --file "data/2026年帮扶政策汇总_入库格式.xlsx"
```

实测结果（40 行省级政策，只读，未写库）：

| 模式 | 行数 | 错误 | 层级 | 地区桶 | 库内动作 | 退出码 |
|---|---|---|---|---|---|---|
| --offline | 40 | 0 | S=40 | province=40（江西省） | — | 0 |
| dry-run（连生产库） | 40 | 0 | S=40 | province=40（江西省） | create=0 update=39 noop=1 conflict=0 | 0 |

解读：这批行在库里对应的 S 级政策来自另一套汇总（regions 文本与明细不同），所以 diff 判
update=39 是真实差异而非误判；noop=1 说明 diff 能识别完全一致的行。链路（读取→映射→校验→
库内 diff→报告）全通，唯一缺的是真实全国数据文件。

接入真实数据时按 §8 灰度顺序执行：--offline → dry-run → 小样本 --apply（POLICY_IMPORT=1）
→ 校验 import_batch/outbox → 全量分批 → 需要时 --rollback 回滚。
