# data/ — 政策与业务数据目录（架构占位说明）

本目录在 GitHub 仓库里**只保留说明文件与公开数据**。真实业务数据（个人信息、政策原始数据、政策库）
体积大且敏感，**不进 Git**；首次部署时从 U 盘「私密数据包」按原路径复制回来。

> 完整契约表（每个文件的目标路径、用途、是否被 SHA256 校验、敏感级别、缺失后果）见
> [`docs/数据准备与目录说明.md`](../docs/数据准备与目录说明.md)。

## 仓库内保留的内容

| 路径 | 说明 | 是否入库 |
|---|---|---|
| `data/README.md` | 本说明 | ✅ |
| `data/个人信息数据/README.md` | 个人信息类数据的分类说明 | ✅ |
| `data/政策数据/README.md` | 政策类数据的分类说明 | ✅ |
| `data/policy_db/README.md` | 政策库（SQLite）说明 | ✅ |
| `data/incoming/README.md` | 全国政策接入目录说明 | ✅ |
| `data/regions/china_divisions.json` | 全国行政区划公开数据（56KB） | ✅ 公开数据 |
| `data/*.xlsx` 等业务文件 | 监测对象、政策汇总、县级工具箱等 | ❌ 私密包恢复 |
| `data/policy_db/smart_policy.db` | SQLite 回退政策库（21MB） | ❌ 私密包恢复 |
| `data/policy_db/exports/户匹配结果_*.xlsx` | 个人信息导出 | ❌ 私密包恢复 |

## 首次启动前必须准备好的数据

1. `data/` 根目录：监测对象帮扶措施、政策汇总、县级帮扶工具箱等 `*.xlsx`；
2. `data/policy_db/smart_policy.db`：政策库回退副本；
3. `data/policy_db/` 下的政策编号台账、政策 PDF 样例、导出示例等运营资料；
4. `src/data/policy_evidence/bundle.json` + `src/data/policy_evidence/external/`：政策证据包；
5. `models/qwen3-embedding-4b/tokenizer.json`：嵌入 tokenizer（**仓库已内置**，无需恢复）；
6. `deploy/web.env`、`config/model_settings.yaml`：真实配置与密钥。

## 重要：data/ 根目录的文件不能改名、不能挪目录

`src/policy_core/local_evidence.py` 启动后会读取 `src/data/policy_evidence/bundle.json`，
按其中 `original_files` 记录的文件名与 SHA256，逐个校验 `data/<原文件名>` 是否存在且哈希一致。
缺失或改名不会导致启动失败，但会让**政策证据链降级**（回答里无法逐字引用政策原文）。
因此：

- 业务 xlsx 必须保持原文件名、放在 `data/` 根目录；
- 政策库必须在 `data/policy_db/smart_policy.db`；
- 证据包必须在 `src/data/policy_evidence/bundle.json`（也可用
  `POLICY_EVIDENCE_BUNDLE` 环境变量指向别处，但 `POLICY_SOURCE_ROOT` 要同步指向数据根）。

## 恢复方法（摘要）

```bash
# 设项目根为 <PROJECT_ROOT>，U 盘私密数据包解压到 /media/usb/smart-help-private-data
cp -a /media/usb/smart-help-private-data/restore/. <PROJECT_ROOT>/
chmod 600 <PROJECT_ROOT>/deploy/web.env <PROJECT_ROOT>/config/model_settings.yaml
# 校验
cd /media/usb/smart-help-private-data && sha256sum -c MANIFEST-SHA256.txt --quiet
```

详细步骤见 [`docs/部署与启动指南.md`](../docs/部署与启动指南.md)。

## 安全提醒

- `data/` 下的个人信息（监测对象姓名、身份证号、家庭情况等）属于敏感个人信息，**禁止**提交
  Git、禁止上传网盘/公网，只在本地与 U 盘之间流转；
- `.gitignore` 已排除 `data/` 下所有业务文件与 `data/policy_db/*`，但提交前仍建议执行
  `git status --short` 复核，确认没有误加。
