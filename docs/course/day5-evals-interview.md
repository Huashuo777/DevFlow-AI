# Day 5 — Agent 评测 / 数据层 / 面试总装

**上午 2.5h**：评测体系 + 数据层速通
**下午 2.5h**：面试总装 + 模拟追问
**权重**：评测 ★★★★（区分"玩过 Agent"和"做过 Agent"的分水岭）／数据层 ★★（够用即可）

---

# 第一部分：评测体系

## 1. 为什么这是分水岭

绝大多数候选人的 Agent 项目**没有评测**。被问"你怎么知道它好用"时只能答"我试了几个 case 感觉不错"。
本项目有两套评测 + 一个前端评测页 + 回归对比 + 失败诊断 + 一键生成 Issue 草稿。**这是你最硬的差异化素材之一。**

## 2. 精读：RAGAS 评测（90 min）

`services/evals/ragas_runner.py`（1229 行，只读下面这些段）：

| 位置 | 看什么 |
|---|---|
| `:32` | `REQUIRED_RAGAS_METRICS = (*CORE_RAGAS_METRICS, "agent_goal_accuracy")` — **5 项门禁指标** |
| `:626-666` | ★**隔离会话**：每个 case 开一个 `Conversation(status="evaluation")`，包在 `db.begin_nested()` **savepoint** 里，跑完 **rollback**（`:635`） |
| — | 复用的是**生产代码**：`EvidenceStore.rebuild_repo` / `ContextAssembler.assemble` / `_build_conversation_tools`（直接从 `routes/chat.py` import） |
| `:173-178` | embedding 复用项目自己的 Qwen embedder |
| `:180-209` | Judge 配置：Ragas 0.4 Collections API + `llm_factory` over AsyncOpenAI，temperature 0；judge key/model 回落到 `LLM_*` |
| `:39-73` + `:247-278` | ★**faithfulness 分块**（~700 字符）+ **按长度加权**，为了适配 judge 的输出 token 预算 |
| `:549-593` `_hard_rules` | 硬规则：期望来源命中 / 必需工具出现 / 无工具错误 / 答案与 contexts 非空 / `must_include` / `must_not_include` / `answer_regex` |
| `:496-518` | 检索指标：recall@k / precision@k / MRR / nDCG |
| `:842-885` | ★**双门禁 AND**：硬规则必须在**所有** case 上通过，**且** 5 项语义指标均值达到各自阈值 |
| `:859` | `required_scored` — 任何指标缺分 → 判 `incomplete/error`，**绝不静默通过** |
| `:706-819` | 逐 case 失败诊断 |
| `:1114-1131` `rescore_rag_result` | ★**只重判异常评分**：复用已存的回答和证据，不重跑 ChatAgent |
| `:362-485` | `auto-document-smoke` 套件：从 repo 的文档/issue/PR/失败 CI **自动生成** case |

**5 项指标要能一句话说清定义**：

| 指标 | 衡量什么 |
|---|---|
| Context Precision | 召回的上下文里，相关的排在前面吗（信噪比 + 排序质量） |
| Context Recall | 回答参考答案需要的信息，都被召回了吗 |
| Faithfulness | 回答里的每个论断都能从上下文推出来吗（**直接反幻觉**） |
| Answer Relevancy | 回答有没有答到点上（不跑题、不冗余） |
| Agent Goal Accuracy | Agent 有没有真正完成用户目标（**Agent 特有**，标准 RAGAS 没有） |

> ★**"执行状态"和"质量状态"分开**★（README 也强调了）：
> 低分 = **质量失败**；judge 调用失败或指标缺失 = **评测不完整**。
> 后者绝不能伪装成质量结论。这个区分是很成熟的工程判断，面试重点讲。

## 3. 精读：基础评测（20 min）

`services/evals/runner.py`（224 行）——**用 `NoopLLM`（`:24`）跑，零 token 成本**：

| 检查项 | 位置 |
|---|---|
| Issue 分类准确率（对 3 个期望类别） | `:42-48` |
| PR 关键文件覆盖度 | `:50-62` |
| RAG hit@3（用 `deterministic_embedding` 算余弦） | `:64-75` |
| 上下文压缩质量 | `:78-116` |
| CI pytest 失败检测 | `:118-135` |
| Pydantic schema 校验通过率 | `:136-141` |
| Skill registry 覆盖率 / entrypoint 准确率 | `:145-161` |
| `tool_call_accuracy` — **硬编码 1.0**（原生 tool calling 接管后失去意义） | `:143-144` |

> 这套的价值定位：**不花钱的冒烟/回归测试**，跟 RAGAS 那套"花 judge token 的语义评测"是互补的两层。这个分层本身可以讲。

## 4. ⚠️ 必须在面试前处理的问题

**`docs/datas/evals/` 目录不存在。**

- `services/evals/suites.py:15` 把 `DEFAULT_SUITE_DIR` 指向 `docs/datas/evals`
- README 第 134 行声称"主项目内置的正式基准位于 `docs/datas/evals/rag_cases.json`"
- 实际 `ls docs/` 只有 `api / architecture / course / examples`
- `.gitignore` 里**没有**忽略 `docs/datas`，所以它不是被忽略，是真的缺失

**后果**：固定套件加载会失败，只剩 `auto-document-smoke` 自动生成套件可用。如果你面试时说"我有一套固定基准做回归对比"，对方要求看，就露馅了。

**两个选择（Day 5 必须做一个）**：
- **A（推荐）**：真的写出 `docs/datas/evals/rag_cases.json`，放 8~12 个 case，覆盖"证据充分/证据不足/冲突/需澄清/需要多跳检索"几类。这样"固定基准 + 回归对比"的说法才成立，而且写 case 的过程会强迫你彻底理解 RAG 链路。
- **B**：改口径，只讲 `auto-document-smoke` 自动生成套件 + 双门禁 + rescore 机制，不提"固定基准"。

## 5. 实操（30 min）

1. 前端 `/evals` 页 → 选仓库、评测集、Top-K → 跑
2. 看 `POST /api/evals/rag/run` 立即返回 `queued` + `eval_id`，然后轮询 `GET /api/evals/{eval_id}` 的 `result.progress`
3. 展开一个 case，看实际回答 / 参考答案 / 检索来源 / 工具轨迹 / 诊断
4. 试"仅重试异常评分"（rescore），确认它没重跑 ChatAgent
5. 试两个结果的回归对比，注意它要求**相同的评测集 Hash + Ragas/Judge 配置 + Top-K**
6. 试生成 `create_issue` 类型的 ActionDraft

---

# 第二部分：数据层速通（60 min，够用即可）

Agent 岗不会深挖这块，但**答不上来会显得工程基本功差**。目标是"知道有什么、能说出关键限制"。

## 6. GitHub 同步

`services/repos_sync.py`（201 行）+ `services/github/client.py`（58 行）：

- **Upsert，不是增量 delta**：每次拉最多 `limit`（默认 50）条，按 `(repo_id, github_*_id)` upsert
- **没有 `since` 游标，没有墓碑** → ★**远端删掉的 Issue/PR 会永久残留本地**★（这是你 F1 的正确答案）
- PR files 和 review comments 是 **delete-and-rebuild**（`:90`、`:107`）
- Workflow jobs+steps 拍平成 JSON（`:146-171`）；**只有 `conclusion == "failure"` 才拉日志**（`:172-176`）← 省流量的好设计
- **四个触发点**：手动 `POST /repos/{id}/sync`／连接时（`repos.py:256`）／webhook（`webhooks.py:54-56`，支持 issues/pull_request/workflow_run/push/check_suite/check_run，**HMAC-SHA256 签名校验**在 `:16-24`）／定时循环 300 秒（`scheduler.py:13-29`）
- **认证**：每 repo 一个 PAT，**Fernet 加密**存储（`TOKEN_ENCRYPTION_KEY`，`repos.py:243`）；client 发 Bearer，回落到全局 `settings.github_token`（`client.py:10-18`）；支持 GHE 兼容 base URL
- ★**没有任何 rate limit 处理**★：只有 `raise_for_status()`；files/comments/runs 的失败被吞成空列表（`:94`、`:110`、`:131`），代码里 `:111` 有注释自己承认了这个风险

## 7. 代码子系统（30 min，知道边界即可）

| 文件 | 干什么 | 关键限制 |
|---|---|---|
| `services/code_analysis.py`（403 行） | 浅克隆/pull 到 `.data/repos/{id}-{owner__name}`（`:165-200`，managed 模式）或用用户 `local_path`（local 模式）；**正则**抽符号写 `CodeSymbol`，建 `imports`/`calls` 边写 `CodeRelation` | ★**正则而非 AST/tree-sitter**（`:91-97` 只认 Python `def\|class` 和 TS/JS function/class/interface/type）；上限 500 文件 |
| `services/worktree_manager.py`（218 行） | ★**每个 PR 一个真实 `git worktree`**★ `prepare_pr_worktree:112-166`：把 PR head 分支 fetch 到 `.data/repos/worktrees/{repo_id}/pr-{n}-...`，**带每 PR 锁**，重跑代码图并按 `pr_id/pr_number` 打作用域，写 `.devflow-pr-snapshot.json`；失败回落默认分支 checkout；`cleanup_pr_snapshots:169` 清 >168h 的快照及其符号 | 端点：`POST /pull-requests/{id}/worktree` 和 `/snapshot` |
| `services/code_search.py`（389 行） | 关键词搜 checkout：**有 ripgrep 就用 `rg --json`，否则 Python 扫描**（`:316-337`）；停用词分词；路径逃逸防护 + 密钥文件黑名单（`SECRET_FILENAMES`，`:125-143`） | 返回 `workspace_file` 结果项 |
| `services/knowledge_graph.py`（544 行） | 从 **DB 内容**派生类型化边：PR→issue `resolves`/`mentions`（closing 关键词正则 `:25-29`）、PR→code_file `touches_file`、失败 CI→PR/issue `failed_in_ci`（`:224-235`）、issue assignee→team_member、记忆决策与 AgentRun 引用→`cites`/`used_as_evidence`；BFS 子图上限 72 节点（`:367-404`） | **和符号级 code graph 是两回事**，别混 |
| `services/project_indexing.py`（579 行） | 发现 README/docs/**.md**/manifest（分级 `authoritative\|soft_clue\|derived`，`:151-164`）；尊重 `.devflow/index.yml` 白/黑名单（`:196-229`）；切片 1800/180 入 Document + Milvus；mtime/size 指纹判过期；`ProjectIndexState` 状态机 missing/stale/building/ready/failed + snooze；最后刷新 EvidenceItem 并重建知识图（`:490-563`） | — |

> **F2 的答案（你完全不知道）**：git worktree 是 git 原生的"同一个仓库多个工作目录"能力，共享 `.git` 对象库但各自 checkout 不同分支，比 clone 省空间省时间。
> **本项目为什么需要它**：要审查 PR 就得看到 PR 那个版本的代码，但不能污染主 checkout（用户可能正在别的分支上工作）。所以每个 PR 建一个独立 worktree，代码图的符号按 `pr_id/pr_number` 打作用域，审查完 168 小时后清理。
> **这是很扎实的工程细节，值得讲。**

> **F3 的答案**：符号图是**正则级**的，不是 AST。能抓到 Python 的 `def`/`class` 和 TS/JS 的 function/class/interface/type，建 `calls`/`imports` 边。
> **主动承认精度限制**：正则会被注释和字符串里的假匹配骗到，也解析不了跨行签名、装饰器、动态调用。**这是个有意识的取舍**——AST 精确但要按语言装解析器（tree-sitter），项目选择在 500 文件上限内用轻量方案。

## 8. 数据模型（20 min，只需能分组说出）

`db/models.py`（548 行），30+ 表，全部 UUID4 主键，几乎都 FK 到 `repositories.id`。
**没有 Alembic**：启动时 `Base.metadata.create_all` + `main.py:104-177` 一堆 ad-hoc `ALTER TABLE`（← 这本身是个可以承认的技术债）。

| 分组 | 表 |
|---|---|
| GitHub 镜像 | `Repository:26` → `Issue:96` / `PullRequest:116` → `PRFile:138` + `PRReviewComment:152`（cascade delete-orphan）；`WorkflowRun:168`（jobs/steps 存 JSON，失败时存 `logs_text`） |
| 会话与记忆 | `Conversation:84` → `ChatMessage:302` / `ChatSession:316`；`ThreadMemory:332` / `ConversationMemory:349`；`MemoryCandidate:415` / `RecallEvent:397` / `EvidenceItem:367` |
| RAG | `Document:199`（chunk 全文，PG 侧）/ `KnowledgeBaseConfig:212` / `KnowledgeSourceDocument:223`（上传件+checksum）/ `RetrievalTestRun:253` / `KnowledgeGraphEdge:381` |
| 代码图 | `CodeSymbol:265`（带 branch/commit_sha/pr_id/pr_number 作用域）/ `CodeRelation:284` |
| 工作流 trace | `AgentRun:432` / `AgentWorkflowRun:472` → `AgentTaskRun:488` / `AnalysisResult:186` |
| 评测 | `EvalRun:505`（name + 完整 result_json blob） |
| 安全写回 | `ActionDraft:514`（draft_type / risk_level / status `pending_confirmation→executed\|failed\|cancelled` / created_by / confirmed_by） |
| 反馈 | `ChatFeedback:446`（每 assistant_message_id 唯一，rating/reason/review_status/notification_status） |
| 其他 | `User:12`（role + 加密 token）/ `Workspace:53`（JSON repo_ids）/ `ProjectIndexState:65` / `AuditLog:536` |

## 9. 测试覆盖（10 min，F4 的答案）

**27 个 pytest 文件**，在 `backend/app/tests/`。

- ✅ **覆盖好**：agents（chat/issue/pr_review/ci_debug/recommendation/workflow_orchestrator）、RAG（qa/retrieval pipeline/rerank/source policy/project isolation/vector store/chunking）、记忆（context_memory_projection/context_compression/conversation_deletion/chat_feedback）、代码（code_indexing/code_search/knowledge_graph/project_indexing）、repos（connect/sync/deletion）、evals（test_eval_runner）、skills（skill_registry）
- ❌ **没覆盖**：`worktree_manager`、`permissions`/`action_drafts`、`webhooks`、MCP server/client、**`ragas_runner` 本身**、**整个 API 路由层**（基本都是 service 级测试）、前端零测试

> 面试话术：*"测试集中在 service 层，因为那里是业务逻辑和确定性规则所在——特别是编排层和 RAG 链路不用 LLM，可以直接单测。路由层和 worktree 管理覆盖不足，ragas_runner 自己没被测，这是我知道的缺口。"*

## 10. 基础设施（10 min）

- `docker-compose.yml`：postgres（`pgvector/pgvector:pg16` + init SQL）／redis（可选 profile）／etcd + minio（走华为云 SWR 镜像，`:43-61`）／**Milvus v2.5.4 standalone**（`:63-76`）
- **后端和前端不进容器**，本地跑
- MinIO 的坑：2025 年停止免费分发，Docker Hub 仓库被删、quay.io 转私有、`dl.min.io` 返回 410，所以改用 SWR 上仍在镜像 docker.io 的公共副本，拉下来重新 tag 成 `minio/minio:latest`（README 有记录）
- `.env` 配置组（只记组名）：app/CORS · DATABASE_URL+POSTGRES · GITHUB · LLM · EMBEDDING/LOCAL_EMBEDDING/DASHSCOPE · MILVUS+RAG_RERANK_TOP_N · RAGAS（开关/judge/超时/7 个阈值）· CONTEXT_*（约 19 个 token 预算与压缩旋钮）· PROMPT_CACHE · REPO_CHECKOUT_DIR/UPLOAD_DIR · TOKEN_ENCRYPTION_KEY/DEMO_MODE/GITHUB_WEBHOOK_SECRET · FEISHU_FEEDBACK_WEBHOOK · AUTO_SYNC · NEXT_PUBLIC_API_BASE_URL

---

# 第三部分：面试总装（2.5h）

## 11. 3 分钟项目介绍（升级你的 G1）

你的 G1 骨架（输入四链路／输出／工作流两类／评估）是好的，但缺**技术纵深**和**结果**。用这个版本：

> **一句话定位**
> DevFlow-AI 是一个面向 GitHub 研发流程的多 Agent 系统。它把仓库的 Issue、PR、CI 数据镜像到本地，让开发者用自然语言问"这个 PR 能不能合"这类跨领域问题，系统给出带证据引用的分析和**待人工确认的操作草稿**。
>
> **我负责的部分**（钉死战场）
> 后端 Agent 链路：推理循环、上下文工程、多 Agent 编排、RAG 检索和评测体系。
>
> **三个技术要点**（每个 30 秒，留出被追问的钩子）
> 1. **推理循环**：LangGraph 状态机跑原生 tool calling，14 个工具。关键选择是**工具结果不进 message 历史**，而是每步按 token 预算重新投影进 prompt——参照 Claude Code 的 durable transcript / active context 分离。好处是 token 完全可控且 System 前缀稳定能命中 prompt cache，代价是模型看不到完整工具历史，所以配了一个 MCP 工具做按需回捞。
> 2. **上下文工程**：两阶段压缩（先清理可再生的旧工具输出，不够才上 LLM 摘要），带压力分级、熔断器、以及"tool_use/tool_result 不能被切开"的 API 不变量保护，还有一道摘要事实保真度校验。
> 3. **多 Agent 编排**：Planner → 波次并行 → Observer → Synthesis。**编排层完全不用 LLM**，是确定性规则代码，LLM 只在叶子 Agent，且输出必须过 Pydantic 校验再和业务规则 merge。Observer 会做**跨 Agent 冲突检测**——PR Agent 说能合、CI Agent 说阻塞，这种矛盾会被提成 blocker。
>
> **可验证性**
> 两套评测：一套 NoopLLM 的零成本冒烟回归，一套 RAGAS 的语义评测，在 savepoint 隔离会话里跑真实生产 ChatAgent，5 项指标 + 硬规则双门禁，并且把"执行失败"和"质量不达标"分开呈现。27 个 service 层单测。
>
> **收尾（主动亮缺陷）**
> 有几个我知道的问题：pgvector 装了但没用上，向量全走 Milvus；BM25 是进程内全量扫描没倒排索引；权限模型有 RBAC 矩阵但认证是 demo 用户。如果继续做，我会先收敛基础设施。

**注意**：这段里有 5~6 个"钩子"（tool calling 机制、压缩、跨 Agent 冲突、评测门禁、缺陷）。面试官一定会挑一个追问，**而你每一个都读透了**。这就是"主动收窄战场"。

## 12. G2：最难的点（换掉"划分模块层次"）

你原来的答案太虚，面试官会觉得你没深入过。**换成上下文工程**：

> 最难的是**上下文工程**，因为它是一个多方约束的平衡问题，没有标准答案。
>
> 具体难在四点：
> 1. **token 预算和信息保真是矛盾的**。压得狠了模型丢失关键决策依据，压得轻了就超窗口。我们的做法是分两阶段——先做便宜的 microcompact，只清理"重跑工具就能拿回来"的旧观测；不够才上 LLM 摘要。
> 2. **压缩本身会失败**。它也要调 LLM，会超时、会没额度。所以加了熔断器，连续失败就停止自动压缩，避免每次请求都白白重试一次失败操作。
> 3. **有硬性的 API 不变量**。保留边界不能把 `tool_use` 和它配对的 `tool_result` 切开，否则 API 直接报错。这不是业务逻辑，是协议约束，很容易踩。
> 4. **摘要是有损的，而且会悄悄丢掉最关键的东西**。LLM 摘要倾向于保留"叙述"丢掉"事实"——文件路径、具体命令、错误码。所以加了一道保真度校验：这些必须保留的事实在摘要里核验，缺了就作为附录补回去。
>
> 另外还有一个更底层的选择：**工具结果到底进不进 message 历史**。我们选了不进，每步重建。这让 token 完全可控、prompt cache 能命中，但代价是模型看不到完整历史，所以要额外提供一个回捞原始转录的 MCP 工具作为逃生舱。这是个明确的取舍，不是免费的。

## 13. G3：重做会改什么（准备 4 个，按分量排序）

| # | 改什么 | 怎么讲 |
|---|---|---|
| 1 | **收敛基础设施：pgvector 替掉 Milvus** | "pgvector 已经是依赖了，init SQL 也建了 extension，但代码零引用。Milvus standalone 要拖 etcd + MinIO 两个组件，部署复杂度陡增，而 MinIO 还因为 2025 年停止免费分发导致镜像源要绕道华为云 SWR。我会评估用 pgvector 收敛成单一 PostgreSQL——**同时还能把 BM25 迁到 PG 全文检索，一并解决第二个问题**。当然代价是放弃 Milvus 的分布式扩展能力，需要按数据量权衡。" |
| 2 | **BM25 建持久化倒排索引 / 迁 PG FTS** | "现在每次查询在 Python 进程内全量扫描并分词该 repo 的所有文档，是 O(文档数) 的。demo 规模没问题，文档上万就撑不住。而且中文用的是 unigram+bigram，本身就是个近似方案。" |
| 3 | **GitHub 同步做真增量 + 软删除 + rate limit** | "现在是 upsert 无游标，远端删掉的 Issue 永久残留本地，会污染 RAG 证据。而且完全没有 rate limit 处理，失败还被吞成空列表——这会让'同步成功但数据是空的'看起来像正常状态。我会加 `since` 游标、墓碑标记、以及区分'真的没有'和'请求失败'。" |
| 4 | **真实认证 + 代码图升级到 AST** | "RBAC 矩阵是完整的，但 `require_permission` 自动用第一个 demo 用户，所以权限模型是结构性的、没被强制执行。代码符号图是正则级的，会被注释和字符串里的假匹配骗到，跨行签名和装饰器也解析不了——要真正可靠得上 tree-sitter。" |

**加分收尾**：*"还有一个架构层面的：Planner 现在用中英文关键词匹配来决定规划什么任务，这个比较脆。但如果换成 LLM 规划，就要面对不可复现和额外成本。我倾向于保留确定性编排，但把触发条件从关键词改成结构化的意图分类——这样既保住可测试性，又降低脆弱度。"*

## 14. 必备追问预案

| 追问 | 一句话答案 | 详见 |
|---|---|---|
| tool calling 到底怎么工作的？ | 工具 JSON Schema 随请求发给 LLM；LLM 只返回"我想调什么"的结构化文本；执行在你的进程内本地调用；结果再发下一次请求 | D1 §2 |
| 模型死循环调同一个工具怎么办？ | 工具名+参数做指纹，重复 >2 次阻断；外层 max_steps=12；再外层 LangGraph recursion_limit | D1 §1.3 |
| 工具报错怎么处理？ | 7 类错误分类；只有 timeout/rate_limited/transient_network 重试 1 次；**safety/report/workflow 永不重试**（有副作用）；失败变成 observation 喂回模型，不抛异常 | D1 §1.3 |
| 怎么防 RAG 幻觉？ | 五道防线：source_policy 限源 → answer_gate 生成前门禁 → score_threshold → system prompt 强制引用+知识不可信 → 无 LLM 时抽取式兜底 | D2 §1.5 |
| 为什么不用 RRF？ | 先做 max 归一化让两路分数量纲一致，就能加权，还能表达"双路召回"bonus；RRF 只用排名会丢掉分数强度 | D2 §1.3 |
| 换 embedding 模型的风险？ | 不同向量空间混在一个 collection 里，相似度失去意义且**不报错**，只是静默崩坏。所以四层硬校验：配置 API / embed 调用 / upsert / collection schema | D2 §1.2 |
| 为什么需要多 Agent 而不是一个大 prompt？ | 最有力的论据是**跨 Agent 冲突检测**：PR 说能合、CI 说阻塞，单一视角发现不了。其次是各 Agent 有独立的 skill、claim 作用域和输出契约 | D4 §1.4 |
| 任务失败怎么办？ | **skip-downstream，零重试**：下游标 skipped + `dependency_failed`。重试只在工具层，且分错误类型 | D4 §1.3 |
| 为什么编排层不用 LLM？ | 可单测、可复现、零 token 成本、行为可预测。代价是规则硬编码覆盖不全，所以叶子 Agent 的 LLM 输出必须过领域守卫 | D4 §0 |
| 怎么保证 Agent 真的照 Skill 做了？ | 记录 skill 版本、sha256 指令摘要、激活原因，**并事后验证 entrypoint 是否真的执行成功**，写成 `skill_validation` 事件 | D4 §1.7 |
| 怎么防止 Agent 乱写 GitHub？ | 三重强制：Agent 代码里根本没有执行路径，只建 ActionDraft；执行走独立的 `draft:approve` 端点且拒绝非 pending 状态；工作流层 Observer 把缺安全审查变成 blocker | D4 §1.8 |
| 你怎么知道它好用？ | 两套评测。RAGAS 那套在 savepoint 隔离会话里跑**真实生产 ChatAgent**，5 项语义指标 + 硬规则**双门禁 AND**，judge 失败判 incomplete 绝不静默通过，还支持只重判异常评分 | D5 §2 |
| 上下文超了怎么办？ | 请求前先查余量，两阶段压缩，写一条显式 compact_boundary，带 transcript_ref 和 rehydration 状态，配 MCP 工具回捞原始转录 | D3 §1.1 |
| 这项目是你自己写的吗？ | **见下** | §15 |

## 15. "这是你自己写的吗" —— 最重要的预案

如果这项目主要是 AI 辅助生成的，**不要撒谎**，但也**不要自我贬低**。用这个框架：

> "这个项目我是用 AI 辅助开发的，但**架构决策、技术选型和每一处取舍是我做的**，而且我逐行读过并且能解释。
> 我可以举几个不是 AI 会随便给你的决策：
> - 工具结果不进 message 历史、每步重建投影，这是参照 Claude Code 的 durable/active context 分离，代价是模型看不到完整历史，所以配了 MCP 回捞工具；
> - 编排层刻意不用 LLM，Planner/Observer/Synthesis 全是确定性规则，为了可测试和可复现；
> - embedding 向量空间做了四层硬校验，因为混空间是静默失败，生产环境最难查；
> - 评测把'执行失败'和'质量不达标'分开呈现，避免 judge 挂了伪装成低分。
> 同时我也知道它的缺陷在哪：pgvector 装了没用、BM25 没有倒排索引、GitHub 同步没有软删除、权限是 demo 用户。"

**然后主动邀请深挖**：*"您可以随便挑一个模块问我实现细节。"*
这句话只有真读懂了才敢说，而且它会立刻改变面试官对你的判断。

## 16. 模拟面试（最后 1h）

让我扮演面试官，规则：
1. 你先讲 3 分钟介绍
2. 我挑一个点**连环追问三层**（例：工具结果不进历史 → 那模型怎么知道之前调过什么 → 回捞工具的 token 成本谁控制 → 如果回捞的内容又超窗口了呢）
3. 每个追问你必须答出**代码位置**或**明确的"这里我没读，但我推测是..."**
4. 答不上来的记进 `DevFlowLearnLog.md`，作为后续补漏清单

---

## 17. 笔记区

### 我的 3 分钟介绍（最终版）


### G2 最难的点（最终版）


### G3 重做会改什么（最终版）


### 模拟面试答不上来的问题

