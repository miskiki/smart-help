# 19 地区模型与纵向层级策略审计

> 审计日期：2026-11-08；状态：只读审计，不改代码、不写库、不 commit（唯一交付物为本文件）
> 环境：/home/adminroot/smart-help，HEAD=dfc772d，Kingbase policy_db 只读快照（LOAD deploy/web.env 后连接），工作树另有并行未跟踪文件（admin_division.py / data/regions / docs/17-18 / tests/test_admin_division.py）
> 对应任务：task-16；验收 5 项产出见文末索引
> 相关文档：docs/redesign/01-matching-current.md、02-kb-data-current.md、13-rule-readiness-province-only.md、14-m3-data-layer-step1.md、16-region-ask-gate.md

## 0. 结论摘要

1. 现系统的地区模型是「江西省内 + 县区级 + 字符串匹配」：政策数据只有江西，且政策地区值只有两种层级——县区级（policy.regions token 6076 次 / detail 9390 行）与省级（13 条 policy / 13 行 detail，值为「江西省全省」），没有任何城市级、乡镇级、外省值（见 §2.2）。
2. 政策表的 level 只有 X（3057 条，县级）与 S（280 条，省级）；C（国家）=0、U（市级）=0。也就是说「全国政策适配各地」「市级政策」目前连数据入口都不存在（config.py:39 的 DEFAULT_POLICY_REGIONS 里 C='全国' 只是代码约定）。
3. KG 的地区是扁平集合：REGION 节点 109 个（95 个县区 + 13 个功能区 + 1 个伪节点 REGION:江西省全省），REGION 节点之间 0 条边，没有省→市→县层级；政策通过 6088 条 IN_REGION 边直连县区/功能区（3294 条政策有地区边）。「只能做字符串/节点名匹配」是结构决定的，不是实现偷懒。
4. 地区判定目前有 5 套并存口径：kg_retrieval.region_judge 旧启发式（kg_retrieval.py:307-346）、region.py 程序层（region.py:145-408）、grader 地区状态（grader.py:43-64）、retrieval SQL 白名单（retrieval.py:109-120）、smart_matcher 扁平子串（smart_matcher_kb.py:1029-1036），外加 facts 的字符串等价层（facts.py:37-53、:92-94）。唯一收口点是 catalog.applicable_region（catalog.py:76-94），但它只覆盖 evidence 装载路径（flow.py:9、manage.py:6 旧链也走它），召回、规则编译、展示仍各自为政。
5. 最严重的不是「外省不支持」，而是「只到省/市不追问」：家庭写「江西省」时 canonical_levels 判 province、full_flow._region_unresolved 判未解析（full_flow.py:23-34、:196-197），但 service_scope_check（kg_retrieval.py:190-218）对「江西省」返回 ok 而不是 ask，REGION_ASK_GATE 的触发条件要求 scope=='ask'，于是永不触发——不追问、继续在县区政策库里检索、地区 unknown 封顶 B。实测（'江西省' → level=province, scope=ok, unresolved=True）。
6. 程序层与旧层并不等价：政策 regions 为裸「江西省」时旧层对所有江西家庭判不适「无匹配地区」（旧层只认 REGION 节点名或「全省」字样），程序层判适用；数据恰好不用裸「江西省」（只用「江西省全省」），差异暂时潜伏，一旦全国化数据出现「XX省」写法立刻爆发（§3.4 有实测）。
7. REGION_SOFT_FILTER=1 的软过滤没有完全「软」：catalog 只在程序层给出明确 True/False 时覆盖旧结论，程序层返回 None（家庭只到市、zone 与 host 县区等）仍回落旧 region_judge，而旧层会把这类判成「不适」→ evidence_loader 仍硬丢（实测政策「赣州经济技术开发区」× 家庭「赣州市」：程序 None、旧层不适、catalog 级联后为 False → 丢）。这是下一轮 P1 需要修的首要缺口（§3.6 M2/M3）。
8. 功能区是「县区级并列节点」：13 个功能区（geo.py:39-53）作为独立县区维度，与同市其他县区互不覆盖，实测章贡区家庭 vs 赣州经开区政策判 disjoint（False，硬丢）。FP=0 安全（少卡不误放），但语义上功能区由 host 县区部分区域组成，属于已知误伤，建议维表补 parent/overlap 关系后改为 unknown 保留。
9. 政策数据覆盖不是均匀县区级，而是「按发文/落点县区」：5 个县区 0 条政策（东湖区、西湖区、青云谱区、青山湖区、浔阳区），赣州经开区有 112 条明细却与其 host 区县互不覆盖；S 级 40 条有 regions 但无明细 = 全省通用政策，靠 DEFAULT_POLICY_REGIONS['S']='江西省全省' 兜底（config.py:39）。这个默认值在全国化后会把外省 S 级政策错误地默认成「江西全省」，是 FP 方向风险（§3.6 M10）。
10. 已有全国化雏形但未接入：src/policy_core/admin_division.py（395 行）+ data/regions/china_divisions.json（55,463 字节，源 modood/Administrative-divisions-of-China 的 pca.json，MIT，31 个大陆省份、省/市/县区三级名称）+ deploy/build_china_divisions.py + tests/test_admin_division.py。repo-wide grep 无调用点，主链未接入；其 canonical 已能解析北京市朝阳区、湖北省仙桃市、河南省济源市、赣州经济技术开发区等 region.py 解析不了的结构，但其 relation() 对 disjoint 返回 applicable=None（不硬判），与 region.py 的 False 语义不同，集成前必须统一契约（§5.5）。
11. 可观测口径不一致：region.normalized 有 2 个发射点——full_flow.py:198-205 只带 region_status，evidence_loader.py:146-149 带 province/city/county/relation/source；events.py 的 SAFE_KEYS 尚未包含 province/city/county/relation/source（T1 待集成）。回放侧同一事件名字段集不同，会误导地区漏斗分析。
12. FP=0 的现状边界清楚：明确外省 blocked（kg_retrieval.py:204-205）、明确 disjoint 丢弃、unknown/未确认保留并标注、规则/卡片口径未放宽（grader.py:74-125 地区 unknown 封顶 B）。缺口集中在两个方向——「只到省/市该追问而不是放行/丢弃」与「zone/host 重叠、程序 None 被旧层硬判」；后者只少卡不放行。

## 1. 江西硬编码点全清单

### 1.1 分类口径

按「全国化时是否需要改代码」把硬编码分成三类：

- A 类（数据决策）：政策库/规则库/知识图谱里的数据本身就是江西数据（或江西文号域）。全国化不靠改这行代码，靠扩数据；代码只需保证不假设「数据只有江西」。
- B 类（代码假设）：代码词表/正则/默认值/文案写死江西、11 市、100 县区或「仅支持江西」。全国化必须移除/外置，否则新省份接不进来。
- C 类（混合/默认）：兜底默认值、开关、配置里含江西语义，需要显式化或加开关。

### 1.2 A 类：数据决策（不改逻辑，扩数据）

| # | 位置 | 内容 | 后果 | 全国化处置 |
|---|------|------|------|-----------|
| A1 | policy 表（3337 行，见 §2.2） | 只有江西政策，level 仅 X/S | 无全国/市级政策可匹配；C/U 代码路径无数据可验 | 数据治理扩省；level 语义与地区范围绑定校验 |
| A2 | policy.regions / policy_detail.region | 573 / 109 个不同值，全部江西县区或「江西省全省」 | 召回、过滤、规则都基于这些值 | 迁入 region 维表（§5） |
| A3 | KG REGION 节点 109 + IN_REGION 6088 边 | 95 县区 + 13 功能区 + 1 全省，无层级边 | 旧判定只能做节点名/子串匹配 | 全国化时重建节点或废弃 KG 地区匹配 |
| A4 | data/regions/china_divisions.json（新，未接入） | 31 省/市/县区名称表，55KB | 目前无代码引用 | 作为全国区划维表种子（建议改带行政区划代码的源） |
| A5 | geo.py:39-53 FUNCTIONAL_ZONES（13 个） | 功能区→市映射 | 开发区/新区政策无法按普通县区匹配 | 外置为维表数据（zone 节点 + parent/host） |
| A6 | kg_normalization.py:5 文号词表（赣财规/赣卫基层字/九医保字/江西省人） | 江西文号/机关词 | 文号识别是江西域 | 按省份配置词表，不写死 |
| A7 | kg_source_update.py:61 发文机关正则含江西省 | 江西机关名 | 同上 | 按省配置 |
| A8 | policy_rule 表（453 条/254 政策/region_scope 6 值） | 规则库只有 5 县 + 通省 | 其余县走全模型 | 数据扩县；region_scope 改层级化（§4） |
| A9 | 示例/评测文本（showcase.py:166,178、src/eval/*） | 江西场景样例 | 仅演示/评测 | 补省外样例 |

### 1.3 B 类：代码假设（全国化必须移除或外置）

| # | 位置 | 内容 | 后果 |
|---|------|------|------|
| B1 | region.py:19-21 | JX_PROVINCE='江西省'、_JX_PROVINCE_ALIASES=('江西','江西省','赣','赣省') | 归一化只认江西写法 |
| B2 | region.py:27-44 | _OTHER_PROVINCE_FULL 33 个省级全称 + _OTHER_PROVINCE_SHORT 简称 | 外省只识别到省，无地市/县区；外省判定永远粗粒度 |
| B3 | region.py:55-65 | _CITY_CANON 11 市简称表、_CITY_OF_COUNTY、_CITY_OF_COUNTY_ALL（县区+功能区归市） | 城市层级硬编码江西 11 市 |
| B4 | region.py:68-82 | _strip_admin_suffix、_COUNTY_CANON（县区去后缀简称表） | 只覆盖 geo 的 113 个单位 |
| B5 | region.py:100-128 | _jx_marker、_outside_phrase、_other_province_in、_short_other_hit | 「江西以外」靠语序正则，不靠维表 |
| B6 | region.py:145-229 | canonical_levels：江西词表匹配 + 外省 33 省名 + 反推「江西省」 | 全国化核心单点：需要换全国维表 |
| B7 | region.py:236-275 | _has_place / _covers / _same_place / _known_conflict | 层级包含关系建立在江西词表上 |
| B8 | region.py:278-295 | _relation_from_parsed：外省无层级→disjoint、政策 nation→ancestor 等 | 关系定义可复用，但解析输入受 B6 限制 |
| B9 | region.py:310-323 | region_relation(household, policy_region) | 同上（接口保留） |
| B10 | region.py:354-384 | policy_region_verdict：程序优先裁决 True/False/None | 同上 |
| B11 | region.py:387-408 | equivalent_regions / more_specific_region（facts 变体等价用） | 依赖江西层级比较 |
| B12 | geo.py:14-34 | JIANGXI_COUNTY：11 市、100 个县区 | 原始识别词表的唯一来源 |
| B13 | geo.py:39-53 | FUNCTIONAL_ZONES 13 个 → 市 | 江西功能区硬编码 |
| B14 | geo.py:56-63 | CITY_NAMES=tuple(JIANGXI_COUNTY.keys())、_COUNTY_TO_CITY（县区→市集合） | 市与县区映射硬编码 |
| B15 | geo.py:73-127 | normalize_address：province 仅当 '江西省' in text；outside 仅 户籍/户口/籍贯/老家 后 0-8 字含 省外/外省/江西以外/不是江西 | 外省地市/县区无法解析；「现居省外」等表述不判 outside |
| B16 | geo.py:130-167 | apply_geo_to_facts：仅户籍语境补 hukou_city/hukou_region | 居住地 vs 户籍地语义靠语境词 |
| B17 | kg_retrieval.py:143-157 | _prov_of：regex 取省名；直辖市仅全称 4 个 | 省名解析粗糙（外省地市无法继续） |
| B18 | kg_retrieval.py:161-162 | JX_CITY_NAMES=('南昌','九江',...11 市) | 江西 11 市硬编码 |
| B19 | kg_retrieval.py:165-187 | _is_jx_region：城市名+市/县/区 或 r==c 或 r=='江西省'+c | 只认江西 |
| B20 | kg_retrieval.py:190-218 | service_scope_check：非江西→blocked「当前系统仅支持江西省范围」，江西或无法识别→ok/ask | 全国化最大单点死门：外省 100% 拦死；只到省返回 ok 导致不追问（M1） |
| B21 | kg_retrieval.py:221-240 | match_region_nodes：精确/后缀/前缀匹配 109 个 REGION 节点 | 与 KG 扁平结构绑定 |
| B22 | kg_retrieval.py:243-304 | _region_apply：全国→适用；全省→比省；节点匹配；启发式子串；matched 空→「不适 无匹配地区」 | 旧判定语义源头；裸「江西省」判不适（§3.4） |
| B23 | kg_retrieval.py:307-346 | region_judge：政策级空→DEFAULT_POLICY_REGIONS；明细级继承/冲突 | 旧口径入口；四值 适用/不适/冲突/地区待细化 |
| B24 | config.py:39 | DEFAULT_POLICY_REGIONS={'C':'全国','S':'江西省全省'} | S 级无 regions 默认江西全省；全国化后外省 S 级政策被默认成江西（FP 风险） |
| B25 | retrieval.py:109-120 | SQL 地区白名单：d.region IS NULL OR IN ('','江西省','全省','全国','国家','省级') OR LIKE '%全省%' OR LIKE '%全国%' | 白名单是江西语义；外省/地市无法表达；county 不存在时完全不限制地区 |
| B26 | evidence_loader.py:82 | 旧行为：region 为空直接 return 0 pack（reason='region_unknown'） | REGION_SOFT_FILTER=1 已软化；开关关闭时是硬 0 |
| B27 | catalog.py:76-94 | applicable_region：程序层明确才覆盖，否则回落 region_judge；旧路径 nodes 默认 _load() | 软过滤不彻底的结构点（M3） |
| B28 | full_flow.py:19-21,23-51 | REGION_ASK_GATE 默认开；_region_unresolved 把 level∈(unknown,province) 视为未解析；_region_gate_advice 文案「江西省服务范围」 | 只到省不追问（M1）；文案江西化 |
| B29 | full_flow.py:196-211 | region_ask 触发条件 gate and scope=='ask' and region_known and _region_unresolved；coverage region_gate/region_status unknown；event region.gate | 触发条件与 scope 语义耦合过紧 |
| B30 | fields.json（region 字段） | label 家庭所在地；meaning/question 写「当前政策库仅收录江西省政策」「当前仅支持江西省内政策」 | 前端/追问文案全国化需改 |
| B31 | recognize.py:31-32,35-49,53,72,283 | JX_CITIES 11 市；REGION_LEXICON 108 值；长名优先；江西文号前缀正则；'江西省全省'/'江西省'→'全省' 归一 | 识别层与江西绑定 |
| B32 | household_flow.py:33-34,346-347 | 追问「家庭所在市／县（区）全称」；省外务工选项「江西省内/江西省外」 | 文案与选项江西化 |
| B33 | household_facts.py:133-135 | work_location 取值 江西省内/江西省外 | 同上 |
| B34 | eligibility_gates.py:116-117 | 地区相关 gate 文案 | 同上 |
| B35 | compile_rules.py:28,31,94-107,149 | region_text_for 用 geo 真实后缀拼「江西省+市+县」；region_tag in ('全省','全国','国家','省级','江西省','')→'*' | 规则编译硬编码江西；'*' 语义混合 |
| B36 | kb_rag.py:825-838 | profile_stream 阶段二 service_scope_check：blocked 直接返回话术、不进召回；ask 追加 region:0 追问 | 与 full_flow 两套地区门 |
| B37 | smart_matcher_kb.py:1027-1036 | 允许 C/S/U/X；地区召回=政策 regions（空则 DEFAULT）与家庭 region 的扁平双向子串匹配，'全省'/'全国' 直接放行 | 无层级、无归一；注释自述 S-ZB-016 新建区漏召回 |
| B38 | policy_preview.py:41-46,89 | _region_matches_county 双向子串；regions 空回落 DEFAULT | 预览与主链口径不同 |
| B39 | kg_rebuild.py:143 | REGION 节点排除 ('江西省全省','全省','江西省') | 图谱构建江西化 |
| B40 | events.py:497 | narrative「家庭所在地区还不能确认属于江西省服务范围」 | 事件文案江西化 |
| B41 | full_advice.py:68；reasoning.py:26,28,39；full_questions.py:253 | 提示词/文案含江西或「本市县」假设 | 模型输出被地域预设 |
| B42 | facts.py:37-53,60-61,74-84,92-94 | _REGION_FIELDS=('region','hukou_region','hukou_city') + region 专用等价/更具体回调（T4 新增） | 等价层依赖 region.py 江西词表 |
| B43 | flow.py:9,30-38；manage.py:6,22-27 | 旧链路 service_scope_check/match_region_nodes/applicable_region | 旧链路也受 B20/B22 影响 |
| B44 | retrieval.py:95,99-102 | county=geo.normalize_address(region)；wide_city=canonical_levels(region).city（软过滤时只加宽） | 依赖 geo/region 词表；宽召回正确但城市值只对江西有效 |

### 1.4 收敛结论

全国化必须移除的「词表级代码假设」12 处：region.py:19-21、:27-44、:55-65、:68-82、:100-128、:145-229、:236-275、:387-408；geo.py:14-34、:39-53、:56-63、:73-127。它们应被「区划维表 + 别名表」替换，region.py 保留为江西兼容层一个版本周期。

全国化必须显式化的「默认/兜底」6 处：config.py:39、kg_retrieval.py:243-304 的「全省→比省」与「无匹配地区→不适」、kg_retrieval.py:307-346 的 DEFAULT 回落、smart_matcher_kb.py:1029-1036 的空 regions 回落、policy_preview.py:89、compile_rules.py:149 的 '*' 语义。

## 2. 纵向层级现状

### 2.1 层级能力矩阵

| 层级 | region.py 识别 | admin_division（未接入） | 政策数据 | KG 节点 | 规则 region_scope | 召回 SQL | 判定 |
|------|---------------|--------------------------|----------|---------|-------------------|----------|------|
| 全国 | _NATION_WORDS（region.py:24） | NATION_WORDS + national 标记 | C 级 0 条 | 0 | '*'（与省级混用） | 白名单含 '全国'/'国家' | region.py ancestor→适用；旧层「全国」→适用 |
| 省 | 江西省 + 33 个外省全称/简称 | 31 省全称/别名 | 13 条「江西省全省」（13 行 detail） | 1 个伪节点 REGION:江西省全省 | 39 条 '*'（doc 02:153） | 白名单 '江西省'/'全省' | region.py ancestor/descendant；旧层「全省」→比省；裸「江西省」→不适 |
| 市 | 11 市全名/简称（region.py:55） | 地级市（含伪地级「市辖区」「省直辖县级行政区划」） | 0 条城市级值（U=0） | 0（无市节点） | 无 | 无法表达 | 程序层 ancestor/disjoint；旧层靠子串启发式 |
| 县区 | 100 县区 + 13 功能区（region.py:77-82、geo.py:14-53） | 全国县区 | 6076 token / detail 9390 行 | 95 县区 + 13 功能区 | 5 县 + '*'（吉安/万安/莲花/新建/于都） | county LIKE + 白名单 | 同县 same/不同县 disjoint；跨级 unknown/ancestor |
| 乡镇/街道 | 不识别（'莲花县琴亭镇'→county 莲花县，靠县级子串命中） | 不包含 | 0 | 0 | 无 | 无 | 归到县区，不细分 |
| 功能区 | 13 个（region.py:61-65 归市；geo.py:39-53） | is_zone + _zone_city | 13 个作为 detail 值（如赣州经开区 112 行） | 13 个县区级节点 | 无 | county LIKE 可命中 | zone vs host 县区→disjoint；zone vs 市→descendant/None |

### 2.2 数据纵向结构（Kingbase 只读快照，2026-11-08）

- policy：总计 3337，在用 3333，停用 4；level 分布 X=3057、S=280、C=0、U=0。
- policy.regions：非空 3297 行，NULL 40 行（全部 S 级），空串 0；不同值 573 个；含「、」的多值拼接 509 行。
- policy.regions token：出现 6090 次、不同 token 110 个；按层级：县区 6076、省级 13（江西省全省）、未知 1（全省）；无外省 token；政策 token 全部能命中 geo 的 113 个县区/功能区词表（唯一例外「全省」）。每政策 token 数：1 个 2788 条、2 个 217 条、3 个 75 条、4 个 39 条、5 个 24 条……最多 52 个（长尾多县政策）。
- policy_detail：总计 9403 行；region 非空 9403、无 NULL/空；不同值 109 个；按层级：县区 9390 行、省级 13 行（detail_id 9391-9403，值为「江西省全省」，对应 13 条 S 政策：S-JY-056 省外务工交通补贴、S-JY-057 一次性创业补贴、S-JY-058 职业技能鉴定补贴、S-JR-010 帮扶小额信贷贴息、S-CY-007 到户产业奖补、S-JK-032 倾斜救助、S-QJ-044 学前教育资助、S-QJ-045 中职免学费、S-QJ-046 本专科国家助学金、S-QJ-047 雨露计划、S-ZB-090 困难残疾人生活补贴、S-ZB-091 重度残疾人护理补贴、S-ZB-089 城乡居民养老保险代缴及待遇）。
- 层级结构自洽：detail 的 109 个不同值 = 95 个县区 + 13 个功能区 + 1 个「江西省全省」，与 KG 的 109 个 REGION 节点一一对应；5 个县区从未出现（东湖区、西湖区、青云谱区、青山湖区、浔阳区）。
- 40 条 S 级政策既无 regions 也无明细，是全省通用政策（S-QJ-043 雨露计划、S-JK-024~031、S-QJ-032~042、S-ZB-079~088、S-JY-052~055、S-JR-009、S-CY-006、S-ZF-007~009、S-QT-001 措施/项目 等），代码靠 DEFAULT_POLICY_REGIONS['S']='江西省全省' 兜底。
- 唯一数据不一致：X-JY-038（level='S'、regions='全省'、名称「江西省职业技能鉴定补贴管理办法」）但明细 region 为 安义县/红谷滩区；其余 3296 条非空政策 regions 与明细 token 集合一致。
- KG：节点类型 SUBSIDY 4164、CONDITION 3867、POLICY 3333、DOC 1624、SOURCE_NOTE 125、REGION 109、ORG 101、POPULATION 47、CATEGORY 15、RISK 12；REGION 之间无任何边；kg_edge 中 REGION 只有 IN_REGION 6088 条，3294 条政策有地区边（政策→县区/功能区，扁平）。
- 规则库：policy_rule 453 条 / 254 个政策编号；region_scope 只有 6 个取值：吉安 131、万安 87、莲花 79、新建 59、于都 58、'*' 39；其中 396 条 status=partial（完整仅 55 条）（见 docs/redesign/02-kb-data-current.md:15、:153）。

### 2.3 缺层/边界场景的当前处理

| # | 场景 | 当前行为（实测/代码） | 风险/缺口 | 建议 |
|---|------|----------------------|-----------|------|
| 1 | 家庭只到省（'江西省'） | canonical level=province；service_scope_check=ok；_region_unresolved=True；但 REGION_ASK_GATE 要求 scope=='ask' → 不追问；县区政策程序层多为 unknown（保留），规则路径不启用，卡片地区 unknown 封顶 B | 违背 docs/16「省名也应追问」意图；用户以为系统在按属地核验 | 追问条件改为 level∈(unknown, province) 或 scope!='ok'；P1 |
| 2 | 家庭只到市（'江西省赣州市'） | level=city；scope=ok；程序层对同市县级政策 unknown（保留）；对非同市县级政策 disjoint（丢）；旧层对部分政策判不适（如 zone 政策），catalog 回落旧层后仍可能丢 | 「只到市」是常见输入，当前大量少卡或按市错误裁决 | 政策县级 vs 家庭市级 → unknown/保留；catalog 程序 None 不回落旧层硬判（M3） |
| 3 | 家庭只到县区（'新建区'） | county 归一成功；同县政策 same；邻县政策 disjoint（硬丢）；省级政策 ancestor | 邻县同名政策被丢（doc 02 的 153→3 主要现象）；软过滤对 unknown 有效、对明确 disjoint 仍丢 | 邻县同名政策可作为参考候选保留并标注，不进入断言适用；P1/P2 |
| 4 | 家庭带街道（'青山湖区上海路街道'） | geo/normalize 命中青山湖区；region.py canonical level=county、county=青山湖区；service_scope=ok | 街道信息被忽略（当前模型无乡镇层），可接受 | 保持；乡镇级等维表/数据具备后再做 |
| 5 | 政策只到省（「江西省全省」） | 程序层 ancestor → 适用；旧层「全省」→比省 → 适用 | 无 | 保留 |
| 6 | 政策只到省但写「江西省」（裸省名） | 程序层适用；旧层判不适「无匹配地区」（实测）；数据当前不用该写法 | 潜伏；全国化后新数据若写「XX省」即爆发（§3.4） | 数据规范 + 旧层退役（P0/P1） |
| 7 | 政策只到市（数据 0 条，代码支持） | 程序层 ancestor（同市）；旧层无市 REGION 节点→多数判不适 | 市级政策一上线旧路径就丢 | 程序层优先 + 市级政策走维表；P0 |
| 8 | 政策只到全国（C 级，数据 0 条） | 程序层 ancestor（对任何家庭）；旧层「全国」→适用；但 service_scope_check 先把外省 blocked，外省家庭到不了判定 | C 级政策无法服务外省（与「全国政策适配各地」直接冲突） | service_scope 全国化（P0） |
| 9 | 直辖市（'北京市朝阳区'） | region.py：province=北京市、outside=True、county 无法解析；service_scope_check=blocked；admin_division（未接入）：province=北京市、city=None、county=朝阳区 | 外省 blocked 是当前预期；内部结构上直辖市缺市层，政策若写「北京市」/「朝阳区」需要维表 | 维表把直辖市建模为省+县区（city 可空），按省/县区判定；P0 |
| 10 | 省直辖县级市（'湖北省仙桃市'、'河南省济源市'） | region.py：province=湖北省/河南省、outside=True、county 无法解析；admin_division：county=仙桃市/济源市、city=None | 同上；政策写「仙桃市」时按县级匹配，写「湖北省」时按省级 | 维表 level 允许 county 直接挂省（city=None）；P0 |
| 11 | 功能区（'赣州经济技术开发区'） | 作为县区级节点；家庭功能区 vs 政策功能区=same；家庭功能区 vs 政策赣州市=ancestor；家庭章贡区 vs 政策赣州经开区=disjoint（硬丢）；'赣州市经济技术开发区'（插「市」）→ region.py 只到 city，程序 None、旧层 F | zone 与 host 县区重叠关系无数据，导致少卡；功能区名变体未覆盖 | 维表加 zone.parent_county/overlap；zone 对 host 家庭改 unknown；P1 |
| 12 | 多值政策（'赣州市、吉安市'） | _policy_scopes 分割；_combine_relations 任一适用→ancestor；旧 _region_apply 逐段 | 任一 token 适用即整体适用，正确；但 detail 级单值，p/d 复合可能冲突 | 保持；政策生命周期录入时校验多值一致性 |
| 13 | 多值家庭（'新建区、进贤县'） | region.py canonical：city=南昌市、county=None、ambiguous=True；程序层对县级政策 None（保守）；旧层对命中 token 可判适用 | 程序层保守 → 保留标注；旧层可能过度判适用（依赖子串） | 家庭多值保持 unknown（保守）；旧层退役 |
| 14 | 空地区 / detail.region 缺失 | REGION_SOFT_FILTER=1：region 空不再早退，候选带 region_status=unknown 保留；=0：0 pack（旧行为）。catalog：region 空→None；policy regions 空且 detail region 空→None（交给上层缺省/追问） | 软过滤开启后空地区会进入全模型路径（封顶 B），但 service_scope_check 返回 ask 时 REGION_ASK_GATE 会追问（gate 开） | 保持；文案与 coverage 标注一致 |
| 15 | 明确外省（'湖南省株洲市'） | service_scope_check=blocked（话术「当前系统仅支持江西省范围」）；region.py explicit_other=True；grader 地区状态 explicit_other → blocker | 硬拦符合当前数据边界；全国化后需改为「支持省集合」配置 | P0 配置化 |
| 16 | 缺中间层 | 家庭只到市 vs 政策县级 → 程序 unknown（保留）；家庭只到省 vs 政策县级 → 程序 unknown（保留）；政策市级 vs 家庭县级 → ancestor/unknown；均无「按缺失层级补问」逻辑 | 缺层信息的处理分散在多个判定器，口径不一 | 统一到 region 关系契约（§5.2）+ 追问补齐 |

### 2.4 纵向深度结论

- 实际支持到「县区级」，且县区级之上/之下都只有粗粒度：省级只识别到省名与「全省」字样，市级靠 11 市词表 + 子串，乡镇/街道完全不识别（归到县区或城市）。
- 代码支持全国但数据只有江西：region.py 能识别 33 个外省省级名，但无外省地市/县区；admin_division 有全国三级但未接入；政策数据 0 条外省/C/U。
- 直辖地区排布没有统一策略：region.py 对直辖市只取省名（北京市），admin_division 用「市辖区」伪地级并把县区直接挂省；旧层 _prov_of 对直辖市也按省名比较。全国化前需要确定：直辖市=省级+县区两级（city 空），省直辖县级市=省级+县区（city 空），普通地级市=省+市+县区三级；功能区=县区级虚拟节点+host 关系。
- 缺中间层的处理没有一致规则：当前程序层用 _covers/_known_conflict 保守推断（unknown/ancestor/descendant），旧层用子串启发式（可能过度或不足）；建议把「缺层」显式建模为「政策范围 ⊇ 家庭已知范围」的 containment 判定（§5.2）。

## 3. 判定关系定义与误判场景

### 3.1 判定器盘点

| 判定器 | 位置 | 输入 | 输出 | 调用方 | 口径问题 |
|--------|------|------|------|--------|---------|
| region_judge（旧） | kg_retrieval.py:307-346 | policy.regions、detail.region、家庭 region 文本、KG nodes | 适用/不适/冲突/地区待细化 | catalog 旧回退、flow.py:9、manage.py:6、kg_search | 扁平节点/子串；裸「江西省」判不适；DEFAULT 兜底 |
| region.py 程序层（T4） | region.py:145-408 | 文本 → canonical_levels → relation → verdict | same/ancestor/descendant/disjoint/unknown + True/False/None | catalog:87、grader:43-64、evidence 标注 | 江西词表；外省只到省；zone/host 缺重叠关系 |
| grader 地区状态 | grader.py:43-64 | policy、detail、家庭 region | applicable/parent_applicable/unknown/explicit_other/... | 审核分层 | 依赖 region.py；unknown 封顶 B（grader.py:104-117） |
| retrieval SQL 白名单 | retrieval.py:109-120 | 家庭 region 文本 → county | SQL 条件 | 词面召回 | 白名单是江西语义；county 缺失时完全不限制 |
| smart_matcher 扁平子串 | smart_matcher_kb.py:1029-1036 | 政策 regions（空则 DEFAULT）与家庭 region | 召回/不召回 | legacy 匹配 | 无层级无归一；注释自述漏召回案例 |
| facts 字符串等价（T4） | facts.py:37-53、60-61、74-84、92-94 | 定义 + 两值 | 等价/更具体 | facts.record 冲突自愈 | 依赖 region.py；不改用户原话 |

### 3.2 region.py 关系定义（T4 已上线口径）

| 关系 | 定义 | 典型例子（实测） | relation_is_applicable | 置信说明 |
|------|------|------------------|------------------------|---------|
| same | 双方解析到同一最具体层级且各地层级一致 | 家庭「南昌市新建区」vs 政策「新建区」 | True | high |
| ancestor | 政策地区覆盖家庭地区（政策更宽） | 政策「江西省」「南昌市」「全国」vs 家庭县区 | True | high（省级/全国 medium，当家庭只到省/外省时降级） |
| descendant | 家庭地区覆盖政策地区（家庭更宽/信息更少） | 家庭「江西省」「江西省南昌市」vs 政策「南昌市新建区」 | False（不裁决→None） | high |
| disjoint | 双方在同一层级上明确不同，或明确外省 | 「南昌县」vs 家庭「新建区」；「湖南省株洲市」vs「江西省」 | False | high（省级/外省 medium） |
| unknown | 任一方无法解析，或层级不可比 | 家庭「火星镇」；政策「全省」；家庭只到市 vs 政策县级 | None | low |

补充规则：政策地区为全国 → ancestor（不论家庭，包括外省；但上游 service_scope_check 会先拦外省）；家庭明确外省且无省市县层级（如「外省」）→ disjoint；任一层级双方明确不同 → disjoint；政策多值任一适用 → ancestor、全部 disjoint → disjoint、否则 unknown（region.py:333-341）；政策级与明细级复合见 §3.3。

### 3.3 政策级/明细级复合口径

- region.py policy_region_verdict（region.py:354-384）：家庭无信息→None；policy regions 与 detail.region 都空→None；有 detail.region 时优先看明细：明细适用且（政策空或政策适用）→True、明细 disjoint 且（政策空或政策 disjoint）→False、其余 None；无 detail.region 时政策适用→True、政策 disjoint→False、其余 None。
- 旧 region_judge（kg_retrieval.py:307-346）：政策级未知且明细不适→不适；政策与明细矛盾→冲突（catalog 映射 None 保留）；明细未知→地区待细化（None）；政策级空→DEFAULT 兜底后再判。
- 差异点：旧层「政策『江西省』+明细『江西省全省』」会判冲突（实测 catalog off=None），程序层判适用（on=True）；旧层对裸省名政策判不适、程序层适用。

### 3.4 实测对照矩阵（catalog.applicable_region，env=1 vs env=0；region.py 关系/程序结论并排）

| policy.regions | 家庭 region | 关系 | 程序层 | 旧层（env=0） | catalog（env=1） |
|----------------|-------------|------|--------|----------------|------------------|
| 江西省 | 南昌市新建区 | ancestor | True | False | True |
| 江西省全省 | 南昌市新建区 | ancestor | True | None | True |
| 南昌市 | 南昌市新建区 | ancestor | True | True | True |
| 南昌县 | 南昌市新建区 | disjoint | False | False | False |
| 南昌市新建区 | 南昌市新建区 | same | True | True | True |
| 南昌市新建区 | 江西省南昌市 | descendant | None | True | True |
| 江西省 | 江西省 | same | True | False | True |
| 全国 | 湖南省株洲市 | ancestor | True | True | True |
| 全国 | 南昌市新建区 | ancestor | True | True | True |
| 赣州市 | 赣州市章贡区 | ancestor | True | True | True |
| 章贡区 | 赣州市章贡区 | same | True | True | True |
| 赣州经济技术开发区 | 赣州市章贡区 | disjoint | False | False | False |
| 赣州经济技术开发区 | 赣州市 | descendant | None | False | False（回落旧层） |
| 赣州经济技术开发区 | 赣州经济技术开发区 | same | True | True | True |
| 江西省 | 湖南省株洲市 | disjoint | False | False | False |

读法：catalog 程序层非 None 时以程序层为准；程序层 None 时回落旧层——所以第 13 行「家庭只到市 vs zone 政策」在软过滤开启时仍被硬丢（旧层不适），这就是 §3.6 M3。第 6 行反向说明旧层可能把「家庭只到市 vs 县级政策」判成适用（子串启发式），程序层则 unknown（保留）。

### 3.5 zone（功能区）口径

- 定义：13 个功能区（geo.py:39-53）作为县区级独立节点；region.py 的 _CITY_OF_COUNTY_ALL = 县区 + 功能区 → 市，功能区有市归属但无 host 县区归属。
- 实测：家庭功能区 vs 政策所属市 → ancestor（True）；家庭 host 县区（章贡区）vs 政策功能区（赣州经开区）→ disjoint（False）；家庭「赣州市」vs 政策功能区 → descendant（程序 None）；家庭写「江西省赣州市经济技术开发区」（功能区名插「市」）→ 程序只能解析到 city，对 zone 政策 descendant（None），旧层不适。
- 语义缺口：功能区通常由 host 县区（或若干街道/镇）部分区域组成，与 host 县区不是互斥关系；当前「disjoint 硬丢」在 FP=0 上安全（少卡不误放），但会丢真实可参考政策。建议维表补 zone.parent_county / overlaps，且对「家庭 host 县区 vs 政策 zone」返回 unknown（保留标注），不判适用（不误放行）。
- 开关：REGION_FUNCTIONAL_ZONES 默认开，置 0 时 region.py 退化为「只认民政县区」，功能区政策失效（docs/redesign/14-m3-data-layer-step1.md:65、:91）。

### 3.6 误判场景清单（按严重度）

M1【P0，行为缺口】家庭只到省不追问：'江西省' → level=province、scope=ok、_region_unresolved=True，但 REGION_ASK_GATE 要求 scope=='ask' → 永不触发；继续检索、地区 unknown 封顶 B，不追问。用户以为系统按属地核验。docs/16 的「省名也应追问」意图未实现。

M2【P0，口径冲突】裸省名政策：政策 regions='江西省' 时旧层全判不适（实测），程序层适用；数据当前只用「江西省全省」，差异潜伏。全国化后政策写「XX省」会立刻导致旧回退路径丢政策或误判。

M3【P0，软过滤不彻底】程序层 None 时回落旧层硬判不适：如「赣州经开区」政策 × 「赣州市」家庭（实测 catalog env=1=False）、「赣州经开区」×「章贡区」（程序 False）。REGION_SOFT_FILTER=1 只在程序明确 True/False 时生效，程序 None 被旧层「不适」覆盖后仍硬丢 → 违背「unknown 保留并标注」的改造目标。建议：软过滤开启时，程序 None 且旧层为不适 → 返回 None（除非 region.explicit_other 明确外省），保留候选并标注 region_signal=unconfirmed。

M4【P1，少卡】县级政策 vs 家庭只到市：旧层部分判不适（zone 例）；程序 None。若开关关闭或 M3 未修，丢真实可参考政策。

M5【P1，少卡】邻县同名政策：家庭新建区 vs 政策南昌县/其他县 → disjoint 硬丢（doc 02 的 153→3 主因之一）。可考虑把「同市邻县」降级为参考候选（标注 not_applicable_but_reference），不进入断言适用。

M6【P1，少卡】zone/host 重叠：见 §3.5。

M7【P1，变体】功能区名插「市」（'赣州市经济技术开发区'）：程序只到 city、旧层不适；建议词表增加「X市X开发区」变体。

M8【P2，过度适用风险】旧层子串启发式：旧 _region_apply 用「r0 in pp or pp in r0」判适用，可能把简称与县名互相包含判成适用；程序层较保守。若全国化后旧层仍参与回落，需用矩阵单测固定每类变体。

M9【P1，结构缺口】直辖市/省直辖县级市：region.py 只解析到省，区县不识别；外省 service_scope 直接 blocked，所以当前不影响生产；但全国化后如果只把 service_scope 放开，这些结构会落到 unknown/错判。admin_division 已能解析，需接入。

M10【P0，FP 风险】DEFAULT_POLICY_REGIONS：S 级无 regions 政策默认「江西省全省」。全国化后外省 S 级政策会被默认成江西（对江西家庭误判适用，对外省家庭又 blocked）——这是会误放行的方向。必须改为显式政策归属省份/无默认。

M11【P1，FP 风险】compile_rules region_scope='*'：当前 '*' 同时表示「省级」「通省」「无法细分」，规则加载 where region_scope=county or '*'（docs/01:124）。全国化后 '*' 可能把全国政策规则套到某县，或把某市规则跨市共享。需按层级拆 national/province/city/county。

M12【P2，FP 风险】X-JY-038 类数据不一致（level='S' 但明细是县区）：规则编译/默认兜底路径可能把它当省级；政策生命周期需要「level 与 region 层级一致性」校验。

M13【P2，幻觉风险】多值政策任一 token 适用即整体适用：region.py 与旧层都是并集语义，正确；但如果数据把多个不相关县拼错（509 行多值），会扩大适用面。需录入校验。

M14【P2，可观测】region.normalized 双发射（full_flow.py:198-205 与 evidence_loader.py:146-149），字段集不同；events.py SAFE_KEYS 未含 province/city/county/relation/source，envelope detail 会丢字段（T1 集成待办）。

M15【P2，集成】admin_division（未接入）与 region.py 语义不一致：admin_division.relation 对 disjoint 返回 applicable=None（不硬判），region.py policy_region_verdict 对 disjoint 返回 False（丢）；同时存在两套 canonical/级联词表，接入前必须统一契约、避免双真源。

M16【P3，旧链】flow.py/manage.py 旧链仍直接调 region_judge/match_region_nodes；full_flow 与 kb_rag.profile_stream 两套地区门（B36）可能对同一家庭给出不同结论。

### 3.7 FP=0 边界（当前实现）

| 情形 | 处理 | 是否可能误放行 |
|------|------|----------------|
| 明确外省（省名可识别） | service_scope_check blocked；region.py explicit_other；grader explicit_other blocker | 否 |
| 明确 disjoint（同层不同县/市/省） | catalog False → evidence 丢弃 | 否（只会少卡） |
| unknown / 未确认 | evidence 保留并标 region_status=None、region_signal unknown/unconfirmed；grader 封顶 B | 否（卡片边界声明「未按属地逐条核验」） |
| 家庭只到省/市 vs 政策县级 | 程序 None/descendant → 保留；但 M3 时旧层可能判不适 → 丢 | 否（丢） |
| 政策多值任一适用 | ancestor → 适用 | 需数据正确，否则可能扩面（M13） |
| 政策 level=S 且 regions 空 | DEFAULT 江西全省 → 对江西家庭适用 | 全国化后是（M10） |
| 全国 C 级政策 | 程序/旧层均适用；外省被 service_scope 拦 | 当前不会（外省拦死）；放开后需确认全国语义 |

## 4. 全国化最小改造集合

### 4.1 P0（必须，约 1-2 人日；不阻塞数据侧）

1. region 维表数据落地：以 data/regions/china_divisions.json（55KB 名称表）为种子，补行政区划代码/层级/别名/直辖市与省直辖县级市结构；数据源建议换 modood 仓库带 code 的 pcas-code.json 或民政部代码表，产出 region_dim + region_alias（schema 见 §5.1）。build 脚本已有 deploy/build_china_divisions.py（--check 校验覆盖数）。
2. region.py 接入全国词表（或直接在 region.py 内加载 region_dim）：canonical_levels 不再只认江西；保留江西兼容（一个版本周期内江西词表优先，未命中再走全国维表）；relation/verdict 接口不变（§5.2）。
3. config.py:39 DEFAULT_POLICY_REGIONS 去默认：S 级无 regions → 不再默认「江西省全省」，改为显式「待核验/不适用」或由政策所属省份字段推导；ingest 时强制校验（policy_ingest.py:65-69 现在用 DEFAULT 兜底，掩盖缺失）。
4. service_scope_check 全国化（kg_retrieval.py:190-218）：改为「支持省份集合可配置」：省份在支持集合→ok；省份明确不在→blocked（可关）；无法识别/只到街道/只到省→ask（交给追问门）；新增 REGION_NATIONWIDE 开关（默认 0 回退当前江西-only 行为）。

### 4.2 P1（关键，约 2-3 人日）

5. catalog.applicable_region 软过滤语义修正（M3）：REGION_SOFT_FILTER=1 且程序层 None 时，旧层「不适」降级为 None（保留标注），除非 region.explicit_other 明确外省；关闭开关严格回退旧行为。此改动只影响候选可见性，不改变资格口径（FP=0）。
6. 追问门条件修正（M1）：full_flow.py:196-197 的触发条件从 scope=='ask' 放宽为「scope!='blocked' 且 _region_unresolved」或直接把 level∈(unknown, province) 纳入；确保「只到省/市」获一次追问（文案走确定性分支，不出卡）。
7. 召回层接维表（B25）：retrieval.source_keyword_hits 的 SQL 白名单改为「region_dim join / IN（全国、全省、用户省、用户市、用户县）」，保持宽召回（NULL/全国/全省 + 相邻未知保留），只在明确外省/明确 disjoint 时收缩；不再写死 '江西省'。
8. compile_rules 层级化：region_scope 从 county/'*' 改为 (level, region_id)：national/province/city/county；'*' 退役，规则加载按层级覆盖（全国⊇省⊇市⊇县）；避免 M11 的跨层误用。
9. 事件/文案统一：region.normalized 单发射点（建议 evidence_loader 或 full_flow 二选一）、events.py SAFE_KEYS 补 province/city/county/relation/source（T1 集成）；fields.json/household_flow/full_flow/events 的「仅支持江西省」文案改为「支持省份集合」动态文案。

### 4.3 P2（结构性，约 3-5 人日）

10. KG 地区层：给 REGION 节点补省→市→县层级边（或直接废弃 KG 地区匹配，统一走 region.py/维表）；IN_REGION 边保留为政策→区域范围（多值）。
11. 数据库 region_dim + policy_region 关联表落地（§5.3），policy.regions/policy_detail.region 保留文本兼容但以关联表为准；迁移校验脚本（每条值必须解析到已知区划，否则进人工队列）。
12. 评测与用例：用 region_dim 自动生成「层级 × 省份」矩阵（本次审计的 113 × 110 样本可脚本化）；补省外/直辖市/省直辖县级市/多值/空值用例；e2e 回归地区断言。

### 4.4 不改造的代价

- 全国政策无法落地：C 级数据 0 条、retrieval SQL 白名单全江西值、service_scope 外省 blocked；即使新增 C 级政策，外省用户也拿不到。
- 新省份接入需同时改 7+ 文件（region.py/geo.py/kg_retrieval.py/retrieval.py/config/fields/文案），每次都要重跑地区矩阵；回归成本随省份数线性增长。
- 政策录入错误被 DEFAULT 兜底掩盖（policy_ingest.py:65-69），审核阶段才发现地区语义不对（X-JY-038 类）。
- 地区漏斗不可解释：召回 SQL、catalog、KG 三处口径不一致；生产 trace 153→150→3 的丢因无法逐条归因到「关系判定」。
- 「只到省/市」用户的体验持续是「不追问 + 不按属地核验 + 封顶 B」，与 docs/16 已上线的追问门意图冲突。

## 5. region 维表与归一接口建议（政策生命周期管理）

### 5.1 维表 schema 草案（建议，不在本次实现）

region_dim（一行一个行政区划/功能区节点）：
- region_id（主键；建议行政区划代码 6 位；功能区用虚拟 code 前缀）
- parent_id（上级 region_id；直辖市/省直辖县级市允许 city 层为空，县区直接挂省）
- level（nation/province/city/county/zone；town 预留）
- name（全称）、short_names（简称数组）、aliases（旧称/俗名/文号简称数组）
- province_id/city_id/county_id（冗余路径，便于 SQL join）
- zone_parent_county_id（功能区 host 县区；普通节点为空）
- status（有效/撤销/合并）、effective_from/effective_to（区划调整版本化）
- source（数据来源）、updated_at

region_alias（别名/写法归一，解决 573 个自由文本值）：
- alias、region_id、alias_type（简称/旧称/俗名/功能区/文号/常见误写）、confidence、source。

policy_region（政策地区范围关联，替代 policy.regions 字符串）：
- policy_code（或 policy_id）、region_id、scope_level、source_field（policy.regions / detail.region）、raw_text（保留原值）、confidence。
- 多值 policy.regions 拆成多行；detail.region 单值一行；「全国」对应 region_id=CN。

### 5.2 归一接口契约（纯函数、无 IO，可直接单测）

建议统一放在 src/policy_core/region.py（或把 admin_division.py 作为实现、region.py 作为兼容封装），接口保持：

- canonical(text, default_province=None) → dict(level, nation, province, city, county, zone, ambiguous, matched, outside)；输入用户原话/政策地区文本，输出结构化层级；无法解析时 level=unknown。
- relation(household, policy_region) → dict(relation: same/ancestor/descendant/disjoint/unknown, confidence: low/medium/high, reason, note)；household/policy 参数接受文本或 canonical 结果（避免重复解析）。
- relation_is_applicable(relation) → bool（仅 same/ancestor 为 True；disjoint False；unknown None 语义）。
- verdict(policy, detail, household) → True/False/None（政策级+明细级复合，供 catalog/evidence 用）。
- equivalent(a, b) / more_specific(a, b)（供 facts 变体等价/更具体保留用）。
- service_scope(household, supported_provinces) → ok/ask/blocked（从 full_flow 解耦；supported_provinces 由配置/task 提供）。
- 约束：不接受数据库连接、不做网络/文件 IO（词表在模块加载时读一次或注入），异常一律回落 unknown；字段不要输出用户原话（事件只发安全标量）。

当前 region.py 已实现 canonical_levels/explicit_other/region_relation/relation_is_applicable/policy_region_verdict/equivalent_regions/more_specific_region（含单测 tests/test_region.py 41 例）；admin_division.py 已实现 canonical/relation/provinces/cities_of/counties_of/is_zone（未接入，含 tests/test_admin_division.py）。两者合并时注意：admin_division.relation 对 disjoint 返回 applicable=None，region.py verdict 对 disjoint 返回 False（过滤语义）；建议对外统一 applicable 布尔/None 三态 + 上层显式决定丢弃策略。

### 5.3 与 policy/policy_detail 的关系与迁移

- 保留 policy.regions、policy_detail.region 文本列（兼容与审计），新增 policy_region 关联表作为判定/召回的唯一真源；写入时由 ingest 解析拆分（多值按「、」等分割），解析失败进 region_unknown 审核队列（不允许静默用 DEFAULT 兜底）。
- 召回 SQL 不再 LIKE 文本，改为 join policy_region + region_dim：候选条件 = policy_region.region_id ∈ {全国, 用户省, 用户市, 用户县, 用户所在 zone 的 host 县区}；NULL/空/「全省」等旧值继续按宽召回处理（迁移期）。
- KG 的 IN_REGION 边与 REGION 节点可从 policy_region/region_dim 重建，不再从文本字符串构建（kg_rebuild.py:143 的排除逻辑可删）。

### 5.4 生命周期治理点

- ingest（政策入库）：region 值校验（区划存在/层级合法/多值去重/level 与 region 层级一致，如 S 级应到省、U 级应到市）；缺失必须显式标「全国/待核验」，禁止 DEFAULT 兜底（当前 policy_ingest.py:65-69）。
- compile（规则编译）：region_scope 用 (level, region_id) 而非县名/'*'；规则可见范围按层级闭包（全国规则对所有省开放——但当前 C 级无数据）。
- retrieve（召回）：维表 join 宽召回；记录 retrieval.region_scope 事件（用户省/市/县、命中的 region_id）。
- filter（过滤）：软过滤保留候选 + region_status/region_signal；只有 explicit_other/明确 disjoint 才丢。
- review（审核/出卡）：地区 unknown 封顶 B；卡片/边界文案声明「未按属地逐条核验」；不允许把「参考候选」写成「适用」。
- replay（回放/评测）：region.normalized 单发射点（安全标量），retrieval/evidence 事件带 region 维度；用 region_dim 生成回归矩阵。

### 5.5 迁移路线与开关回退

- v0（现状，已上线）：region.py 江西词表 + REGION_SOFT_FILTER=1；REGION_FUNCTIONAL_ZONES=1；REGION_ASK_GATE=1。回退：REGION_SOFT_FILTER=0 严格回旧（evidence region 空 0 pack、catalog 走旧 region_judge、retrieval 不加 city OR、facts 不启用地区专用等价、full_flow 地区闸门恢复 region known 才检索）。
- v1（P0/P1）：admin_division 全国维表接入 canonical/relation；region.py 保留江西兼容（先江西词表、未命中再全国）；REGION_NATIONWIDE=1 打开 service_scope 配置化；catalog 软过滤修正（程序 None 不回落旧层硬判）；开关全默认 0（回退 v0）。
- v2（P2）：region_dim/policy_region 落库；retrieval/compile/catalog 全部走维表；KG 地区匹配退役；region.py 江西词表退役。回退：保留文本列与旧 region_judge 一个版本，开关切回。

## 6. 验证方法与限制

- 方法：读代码（HEAD dfc772d + 工作树未跟踪新文件）+ Kingbase 只读 SELECT + 导入模块做判定矩阵（127.0.0.1:54321 policy_db，只读）。
- 未做：全量 113 县区 × 110 token 的旧层/程序层网格统计（脚本受运行环境限制未完成，结论用 15 组代表性实测 + docs/redesign/02 的生产 trace 数据支撑）；真实请求 e2e 未跑；未改动任何生产服务/数据。
- 数字口径：REGION 节点 109 = 95 县区 + 13 功能区 + 1 伪节点「江西省全省」；policy_detail 109 个不同值同构；geo 词表 100 县区 + 13 功能区 = 113；文档 docs/redesign/02-kb-data-current.md:127 的「109 个值」即上述 109（含「江西省全省」）。
- admin_division.py（395 行）与 tests/test_admin_division.py 为并行新增未跟踪文件，未接入主链；本审计把它作为「已具备雏形」引用，接入契约以 §5.2 为准。
- 文档 01/02 中部分行号是历史版本（如 01 说 full_flow.py:146/158），本审计以当前文件行号为准；docs/16-region-ask-gate.md 的验收/生产数据是 2026-11-08 早期快照。

## 7. 附录：关键行号索引

- 地区归一/关系：src/policy_core/region.py:19-44（江西+外省词表）、:55-82（市县县区映射）、:91-93（REGION_SOFT_FILTER）、:120-142（explicit_other）、:145-229（canonical_levels）、:232-275（_has_place/_covers/_same_place/_known_conflict）、:278-323（_relation_from_parsed/region_relation）、:326-341（relation_is_applicable/_combine_relations）、:344-384（_policy_scopes/policy_region_verdict）、:387-408（equivalent_regions/more_specific_region）。
- 识别词表：src/policy_core/geo.py:14-34（JIANGXI_COUNTY 100 县区）、:39-53（FUNCTIONAL_ZONES 13）、:56-63（CITY_NAMES/_COUNTY_TO_CITY）、:73-127（normalize_address）、:130-167（apply_geo_to_facts）。
- 旧判定：src/kg_retrieval.py:143-157（_prov_of）、:161-162（JX_CITY_NAMES）、:165-187（_is_jx_region）、:190-218（service_scope_check）、:221-240（match_region_nodes）、:243-304（_region_apply）、:307-346（region_judge）。
- 召回/证据：src/policy_core/retrieval.py:82-131（source_keyword_hits，:109-120 SQL 白名单）、src/policy_core/evidence_loader.py:77-155（load；:82 空 region 早退、:112 applicable_region、:118-124 region_signal、:139-153 统计/事件）、src/policy_core/catalog.py:76-94（applicable_region 程序优先）。
- 主链：src/policy_core/full_flow.py:19-51（REGION_ASK_GATE/_region_unresolved/_region_gate_advice）、:176-211（地区状态/门/事件）、:225-239（检索门/load_evidence）、:367-377（追问/文案）；src/policy_core/grader.py:43-64（region_status）、:74-125（分层）。
- 配置/字段/文案：src/config.py:39（DEFAULT_POLICY_REGIONS）、src/policy_core/fields.json（region 字段）、src/policy_core/household_flow.py:33-34、:346-347；src/policy_core/household_facts.py:133-135；src/policy_core/compile_rules.py:28-31、:94-107、:149；src/smart_matcher_kb.py:1027-1036；src/kb_rag.py:825-838；src/policy_preview.py:41-46、:89；src/kg_rebuild.py:143；src/events.py:497；src/policy_core/facts.py:37-53、:60-61、:74-84、:92-94。
- 相关文档：docs/redesign/01-matching-current.md:13（三套地区语义）、:178-181（地区硬闸门）、:218-228（判定/召回/证据/改动）；docs/redesign/02-kb-data-current.md:12（153→3）、:59（region_judge）、:119（573 值/509 多值/40 空）、:127（109 值）、:217-219（KG 地区语义）、:238-244（region 丢弃样本）、:262-265（R3 元凶）、:319-321（缺维表）；docs/redesign/13-rule-readiness-province-only.md；docs/redesign/14-m3-data-layer-step1.md:9、:61-65、:91、:97；docs/redesign/16-region-ask-gate.md（追问门现状与遗留）。

