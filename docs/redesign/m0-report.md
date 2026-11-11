# M0 实施报告：可观测性、地区软过滤、冲突程序化与预算止血

> 状态：已完成并上线（生产 127.0.0.1:5000，2026-11-07 09:02 平滑重载）。
> 基线：`baseline-20261106`（0d20ef1）；分析：43fe976；M0 提交：52a4fc1 → 7bd3d11。
> 验收口径：每轮用户可见事件 ≥20、刷新可回放、地区写法变体不再 0 卡、模型调用 ≤60、
> FP=0（只见"已满足部分条件"，绝无资格结论）。

## 一、一句话结论

M0 把"系统内部在做什么"从 7 个用户事件、无 trace_id、刷新即丢失，升级为
**每轮 ≥20 个真实事件、trace_id 贯穿、JSONL 可回放、前端阶段树形时间线**；
地区写法差异不再把整轮样例打成 0 卡；已验证冲突先程序化求值、只把语义歧义交给
模型；单轮模型调用预算从 96 收紧到 60。生产真实 HTTP 冒烟 5.9s / 22 个用户事件 /
30 条回放事件，9 案例回归 9/9 成功、平均 13.6s、52 张卡，口径未变。

## 二、交付清单（按提交）

| 提交 | 内容 | 关键文件 |
| --- | --- | --- |
| 52a4fc1 | 事件层 v2 + SSE 帧/回放 + qa_trace 侧表 | src/events.py、src/chain_trace.py、src/models.py、src/conversations.py、deploy/migrate_m0.py |
| a07352e | 规则链真实进度事件、收尾事件入流、e2e 验收 | src/events.py、src/policy_core/rule_flow.py、src/conversations.py、tests/e2e_m0_events.py |
| cb0dea0 | 地区软过滤 v0（确定性行政区语义层） | src/policy_core/region.py、catalog.py、evidence_loader.py、facts.py、retrieval.py、full_flow.py |
| 9e0524a | 冲突口径程序化裁决 + 作用域审核缓存 | src/policy_core/conflict_scope_program.py、reasoning.py |
| b66a1d2 | 前端树形时间线与历史回放 | src/static/ask.html |
| 7bd3d11 | 预算收敛 + 5 个功能开关默认开启 | deploy/web.env、docs/superpowers/plans/2026-11-07-m0-observability-region.md |

测试：tests/ 下 96 个 unittest 全绿（事件 17、地区 41、冲突 31、轮次/SSE 7）。
契约与计划：docs/redesign/m0-contracts.md、docs/superpowers/plans/2026-11-07-m0-observability-region.md。

## 三、机制细节

### 1. 事件层（src/events.py + src/chain_trace.py）

- 事件注册表：22 个阶段 + 60 个模型 purpose + 25 个领域事件 + 20 个顶层事件，
  统一分类为 7 个阶段（request/understand/retrieve/evaluate/decide/answer/persist）、
  中文标题、status（running/ok/error/skipped）、exposure（user/audit/internal）。
- envelope：`{v,seq,trace_id,turn_id,kind,phase,exposure,title,status,ts,detail,metrics}`；
  只保留白名单安全字段（87 个键，容器只留计数），家庭地区值不进入 envelope。
- chain_trace：_emit 在锁内递增 state.seq 并写 JSONL（含 seq/turn_id），
  订阅者收 progress_view；模块内 ring 保留最近 50 轮（每轮 ≤4000 事件）供即时回放，
  超出后回扫 CHAIN_TRACE_DIR 的 chain_trace-*.jsonl*。
- SSE v2 帧：`id: <seq>\nevent: <kind>\ndata: <json>\n\n`；15 秒无事件发 `: ping`；
  progress 携带 envelope；session/answer/done/error 帧带 trace_id。
- 回放：GET /api/qa/<qa_id>/events?since=<seq>，校验本人或 admin；EVENT_REPLAY=0 关闭。
- 事件示例：turn.started、region.normalized、facts.updated、rule.loaded、
  rule.evaluated、rule.card、rule.cards、questions.planned、answer.composed、
  conflict.scope.classified/program_route/fallback/audit_cached、model.call、
  stage.*、turn.complete、request.*、trace_summary。

### 2. 地区软过滤 v0（src/policy_core/region.py）

- 纯函数：canonical_levels / explicit_other / region_relation / relation_is_applicable /
  policy_region_verdict / equivalent_regions / more_specific_region。
- 口径：same/ancestor 才判适用；只有明确互不包含才 disjoint（不适用）；
  descendant、无法解析、歧义一律 unknown（保留候选并标注）。
- 接入点：
  - facts.py 地区类字段（region/hukou_region/hukou_city）用行政区等价/更具体规则，
    修 `江西省` vs `江西省新建区` 伪冲突；
  - catalog.applicable_region 程序优先、明确才裁决，其余回落 KG 判定；
  - evidence_loader 地区未知不再整批返回空，保留并标 region_signal；
  - retrieval.sql 地区范围只加宽（追加同市 OR），不收缩召回；
  - full_flow 地区未知仍检索；规则闸门仍要求 region known（规则库按地区编译）。
- REGION_SOFT_FILTER=0 时各接入点逐一断言回退旧行为（tests/test_region.py）。

### 3. 冲突口径程序化（src/policy_core/conflict_scope_program.py + reasoning.py）

- 三分类：program（全部冲突原子可严格求值为 False，或等价变体"事实不弱于要求"）/
  source_missing（引文来自标题等元数据，或决策本身有 source_gaps，保留冲突不排除）/
  semantic（无已验证冲突的 related、未知事实、结构不支持等）。
- program 命中：直接构造与模型同 schema 的 route（单原子叶子 / 多原子 all），
  经同一 _bind/_validate_logic_structure/_scope_value/engine 复核；只有严格 False
  才 exclude，True → related/unconfirmed，None → 回落模型。
- 模型只在 semantic+回落子集非空时调用 core_full_conflict_scope；
  按原顺序合并，模型丢弃的 contested 决策同样丢弃（语义不变）。
- 作用域审核缓存：key 绑定 validation_revision + fact_revision + source_hash +
  logic sha + evidence sha，LRU 256，只缓存 supported/source_unresolved 且无 issues
  的成功结果；EVIDENCE_CACHE=0 完全旁路。
- 安全边界：任何异常（单条/整体）都回落原模型路径；不新增放行路径。

### 4. 前端（src/static/ask.html）

- SSE v2 帧解析兼容旧 data-only 帧、ping、CRLF；envelope 归一（span_start/span_end 合并、
  按 seq 排序）；7 阶段分组、error 置顶、internal 折叠为"内部步骤 N"。
- 树形时间线：状态图标、中文 title、耗时 metrics.ms、detail 计数；默认展开最近阶段。
- 历史消息保存 trace_id；刷新后点"分析链路"按 qa_id 回放重建时间线；
  失败提示"该轮链路未保留"。
- 后端 SSE_EVENTS_V2=0 时前端行为退回旧版。

### 5. 预算与开关（deploy/web.env）

| 开关/参数 | 现值 | 作用 | 回退 |
| --- | --- | --- | --- |
| FULL_MODEL_MAX_CALLS | 60（原 96） | 单轮模型调用上限 | 调回 96 |
| FULL_MODEL_DEADLINE | 300（原 420） | 单轮总预算秒 | 调回 420 |
| FULL_MODEL_MAX_SOURCES_PER_TURN | 10 | 每轮核验来源上限 | 原值 |
| SSE_EVENTS_V2 | 1 | SSE v2 帧 | 0 |
| EVENT_REPLAY | 1 | 回放接口 | 0 |
| REGION_SOFT_FILTER | 1 | 地区软过滤 | 0 |
| CONFLICT_SCOPE_PROGRAM | 1 | 冲突程序化 | 0 |
| EVIDENCE_CACHE | 1 | 作用域审核缓存 | 0 |
| RULE_ENGINE | on | 规则闸门（原有） | off |

- web.env 备份：runtime/web.env.bak-m0-20261107。
- 生产重载：`kill -HUP 361754`（gunicorn master，平滑；worker 重拉后 527949）。

## 四、验收证据（2026-11-07）

### 真实 HTTP（生产 gunicorn，管理员 Bearer，莲花县 c1 家庭）
- HTTP 200，5.9s；32 帧，其中 v2 帧 25/25，progress 25 全带 envelope，ping 0；
- 用户可见事件 22 个（answer.composed / facts.extracted / facts.updated / model.call /
  questions.planned / region.normalized / rule.card / rule.cards / rule.evaluated /
  rule.loaded / stage.model.answer / turn.started）；
- done.qa_id=611，trace_id=25860a8a…；回放 200、30 事件、含 turn.complete；
- qa_log.status=complete、answer_len=1405、match_json 完整；qa_trace.trace_id 与 SSE 一致。

### 全模型路径（in-process e2e，江西省仅省无县）
- 113.95s，cards=2，42 model_calls（≤60），210 事件，回放含 turn.complete；
  trace 70b018705e4443d1aef1de449d05a0a9（该轮早于 T5 代码，用于验证事件规模与预算）。

### 9 案例回归（eval_run，规则路径 + 全模型混合）
- 9/9 成功，平均 13.6s，卡片 52 张；莲花/吉安规则路径 3–7s（原 113–223s）；
- c6 仅省无县走全模型 83.3s / 2 卡；A/B 关闭程序化+缓存 115.0s / 同 2 卡
  （X-ZB-248、X-ZB-399），结论集合一致、耗时约 -28%（单轮样本，非确定性）；
- 卡片仍为"已满足部分条件、政策依据待补齐"口径；62 岁成员未再命中残疾儿童类政策。

### 单元测试
- `cd /home/adminroot/smart-help && /home/adminroot/miniconda3/envs/smart-help-web/bin/python -m unittest discover -s tests -p 'test_*.py'`
- 96 tests OK；覆盖事件分类/白名单/回放、SSE 帧、地区归一与开关回退、冲突三分类/
  等价方向/缓存 LRU/开关旁路。

## 五、数据层偏差（已解决）

qa_log 属主为 system，应用账号无 ALTER 权限（实测 InsufficientPrivilege，
system 超级用户凭据不可用于本集群）。M0 改用侧表 qa_trace：

- 建表：`deploy/migrate_m0.py`（幂等；已执行，owner=应用账号）；
- 结构：qa_trace(qa_id integer PK, trace_id varchar(64) NOT NULL, created_at varchar(32))；
- 应用侧缺失表时降级为 trace_id=None（不阻断问答）；
- 若未来 DBA 放开 ALTER，可平滑改为 qa_log.trace_id 列，回放 API/前端无需改动。

## 六、遗留风险与边界

1. 规则快速路径的"真实实况"仍只到规则加载/求值/出卡粒度，看不到逐条规则的内部
   证据推理——需要 M1 的统一条件求值事件补齐。
2. 前端 internal 事件只折叠展示，未做按需"查看全部"开关；答案仍是整段返回，
   未做分段流式（M2）。
3. 冲突程序化是保守上界：可能多排除（不会多放行），不同家庭上卡片集合可能与
   模型路径不同，需在 M1 用 A/B/C/D 档位与影子对比继续验证。
4. 地区软过滤 v0 只处理省/市/县级别，乡镇/街道/村居与"人户分离"未建模。
5. 回放依赖 ring（最近 50 轮）+ JSONL 文件；日志轮转（10MB×5）后过期轮次不可回放。
6. 规则覆盖仍依赖地区规则库编译（rule_worker），新建区等地区覆盖需持续巡检。

## 七、M1 入口

- 统一条件表示：conditions.py（类型化 atom）+ region.py 深化 + fact_revision 图；
- 候选池统一与分级：A/B/C/D 档、五道硬门转评分、claim ledger；
- 规则前置 + 限定 ReAct 慢路径（≤6 轮/≤12 工具/≤90s/≤12 calls）；
- 回答 300–600 字 claim 驱动逐条引用；segment superseded 口径；
- 观测继续演进：internal 事件按需展开、answer 分段流式、trace 归档与检索。
