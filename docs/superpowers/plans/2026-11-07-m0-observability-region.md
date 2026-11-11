# M0 实施计划：可观测性 + 地区软过滤 + 冲突口径程序化裁决 + 预算止血

日期：2026-11-07
目标：让前端看到真实分析链路（>=20 类用户可见事件、刷新可回放），地区不再因写法差异/未知而 0 卡，冲突口径按"程序优先、模型兜底"处理，单轮模型调用从 96-420 收敛到 24-60，FP=0。
依据：用户 m00221 批准（按建议实施）；docs/redesign/00-overall.md §7/§9。

## 全局约束
- 不改判定口径：卡片仍为"已满足部分条件、政策依据待补齐"，禁止资格结论；任何程序化裁决必须保守（宁可标"未确认"，不得误放行）。
- 所有新行为走功能开关，可一键回退：SSE_EVENTS_V2、EVENT_REPLAY、REGION_SOFT_FILTER、CONFLICT_SCOPE_PROGRAM、EVIDENCE_CACHE。
- 生产 Python：/home/adminroot/miniconda3/envs/smart-help-web/bin/python；测试用标准库 unittest（不新增依赖）。
- 改 py 后：kill -HUP 361754（gunicorn master）平滑重载，验证进程还在、日志无异常。
- 小步提交；每个任务独立可验证。
- 只写任务声明范围内的文件；跨模块接口先对齐 docs/redesign/m0-contracts.md。

## 架构 / 接口
1. src/events.py（新）：事件注册表 + envelope（v=evt-1，seq/trace_id/turn_id/kind/phase/exposure/title/status/detail/metrics）+ 标题映射（阶段/模型目的/领域事件）+ 安全字段白名单。
2. src/chain_trace.py：state 增加 seq/turn_id；_emit 为每个事件生成 envelope，订阅者收到 legacy+envelope 合并对象；新增 set_turn/current_trace_id/replay/read_trace。
3. src/conversations.py：prepare 写 qa_log.trace_id；run_turn 发 turn.started/turn.complete/turn.error/cancelled；session/done/error 事件带 trace_id；stream 输出 SSE v2 帧（id/event/data + 15s 心跳）；新增 GET /api/qa/<qa_id>/events（回放，鉴权）。
4. src/models.py + deploy/migrate_m0.py：qa_log 增 trace_id varchar(64)（幂等迁移）。
5. src/static/ask.html：思考面板升级为树形时间线（阶段分组、状态、耗时、条数、模型调用、错误），消息保存 trace_id；刷新历史后可点"分析链路"从回放 API 重建。
6. src/policy_core/region.py（新）+ evidence_loader/retrieval/full_flow/catalog/geo 接入：地区归一/关系判定；仅 explicit_other 硬拦；unknown 保留并标注 region_status=unknown。
7. src/policy_core/reasoning.py：冲突口径三分类（程序可裁决/来源缺失/语义歧义）；程序化处理 + 作用域审计缓存；不确定一律回落模型。
8. deploy/web.env + full_settings.py：预算收敛（MAX_CALLS=60、DEADLINE=300、MAX_SOURCES_PER_TURN=10，可用环境变量覆盖）。

## 任务
T1 事件基础（Lead）：events.py + chain_trace 改造 + 单测 tests/test_events.py。
T2 持久化与回放（Lead）：qa_log.trace_id 迁移、prepare/run_turn 接线、SSE v2 + 心跳 + 回放 API、tests/test_trace_replay.py。
T3 前端时间线（teammate A，仅 src/static/ask.html）：消费 envelope、树形时间线、失败/缺口可见、历史回放入口。
T4 地区软过滤 v0（teammate B）：region.py + 证据加载/检索/全链路接入 + tests/test_region.py。
T5 冲突口径程序化（teammate C）：reasoning.py 三分类 + 缓存 + 开关 + tests/test_conflict_scope_program.py。
T6 预算与开关（Lead）：web.env/full_settings/config。
T7 集成验证（Lead）：单测全绿；生产重载；真实提问 e2e：user 事件>=20、回放成功、地区变体不 0 卡、单轮 calls<=60、FP=0；提交。

## 验收
- SSE 每轮 progress 中 exposure=user 的 envelope >= 20，含 phase/title/status/detail/metrics；刷新后 /api/qa/<id>/events 可回放。
- 地区："江西省" vs "江西省新建区" 不再产生 conflict；region unknown 仍检索并标注；外省仍硬拦。
- 冲突：程序路径命中时 core_full_conflict_scope 调用减少；未命中回退模型；最终卡片判定与回退口径一致（回归样例）。
- 预算：FULL_MODEL_MAX_CALLS=60，真实轮次 calls<=60（不含规则快速路径）。
- FP=0：回归 8 案例 + 至少 3 个地区/冲突场景。

## 实施状态（2026-11-07 09:00）
- T1/T2 已完成并提交 52a4fc1（events.py、chain_trace、SSE v2、回放 API、qa_trace 侧表）。注意：qa_log 属主为 system，应用账号无 ALTER 权限，trace 关联改用 auth 库 qa_trace(qa_id,trace_id) 侧表，见 docs/redesign/m0-contracts.md §7。
- T3/T4/T5 已由 teammate 交付并通过 Lead 复核（工作区，未提交）：
  - T3：src/static/ask.html 树形时间线 + 历史回放，20/20 离线纯函数测试、Vue 模板编译通过。
  - T4：src/policy_core/region.py（390 行）+ catalog/evidence_loader/retrieval/facts/full_flow 接入，tests/test_region.py 41 tests 全绿，REGION_SOFT_FILTER=0 严格回退。
  - T5：src/policy_core/conflict_scope_program.py（263 行）/ reasoning.py +174 行，31 tests 全绿；CONFLICT_SCOPE_PROGRAM=0、EVIDENCE_CACHE=0 可回退，程序路径只做保守排除、不新增放行。
- T6：deploy/web.env 已改（MAX_CALLS 96→60、DEADLINE 420→300、SOURCES≈10）+ 五开关；备份 runtime/web.env.bak-m0-20261107。
- T7 集成验证：
  - 单测 96 tests OK（含新增 conflict 事件注册用例）。
  - e2e 快路径：3.2s、25 progress 帧、user 事件 22、回放 30 事件，PASS。
  - e2e 全模型路径（仅省无县）：113.95s、42 次模型调用（<=60）、210 events 可回放，PASS。
  - 9 案例回归（生产环境变量）：9/9 成功、平均 13.6s、卡片 52 张（莲花 6-7、吉安 10、无地区 2、雨露 3）；对比旧基线（平均 100-250s、0-2 卡），地区/规则链路恢复且不再 0 卡。
  - 待办：生产重载 + 真实 HTTP 验证 + 最终提交。

