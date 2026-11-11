# 23 灰度实例与只读全国模式验证

- 状态：已实现（脚本 + 文档 + 本机临时环境验证通过；生产灰度未启动）
- 日期：2026-11-08
- 任务：T25（单独灰度实例与验证脚本，与生产隔离）
- 相关：docs/redesign/20-national-policy-lifecycle-plan.md（生命周期与开关矩阵）、docs/redesign/21-policy-lifecycle-implementation.md（实施记录）、docs/redesign/16-region-ask-gate.md（地区追问门）、docs/redesign/m0-contracts.md

## 1. 目标与边界

目标：在 127.0.0.1:5001 起一个与生产 127.0.0.1:5000 完全隔离的灰度实例，开启"只读全国模式"，用真实页面/接口验证全国口径与"暂无收录"文案，同时满足：

- 生产 5000 完全不动：不 kill -HUP、不重启、不改 deploy/web.env、不写生产库；
- 一键回退：一条命令停掉灰度，生产无感；
- 可重复验证：启动/状态/停止脚本全部幂等，不删除任何数据文件。

非目标（本任务不做）：

- 不改变判定口径：卡片仍固定为"已满足部分条件、政策依据待补齐"，禁止任何资格结论；
- 不做写侧操作：不发布/停用/删除政策，不导入政策，不消费 outbox；
- 不替换生产、不切流量；全国政策库仍无数据，灰度只验证"未收录"路径与只读语义；
- 不新增依赖、不改生产进程模型。

一句话：灰度 = 同一份代码 + 同一套数据库连接（只读用途） + 独立进程/端口 + 只读全国模式开关；回退 = gray_down.sh。

## 2. 隔离边界与共享资源

| 资源 | 生产 5000 | 灰度 5001 | 隔离/共享说明 |
| --- | --- | --- | --- |
| 进程 | gunicorn master 361754（生产，勿动） | 独立 gunicorn master，pid 记 run/gray.pid | 脚本用 /proc/<pid>/cmdline 校验 gunicorn + 本实例 bind，绝不操作 5000 |
| 端口 | 127.0.0.1:5000 | 127.0.0.1:5001（GRAY_BIND 可覆盖，禁止 5000） | 启动前 socket bind 探测端口空闲 |
| 配置 | deploy/web.env | deploy/web.env + deploy/web.gray.env（后者覆盖开关） | web.env 只读复用，不修改 |
| 日志 | runtime/logs/chain_trace-<pid>.jsonl 等 | run/gray.log；trace 文件按进程 pid 单独命名 | 同目录不同文件；runtime/ 已被 .gitignore 忽略 |
| 数据库 | Kingbase policy_db_mig20261021 + auth 库 | 连接同一套库（只读用途） | 灰度不写 policy 库（写侧全关）；问答会写认证库会话记录，见 §3 |
| outbox | 生产无 outbox 表、POLICY_WORKER=0 | 同样 | 双重保证：没有表、没有消费者 |
| 模型/嵌入服务 | 共享 | 共享 | 灰度压测会争抢资源，验证时段需限流 |
| 代码 | 生产进程已加载的旧版本 | 启动时加载当前工作树文件 | 灰度验证前固定版本；工作树未 review 的改动可能被灰度加载，见 §10 |

## 3. 只读语义边界（重要）

- **policy 库只读**：POLICY_LIFECYCLE=0、POLICY_IMPORT=0、POLICY_AUTOPILOT=0、POLICY_WORKER=0；灰度没有写 policy 表的入口；生产当前也不存在 outbox 表，即使有事件也无人消费。
- **认证库不是只读**：问答接口（/api/qa 等）会写 conversation / qa_log / qa_trace，这是历史行为，与开关无关。因此：
  - 只做页面浏览与只读 HTTP 探测：不写库（本机验证中临时 sqlite 文件均未创建）；
  - 在灰度提真实问题：会写生产认证库的会话记录（qa_log 等）。验证时需在受控窗口进行并接受该记录，或临时把 SMART_HELP_AUTH_DATABASE_URL 指向副本/临时库（本机验证采用临时 sqlite）；
  - 本文档验证清单里的"日志无写库语句"指 policy 库；认证库写入是已知副作用，见 §10。
- 只读 ≠ 不调用模型：灰度仍会调用共享模型服务做推理，只是不写业务表、不给资格结论。

## 4. 前置条件

1. 5001 空闲：ss -tln | grep ':5001'（无输出）；若被占用，脚本会打印占用详情并退出，不要 kill 未知进程。
2. 生产健康：curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:5000/ 返回 200；master 361754 存活（gray_status 只读探测，不触碰）。
3. Python 与 gunicorn：/home/adminroot/miniconda3/envs/smart-help-web/bin/python 可执行，且 -m gunicorn --version 正常（当前 gunicorn 26.2.0）。
4. 版本确认：灰度启动会加载当前工作树代码，启动前用 git status / git log 固定要验证的版本，避免把未 review 的改动当现网行为。
5. deploy/web.env 存在（只读复用，不要修改；脚本不写入 web.env）。
6. runtime/ 可写（日志与 pid 目录；脚本会 mkdir -p run 与日志目录，并把 pid 目录 chmod 700）。

## 5. 灰度配置准备

灰度配置含真实连接串/口令，**不要提交**。deploy/web.gray.env.example 只列开关、不含任何口令。

推荐方式 A（仓库外副本，runtime/ 已被 .gitignore 忽略）：

~~~
mkdir -p runtime
cp deploy/web.env runtime/gray.env
cat deploy/web.gray.env.example >> runtime/gray.env
chmod 600 runtime/gray.env
~~~

方式 B（仓库内副本）：cp deploy/web.env deploy/web.gray.env 后追加示例、chmod 600、不要 git add。当前 .gitignore 未忽略 deploy/web.gray.env，建议后续由 Lead 加忽略规则（不在本任务 write scope）。

加载顺序：gray_up.sh 先 source deploy/web.env，再 source 灰度 env（后者覆盖前者）。灰度 env 仅覆盖以下 8 个开关：

| 开关 | 值 | 作用 |
| --- | --- | --- |
| SERVICE_SCOPE | national | 地区口径不再默认江西（region.py service_scope_mode）；外省不再一刀切拦截 |
| REGION_NATIONWIDE | 1 | 江西词表解析不出的地区回落全国行政区划库（region.py nationwide_enabled；national 模式强制开） |
| NATIONAL_READONLY | 1 | 只读全国模式：政策库未收录该地区时 0 卡 + 如实说明（full_flow.py 仅此开关生效） |
| POLICY_SCOPE | 1 | 检索阶段用 policy_scope 结构化地区范围三态过滤候选 |
| POLICY_LIFECYCLE | 0 | 关闭 8 条生命周期写 API（404，无副作用） |
| POLICY_IMPORT | 0 | 关闭导入管线 |
| POLICY_AUTOPILOT | 0 | 关闭自动管理 CLI/worker |
| POLICY_WORKER | 0 | 关闭 outbox 消费线程（绝不消费事件） |

默认值：以上开关默认全部关闭（SERVICE_SCOPE 默认 jiangxi、REGION_NATIONWIDE=0、NATIONAL_READONLY=0、POLICY_SCOPE=0、四个写侧默认 0）；不设置即保持现网行为，新增行为全部由 env 开关控制。

可覆盖的运行参数（GRAY_* 环境变量，不必写进 env 文件）：GRAY_ENV、GRAY_BASE_ENV、GRAY_BIND、GRAY_PID_FILE、GRAY_LOG_FILE、GRAY_PYTHON、GRAY_START_TIMEOUT、GRAY_STOP_TIMEOUT；GRAY_ALLOW_WRITES=1 仅用于本机诊断（绕过只读守卫），灰度验证禁止使用。

## 6. 启动 / 查看 / 停止

### 6.1 启动

~~~
GRAY_ENV="$PWD/runtime/gray.env" bash deploy/gray_up.sh
~~~

行为：
- 载入 web.env + 灰度 env，导出 PYTHONPATH=仓库/src:仓库/deploy；
- 校验：只允许绑定 127.0.0.1、拒绝 5000、只读开关必须满足（SERVICE_SCOPE=national、REGION_NATIONWIDE/NATIONAL_READONLY/POLICY_SCOPE 必须开，POLICY_LIFECYCLE/IMPORT/AUTOPILOT/WORKER 必须关）、gunicorn 可用、端口空闲；
- 进程模型与生产一致：gunicorn --chdir src --bind 127.0.0.1:5001 --workers 1 --timeout 600 --graceful-timeout 15 --worker-class gthread --threads 2 app:app，日志追加到 run/gray.log，pid 写 run/gray.pid；
- 幂等：本实例已在运行（pid 存活且 cmdline 含 gunicorn + 本 bind）时打印"已在运行"并 exit 0；pid 记录陈旧时截断记录后继续；pid 指向其它进程时拒绝启动，避免误杀；
- 启动超时（默认 30s）内未监听则打印日志末尾、TERM/KILL 干净退出并返回非 0。

### 6.2 查看

~~~
bash deploy/gray_status.sh
~~~

只读检查：pid 文件/进程存活/gunicorn 类型/bind 参数、端口监听、GET / 状态码、/api/stats 状态码（401/403 视为需登录、接口可达）、生产 5000 首页只读探测、8 个关键开关值、日志文件大小与 mtime（不回显日志正文，避免连接串/口令泄漏）。退出码：0=健康；1=异常或未运行。

### 6.3 停止

~~~
bash deploy/gray_down.sh
~~~

TERM → 等待 GRAY_STOP_TIMEOUT（默认 30s）→ 仍未退出则 KILL；幂等（未运行直接 exit 0）；只操作 cmdline 含 gunicorn + 本 bind 的进程；cmdline 含 5000 或 foreground-5000.ctl 时拒绝停止；只截断 pid 文件，不删除日志/数据；成功打印"生产 5000 未受影响"。

### 6.4 安全守卫汇总

- 启动：只允许 127.0.0.1；端口不得 5000；只读开关不满足则拒绝启动；端口被占用则拒绝；pid 指向非本实例则拒绝。
- 停止：cmdline 必须含 gunicorn + 本实例 bind；含 5000 参数则拒绝；不删除任何数据文件。
- 两个脚本都不引用生产 master 361754 的 pid，不使用 kill -HUP。

## 7. 验证清单

### 7.1 本机已验证项（2026-11-08，临时端口 5002 + 临时 sqlite，见 §11）

| # | 检查项 | 命令/方法 | 期望 | 本机结果 |
| --- | --- | --- | --- | --- |
| 1 | 脚本语法 | bash -n deploy/gray_up.sh / gray_status.sh / gray_down.sh | 全部通过 | 通过 |
| 2 | 首次启动 | GRAY_BIND=127.0.0.1:5002 bash deploy/gray_up.sh | exit 0、打印 pid | 通过（pid=935306） |
| 3 | 重复启动幂等 | 再次执行 gray_up.sh | exit 0、提示已在运行 | 通过 |
| 4 | 只读状态 | bash deploy/gray_status.sh | 全绿、exit 0 | 通过（进程/端口/首页 200/stats 401/8 开关） |
| 5 | 停止优雅 | bash deploy/gray_down.sh | exit 0、日志有 TERM/Worker exiting/Shutting down | 通过 |
| 6 | 重复停止幂等 | 再次执行 gray_down.sh | exit 0、提示未运行 | 通过 |
| 7 | 端口释放 | ss -tln 查 5002 | 已释放、5000 仍在 | 通过 |
| 8 | 生产隔离 | ps -p 361754、curl 5000 | master 存活、5000 首页 200 | 通过，未触碰生产 |
| 9 | 不写库 | 检查临时 sqlite 文件与 run/gray.log | 库文件未创建、日志无 INSERT/UPDATE/DELETE/CREATE TABLE | 通过（均未出现） |
| 10 | 绑定守卫 | GRAY_BIND=127.0.0.1:5000 bash gray_up.sh | 拒绝并 exit 非 0 | 通过（拒绝绑定 5000） |
| 11 | 缺配置守卫 | GRAY_ENV=不存在 bash gray_up.sh | 拒绝并给出首次使用指引 | 通过 |
| 12 | 只读守卫 | 灰度 env 去掉 NATIONAL_READONLY 后启动 | 拒绝启动 | 通过（必须为 1） |

### 7.2 灰度 5001 上待人工/前端验证项

| # | 检查项 | 操作 | 期望 |
| --- | --- | --- | --- |
| 13 | 江西案例行为不变 | 在灰度提江西样例（如"江西省赣州市章贡区…"），与生产 5000 同问题对比 | 卡片数/卡片状态/文案与生产一致；仍是"已满足部分条件、政策依据待补齐"口径；无资格结论 |
| 14 | 外省未收录 → 0 卡 | 提外省样例到市/县区级（如"内蒙古自治区呼和浩特市新城区…"） | 0 卡；回答含"当前政策库还没有收录…本次为只读模式，仅说明政策库收录情况，不生成政策卡片，也不作资格判断。"；trace 有 region.coverage(status=none) |
| 15 | 只读不出资格结论 | 检查 13/14 回答全文与卡片 | 不出现"符合/不符合/可以申领/不符合条件"等资格结论；卡片固定口径 |
| 16 | 地区追问门优先 | 外省只给到省级（如"广东省"） | 先追问市/县区全称（不直接 0 卡，也不套江西政策）；补全后再走 14 |
| 17 | outbox 表不存在 | 只读 SQL（见下） | 生产/灰度共享库中无 outbox 表；灰度不消费事件 |
| 18 | 日志无写库语句 | grep run/gray.log 找 INSERT/UPDATE/DELETE/CREATE TABLE/ALTER TABLE | 0 命中（policy 库方向） |
| 19 | 生产库快照不变 | 验证前后只读 count 对比 policy 行数与 status 分布 | 一致（本次实测 3337 = 在用 3333 + 停用 4） |
| 20 | 认证库副作用登记 | 若在灰度提问，记录 qa_log 新增 | 已知副作用；如要求零写入，用临时 AUTH 库副本 |

只读 SQL（在生产库上执行，必须只读会话；不会写任何表）：

~~~
-- 会话：SET TRANSACTION READ ONLY
SELECT table_name FROM information_schema.tables
 WHERE table_schema='public' AND table_name ILIKE '%outbox%';

SELECT status, count(*) FROM policy GROUP BY status ORDER BY status;
~~~

本机 2026-11-08 只读探测结果：73 张表、无 outbox 相关表、policy 无 deleted_at 列、status 分布 在用 3333 / 停用 4（与 T18 快照一致，未写库）。

## 8. 回退步骤

1. 停灰度：bash deploy/gray_down.sh（TERM 优雅退出；超时才 KILL）。
2. 再执行一次 gray_down.sh 确认幂等（输出"未运行"、exit 0）。
3. 确认 127.0.0.1:5001 已释放、进程已消失：ss -tln、ps 查 gunicorn 参数含 5001。
4. 确认生产：curl 5000 首页 200、ps -p 361754 存活。
5. 数据文件保留：run/gray.log、runtime/logs/chain_trace-*.jsonl 供排查（脚本不删除；如需清理由人工确认后处理）。
6. 进程异常残留时：先 cat run/gray.pid，确认 /proc/<pid>/cmdline 是绑定 5001 的 gunicorn，再 kill -TERM；不要按 5000 的 master 361754 操作。
7. 开关回退：不启动灰度即零影响；deploy/web.env 全程未被修改；生产 5000 不需要重启。

回退成本：一条命令、秒级；生产无感。

## 9. 切正式准入条件（全部满足才可考虑）

- [ ] §7 清单全绿：江西回归样例与生产一致、外省 0 卡与"暂无收录"文案通过产品/合规确认；
- [ ] FP=0：无资格结论，卡片口径不变，未知/歧义一律回落或 unconfirmed；
- [ ] 认证库写入策略确认（问答会话记录写入生产 auth 库可接受，或拆分/复制认证库）；
- [ ] 写侧能力演练完成：生产 outbox 表首次创建、POLICY_WORKER 消费者、生命周期写 API、导入管线在预发/灰度验证通过（当前生产：无 outbox 表、policy 无 deleted_at 列，直接开启写操作前必须受控迁移）；
- [ ] 性能与资源：模型服务/嵌入服务并发余量评估（灰度与生产共享）；5001 只作验证，不承接生产流量；
- [ ] 可观测：region.coverage、coverage_probe 相关事件、错误回落可见；
- [ ] 回退演练通过（本脚本）；
- [ ] Lead review 并统一提交后，按发布流程切换 5000（灰度实例不参与正式流量）。

## 10. 已知风险与限制

1. **全国政策库尚无数据**：只读模式只对"未收录"给出如实说明，不等于提供全国政策服务；外省政策需导入管线落地后才有卡片。
2. **外省只到省级会先追问**：地区追问门（docs/redesign/16-region-ask-gate.md）优先于收录探测；验证外省 0 卡必须给到市/县区级。
3. **grader 口径缺口**：docs/redesign/21-policy-lifecycle-implementation.md §10.3 记录 grader 对 explicit_other 仍可能判 D 硬拦，national 下会误拦外省；灰度验证时关注回答与 grader 差异。
4. **灰度加载当前工作树代码**：生产 5000 进程仍是启动时加载的旧代码，灰度启动时会加载当前文件；验证前固定版本，避免把未提交改动当成现网行为。
5. **共享认证库写入**：灰度提问会写生产 auth 库的 conversation/qa_log/qa_trace；仅只读探测不写。
6. **共享模型/嵌入资源**：灰度并发会影响生产响应，验证需限流并选低峰时段。
7. **灰度 env 口令风险**：部署在仓库内时有误提交风险；推荐 runtime/ 副本（.gitignore 已忽略）、chmod 600、永不 git add；建议后续补 .gitignore 规则。
8. **端口冲突**：5001 被占用时脚本报占用详情并退出，不要 kill 未知进程；可换 GRAY_BIND 临时端口验证。
9. **共享数据库读数**：生产数据变化会实时反映到灰度；前后快照对比需限定时间窗。

## 11. 本机临时环境验证记录（2026-11-08）

为满足"实跑一次 up/status/down 并清理"的要求，使用与生产完全隔离的临时环境：

- 临时 gray env：8 个灰度开关 + ADMIN_DB_URL=sqlite:////tmp/gray_t25/policy.db + SMART_HELP_AUTH_DATABASE_URL=sqlite:////tmp/gray_t25/auth.db + 测试用 JWT secret；GRAY_BIND=127.0.0.1:5002、pid/log 在 /tmp/gray_t25；
- 未使用 5001（保留给正式灰度）、未写生产库、未触碰生产进程。

实测结果：
- gray_up 首启：打印"灰度实例已启动：pid=935306"，exit 0；
- gray_up 再次：打印"灰度实例已在运行：pid=935306 bind=127.0.0.1:5002（幂等退出）"，exit 0；
- gray_status：进程存活、gunicorn、绑定匹配、5002 监听、首页 200、/api/stats 401（需登录接口可达）、生产 5000 首页 200（只读）、8 开关全部通过；结论"灰度实例正常"，exit 0；
- 启动日志关键行：Starting gunicorn 26.2.0 / Listening at: http://127.0.0.1:5002 / Booting worker；
- gray_down：TERM 优雅退出，日志依次出现 Handling signal: term / Worker exiting / Shutting down: Master，exit 0；再次 down 打印未运行、exit 0；
- 停止后 5002 释放、灰度 gunicorn 消失；生产 master 361754 仍存活、5000 仍监听；
- 只读证据：/tmp/gray_t25/policy.db 与 auth.db 均未创建；run/gray.log 中 INSERT/UPDATE/DELETE/CREATE TABLE/ALTER TABLE 命中 0；
- 守卫实证：绑定 5000 被拒绝；灰度 env 不存在被拒绝并给出首次使用指引；去掉 NATIONAL_READONLY 被拒绝启动；
- 关联单测（只跑不改）：tests.test_national_readonly + tests.test_readonly_national_flow 共 41 tests OK（0.433s），覆盖"未收录→0 卡文案、covered=None 放行、地区追问优先、探测不写行"等；
- 清理：临时实例已停止、/tmp/gray_t25 已删除；runtime/logs 下仅新增本机验证产生的 chain_trace-935311.jsonl（约 1KB，runtime/ 已被 .gitignore 忽略）。

## 12. 文件清单

| 文件 | 行数 | 说明 |
| --- | --- | --- |
| deploy/web.gray.env.example | 23 | 灰度开关覆盖示例（不含口令），生成灰度 env 时追加到 web.env 副本 |
| deploy/gray_up.sh | 167 | 启动：配置加载、安全守卫、端口预检、幂等、gunicorn 启动与超时处理 |
| deploy/gray_status.sh | 141 | 只读状态：进程/端口/HTTP/开关/日志信息，健康 exit 0 |
| deploy/gray_down.sh | 78 | 优雅停止：TERM→KILL，幂等，拒绝非灰度进程 |
| run/.gitignore | 2 | 忽略 run/ 下 pid/log 运行产物 |
| docs/redesign/23-gray-rollout.md | 本文件 | 灰度步骤、验证清单、回退与准入条件 |

关联文档：docs/redesign/20-national-policy-lifecycle-plan.md、docs/redesign/21-policy-lifecycle-implementation.md、docs/redesign/16-region-ask-gate.md、docs/redesign/17-policy-coverage-audit.md。
