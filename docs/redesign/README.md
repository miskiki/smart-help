# 机制升级分析文档集（2026-11-06）

> 背景：用户反馈内部机制"不会思考"、匹配机制硬性条件多/呆板、模型智能体参与工作量少、SSE 前端看不到内部真实实况、老链路（区域优先匹配等）逻辑有问题、政策收录与知识库匹配原理需重新审视、回答样式需重做。
> 本轮**只做扫描、分析、重新设计**，不改生产代码。基线：git tag `baseline-20261106`（commit 0d20ef1）。

## 铁律（所有分析员必须遵守）

1. **只读代码**：不得修改 `src/`、`deploy/`、`data/`、`config/` 下任何文件；只允许写自己名下的这一份文档。
2. **不做运行期变更**：不重启/不 kill 服务、不写数据库、不改 env、不训练模型。只读查询（SELECT）可以。
3. **证据制**：每条结论必须给 `文件:行号` 或真实日志/数据库证据；禁止"我认为/大概"。
4. **精确**：给出常量、阈值、调用次数、表名、行数、耗时等硬数据。
5. **中文**：文档用中文，术语保留英文原名（如 planner、tool call、SSE event）。

## 文档清单

| 文件 | 主题 | 负责人 |
|---|---|---|
| 00-overall.md | 总览、跨切分析、目标架构与路线图 | lead |
| 01-matching-current.md | 匹配机制现状（三条链路/决策点/硬条件/根因） | analyst-matching |
| 02-kb-data-current.md | 政策收录、数据资产、知识库匹配原理 | analyst-kb |
| 03-llm-answer-current.md | LLM/智能体参与度、prompt、回答生成 | analyst-agent |
| 04-sse-observability-current.md | SSE 事件流与前端可观测性现状 | analyst-sse |
| 05-zhishu-reference.md | 智枢文渊参考架构（智能体/事件流/前端） | analyst-zhishu |
| m0-report.md | M0 实施报告（事件可观测性/地区软过滤/冲突程序化/预算，已上线验证） | lead |
| 08-p0-acceptance.md | P0 事件流重构独立验收（已 PASS） | analyst-sse |
| 09-p1-intake-contract.md | P1 输入理解层契约（已上线） | lead |
| 10-p2-match-core-contract.md | P2 匹配核心 v2 契约与实施记录（已上线） | lead |

> 11–29 为后续三轮迭代（回答样式/地区判定、地区全国化与选区、政策生命周期与自动管理）。
> 入口：`20-national-policy-lifecycle-plan.md`（总设计）、`21-policy-lifecycle-implementation.md`（后端实施记录）、
> `29-closeout-report.md`（收尾报告：交付物、开关矩阵、启用顺序、已知缺口）。

## 每份文档必须包含的段落

1. **一句话结论**（这份文档最重要的 3-5 条发现）
2. **现状架构**（mermaid 图 + 关键代码位置表）
3. **机制细节**（决策点/调用链/数据流，带 file:line 与常量）
4. **问题与根因**（按机制归类，不按个案；每条给证据与影响）
5. **与目标形态的差距**（目标：会思考的智能体 + 可解释的实时内部实况 + 智能匹配）
6. **改造切入点**（可复用资产 / 必须替换的部分 / 风险）
