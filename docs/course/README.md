# DevFlow-AI 学习课程

面向 **Agent 开发岗** 的 5 天精读计划。目标不是读完 25k 行代码，而是达到：
**能自洽地讲清架构 + 扛住 2~3 层追问 + 有 2 个能深挖的技术亮点。**

## 岗位定位与战场收窄

Agent 开发岗面试官关心的是：推理循环怎么设计、上下文怎么管、多 Agent 怎么编排、效果怎么评测。
不关心：前端实现细节、GitHub API 字段、数据库迁移。

因此自我介绍时就主动钉死范围，把追问引到读透的地方：

> "这个项目我主要负责**后端 Agent 链路**——推理循环、上下文工程、多 Agent 编排和评测体系。
> RAG 检索链路我也做了完整实现。前端是 Next.js + React Query，能改能调，但不是我的重点。"

## 5 天计划

| 天 | 主题 | 文档 | Agent 岗权重 |
|---|---|---|---|
| D1 | 一条消息的一生 / tool calling / LangGraph 循环 | [day1-agent-loop.md](day1-agent-loop.md) | ★★★★★ |
| D2 | RAG 全链路 / Agentic RAG | [day2-rag.md](day2-rag.md) | ★★★★ |
| D3 | 上下文工程 / 三层记忆 / 压缩 | [day3-context-memory.md](day3-context-memory.md) | ★★★★★ |
| D4 | 多 Agent 编排 / Skill Runtime / 安全写回 | [day4-multi-agent-skills.md](day4-multi-agent-skills.md) | ★★★★★ |
| D5 | Agent 评测 / 数据层 / 面试总装 | [day5-evals-interview.md](day5-evals-interview.md) | ★★★★ |

每天结构固定：**精读清单（文件+行号）→ 实操验证 → 产出物料 → 自测题**。
每天末尾有笔记区，把自己的话写进去——能用自己的话写出来才算学会。

## Agent 设计模式对照表（面试高频，必背）

面试官会用教科书名词提问。本项目每个模式的落点：

| 模式 | 本项目实现 | 代码位置 |
|---|---|---|
| **ReAct**（Reason+Act） | LangGraph `model_decision` ↔ `tool_action` 循环 | `services/agents/chat_agent.py:394` |
| **Plan-and-Execute** | PlannerAgent 产 WorkflowSpec → Orchestrator 执行 | `services/agents/planner_agent.py` |
| **Reflection / Critic** | ObserverAgent 产 finding → 触发 replan（≤2 轮） | `services/agents/observer_agent.py` |
| **Orchestrator-Worker** | Orchestrator 调度专用 Agent 作为 worker | `services/agents/workflow_orchestrator.py:154` |
| **LLM-as-Router** | ChatAgent 用 tool choice 决定走单 Agent 还是工作流 | `chat_agent.py:449` |
| **Function / Tool Calling** | 14 个工具，`bind_tools` 原生调用 | `api/routes/chat.py:935` |
| **Naive RAG** | 调 Agent 前预检索注入证据 | `services/chat_memory.py:463` |
| **Agentic RAG** | 循环内可多次改写 query 重查、交叉验证 | `rag.search_similar_documents` 工具 |
| **LLM-as-Judge** | RAGAS 5 指标 + judge 模型 | `services/evals/ragas_runner.py:32` |
| **Human-in-the-loop** | ActionDraft 草稿 → 独立 confirm 端点执行 | `api/routes/action_drafts.py:158` |
| **短期/长期记忆分层** | ChatMessage / ConversationMemory / MemoryCandidate | `services/chat_memory.py` |

## 三个"反直觉"设计（最好的深挖素材）

1. **工具结果不进 message 历史**。代码建了 `ToolMessage` 然后丢弃（`chat_agent.py:849`），每步按 token 预算把观测结果重新投影进 user prompt（`_runtime_tool_context:1474`）。刻意模仿 Claude Code 的 durable transcript / active context 分离。
2. **Planner / Observer / Synthesis 全都不用 LLM**，是确定性规则代码。LLM 只在叶子 Agent，且输出永远与启发式兜底 merge + 领域守卫。→ 可测试、可复现、便宜。
3. **RAG 有答案门禁**（`services/rag/answer_gate.py:75`）：生成前规则检查，证据不足/冲突/指代不清直接拒答或要求澄清，不进 LLM。

## 三个真实缺陷（主动说出来是加分项）

| 缺陷 | 事实 | 改进方案 |
|---|---|---|
| pgvector 名不副实 | 装了依赖、`scripts/init_pgvector.sql` 建了 extension，但应用代码零引用，向量全走 Milvus | 用 pgvector 收敛基础设施，砍掉 Milvus+etcd+MinIO 三个组件 |
| BM25 无倒排索引 | 每次查询在 Python 进程内全量扫描+分词该 repo 所有文档（`rag/retrieval.py:263`） | 迁到 PostgreSQL FTS 或建持久化倒排索引 |
| 权限模型非强制 | `services/permissions.py` 有完整 RBAC 矩阵，但 `require_permission` 自动创建并使用第一个 demo 用户，无真实认证 | 接 OAuth/JWT，按真实身份鉴权 |

另外两个小缺陷可备用：GitHub 同步无软删除（远端删掉的 Issue 永久残留本地）；`frontend/app/page.tsx` 单文件 5933 行。

## 明确放弃的内容

- **前端 10k 行**——零基础、5 天学不完、Agent 岗不深挖
- `services/knowledge_graph.py` 细节（知道它是"从 DB 内容派生类型化边"即可）
- ragas 库内部实现（只需知道 5 个指标的定义）
- 逐个读 27 个测试文件（只需知道覆盖了什么、没覆盖什么）
- MCP 的 JSON-RPC 手写协议细节（知道"手写 stdio MCP server，暴露 3 个记忆工具"即可）

## 进度追踪

- [ ] Day 1 — Agent 推理循环
- [ ] Day 2 — RAG 全链路
- [ ] Day 3 — 上下文工程与记忆
- [ ] Day 4 — 多 Agent 与 Skill
- [ ] Day 5 — 评测与面试总装
