# 27 政策管理后台页面（/admin）

状态：已实现（2026-11-08），生产已上线页面；写操作受 `POLICY_LIFECYCLE` / `POLICY_IMPORT` 开关控制，默认关闭。
提交：83f96de（页面 + /admin 路由 + 12 例测试）。

## 1. 目标

把已落地的后端能力（生命周期 8 条路由、导入 5 条路由）接成一个人可操作的界面，让"删除旧政策/导入新政策"
不必只靠 CLI；同时保证开关关闭时页面优雅降级、页面加载零写请求。

## 2. 页面结构

| 区块 | 内容 | 数据来源（只读） |
|---|---|---|
| 顶部状态条 | `POLICY_LIFECYCLE` / `POLICY_IMPORT` 是否启用 | `GET /api/policies/__probe__/impact`、`GET /api/policies/import/__probe__`：禁用返回 `policy_lifecycle_disabled` / `policy_import_disabled` |
| ① 政策检索 | 关键词/层级/状态/地区/一级分类筛选、分页、行选择 | `GET /api/policies`、`GET /api/policies/filters` |
| ② 生命周期操作 | suspend / archive / restore / delete / publish / rollback；操作前先读 impact 预检与版本链；restore/delete 二次确认；原因 ≥4 字符 | `GET /api/policies/<code>/impact|versions`，`POST /api/policies/<code>/<action>` |
| ③ 批量导入 | 选 csv/xlsx/json → preview → 创建批次 → commit → 报告 → rollback | `POST /api/policies/import/preview|import|import/<id>/commit|rollback`、`GET /api/policies/import/<id>` |
| 详情抽屉 | 明细数/scope 行数/规则数/版本链 | 同 ② 的只读接口 |

页面加载顺序：`GET /api/me` → 开关探测（只读）→ filters → policies；**初始不产生任何 POST**。

## 3. 开关与降级

- 两个开关都关（默认）：页面可正常打开、可检索；两个写区块置灰并显示"接口未启用（POLICY_LIFECYCLE=0）/（POLICY_IMPORT=0）"。
- 只开 `POLICY_LIFECYCLE=1`：生命周期区块可用；导入仍置灰。
- 只开 `POLICY_IMPORT=1`：导入区块可用（需另开 `POLICY_WORKER=1` 才会消费 outbox 级联）。
- 打开顺序与回滚见 docs/26。

## 4. 安全约束

- 所有 API 由后端 `@admin_required` 校验；页面自身不做权限判断，只按 401/403 提示。
- 写请求带 `X-CSRF-Token`（登录后后端下发的 `helpdesk_csrf` cookie）与 `credentials:'same-origin'`。
- 前端渲染统一走 `esc()` 转义（`& < > " '`），服务端数据不直接拼未转义 HTML。
- `/admin` 路由只读静态文件并设 `no-store, must-revalidate`，不读库不写库。
- 开关探测复用只读 GET：导入探测 `GET /api/policies/import/__probe__` 内部的 `ensure_schema` DDL 随接口
  `finally` 的 `db.session.rollback()` 一并回滚（见 src/api.py:1470-1496 注释），不产生持久写入。

## 5. 验证

- `tests/test_admin_page.py` 12 例：静态契约（三区块 DOM、无外链、6+5 路由真实拼出、CSRF、初始无 POST、二次确认文案、`node --check`）+ 真实 Flask 路由（/admin 200 text/html + no-store、/admin.js 200、渲染时无 DB 调用、/ 仍 200）。
- 全量单测 783 OK；`node --check src/static/admin.js` 通过。
- 生产（5000，HUP 后）：`/admin` 200 / 15489 B、`/admin.js` 200 / 40332 B、`/` 200、`/ask` 200；响应头 no-store/Pragma/Expires 齐全。

## 6. 已知限制

- 未做浏览器人工走查（当前环境无浏览器）；已用契约测试 + curl DOM 摘录替代。
- 未做真实登录后的写操作联调（需打开开关并写库，按灰度纪律未执行）。
- 前端按 `api.py` 现有返回字段渲染；后端若改 impact/versions/report 字段名需同步 `src/static/admin.js` 的 render 函数。
- 导入探测依赖"GET 批次接口不持久写"这一约定（api.py 注释已锁定）；若未来改为提交建表，需改用其它只读探测。
