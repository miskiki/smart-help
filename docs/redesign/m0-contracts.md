# M0 契约：事件 / SSE / 回放 / 前端

## 1. SSE v2 帧（开关 SSE_EVENTS_V2=1；默认开）
每个事件块：
```
id: 12
event: evidence.load
data: {"type":"progress", ...}
```
- id 仅 envelope 事件带；event 为 kind；data 为单行 JSON。
- 心跳：```: ping\n\n```（无 data 行，前端自动忽略；间隔 15s）。
- 兼容：data 同时保留旧字段 key/stage/purpose/done/ms/error，旧解析器可用。
- 非 progress 事件（session/sources/cards/answer/done/error）data 结构不变，仅追加 trace_id。

## 2. progress data（envelope 事件）
```json
{"type":"progress","key":"stage:retrieval","stage":"retrieval","purpose":null,
 "done":false,"ms":null,"error":false,
 "event":{"v":"evt-1","seq":12,"trace_id":"...","turn_id":530,
  "kind":"retrieval.candidates","phase":"retrieve","exposure":"user",
  "title":"检索候选政策","status":"ok","ts":"2026-11-07T08:30:00+08:00",
  "detail":{"unique_candidates":36},"metrics":{"ms":812.4}}}
```
- phase 枚举：request/understand/retrieve/evaluate/decide/answer/persist。
- status：running/ok/error/skipped。
- exposure=user 才进思考面板；audit/internal 仅回放可见（前端可折叠显示"内部步骤 N"）。
- title 由后端生成中文，前端不翻译 event 名；detail/metrics 只含安全标量（数字/计数/短代码），无用户原文。

## 3. 回放 API
GET /api/qa/<qa_id>/events?since=<seq>
鉴权：登录用户且 qa_log.user_id=当前用户（或 admin）。
返回：
```json
{"ok":true,"qa_id":530,"trace_id":"...","events":[
  {"type":"progress","event":{...}}, ...]}
```
- 优先进程内 ring（最近 50 轮），否则读 JSONL 日志，按 seq 升序；缺失返回 events=[] 且 complete=false。

## 4. 前端（ask.html）
- 消息对象新增 traceId；session/done 事件写入；历史消息从 qa_dict.trace_id 恢复。
- 思考面板：按 phase 分组树形时间线；用户事件默认展开最近阶段，audit 折叠；显示 title、status 图标、耗时、detail 关键计数；error 红色置顶。
- 完成后保留"分析链路"按钮，点击 GET /api/qa/<qa_id>/events 重建完整时间线（含内部步骤）。
- 刷新页面后：历史消息按钮同样可回放；回放失败显示"该轮链路未保留"。

## 5. 事件注册（M0 用户可见目标 >=20 类）
span 阶段：household.parse/policy.load/rules.match/model.parse_judgment/graph.recall/eligibility.verify/vector.search/candidates.merge/consultation.pipeline/answer.pipeline 等，结束事件带 title/耗时/计数。
领域事件：turn.started, region.normalized, facts.updated, retrieval.plan, retrieval.candidates, evidence.load, conflict.scope.requested/resolved/rejected, rule.evaluate, model.call, answer.compose, turn.complete, turn.error, turn.interrupted。
模型调用：full_model.model_call → kind=model.call，title=“模型：<目的中文>”，metrics=耗时。
未知事件：exposure=audit，title 用 kind 兜底，不丢。

## 6. 兼容回退
SSE_EVENTS_V2=0：帧退回纯 data、无 envelope（旧前端/旧行为）。
REGION_SOFT_FILTER=0：地区逻辑回退旧 applicable_region + region_unknown 直接空结果。
CONFLICT_SCOPE_PROGRAM=0：冲突口径全部走原模型路径。

## 7. 回放持久化（qa_trace 表）与迁移

- 目标：刷新页面/事后按 qa_id 找到该轮 trace_id，从 chain_trace ring 或 JSONL 回放事件。
- 现实约束（已实测）：qa_log 表属主是数据库超级用户 system，应用账号 smart_help_auth_app 没有 ALTER 权限，直接加列会报 `must be owner of table qa_log`；但 public schema 对应用账号开放 CREATE。
- 方案：独立映射表（应用账号可建、可写、可查，不动 qa_log）：
  ```sql
  CREATE TABLE public.qa_trace (
    qa_id integer PRIMARY KEY,
    trace_id varchar(64) NOT NULL,
    created_at varchar(32)
  );
  CREATE INDEX ix_qa_trace_trace_id ON public.qa_trace (trace_id);
  ```
- 写入：conversations.prepare() 在 qa_log flush 拿到 qa_id 后，同事务 INSERT qa_trace；commit 在外部模型调用前完成。
- 读取：qa_dict.trace_id / GET /api/qa/<qa_id>/events 都通过 qa_trace 查（表不存在时自动降级 trace_id=None，不影响问答）。
- 迁移：`python deploy/migrate_m0.py --dry-run` 检查，`python deploy/migrate_m0.py` 执行（幂等）。
- 可选升级：DBA 如需把 trace_id 并入 qa_log，可手工 ALTER + 建索引；应用代码不依赖该列，无需改代码。
- 运维顺序：先跑迁移建表 → 再 kill -HUP gunicorn master 重载代码（旧代码不写 qa_trace，新代码表缺失也能降级，但回放会缺数据）。
