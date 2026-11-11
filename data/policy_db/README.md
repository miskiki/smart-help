# data/policy_db/ — 政策库与运营资料（运行时数据，不入库）

本目录在仓库里只保留本说明；真实内容由 U 盘私密数据包恢复。

## 目录内容（私密包恢复）

| 文件 | 说明 | 必需 |
|---|---|---|
| `smart_policy.db` | SQLite 政策库回退副本（约 21MB）。未配置 `ADMIN_DB_URL`（Kingbase）时使用 | ✅ |
| `政策编号台账.xlsx`、`政策编号台账_备份_*.xlsx` | 政策编号台账及历史备份 | 运营需要 |
| `政策匹配结果导出示例.xlsx` | 匹配结果导出格式样例 | 否 |
| `政策PDF样例/*.pdf` | 政策原文附件样例 | 否 |
| `命名规则.md`、`政策台账自动化设计方案.md` | 政策台账运营说明 | 否 |
| `exports/户匹配结果_*.xlsx` | 个人信息导出（敏感，禁止入库/外传） | 否 |
| `smart_matcher.py` | 历史遗留脚本（当前运行时代码在 `src/`） | 否 |

## 运行时读取位置

- `src/config.py`、`src/policy_core/policy_import.py`、`src/policy_core/policy_scope.py` 默认回退到
  `data/policy_db/smart_policy.db`；
- 生产环境优先使用 Kingbase ES V8（`deploy/web.env` 的 `ADMIN_DB_URL`），SQLite 只是降级回退。

## 注意

- `smart_policy.db` 含政策正文与业务数据，**不提交 Git**；
- 若生产使用 Kingbase，仍建议保留一份 SQLite 回退库，便于演示/离线排查；
- `exports/` 下的户匹配结果含个人信息，禁止提交、禁止外发。
