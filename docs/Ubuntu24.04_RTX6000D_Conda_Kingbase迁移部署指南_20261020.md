# 智能帮扶：Ubuntu 24.04 LTS＋RTX 6000D＋Conda＋Kingbase 迁移部署指南

更新日期：2026-10-20。本文替代此前“尚待提供 Ubuntu/GPU 信息”的部署说明。

**本次交付的是部署文档和模板，不是已在目标机执行成功的记录。没有修改当前业务代码、数据库、账号、模型连接配置或服务。** 文中的 Linux 适配要先在迁移副本实施；未实施前，不应直接启动。当前阶段四业务验收仍有未完成项，迁移成功不等于政策匹配质量已验收。

## 1. 针对你这台机器的结论

| 项目 | 已知信息与部署选择 |
|---|---|
| 系统 | Ubuntu 24.04 LTS；还需用 `uname -m` 确认 CPU 架构 |
| GPU | `NVIDIA RTX 6000D`，驱动报告总显存 85,651 MiB，约83.6 GiB；不根据名称推断为其他型号或96GB显存 |
| 驱动 | 595.104.02，`nvidia-smi` 正常；本轮保留，不重装、不降级 |
| CUDA显示 | 13.2是驱动报告的CUDA支持能力，不代表已安装13.2 Toolkit，也不要求PyTorch必须使用cu132 |
| Web环境 | `smart-help-web`，Python 3.10，先贴近当前Windows测试运行时，降低同时升级风险 |
| GPU环境 | `smart-help-gpu`，Python 3.11；首选安装候选 `torch==2.11.0` 的 `cu130` wheel，必须通过目标机实算后才锁定 |
| 模型 | 迁移原embedding与reranker权重；远程生成式LLM继续用原API，不新增本地大语言模型 |
| 数据库 | 目标仍为KingbaseES；政策库、独立账号/历史库分别迁移 |
| 服务 | Web内部5000，GPU内部8090；两套Conda、两个systemd服务；Kingbase独立安装，不装进Conda |

NVIDIA兼容说明给出CUDA 13.x的最低驱动分支为R580；新驱动有向后兼容能力。因此595驱动可作为运行cu130的基础，最终以本机计算与两个模型实际推理为准。[NVIDIA官方兼容说明](https://docs.nvidia.com/deploy/cuda-compatibility/minor-version-compatibility.html)

**源端实测补充：** 本次在当前Windows用户配置下，以只读事务分别连接成功：`127.0.0.1:54321/policy_db`、`127.0.0.1:54321/smart_help_auth`，backend均为`postgresql+psycopg2`。两库`SELECT version()`均返回`KingbaseES V008R006C008B0014 on x64, compiled by Visual C++ build 1800, 64-bit`；`SHOW database_mode`均为`oracle`，`server_encoding=UTF8`、`lc_collate=C`、`lc_ctype=C`。未查询业务用户密码哈希，未输出数据库口令或业务密钥。正式备份前仍须核对服务使用同一配置。

PyTorch官方列出了2.11.0的cu130安装组合；这里选择一个明确版本作为迁移候选，并非要求追最新。[PyTorch官方版本安装表](https://pytorch.org/get-started/previous-versions/)

RTX 6000D相关SKU的部分Nsight/CUPTI分析工具存在单独支持限制；分析工具不支持不等于普通PyTorch推理不能运行。本文用真实张量和模型请求验收，不用profiler可用性代替推理验收。[NVIDIA相关SKU工具说明](https://developer.nvidia.com/err_nvgpu/)

## 2. 总体结构与迁移顺序

```text
Ubuntu 24.04
├── 系统级：现有NVIDIA驱动、Nginx、KingbaseES
├── /home/adminroot/miniforge3/envs/smart-help-web
│   └── Flask / Gunicorn / SQLAlchemy / psycopg2 / tokenizer
├── /home/adminroot/miniforge3/envs/smart-help-gpu
│   └── PyTorch / Sentence Transformers / embedding / reranker
├── /home/adminroot/smart-help
│   ├── Langchain-Chatchat-master/
│   ├── chatchat_data/model_settings.yaml
│   ├── data/                         原始政策Excel/PDF等
│   ├── models/                       两套原权重
│   ├── tests/                        测试与固定夹具
│   ├── deploy/                       本指南配套模板复制到这里
│   ├── migration/                    备份、恢复日志、对比清单
│   └── runtime/logs/
├── GPU模型服务：127.0.0.1:8090
├── Web服务：127.0.0.1:5000
└── Nginx：受控网络入口 → Web
```

顺序：源端盘点 → 复制代码/原文/权重 → 两个Conda环境 → Linux路径适配 → Kingbase安装和恢复演练 → GPU验收 → Web自检 → 原账号及业务测试 → 正式停写备份/切换 → systemd/Nginx。

这张显卡用于加速本地embedding和reranker；如果家庭事实提取、推理及建议仍走远程LLM API，迁移不会自动消除这些API耗时，也不会自动修复当前尚未完成的业务质量问题。不要为了利用显存而同时更换生成模型，扩大本次迁移变量。

以下统一假设登录用户为`adminroot`、家目录为`/home/adminroot`。先运行`getent passwd adminroot`；若实际家目录不同，替换所有绝对路径，包括systemd模板。不要将`~`或`$HOME`留在systemd的ExecStart、WorkingDirectory、EnvironmentFile中。

## 3. 目标机系统准备

```bash
cat /etc/os-release
uname -m
getent passwd adminroot
nvidia-smi
free -h
df -h
ss -lntp
```

下面的PyTorch安装方案以Linux x86_64为前提；若是aarch64，先核对该wheel的架构供应，不能拿x86_64 wheel硬装。

```bash
sudo apt update
sudo apt install -y build-essential libpq-dev libgomp1 \
  git curl unzip rsync ca-certificates nginx
mkdir -p "$HOME/smart-help/deploy" "$HOME/smart-help/migration" \
  "$HOME/smart-help/runtime/logs" "$HOME/smart-help/models"
chmod 700 "$HOME/smart-help/migration"
```

不执行`ubuntu-drivers install`、不卸载驱动、不停止Xorg或awesun。通常不需要另装系统级CUDA Toolkit/cuDNN；预编译PyTorch会安装其所需运行依赖。只有实际需要编译CUDA扩展时才另行规划工具链。

## 4. 创建两个Conda环境

已装Conda就复用，不重新覆盖其目录。未安装时从[Miniforge官方发行页](https://github.com/conda-forge/miniforge)下载对应架构Linux安装包，核对该发行包的校验值并记录版本，然后：

```bash
# 将实际下载的文件名代入；这里不是一个已经存在的文件。
bash Miniforge3-Linux-x86_64.sh -b -p "$HOME/miniforge3"
source "$HOME/miniforge3/etc/profile.d/conda.sh"
conda create -n smart-help-web python=3.10 pip -y
conda create -n smart-help-gpu python=3.11 pip -y
```

Conda隔离Python依赖，不隔离驱动、数据库或端口。不要复制Windows虚拟环境、`.pyd`、DLL、`__pycache__`作为Linux环境。

### 4.1 Web依赖

代码复制到位后执行：

```bash
conda activate smart-help-web
cd "$HOME/smart-help"
python -m pip install --upgrade pip setuptools wheel
python -m pip install -r Langchain-Chatchat-master/admin_panel/requirements.txt
python -m pip install requests PyYAML 'numpy<3' PyMuPDF pypdf tokenizers gunicorn
python -m pip check
```

现有requirements主要列Flask、SQLAlchemy、openpyxl、JWT、psycopg2，未覆盖全部直接依赖。上面补充源码实际使用的包；完整导入和回归后保存Linux锁定文件。已有经验证的Linux锁文件优先使用锁文件，不盲装Windows的完整pip freeze。

### 4.2 GPU依赖：保留595驱动，安装cu130版PyTorch

```bash
conda activate smart-help-gpu
python -m pip install --upgrade pip setuptools wheel
python -m pip install torch==2.11.0 --index-url https://download.pytorch.org/whl/cu130
python -m pip install 'sentence-transformers>=5,<6' 'transformers>=4.41,<5' flask gunicorn
python -m pip check
```

这两个文本模型不必额外安装torchvision/torchaudio。首次安装后记录实际解析版本；验收后冻结，正式环境不能一直跟随区间内的最新版。若后续依赖安装意外替换torch版本，重新核对，不把安装成功当作GPU通过。

### 4.3 GPU实际计算检查

```bash
python - <<'PY'
import torch
print('torch:', torch.__version__)
print('wheel runtime:', torch.version.cuda)
print('available:', torch.cuda.is_available())
assert torch.cuda.is_available(), 'CUDA不可用，停止GPU部署步骤'
print('device:', torch.cuda.get_device_name(0))
print('capability:', torch.cuda.get_device_capability(0))
print('compiled arches:', torch.cuda.get_arch_list())
x = torch.randn(1024, 1024, device='cuda')
y = x @ x
torch.cuda.synchronize()
assert torch.isfinite(y).all().item()
print('matmul passed:', tuple(y.shape))
PY
```

设备名称以实机输出为准，不仅凭产品名硬写compute capability。如果出现`no kernel image`、`invalid device function`或CUDA不可用，停在此步，记录wheel/runtime/架构后核对官方支持，不先重装系统驱动，也不直接换nightly碰运气。

## 5. 迁移文件：不只是代码和一个.db

迁移并保留相对目录结构：

| 内容 | 目的 |
|---|---|
| 当前业务代码及测试、固定夹具 | 包括本轮修复，不用旧7项演示副本 |
| `data/`下原始政策文件 | 原字节复制，避免Excel另存导致哈希变化 |
| `admin_panel/data/policy_evidence/bundle.json`及关联依据 | 来源回读依赖原文和SHA256；bundle不是数据库备份 |
| `chatchat_data/model_settings.yaml` | 保留生成模型及平台配置；修改本地服务地址而不改模型语义 |
| embedding、reranker完整权重目录 | 包括tokenizer、配置、modules、safetensors/bin等，不只复制权重文件 |
| uploads及其他被数据库引用的文件 | 数据库只保存路径的附件不会被sys_dump包含 |
| 政策库和独立账号/历史库备份 | 使用下一节的整库方式 |
| 持久化规则/来源关联/图谱配置 | 跟随真实存储位置盘点，不假定全部在smart_policy.db |

向量目前存于Kingbase的`policy_vector.vector` BYTEA，进程内矩阵只是缓存；并非需要另装Chroma或凭空安装pgvector。迁移数据库保留原始向量、detail_id、policy_version、embed_model等关联。

迁移阶段**不更换embedding模型、维度、归一化方式、查询前缀或tokenizer，不主动重建向量索引**。即使维度相同，更换权重也不能复用旧向量。若源库实际不是bge-base-zh-v1.5，以源端真实模型为准，停止执行下面针对bge的示例配置，先调整模型服务使其完全一致。

模型目录示例：

```text
models/bge-base-zh-v1.5/
models/bge-reranker-base/
```

迁移前后对代码、原文件、bundle和权重生成相对路径SHA256清单。不要把密钥文件内容写进普通报告。`local_evidence.py`会用`POLICY_SOURCE_ROOT`下对应文件校验bundle记录的哈希；复制后必须实际运行来源加载检查，不只确认文件存在。

## 6. Kingbase迁移：先确认源库，再备份恢复

### 6.1 先记录这些真实参数

| 参数 | 如何确认 |
|---|---|
| 源Kingbase完整版本/build、数据库兼容模式、编码/排序规则 | 源库`SELECT version()`和实例安装信息；不能只写V8 |
| 实际政策库连接 | `config.DATABASE_URL`解析后的backend/host/port/database；不打印密码 |
| 独立账号库连接 | `config.AUTH_DATABASE_URL`；不能假定与政策库同库 |
| 目标CPU架构及Kingbase安装包 | Ubuntu24.04、对应CPU、有效许可证、厂商兼容范围 |
| 源数据库所有业务schema/表、序列、扩展、角色与权限 | 整库盘点；以实库为准，不只按ORM模型挑表 |

优先目标保持与源相同Kingbase主版本、兼容模式及经过确认的build组合。**实测源模式为Oracle兼容，目标应保留Oracle兼容模式；不要改成PG模式。** 当前项目使用`postgresql+psycopg2`协议/方言，并对Kingbase版本串按PG12作了兼容补丁；连接协议、SQLAlchemy方言和Kingbase实例兼容模式是不同概念，不能因为驱动名含postgresql就认定源模式为PG。

源端完整build已确认是`V008R006C008B0014`，应优先获取对应Linux x86_64包并核对Ubuntu24.04支持情况。若厂商要求不同build，先在副本完成兼容演练，不把数据库升级与迁移视为同一项已验证动作。模式oracle、编码UTF8、排序/字符分类C已只读记录；正式切换前复核没有变化。

金仓公开文档说明支持Ubuntu系列，但这不等于本文已确认某一个安装包认证Ubuntu24.04。获取与源版本兼容、明确支持目标系统/架构的安装包，核对许可证与厂商发行说明。不要执行不存在的`apt install kingbase`。[Kingbase安装准备说明](https://help.kingbase.com.cn/v8.6.7.12/install-updata/install-linux/install-linux-2.html)

### 6.2 数据库范围

政策库至少关注：`policy`、`policy_detail`、`policy_version`、`policy_vector`、`app_config`、审计及实际图谱/来源关联表。图谱表的准确名称以源库盘点为准，不按本文随意重建。

账号库中源码明确使用：`admin_user`、`qa_log`、`conversation`、`login_session`、`auth_audit`、`survey_result`、`delete_confirmation`。必须保留用户主键、密码哈希、角色、安全状态、问答正文、`match_json`、历史关联及序列。

政策库里可能也残留旧admin_user/qa_log；它们不代表当前独立账号库已经迁移。

**禁止把现有`admin_panel/migrate_to_kingbase.py`作为这次搬迁命令。** 它是旧SQLite导入脚本，源码会`DROP TABLE ... CASCADE`，只覆盖旧表清单，不包含完整向量/图谱/新账号结构，且有Windows路径和winreg依赖。也不要运行重置管理员、种子演示库或`create_all`来替代恢复。

### 6.3 安装目标KingbaseES

使用厂商对应版本的Linux安装指南。独立系统用户`kingbase`安装和运行数据库，不用root运行数据库进程；默认端口示例54321，以实际实例为准。

```bash
# 仅尚不存在此用户时创建；不改变业务账号密码。
id kingbase
# 若不存在，由管理员创建：sudo useradd -m -s /bin/bash kingbase
```

安装、初始化数据目录、许可证、UTF8及**Oracle兼容模式**按对应版本完成；通过对应版本安装器/初始化工具选择模式，不在已初始化实例上随意修改模式参数。只对本机应用监听127.0.0.1时，不必对公网开放54321。需要远程导入时，使用受控连接和适当认证，不设置全网trust。

安装路径以下用示例`/opt/Kingbase/ES/V8/Server/bin`，**需替换为实际bin目录**。确认：

```bash
export KB_BIN=/opt/Kingbase/ES/V8/Server/bin
"$KB_BIN/ksql" --version
"$KB_BIN/sys_restore" --version
```

### 6.4 源端逻辑备份：Kingbase → Kingbase为主路径

先演练；正式切换时暂停应用写入、后台同步及外部导入，再对两个库做最终备份。单库备份有一致性快照，但两个库不是天然同一时刻，停写用于保证业务关联一致。本文不在当前Windows上替你停止任何服务。

Windows PowerShell示例，源版本工具路径、库名、端口必须按盘点填写：

```powershell
$kbBin = 'C:\实际Kingbase安装目录\Server\bin'
$backupDir = 'D:\smart-help-migration\final'
New-Item -ItemType Directory -Path $backupDir -Force | Out-Null

& "$kbBin\sys_dump.exe" -h 127.0.0.1 -p 54321 -U system -W `
  -Fc -f "$backupDir\policy.dump" policy_db
if ($LASTEXITCODE -ne 0) { throw '政策库备份失败，停止' }

# 本次只读核查确认源账号库名为smart_help_auth。
& "$kbBin\sys_dump.exe" -h 127.0.0.1 -p 54321 -U system -W `
  -Fc -f "$backupDir\auth.dump" smart_help_auth
if ($LASTEXITCODE -ne 0) { throw '账号库备份失败，停止' }

Get-FileHash -Algorithm SHA256 -LiteralPath "$backupDir\policy.dump"
Get-FileHash -Algorithm SHA256 -LiteralPath "$backupDir\auth.dump"
```

`-W`交互输入数据库密码，不将密码拼入命令。`sys_dump -Fc`与`sys_restore`是配套逻辑备份恢复工具，备份需要检查退出码及告警。[sys_dump官方说明](https://bbs.kingbase.com.cn/kingbase-html/v9/admin/reference/ref-client/sys_dump.html)

还需由DBA单独盘点角色/权限/表空间；单库dump不包含完整全局角色。不要盲目恢复全局角色SQL，覆盖目标系统管理员口令或权限。

**异常分支：** 本次实测账号库为Kingbase，正常采用上述auth备份。如果正式运行服务的配置与当前用户环境不同，另查出SQLite账号库，停止直接套用auth的sys_dump；先用SQLite备份API取得一致快照，再制定当前auth模型全字段的非破坏性ETL。保留ID、哈希、安全状态、历史JSON、布尔/时间语义，导入新空Kingbase库、事务提交、校准序列并对账。旧迁移脚本不适用，不允许悄悄改连空库。

### 6.5 上传并恢复到新空库

将备份传到目标`~/smart-help/migration/final/`，两端比对SHA256。不要复制运行中的Kingbase物理数据目录跨Windows/Linux启动。

```bash
cd "$HOME/smart-help/migration/final"
sha256sum policy.dump auth.dump
"$KB_BIN/sys_restore" --list policy.dump > policy.toc.txt
"$KB_BIN/sys_restore" --list auth.dump > auth.toc.txt
```

由DBA使用ksql连接目标维护数据库（示例TEST），创建**新的空目标库**：

```sql
CREATE DATABASE policy_db_mig20261020 WITH ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C' TEMPLATE template0;
CREATE DATABASE smart_help_auth_mig20261020 WITH ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C' TEMPLATE template0;
```

连接新库后再次`SHOW database_mode`、`SHOW server_encoding`、`SHOW lc_collate`、`SHOW lc_ctype`，与源端oracle/UTF8/C/C逐项一致后才恢复。建库不负责切换实例模式。若同名库已存在，先核对其用途，改用新名称，不删除已有库。

恢复命令需要交互输入密码时，不要把密码提示重定向到日志。下面直接在终端执行，完成后检查退出码；保存经过脱敏的输出作为恢复记录。

```bash
"$KB_BIN/sys_restore" -h 127.0.0.1 -p 54321 -U system -W \
  --exit-on-error --single-transaction --no-owner --no-privileges \
  -d policy_db_mig20261020 "$HOME/smart-help/migration/final/policy.dump"
# 检查退出码为0和日志无失败后，才执行下一条。

"$KB_BIN/sys_restore" -h 127.0.0.1 -p 54321 -U system -W \
  --exit-on-error --single-transaction --no-owner --no-privileges \
  -d smart_help_auth_mig20261020 "$HOME/smart-help/migration/final/auth.dump"
```

此示例不使用`--clean`、不删除目标表，恢复仅用于新空库。单事务与并行`-j`不能混用；失败时先查原因，不能忽略错误继续启动。[sys_restore官方说明](https://bbs.kingbase.com.cn/kingbase-html/v8/admin/reference/ref-client/sys_restore.html)

`--no-owner --no-privileges`避免强依赖源端所有者，但也意味着权限必须重授。DBA为两个目标库分别创建/授权应用数据库角色，使用安全交互设置新目标数据库角色密码，不改变表内业务用户密码哈希。

权限示例，先创建相应应用角色，再在**各自目标库**执行：

```sql
-- 政策库示例；有其他业务schema时逐一处理。
GRANT CONNECT ON DATABASE policy_db_mig20261020 TO smart_help_policy;
GRANT USAGE ON SCHEMA public TO smart_help_policy;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO smart_help_policy;
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO smart_help_policy;
```

账号库对`smart_help_auth`授予对应库/schema/表/序列权限。先在演练库验证应用实际需求；涉及建索引/迁移的DDL由DBA控制，不能遇到权限错误就把长期应用角色改成system。

### 6.6 对账与账号验收

源停写后的基线与目标逐表对比：表及列类型、行数、主键范围、索引/约束、序列、关键关联。

关键检查包括：

- 政策、明细、向量、版本和app_config总数一致；detail_id不重新编号。
- 向量原始BYTEA哈希、文本、模型标识、维度和版本一致；不是只对比数量。
- 图谱节点/边及来源关联保持，未因只迁移ORM表而遗漏。
- 原始行定位、benefit_source、source_rows、extra及bundle哈希保持。
- 账号ID、username、password_hash、role、安全状态及历史JSON一致；对账报告只保存哈希摘要，不输出密码哈希原文。
- QaLog与Conversation的user_id/session_id对应，不制造孤立历史。
- 序列不小于已有最大ID；restore应恢复序列，不能不检查就认定成功。

用原账号原密码登录，查看旧历史和Excel导出；不要执行管理员重建/密码初始化。业务JWT与Flask密钥从源安全迁移并固定保存，不在每次启动时重新随机生成。浏览器换域名或会话失效可以要求重新登录，不等于需要修改用户密码。

## 7. 必须实施的Linux适配（本次只提供说明，未改源码）

### 7.1 config.py

当前无条件`import winreg`会在Linux直接失败：

```python
try:
    import winreg
except ImportError:
    winreg = None

def _reg_get(name):
    if winreg is None:
        return None
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment')
        value, _ = winreg.QueryValueEx(key, name)
        return value
    except OSError:
        return None
```

此外把默认SQLite及导出台账Windows绝对路径改成配置优先、项目相对路径兜底。目标正式配置必须显式给`ADMIN_DB_URL`与`SMART_HELP_AUTH_DATABASE_URL`，不使用SQLite兜底；不要用`USE_KINGBASE=1`隐式猜主机/库名。

保留并验证当前Kingbase版本串及`level`关键字兼容补丁。其PG12假设应与源/目标版本兼容验证绑定，不能宣称适用于任意Kingbase版本。

### 7.2 recognize.py 与其他路径

把`MODEL_YAML`改为环境变量优先：

```python
from pathlib import Path
MODEL_YAML = os.environ.get(
    'MODEL_SETTINGS_PATH',
    str(Path(__file__).resolve().parents[2] / 'chatchat_data' / 'model_settings.yaml'),
)
```

`MODEL_SETTINGS_PATH`在实施此适配后才生效，当前硬编码源码不会自动读取它。核对其他脚本的Windows路径，不对原始政策内容做全文路径替换。

| 项目 | 处理 |
|---|---|
| `vector_safety.py` | 使用已有EMBED_TOKENIZER_PATH、EMBED_TOKENIZER_MODEL |
| `tools/full_dependencies.py` | 使用已有LOCAL_MODEL_PYTHON、LOCAL_EMBED_PATH、LOCAL_RERANK_PATH；systemd先启动模型，避免自动拉起重复进程 |
| 原始文件/数据库中的附件路径 | 生成原路径→新路径映射，仅修改确实是路径的配置或元数据字段，保留来源内容和可追溯关系 |
| 测试/调试脚本 | 改为基于`__file__`的工作区路径，不能因为Linux路径失败就跳过业务断言 |
| `.bat`、PowerShell启动器 | 不在Ubuntu执行，使用下文systemd |

## 8. 目标环境变量及模型平台

配套目录提供`web.env.example`与`model.env.example`。复制为`~/smart-help/deploy/web.env`和`model.env`，填写真实值；文件权限600。不能把带CHANGE_ME的模板直接用于上线。

关键配置示意：

```text
POLICY_MATCH_MODE=full_model
POLICY_CORE_ENABLED=0
POLICY_SHOWCASE_ENABLED=0
ADMIN_DB_URL=postgresql+psycopg2://smart_help_policy:URL编码后的密码@127.0.0.1:54321/policy_db_mig20261020
SMART_HELP_AUTH_DATABASE_URL=postgresql+psycopg2://smart_help_auth:URL编码后的密码@127.0.0.1:54321/smart_help_auth_mig20261020
```

密码含`@`、`/`、`#`、`%`等字符时必须正确URL编码，建议用SQLAlchemy `URL.create(...).render_as_string(hide_password=False)`在安全配置工具内部生成并直接写文件，不打印、不粘贴进日志。保留原业务密钥，不把示例占位符当默认密码。

Windows注册表不会随代码迁移；显式环境变量必须覆盖原来保存在HKCU的配置。源端盘点需在真正运行服务的Windows用户下执行，避免换管理员后读取另一份注册表。

在目标模型YAML中保留原生成式LLM平台，另为原本地向量平台修改地址。结构示例（合并到现有文件，不覆盖原平台）：

```yaml
DEFAULT_EMBED_PLATFORM: local-offline
MODEL_PLATFORMS:
  - platform_name: local-offline
    api_base_url: http://127.0.0.1:8090/v1
    api_key: local-only
    llm_models: []
    embed_models: [bge-base-zh-v1.5]
    rerank_models: [bge-reranker-base]
    profile:
      embed_dim: 768
      rerank_endpoint: http://127.0.0.1:8090/v1/rerank
```

`local-only`不是外部API密钥，本地现有服务没有实现鉴权，因此8090仅监听回环地址。`EMBED_PLATFORM=local-offline`可覆盖数据库平台选择，但只有源索引确实属于同一embedding模型/权重/维度时才这样设置。生成模型不能选到这个没有chat接口的平台。

## 9. GPU模型服务：先实测，再托管

### 9.1 首轮前台验证

```bash
conda activate smart-help-gpu
cd "$HOME/smart-help"
CUDA_VISIBLE_DEVICES=0 python -u \
  Langchain-Chatchat-master/admin_panel/serve_local_models.py \
  --embed-model "$HOME/smart-help/models/bge-base-zh-v1.5" \
  --rerank-model "$HOME/smart-help/models/bge-reranker-base" \
  --host 127.0.0.1 --port 8090
```

当前脚本由Sentence Transformers自动选择设备；端口正常不能证明跑在GPU。正式托管使用配套`gpu_wsgi.py.example`中的工厂，显式`device='cuda:0'`并检查模型实际设备。它复用现有HTTP端点，不改向量归一化、权重或业务规则；复制到目标`deploy/gpu_wsgi.py`后才生效。

不要直接`gunicorn serve_local_models:app`：现有模型加载只在main中执行，这样导入app会让模型未加载。

### 9.2 HTTP验收

模型启动后在另一个终端：

```bash
curl -fsS http://127.0.0.1:8090/v1/health
curl -fsS http://127.0.0.1:8090/v1/embeddings \
  -H 'Content-Type: application/json' \
  -d '{"model":"bge-base-zh-v1.5","input":["莲花县残疾人帮扶政策"]}' \
  -o "$HOME/smart-help/migration/embedding-smoke.json"
curl -fsS http://127.0.0.1:8090/v1/rerank \
  -H 'Content-Type: application/json' \
  -d '{"model":"bge-reranker-base","input":{"query":"残疾人补贴","documents":["残疾人生活补贴","义务教育学生资助"]}}' \
  -o "$HOME/smart-help/migration/rerank-smoke.json"
nvidia-smi
```

验收：health中ok/embedder/reranker均为true；embedding返回1条768维有限数值且归一化合理；rerank返回两个索引及有限分数。检查工厂日志中的设备以及请求时GPU活动。权重一致前提下比较源端/目标端固定文本相似度和排序；GPU浮点误差不要求逐字节相等，但明显偏移或阈值附近排序变化必须核对。

## 10. Web自检与首次启动

完成Linux适配、数据库恢复和环境配置后：

```bash
conda activate smart-help-web
cd "$HOME/smart-help"
set -a
source deploy/web.env
set +a
python Langchain-Chatchat-master/tools/run_full_model.py --check-only
```

`--check-only`主要检查政策数、明细数、向量数、缺索引数量及embedding健康；它不替代账号登录、reranker实测或业务质量验收。

从`admin_panel`工作目录检查真实配置，输出时仅使用`make_url(...).render_as_string(hide_password=True)`，不要打印原始URL。确认政策库和账号库都指向目标Kingbase，不是演示库、旧机器或空SQLite。

首轮前台可用启动器：

```bash
python Langchain-Chatchat-master/tools/run_full_model.py --port 5000
```

这是前台验证入口，不是正式生产WSGI。正式使用Gunicorn；先关闭刚才自己启动的前台进程，再托管，不能两个进程占5000。

```bash
cd "$HOME/smart-help/Langchain-Chatchat-master/admin_panel"
"$HOME/miniforge3/envs/smart-help-web/bin/gunicorn" \
  --bind 127.0.0.1:5000 --workers 1 --worker-class gthread \
  --threads 2 --timeout 600 --graceful-timeout 30 app:app
```

首版一个Web worker，避免尚未验证的多进程内存状态差异；GPU一个worker、一个线程，避免重复加载和未验证并发。600秒是部署初始上限，不是性能目标；它不会解决重复模型调用、检索或推理预算问题。[Flask正式部署说明](https://flask.palletsprojects.com/en/stable/deploying/)

### 局域网访问（可选）

一键启动器（`deploy/start.sh` → `deploy/foreground.py`）的 Web 监听地址由环境变量 `WEB_BIND_HOST` 控制，默认 `127.0.0.1`（仅本机）。在 `deploy/web.env` 里设为 `0.0.0.0` 即可对局域网开放；该变量只影响 Web 端口，8081/8095/8096/8097 始终只监听回环。改后需重启（`bash deploy/stop.sh` 后重新执行 `deploy/start.sh`，或由 `deploy/autostart.sh --daemon` 自动拉起），启动横幅会打印探测到的局域网地址。

开放前先确认主机防火墙放行（Ubuntu 默认 `ufw` 入站策略为 DROP）：

```bash
sudo ufw allow 5000/tcp   # 只放行 Web 端口
sudo ufw status           # 确认规则已生效且 Status: active
```

管理后台 `/admin` 与问答页共用该端口、一并暴露，请只在可信网段内开启。

## 11. systemd开机托管

配套目录中两个`.service.example`按实际路径复制到`/etc/systemd/system/`，去掉`.example`。先检查用户名、Conda路径、工作目录、env文件和目标数据库服务名称。

在新部署目录中准备附件，以下假设本指南及配套目录已放到`~/smart-help/docs/`；已有同名文件先留存，不覆盖正在使用的配置：

```bash
cd "$HOME/smart-help"
cp -n docs/deploy_ubuntu24_20261020/web.env.example deploy/web.env
cp -n docs/deploy_ubuntu24_20261020/model.env.example deploy/model.env
cp -n docs/deploy_ubuntu24_20261020/gpu_wsgi.py.example deploy/gpu_wsgi.py
cp -n docs/deploy_ubuntu24_20261020/wait_models.py.example deploy/wait_models.py
chmod 600 deploy/web.env deploy/model.env
# 安全填写env中的真实配置，核对路径并完成前述自检后再安装unit。
sudo install -m 644 docs/deploy_ubuntu24_20261020/smart-help-model.service.example \
  /etc/systemd/system/smart-help-model.service
sudo install -m 644 docs/deploy_ubuntu24_20261020/smart-help-web.service.example \
  /etc/systemd/system/smart-help-web.service
sudo systemd-analyze verify /etc/systemd/system/smart-help-model.service \
  /etc/systemd/system/smart-help-web.service
```

Web模板依赖模型服务，启动前执行HTTP健康检查；数据库也必须已就绪。Kingbase的systemd unit名称因安装方式而异，确认后可补到After/Requires，不能猜一个通用名称。

```bash
sudo systemctl daemon-reload
sudo systemctl start smart-help-model
sudo systemctl status smart-help-model --no-pager
curl -fsS http://127.0.0.1:8090/v1/health
sudo systemctl start smart-help-web
sudo systemctl status smart-help-web --no-pager
curl -fsS -o /dev/null http://127.0.0.1:5000/ask
```

确认日志、端点和原账号正常后才设置开机启动：

```bash
sudo systemctl enable smart-help-model smart-help-web
journalctl -u smart-help-model -n 80 --no-pager
journalctl -u smart-help-web -n 80 --no-pager
```

不要同时使用systemd和启动器自动拉起两个模型实例；模型服务不使用Gunicorn `--preload`。依赖中的模型进程重启后，检查Web仍能发出embedding/rerank请求，不仅检查systemd active。

## 12. Nginx局域网入口

配套`nginx-smart-help.conf.example`以8080为示例，便于不覆盖已有80端口站点；若改80/443，先检查已有站点。Web仍实际运行在5000，GPU和数据库不向用户网络开放。

```bash
# 新站点名没有被占用时安装；已存在则先检查，不覆盖别人的站点。
sudo install -m 644 "$HOME/smart-help/docs/deploy_ubuntu24_20261020/nginx-smart-help.conf.example" \
  /etc/nginx/sites-available/smart-help
sudo ln -s /etc/nginx/sites-available/smart-help /etc/nginx/sites-enabled/smart-help
sudo nginx -t
sudo systemctl reload nginx
```

访问：`http://目标机IP:8080/ask`。正式跨不可信网络使用TLS和受控访问。只开放实际需要的入口端口；不要为部署直接启用/重置防火墙规则导致SSH或远程桌面断开。

流式接口关闭代理缓冲；保留Host及转发头，超时与上游协调。当前应用若要信任代理后的客户端地址或HTTPS状态，应仅信任实际Nginx这一层，不直接相信外部传入的Forwarded头。

## 13. 迁移验收、最终切换与回滚

### 13.1 必须实际验证

| 层 | 通过条件 |
|---|---|
| 环境 | 两Conda可独立运行，pip check无依赖错误；GPU张量实算成功 |
| 模型 | embedding、reranker实际GPU推理；模型/权重/tokenizer/维度与原索引一致 |
| 数据 | 全库表、向量字节摘要、图谱与来源关系、序列及关键字段对账 |
| 来源 | 原始Excel/PDF和bundle哈希有效，X-YW-054及残疾/医疗样本可完整回读 |
| 账号 | 原账号原密码登录、权限正确、旧历史与导出正常；无密码重置 |
| 流程 | 自然语言→事实→原库检索→明细→审核→追问→更新→建议，不能只固定候选 |
| 事实 | member_1格式兼容、补充文本联合处理、低保未知→False、不串成员 |
| 判定 | 正常排除/家庭缺项/来源缺项/分析失败明确区分 |
| 建议 | 不虚构门槛/金额/联系人，不把部分关联写成确定领取 |
| 托管 | 两服务重启、健康检查、Nginx和流式响应正常 |

Linux运行当前测试集，保留真实统计；历史500项只是Windows基线，不保证Linux也恰好500项。写死Windows路径的测试要适配，不能把业务失败改成skip后宣布通过。全量发现测试前检查是否有源生产连接或自执行脚本，使用隔离测试存储，不往源库写测试数据。

### 13.2 正式切换

1. 演练恢复、数据对账及业务验收先通过。
2. 安排源端维护窗口，暂停应用写入和后台任务；保留旧机可回滚。
3. 最终备份两个库及附件，核对哈希，再恢复到新的最终目标库。
4. 目标env明确指向最终库，完成原账号、历史、向量及家庭冒烟测试。
5. 切换用户访问入口，确认没有新旧两端同时写入。

### 13.3 回滚

保留源数据库、原代码、原配置及备份，不做原地覆盖。切换后若目标已产生新历史，先停目标写入并保留增量，再决定如何回流；不能简单切回旧库造成新数据丢失。回滚只切回已核对的入口/连接配置，不执行删表或重新初始化账号。

### 13.4 锁定环境

```bash
conda env export -n smart-help-web --no-builds > deploy/environment-web.linux.yml
conda env export -n smart-help-gpu --no-builds > deploy/environment-gpu.linux.yml
conda run -n smart-help-web python -m pip freeze > deploy/requirements-web.linux.lock.txt
conda run -n smart-help-gpu python -m pip freeze > deploy/requirements-gpu.linux.lock.txt
```

同时保存模型权重清单、代码哈希、驱动/PyTorch信息、Kingbase版本与兼容模式、备份哈希和最终验收结果。环境文件中的机器prefix在复建时按新机器处理，密钥不进入版本库。

## 14. 交给实施人员的执行边界

本文已确认源Kingbase完整build、两个实际库名/backend及oracle/UTF8/C/C参数；尚需现场确认目标CPU架构、Ubuntu24.04对应安装包/许可证与实际路径。这些缺口不靠猜值填充。

先在迁移副本实施Linux适配和新空库恢复；按上述步骤验证，不修改旧机账号密码，不用旧SQLite导入脚本覆盖Kingbase，不自动重建向量，不把新增端口可访问当作业务验收通过。需要源端停写的动作安排在正式切换窗口，不在阅读文档时自动执行。
