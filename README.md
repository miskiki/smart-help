# 智能帮扶 · 政策匹配与问答系统

> 面向防止返贫监测帮扶场景的政策匹配与问答系统：录入家庭情况 → 匹配适用的帮扶政策 → 逐条给出政策条款证据 → 支持多轮追问。
> 所有结论只提示“**已满足部分条件，政策依据待补齐**”，不输出资格判定。

本仓库是**代码与文档仓库**：不含个人信息、不含政策业务数据、不含真实密钥、不含模型权重。
运行所需的私密数据与本地模型必须另行恢复，见 [docs/数据准备与目录说明.md](docs/数据准备与目录说明.md)。

| 项 | 值 |
|---|---|
| 生产作答模型 | **Qwen3.6-35B-A3B**（llama.cpp，本地 GPU，经 `config/model_settings.yaml` 注册） |
| 向量 / 重排 | **Qwen3-Embedding-4B（2560 维）+ Qwen3-Reranker-4B**（本地 GPU） |
| 主数据库 | **Kingbase ES V8**（金仓，`127.0.0.1:54321`），政策库 + 账号库 |
| 回退数据库 | SQLite `data/policy_db/smart_policy.db` |
| 技术栈 | Python 3.10 / Flask 3.1 / Gunicorn / SQLAlchemy / 原生 JS（无前端构建） |
| 数据规模 | 规则库与政策数量随导入变化，以 `bash deploy/status.sh` 实时输出为准 |

---

## 1. 项目是什么

系统把“家庭情况”翻译成“可核验的政策匹配结果”：

1. **输入**：农户家庭自述（口语或表单），例如人口、劳动力、残疾/大病、教育、住房、饮水、收入等情况。
2. **处理**：抽取结构化事实（`src/policy_core/fields.json` 共 82 个字段）→ 地区归一与覆盖判定 → 规则引擎出卡（未命中时回退全库模型链路）→ 向量检索 + 重排 → 逐条证据核验。
3. **输出**：政策卡片（政策名称、层级、适用地区、补贴标准、适用条件、命中/未命中理由、政策条款与原始文件出处）+ 缺项追问。

目标用户是县/乡帮扶工作人员：需要在几分钟内判断“这户人家可能符合哪些帮扶政策、依据在哪一条”，并且**不能错误放行**（设计红线：false positive = 0）。

---

## 2. 核心能力与页面

| 路由 | 页面 | 主要能力 |
|---|---|---|
| `/` | 工作台 / 政策台账 | 政策总览与检索、统计、导出、问卷结果、更新日志 |
| `/admin` | 政策管理后台 | 政策检索与筛选、生命周期（下架/归档/恢复/删除/发布/回滚）、批量导入（预览→批次→提交→回滚）、模型管理（注册、凭据、连通性测试、重建索引、设为默认） |
| `/ask` | 政策匹配与问答 | 家庭情况录入 → 匹配卡片 → 逐条政策依据 → 多轮追问（SSE 流式输出） |

页面为原生 HTML/CSS/JS，源码在 `src/static/`（`index.html` / `admin.html` / `ask.html` 及对应 JS），无构建步骤，改完刷新即生效。

后端 API：

- `/api/*`：管理后台与台账、政策导入、问卷、知识图谱、导出等（`src/api.py`，38 个路由）
- `/api/*`：登录鉴权（`src/authentication.py`）
- `/api/*`：问答会话与流式问答（`src/conversations.py`，如 `/api/qa`、`/api/qa/stream`、`/api/qa/refine`）
- `/api/admin/models*`：模型注册表管理（`src/model_admin.py`）
- `/api/policy-core/*`：政策匹配核心（`src/policy_core/routes.py`）

---

## 3. 技术栈与运行架构

| 层 | 选型 | 说明 |
|---|---|---|
| Web | Flask 3.1 + Gunicorn（`gthread`，1 worker × 2 threads，`timeout 600s`） | 启动入口 `src/app.py:create_app` |
| 数据访问 | SQLAlchemy 2.0 + psycopg2 | Kingbase 优先；未配 `ADMIN_DB_URL` 时回退 SQLite |
| 前端 | 原生 JS + SSE | 无 Node 构建链 |
| 作答模型 | `llama-server`（llama.cpp）+ Qwen3.6-35B-A3B GGUF | 端口 8081，MTP 投机解码 |
| 检索 | Qwen3-Embedding-4B（2560 维）+ Qwen3-Reranker-4B | 端口 8095 / 8097，8096 提供 OpenAI 兼容重排 |
| 规则 | 规则库快照 + `rule_worker.py --watch` | 懒编译缺失区县规则包，无常驻端口 |
| 可选云端 | 阿里云百炼（OpenAI 兼容） | `config/model_settings.yaml` 中注册的平台之一 |

### 端口清单

| 端口 | 服务 | 监听地址 |
|---|---|---|
| 5000 | Web 应用（Gunicorn `app:app`） | `WEB_BIND_HOST`，默认 `127.0.0.1`，可设 `0.0.0.0` 开局域网 |
| 5001 | 灰度只读实例（可选） | 仅 `127.0.0.1` |
| 8081 | 本地作答模型 | 仅 `127.0.0.1` |
| 8095 | 嵌入服务 | 仅 `127.0.0.1` |
| 8096 | 重排兼容层 | 仅 `127.0.0.1` |
| 8097 | 重排模型 | 仅 `127.0.0.1` |
| 54321 | Kingbase ES V8 | 仅 `127.0.0.1` |

### 启动链路（`deploy/foreground.py`）

```text
bash deploy/start.sh
  └─ 读取 deploy/web.env → 预检 模型/文件/端口
     └─ deploy/foreground.py（前台进程编排，Ctrl+C 全停）
        ├─ 8081  llama-server      Qwen3.6-35B-A3B
        ├─ 8095  llama-server      Qwen3-Embedding-4B（--embeddings）
        ├─ 8097  llama-server      Qwen3-Reranker-4B（--jinja）
        ├─ 8096  serve_qwen3_rerank.py   重排 OpenAI 兼容层
        ├─ 5000  gunicorn app:app          Web
        └─ ---   rule_worker.py --watch   规则库 lazy 编译 worker
```

一次问答的关键步骤：地址/地区归一（`policy_core/geo.py`、`region.py`、`admin_division.py`）→ 事实抽取（`full_facts.py`）→ 规则引擎闸门（`rule_flow.py`，未命中回退 `full_flow.py`）→ 检索与重排（`kb_rag.py`、`kg_retrieval.py`）→ 证据核验（`local_evidence.py`、`reasoning.py`）→ 卡片渲染（`result_model.py`）。

### 设计约束（改代码前必读）

1. 卡片口径固定：只写“已满足部分条件、政策依据待补齐”，禁止资格结论。
2. 零误放行：宁可少出卡，不可错出卡（验收线 FP = 0）。
3. 审计保留用户原话（`full_facts.ingest(source_text=...)`）。
4. `RULE_ENGINE=off` 是即时回退开关：规则出问题先关它。
5. `--ctx-size` 是 llama-server 的总池，单请求 = `ctx-size ÷ parallel`；当前 `65536 ÷ 4 = 16384`，配错会让所有问答 400 失败（`foreground.py` 启动时会读 `/props` 自检）。

---

## 4. 仓库目录结构

```text
smart-help/
├─ README.md                    本文件
├─ requirements-frozen.txt      生产冻结依赖（Web + GPU 相关）
├─ .gitignore                   私密数据/密钥/模型/日志排除规则
│
├─ deploy/                      运维与数据工具（唯一启动入口）
│  ├─ start.sh / stop.sh / status.sh       启动 / 停止 / 六段式体检
│  ├─ foreground.py                        进程编排（拉起并守护各服务）
│  ├─ local_llm.py                         llama-server 参数（模型/上下文/MTP）
│  ├─ autostart.sh / install_autostart.sh  守护 + 开机自启（用户 cron）
│  ├─ gray_up.sh / gray_down.sh / gray_status.sh   灰度只读实例（5001）
│  ├─ migrate_*.py                         一次性数据库迁移（默认只读）
│  ├─ seed_models.py                       模型注册表 seed
│  ├─ import_policies.py / national_intake.py  政策导入与全国数据接入
│  ├─ policy_autopilot.py / policy_governance.py / policy_data_audit.py  治理工具
│  ├─ build_china_divisions.py             生成公开行政区划数据（一次性）
│  ├─ web.env.example                      环境变量模板（无密钥）
│  └─ policy_autopilot.cron.example        cron 任务参考模板（不自动安装）
│
├─ src/                        Flask 应用
│  ├─ app.py                   应用工厂与页面路由（/、/ask、/admin）
│  ├─ api.py / conversations.py / authentication.py / model_admin.py  各业务蓝图
│  ├─ kb_rag.py                检索与模型调用核心
│  ├─ config.py                配置与数据库连接（Kingbase / SQLite）
│  ├─ models.py                ORM 模型
│  ├─ rule_worker.py           规则库 lazy 编译 worker
│  ├─ serve_qwen3_rerank.py    重排 OpenAI 兼容层
│  ├─ eval_run.py              当前环境回归评测（案例 src/eval/cases.json 随私密包恢复）
│  ├─ policy_core/             政策匹配核心（事实/规则/证据/导入/生命周期/地区归一）
│  ├─ static/                  前端页面与资源
│  ├─ data/                    ⚠ 占位：政策证据包（外部数据，见下）
│  ├─ uploads/                 ⚠ 占位：运行期上传目录
│  └─ logs/                    运行期链路日志（不入库）
│
├─ data/                       ⚠ 占位：业务数据（外部数据，见下）
│  ├─ *.xlsx / *.wps / *.docx  政策原始资料（业务文件，固定路径）
│  ├─ policy_db/               SQLite 回退库、政策编号台账、导出样例
│  ├─ regions/                 公开行政区划数据（在仓库内，无需外部提供）
│  ├─ incoming/                全国政策投递目录
│  ├─ 个人信息数据/            分类说明 README（真实文件仍在 data/ 根）
│  └─ 政策数据/                分类说明 README
│
├─ models/                     Qwen3 嵌入 tokenizer（GGUF 权重另行准备）
├─ config/                     模型平台配置（example 模板 + 本地真实配置）
├─ docs/                       架构设计、部署指南、审计与历史文档
├─ tests/                      回归与契约测试
├─ runtime/                    运行期 pid / 锁 / 日志（不入库）
└─ run/                        灰度实例 pid / 日志（不入库）
```

目录中带 ⚠ 的位置在仓库里只有 `README.md` 占位与 `.gitignore` 规则，**真实文件需要按 [docs/数据准备与目录说明.md](docs/数据准备与目录说明.md) 恢复**。

---

## 5. 快速启动（5 分钟；首次需先恢复数据与模型）

> 完整的首次部署（系统依赖、Kingbase 建库恢复、模型准备、灰度与排障）请按 [docs/部署与启动指南.md](docs/部署与启动指南.md) 逐节执行。
> 下面的命令都在**项目根目录**执行。

```bash
# 1) 获取代码（公开 main 分支只含代码/脚本/文档/模板/公开区划数据）
git clone https://github.com/miskiki/smart-help.git
cd smart-help

# 2) 恢复私密数据（U 盘私密包的 restore/ 目录，详见数据准备文档）
rsync -a /media/usb/smart-help-private-data/restore/ ./

# 3) 生成/核对真实配置（私密包已带真实配置则跳过复制，但要按部署指南轮换密钥）
cp deploy/web.env.example deploy/web.env
cp config/model_settings.example.yaml config/model_settings.yaml
chmod 600 deploy/web.env deploy/web.gray.env config/model_settings.yaml 2>/dev/null || true
${EDITOR:-vi} deploy/web.env            # 把 CHANGE_ME 换成真实值
${EDITOR:-vi} config/model_settings.yaml

# 4) Python 环境（已有同名 conda 环境则跳过）
conda create -n smart-help-web python=3.10 -y
conda activate smart-help-web
pip install -r requirements-frozen.txt

# 5) 数据库迁移（先只读检查，再加 --apply；脚本幂等）
python deploy/migrate_policy_admin.py --apply
python deploy/migrate_policy_scope.py --apply
python deploy/migrate_models.py --apply
python deploy/seed_models.py --apply
python deploy/migrate_m0.py
python deploy/migrate_vector_multi_model.py --apply

# 6) 启动（前台，Ctrl+C 停止；开机自启见第 7 节）
bash deploy/start.sh

# 7) 验收（另开一个终端，同样在项目根）
bash deploy/status.sh                   # 六段体检，末尾给 ✅ / ⚠️ 结论
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:5000/
```

启动前必须就位的外部依赖（`deploy/start.sh` 会预检并在缺失时直接报错）：

- `llama-server` 可执行文件（需含 Blackwell sm_120 原生 kernel 的源码编译版）
- `Qwen3.6-35B-A3B-UD-Q4_K_M_MTP.gguf`（约 22 GB，作答模型）
- `Qwen3-Embedding-4B-Q4_K_M.gguf`（约 2.4 GB）、`Qwen3-Reranker-4B.Q4_K_M.gguf`（约 2.6 GB）
- `models/qwen3-embedding-4b/tokenizer.json`（缺失时 `foreground.py` 会尝试自动下载）
- `config/model_settings.yaml`（真实配置，含云端平台 api_key，不入库）
- Kingbase 已启动且两个库可连（或临时用 SQLite 回退，见部署指南 §5）

模型路径通过 `deploy/web.env` 的 `LOCAL_LLM_SERVER` / `LOCAL_LLM_MODEL` / `LOCAL_EMBED_GGUF` / `LOCAL_RERANK_GGUF` 指定；不设时使用 `deploy/start.sh` 内置默认路径。

访问：<http://127.0.0.1:5000/>（工作台）、<http://127.0.0.1:5000/ask>（问答）、<http://127.0.0.1:5000/admin>（政策管理）。

---

## 6. 数据与密钥政策

**本仓库（GitHub main 分支）不包含**：

- 任何个人信息（监测对象名单、户匹配结果、问卷结果、问答日志、上传 PDF 等）
- 任何政策业务数据（政策汇总表、工具箱、台账、PDF 样例、证据包 `bundle.json`、SQLite 政策库）
- 任何真实密钥（数据库口令、JWT secret、后台 secret、模型平台 api_key）
- 任何模型权重与 GGUF 文件

这些内容通过**项目外私密包**（U 盘 `smart-help-private-data/`）交付，恢复后只存在本机磁盘，`.gitignore` 已排除。数据清单、目标路径、敏感级别与 SHA256 校验规则见 [docs/数据准备与目录说明.md](docs/数据准备与目录说明.md)。

三条必须遵守的规则：

1. 私密数据**只放本地**，不要提交、不要上传网盘、不要贴进 issue/聊天记录。
2. `deploy/web.env`、`deploy/web.gray.env`、`config/model_settings.yaml` 权限保持 `600`，且永不入库。
3. `data/` 根目录的业务文件**不能改名、不能挪目录**（证据链按文件名 + SHA256 校验），详见数据准备文档。

---

## 7. 常用运维命令

```bash
# 启动 / 停止（停止不会停 Kingbase）
bash deploy/start.sh
bash deploy/stop.sh

# 体检（推荐每天第一条命令）
bash deploy/status.sh

# 守护进程（崩溃自愈）与开机自启
bash deploy/autostart.sh --status
bash deploy/autostart.sh --stop
bash deploy/install_autostart.sh            # 安装/修复（幂等）
bash deploy/install_autostart.sh --remove   # 卸载
bash deploy/install_autostart.sh --status

# 灰度（只读全国模式，独立进程 5001，不影响生产 5000）
bash deploy/gray_up.sh
bash deploy/gray_status.sh
bash deploy/gray_down.sh

# 日志
tail -f runtime/logs/autostart.log                 # 守护/启动总日志
tail -f runtime/logs/service_5000.log              # Web
tail -f runtime/logs/service_8081.log              # 本地作答模型
tail -f runtime/logs/chain_trace-<pid>.jsonl       # 问答链路追踪
ls src/logs/                                       # 历史链路追踪

# 回归评测（改动规则/提示词/参数后跑）
cd src && python eval_run.py
```

`deploy/status.sh` 的六段输出：前置依赖 → 服务与端口 → 数据库与规则库 → 守护与自启 → 3 次解码测速 → 结论。任何 ✗ 都会在末尾汇总，并给出去向。

---

## 8. 排障入口

| 现象 | 排查命令 | 常见原因 / 处理 |
|---|---|---|
| `start.sh` 立即退出，打印“启动预检失败” | 重跑 `bash deploy/start.sh` 看首行 | 缺 `llama-server` / GGUF / `tokenizer.json` / `config/model_settings.yaml`；或 8081/8095/8096/8097/5000 被占用。先 `bash deploy/stop.sh`，再 `ss -tlnp \| grep -E ':(5000\|8081\|8095\|8096\|8097) '` |
| Web 启动报 `请先运行账号数据库迁移初始化命令` | 检查 `deploy/web.env` 的 `AUTH_DATABASE_URL`、`SMART_HELP_AUTH_JWT_SECRET` | 认证库未配置，或 JWT secret 长度 < 32（`src/app.py` 硬性校验） |
| 金仓连不上 / `password authentication failed` | `bash deploy/status.sh`；`psql` 或 `ksql` 手连 | `ADMIN_DB_URL` 写错；口令含 `@ # % /` 未做 URL 编码；`kingbased` 服务未启动 |
| 8081 起不来或 `/health` 长时间不 200 | `tail -50 runtime/logs/service_8081.log`；`nvidia-smi` | 显存不足、GGUF 损坏、`llama-server` 非 sm_120 编译版、权重路径写错 |
| 问答报上下文不足 / 全部 400 | `curl -s 127.0.0.1:8081/props` | `--ctx-size ÷ --parallel < 16384`；改 `deploy/local_llm.py` 的 `ctx-size`/`parallel` |
| 卡片正常但政策依据缺失 / 证据降级 | `grep -E 'local_evidence_bundle_(missing\|invalid)\|original_file_changed_or_missing' runtime/logs/*.log` | `bundle.json` 缺失，或 `data/` 根文件被改名/替换（SHA256 不符）。按数据准备文档恢复原文件 |
| 检索不到政策 | `bash deploy/status.sh` 看“向量平台/索引维度/读取表” | 8095 未起；`EMBED_PLATFORM` 或 `POLICY_VECTOR_TABLE` 与库不一致 |
| 规则库不更新 | `pgrep -f 'rule_worker.py --watch'`；看 `service_5000.log` | worker 未起（非致命，可重启拉起）；`RULE_WORKER_INTERVAL` 过大；任务积压 |
| 回答慢 | `bash deploy/status.sh` 的解码速度 | GPU 被其它进程占用、`llama-server` 预编译版缺 sm_120 kernel、显存不足导致分层卸载 |
| 页面 502 / Nginx 超时 | `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:5000/` | Web 未起，或 Nginx `proxy_read_timeout` 小于 600s |

更多“现象 → 排查 → 原因 → 处理”见 [docs/部署与启动指南.md](docs/部署与启动指南.md) 第 10 节。

---

## 9. 文档索引

| 文档 | 内容 |
|---|---|
| [docs/部署与启动指南.md](docs/部署与启动指南.md) | 从零部署：环境前置、数据恢复、配置、数据库迁移、模型准备、启动验收、自启、灰度、故障处置 |
| [docs/数据准备与目录说明.md](docs/数据准备与目录说明.md) | 外部数据契约表：文件 → 目标路径 → 用途 → 是否 SHA256 校验 → 敏感级别 → 缺失后果 |
| [docs/Ubuntu24.04_RTX6000D_Conda_Kingbase迁移部署指南_20261020.md](docs/Ubuntu24.04_RTX6000D_Conda_Kingbase迁移部署指南_20261020.md) | 原迁移现场记录：系统准备、两个 conda 环境、Kingbase 备份恢复、GPU 服务、systemd/Nginx |
| `docs/deploy_ubuntu24_20261020/` | systemd / Nginx 模板与旧版 env 示例（**历史方案**，当前生产用 `deploy/start.sh` + cron 自启） |
| `docs/redesign/` | 架构与契约设计（00 总览、匹配链路、SSE 可观测性、政策生命周期、灰度、数据治理等 30+ 篇） |
| `docs/redesign/23-gray-rollout.md`、`25-national-readonly-gray.md`、`26-switch-rollout-runbook.md` | 灰度只读实例与切换回滚手册 |
| `docs/redesign/30-custom-model-registry.md` | 模型注册表（`models` 表）设计与迁移 |
| `docs/reports/` | 调研报告与工作日报 |
| `docs/superpowers/` | 开发过程规格与实施计划（留档） |
| `docs/history/` | 历史文档归档（不入库，本机留档） |

---

## 10. 示例环境说明

本文档中的命令一律使用**相对项目根路径**，不绑定具体机器。作为参考，当前维护机（示例环境）的布局是：

- 项目根：`/home/adminroot/smart-help`
- conda：`/home/adminroot/miniconda3/envs/smart-help-web`（Web）、`.../envs/smart-help-gpu`（GPU 工具）
- 模型：`/home/adminroot/Downloads/llama-src/build/bin/llama-server`、`/home/adminroot/Downloads/llama-b11753/models/*.gguf`

⚠️ `deploy/start.sh`、`deploy/stop.sh`、`deploy/status.sh`、`deploy/foreground.py` 中把 Web 的 Python/Gunicorn 路径**硬编码**为 `/home/adminroot/miniconda3/envs/...`。换机器部署时若 miniconda 安装位置或环境名不同，需要修改这几处（或建立等价软链），逐处清单见 [docs/部署与启动指南.md](docs/部署与启动指南.md) §0.4。
