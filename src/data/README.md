# src/data/ — 运行时数据资产（不入库）

本目录在仓库里只保留本说明。运行时数据由 U 盘私密数据包恢复，`.gitignore` 已排除本目录下除
README 以外的全部内容。

## 目录内容

| 路径 | 说明 | 必需 | 敏感级别 |
|---|---|---|---|
| `policy_evidence/bundle.json` | 政策证据包（约 34MB）：逐字引用政策原文的底座，由构建工具生成 | ✅ | 中高 |
| `policy_evidence/external/*.{txt,html}` | 外部政策原文（XJ_REHAB、NC_2026_LIVELIHOOD 等） | 否 | 中 |
| `full_model/interpretations.sqlite` | 大模型解释缓存（自动建表、可删除重建） | 否 | 高（含历史家庭输入） |

## 运行时读取逻辑

- `src/policy_core/local_evidence.py`：读取 `POLICY_EVIDENCE_BUNDLE`（默认
  `src/data/policy_evidence/bundle.json`），并校验其中 `original_files` 每个原文件在
  `POLICY_SOURCE_ROOT`（默认 `data/`）下的 SHA256；任一文件缺失/变更只会让对应来源降级为
  "证据缺口"，不会让进程崩溃；
- `src/policy_core/analysis_cache.py`：缓存库默认
  `src/data/full_model/interpretations.sqlite`，可用 `FULL_MODEL_CACHE_DIR` 覆盖；删除后会自动重建。

## 恢复

```bash
cp -a /media/usb/smart-help-private-data/restore/src/data/policy_evidence/. <PROJECT_ROOT>/src/data/policy_evidence/
cp -a /media/usb/smart-help-private-data/restore/src/data/full_model/. <PROJECT_ROOT>/src/data/full_model/   # 可选
```

`bundle.json` 缺失时系统仍可启动，但政策证据链会降级；生产环境必须提供。
