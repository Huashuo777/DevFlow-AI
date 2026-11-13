# Day 3 — 上下文工程与三层记忆

**目标**：这是你自评最高的模块（2.5），要补到能扛三层追问，并把它做成你的**头号面试亮点**。
**权重**：★★★★★（2026 年 Agent 岗最热的方向就是 context engineering）

---

## 0. 为什么这个模块是你的最佳武器

大多数候选人的 Agent 项目只有"调 API + 拼 prompt"。本项目在上下文管理上有 890 + 1529 = **2419 行**专门代码，包含：
分级压力模型、两阶段压缩、熔断器、API 不变量保护、事实保真度校验、持久转录与活跃上下文分离、回捞逃生舱。

**面试时主动把话题往这里引。** 你的 G2（"最难的点"）最佳答案就是这个模块，而不是"划分好模块层次"。

---

## 1. 精读清单

### 1.1 压力分级与压缩策略（90 min）

| 位置 | 看什么 |
|---|---|
| `services/context_compression.py:193-228` | 压力分级：`normal / warning / auto_compact / aggressive / manual_path`，由"有效窗口 − 分层 buffer"算出 |
| `:205` `calculate_context_pressure` | 入口函数 |
| `:55-72` | `MICROCOMPACT_MARKER` 和"可再生工具输出"的判定 hints |
| `:492-572` | **microcompact**：把旧的可再生工具输出替换成标记 + **query 感知的抽取式片段**；`:523` 那行标记文本明确写了"需要精确细节就重跑对应工具" |
| `:313` `adjust_keep_start_to_preserve_api_invariants` | ★保留边界调整：**绝不能把 `tool_result` 和它的 `tool_use` 切开**，否则 API 直接报错 |
| `:693-711` `ensure_compaction_summary_preserves_key_facts` | ★事实保真度：文件路径/命令/错误/决策/todo/偏好这些"必须保留"的事实在摘要里核验，缺了就作为 fidelity appendix 补回去 |

**两阶段压缩**（面试必答）：
```
阶段 1 microcompact（便宜、无损语义）
  只清理"可再生的旧工具输出"（文件列表、搜索结果这类重跑就有的东西）
  → 换成标记 + 抽取式片段
  → 如果腾出的空间够了，就跳过全量压缩（chat_memory.py:677-687）

阶段 2 full compaction（贵、有损）
  LLM 摘要 → 或结构化 session memory → 或抽取式兜底（chat_memory.py:861-892）
  写入一条 system 的 compact_boundary 消息，携带：
    summary / preserved_message_ids / transcript_ref（恢复线索）/ rehydration 状态
    rehydration = 正在处理的文件、激活的 skill、最近用过的工具（chat_memory.py:894-1003）

升级计划：auto_compact → aggressive → manual_path
  逐级缩小保留消息数和摘要 token，直到压缩后压力回到 normal/warning（:736-797）
```

> 你 C1 答对了大方向（"压掉久远原文，保留最近原文 + 摘要 + 原文索引"）。要补的三个细节：
> ① 压缩是**分两阶段**的，先便宜的 microcompact，不够才上 LLM 摘要；
> ② 有一条**显式的 `compact_boundary` 消息**作为分界标记，不是悄悄改掉历史；
> ③ 保留边界要**调整以满足 API 不变量**（tool_use/tool_result 成对）。

### 1.2 触发时机与熔断器（30 min）

- `services/chat_memory.py:648` `ensure_headroom` — **每次请求前**调用（在 `_conversation_sse` 第 2 步），不是"超了才压"
- `chat_memory.py:691-698` — `context_auto_compact_enabled` 开关
- `chat_memory.py:1026-1060` — ★**熔断器**：连续 N 次压缩失败后停止自动压缩

> 熔断器是很好的面试点：*为什么需要？* → 压缩本身要调 LLM，可能失败（超时/额度/网络）。没有熔断器的话每次请求都重试一次失败的压缩，既拖慢又烧钱，还会把真实错误掩盖成"响应慢"。

### 1.3 观测结果投影 ★B4 的完整答案★（60 min）

- `services/agents/chat_agent.py:1474-1582` `_runtime_tool_context`
  - **先读 `:1480-1486` 的 docstring**，明确写了这是"durable transcript vs active context"分离，参照 Claude Code
  - token 预算控制、**最新优先**投影
- `chat_agent.py:1905` `_summarize_observation` — 单个观测怎么被裁剪
- `chat_agent.py:849` — **建了 `ToolMessage` 然后丢弃**。这是 vestigial code，可以主动讲（说明你读得细）
- `chat_agent.py:449-460` — 每步只发 System + Human 两条消息
- `chat_agent.py:459-483` — provider 报 context overflow 时，触发一次"aggressive 重投影"重试
- `context_compression.py:865` — `prompt_cache_model_kwargs`：稳定 System 前缀命中 prompt cache

**完整论证**（背下来）：

| | 累积 message 历史 | 每步重建 prompt（本项目） |
|---|---|---|
| 做法 | 每轮 append `assistant(tool_call)` + `tool(result)` | 只留 System + 一条 Human，观测结果按预算重新投影 |
| token | 单调增长，不可控 | 有硬预算，完全可控 |
| prompt cache | 前缀会变，难命中 | System 前缀稳定，可命中 |
| 模型可见性 | 完整工具历史 | **看不到完整历史** ← 代价 |
| 补救 | — | `devflow_get_thread_context` MCP 工具按需回捞原始转录（分页，带 offset 续读提示） |

### 1.4 三层记忆与存储（60 min）

读 `app/db/models.py` 里这些表，对照你 C3 的答案：

| 层 | 表 | 位置 | 作用域 | 存什么 |
|---|---|---|---|---|
| **持久转录** | `ChatMessage` | `models.py:302` | repo + conversation | 原始消息 + compact_boundary |
| **会话快照** | `ChatSession` | `:316` | 每轮对话 | 完整 `transcript_json`：tool_calls / agent_events / model_usage / diagnostics |
| **短期记忆** | `ConversationMemory` | `:349` | 每会话唯一 | summary / facts / decisions / open_questions / tasks / user_preferences / repo_context / citations |
| **仓库级记忆** | `ThreadMemory` | `:332` | 每 repo 唯一 | 同上结构，作为 conversation 级的 fallback |
| **长期记忆候选** | `MemoryCandidate` | `:415` | FK session_id | `pending` → 人工 approve/reject → 变成 RAG `Document` |
| **召回审计** | `RecallEvent` | `:397` | — | 每次记忆召回都记一条 |
| **检索面** | `EvidenceItem` | `:367` | — | 反规范化的统一搜索面，**每次请求重建** |

- `chat_memory.py:277` `EvidenceStore.rebuild_repo` — 从 repo/issues/PRs/失败CI日志/documents/memories/sessions/agent runs 重新生成 EvidenceItem
- `chat_memory.py:370-443` `EvidenceStore.search` — Milvus 向量优先，关键词兜底
- `chat_memory.py:165-239` `MessageStore.timeline` — **权威读路径，永不删除**
- `services/memory_hub.py:123-214` — MemoryCandidate 的人工批准流程
- `chat_memory.py:1338-1520` — 三个 MCP 记忆工具的实现（精确转录分页、offset 续读）

> 你 C3 答"感知/短期/长期"三层，方向对。要换成**代码里的真实分层**：持久转录（不删）/ 会话快照（sealed）/ 会话记忆 + 仓库记忆（结构化快照）/ 记忆候选（需人工批准才进 RAG）/ 召回审计。
> **关键设计**：长期记忆不是自动写入的，`MemoryCandidate` 必须**人工 approve** 才变成 RAG Document——这是防止错误结论被永久固化的闸门。

### 1.5 封盘（30 min）

- `chat_memory.py:1063-1068` `SessionSealer.seal`
  - **先写原始数据，再做 LLM 摘要增强**
  - 增强失败只降级状态为 `sealed_with_warnings`，**绝不让一轮已完成的对话失效**

> 这个顺序是刻意的设计：先保证不丢数据，再追求质量。反过来做的话 LLM 一挂就丢整轮记录。

---

## 2. C4 的完整答案（你答得不够）

你答"方便开发维护 + 用户看证据来源"。这两点都对，但漏了最重要的消费者：

**落库数据被三条路径消费：**
1. **评测**：`ragas_runner.py` 直接读 `ChatSession` 的 transcript 做回归对比；`runner.py` 用 `AgentRun` 算 tool_call 准确率
2. **记忆召回**：`EvidenceStore.rebuild_repo` 把历史 session 和 agent run 变成可检索证据 → 下次对话复用（这才是"沉淀"的闭环）
3. **可观测性与审计**：token 用量（provider 真实值 vs 估算值，含 cache token）、检索来源、skill 校验、诊断信息

---

## 3. 实操（45 min）

1. 在同一个会话里连续问 15+ 个会触发工具调用的问题，把上下文堆到触发压缩
2. 查 `ChatMessage` 表，找到那条 `compact_boundary` 消息，看它的 summary / preserved_message_ids / transcript_ref
3. 对比压缩前后 `ContextAssembler.assemble` 返回的 `compression_stats`
4. 压缩之后问一个"刚才那个文件的第 30 行是什么"，看模型是否调 `devflow_get_thread_context` 回捞
5. 把 `context_auto_compact_enabled` 关掉，看行为差异

---

## 4. 产出物

1. **压缩流程图**：压力分级 → microcompact → 够不够 → full compaction → 升级计划 → compact_boundary → fidelity appendix
2. **三层记忆表**（默写上面那张表）
3. **"为什么工具结果不进历史"完整论证**（默写那张对比表）
4. **G2 口述稿**：把"最难的点"改成上下文工程，讲清难在哪（token 预算 vs 信息保真的矛盾、API 不变量、压缩本身会失败、有损压缩后的回捞）

---

## 5. 自测题

- **C1** 压缩压掉什么、保留什么？两阶段分别是什么？什么时候触发？
- **C2** 压完之后需要被压掉的细节怎么办？（两个机制：transcript_ref + MCP 回捞工具）
- **C3** 记忆分几层？各存在哪张表？长期记忆为什么需要人工批准？
- **C4** 落库数据被谁消费？（三条路径）
- **新1** 为什么保留边界需要 `adjust_keep_start_to_preserve_api_invariants`？不调整会怎样？
- **新2** 压缩用的也是 LLM，它自己失败了怎么办？
- **新3** `ensure_compaction_summary_preserves_key_facts` 解决什么问题？如果没有它会怎样？
- **新4** microcompact 为什么只清"可再生"的工具输出？清掉不可再生的会怎样？

---

## 6. 笔记区

### G2 口述稿（最难的点 = 上下文工程）


### 三层记忆表（默写）


### 我踩的坑 / 意外发现


### 还没搞懂的问题

