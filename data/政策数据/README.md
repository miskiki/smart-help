# data/政策数据/ — 政策类数据（仅分类说明，不入库）

本目录是**分类占位目录**，说明政策类数据有哪些、运行时真正放在哪里、缺失会怎样。
政策原文、政策库、政策台账等属于未公开业务数据，**不进入 Git 仓库**，由 U 盘私密包交付。

## 运行时真实路径（不要移动到本目录）

| 文件/目录 | 运行时路径 | 用途 | 敏感级别 |
|---|---|---|---|
| 政策库（SQLite 回退） | `data/policy_db/smart_policy.db` | 无 Kingbase 时的回退库（21MB） | 中高 |
| 政策编号台账 | `data/policy_db/政策编号台账.xlsx`（+2 个备份） | 政策编号管理 | 中 |
| 政策匹配结果导出示例 | `data/policy_db/政策匹配结果导出示例.xlsx` | 导出格式样例 | 中 |
| 政策 PDF 样例 | `data/policy_db/政策PDF样例/*.pdf` | 政策原文附件样例 | 中 |
| 政策命名规则/台账方案 | `data/policy_db/命名规则.md`、`政策台账自动化设计方案.md` | 运营文档 | 低 |
| 2026 年政策汇总 | `data/2026年帮扶政策汇总.xlsx`、`data/2026年帮扶政策汇总_入库格式.xlsx` | 政策导入源 | 中高 |
| 县级帮扶工具箱 | `data/县级帮扶工具箱_更新至9月22日.xlsx` | 县级政策主体 | 中高 |
| 政策"明白纸" | `data/2026年防止返贫致贫对象帮扶政策“明白纸”(0715).wps` | 政策宣传材料 | 中 |
| 政策证据包 | `src/data/policy_evidence/bundle.json`（34MB） | 逐字引用政策原文的证据底座 | 中高 |
| 外部证据原文 | `src/data/policy_evidence/external/*.{txt,html}` | 外部政策原文 | 中 |
| 全国政策投递目录 | `data/incoming/` | 待接入的全国政策文件 | 按内容 |

## 从 U 盘私密包恢复

```bash
cp -a /media/usb/smart-help-private-data/restore/data/. <PROJECT_ROOT>/data/
cp -a /media/usb/smart-help-private-data/restore/src/data/policy_evidence/. <PROJECT_ROOT>/src/data/policy_evidence/
```

注意：`data/*.xlsx` 必须保持原文件名放在 `data/` 根目录，`local_evidence` 会按
`bundle.json` 中的 SHA256 校验，改名或移动会导致政策证据链降级（启动不报错，但回答无法逐字引用原文）。

## 政策数据更新

- 新增政策文件：放进 `data/incoming/`，用 `python deploy/national_intake.py` 做只读质量检查，
  确认后再 `--apply` 写库（需 `POLICY_IMPORT=1`）；
- 批量导入：`python deploy/import_policies.py --file <csv> --commit`；
- 每次更新政策库后，如需刷新证据链，按 `docs/数据准备与目录说明.md` 重建 `bundle.json`。

## 合规要求

- 政策数据虽不含个人信息，但含未公开的内部工作数据，不得提交 Git、不得公网传播；
- 对外答复引用政策时，以证据包校验通过的原文为准，不得编造条款。
