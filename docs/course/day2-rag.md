# Day 2 — RAG 全链路 / Agentic RAG

**目标**：能画出 ingestion + retrieval 双链路图；能说清五道防幻觉防线；能回答"你怎么证明 RAG 好用"。
**权重**：★★★★（AI/Agent 岗必问；你的直觉不错但细节空白）

---

## 0. 先建立边界感：什么进 RAG，什么不进

读 `app/services/rag/source_policy.py`（57 行，全读，10 分钟）。这是最短的文件，但决定了整个 RAG 的边界。

```
进 RAG（RAG_SOURCE_TYPES）：issue / pull_request / workflow_run / project_doc / knowledge_file / memory_note
不进 RAG（走"direct"）：  project_overview / team_member / weekly_report / chat 类
硬禁止：                  code_file —— add_document 直接抛异常
```

**为什么源码不进 RAG**（面试必答）：代码 embedding 会随 commit 过期，而 `grep`/`workspace.search_code` 永远读的是当前 checkout。
**为什么结构化字段不进 RAG**：Issue 状态、负责人这种精确字段，语义检索反而不如一条 SQL 准。

> 一句话总结这个设计哲学：**只有"需要跨来源语义召回、会反复使用的非结构化知识"才向量化。**

---

## 1. 精读清单

### 1.1 Ingestion 链路（90 min）

| 文件 | 读哪里 | 看什么 |
|---|---|---|
| `api/routes/rag.py:181-242` | 上传端点 | 12MB 限制、扩展名白名单、**SHA-256 查重**、BackgroundTask |
| `rag/document_extraction.py`（128 行，全读） | 全文 | PDF 加 `<!-- page: N -->` 标记；DOCX 把 Heading/List/Table 转 Markdown + **zip-bomb 防护**；编码回退链 `utf-8-sig→utf-8→gb18030→latin-1` |
| `rag/chunking.py:761` | `chunk_document` 入口 | 按扩展名分派到不同策略 |
| `rag/chunking.py:492` | `_markdown_blocks` | **标题栈** → `section_path`；代码围栏识别；页标记 |
| `rag/chunking.py:346` | `_pack_blocks` | 打包规则：**chunk 绝不跨 section、绝不跨 page** |
| `rag/chunking.py:275` | `_section_prefix` | 每个 chunk 前面加标题面包屑（上限 `min(512, max_chars/3)`） |
| `rag/chunking.py:229` | `_overlap_blocks` | overlap 取尾部 block，**吸附到句子/词边界，跳过代码围栏** |
| `rag/chunking.py:111` | `_split_long_code_fence` | 超长代码块切开后**每一片重新用 ``` 包起来** |
| `rag/chunking.py:44` | `_stable_id` | `chunk_id`/`parent_id` = 稳定输入的 SHA-256 截断 → 支撑父子扩展 |
| `rag/document_processing.py:101-143` | embed + 入库 | embed 的是 `f"{title}\n{content}"`；批大小 64；状态机 `queued→parsing→splitting→embedding→completed/failed` |

**默认参数**：`max_chars=1600, overlap=160`，单文档硬上限 5000 chunks。

> 你的 D1 答"简单按字数切语义不完整"——方向对，但要说得出**这里的解法是结构感知**：先解析成 block（标题/段落/代码围栏/表格行），再打包，且 chunk 不跨 section 边界，还额外给每个 chunk 加标题面包屑当上下文。

### 1.2 Embedding 与向量空间守卫（30 min）

- `rag/embeddings.py`（223 行，全读）
  - `:21-29` `deterministic` fallback = SHA-256 token 哈希词袋向量（**只是 demo/eval 用**）
  - `:63-76` **`validate_embedding_contract`** ★：比对请求的 (provider, model, dims) 与配置的契约，不匹配直接抛错
  - `:155` 本地 Qwen 查询侧带 instruction prompt（`prompt_name="query"`）
- 四层防线（面试亮点）：**配置 API（`rag.py:153` 返回 409）→ embed 调用 → Milvus upsert（`vector_store.py:248`）→ collection schema（`vector_store.py:72`）**，任何一层维度不匹配都拒绝，绝不静默截断/补零。
- collection 名字本身编码了向量空间：`devflow_rag_qwen1024`。

> 你 D5 答对了风险（"向量和原文不匹配，需要重新 embedding"）。补上**后果**：不同向量空间混在一个 collection 里，余弦相似度完全失去意义，而且**不会报错**，只会静默地检索质量崩坏——这是生产环境最难查的一类 bug，所以要在四层都做硬校验。

### 1.3 Retrieval 链路（90 min）★核心★

- `rag/retrieval.py:15-26` — 融合常量，**背下来**：
  ```python
  BM25_K1 = 1.5
  BM25_B = 0.75
  VECTOR_FUSION_WEIGHT  = 0.62
  KEYWORD_FUSION_WEIGHT = 0.30
  DUAL_RECALL_BONUS     = 0.08   # 两路都召回到的额外奖励
  MAX_CHUNKS_PER_PARENT = 2
  NEAR_DUPLICATE_JACCARD = 0.9
  ```
- `:560` `search_similar_documents` — 主入口，看完整流程
- `:127` `_normalise_scores` — **每路先做 max 归一化**（关键！）
- `:197/:222` `_bm25_document_stats` / `_bm25_score` — 进程内 BM25，字段加权：symbol 4.5 / title 4.0 / path 3.0 / labels 2.5 / content 1.0
- `:113` `_tokens` — 中文用 unigram + bigram 分词
- `:316` `_merge_candidates` — 按 `chunk_id` 合并
- `:400-436` 去重 — 归一化文本 SHA-256 指纹 + Jaccard ≥ 0.9 近重复检测，**限制在同一 parent_id 范围内**
- `:464` `expand_parent_context` — **small-to-big**：命中一个 chunk 后换成它父文档的邻近 chunk（`neighbor_window=1`），并用 branch/commit/checksum 做版本守卫（`:455`）

**为什么用加权融合而不是 RRF**（面试高频，你 D2 说"不清楚"）：
> RRF 只用**排名**，丢掉了分数的**绝对强度**。本项目先对两路各自做 max 归一化，让余弦相似度和 BM25 分数落到同一量纲，于是可以直接加权，还能额外给"两路都召回"的候选加 bonus——这是 RRF 表达不了的信号。代价是归一化对异常高分敏感，且权重是手工调的。

### 1.4 Rerank 与降级（30 min）

- `rag/rerank.py:56-108` — 启发式重排器：`0.31*base + 0.20*vector + 0.16*keyword + 0.17*词覆盖率`，再加标题/路径命中、短语 bonus、新鲜度（≤30 天 +0.04）、双路召回 bonus
- `rag/knowledge_base.py:157-216` — 模型重排：DashScope 兼容 `/reranks` 调 `qwen3-rerank`
- `rag/knowledge_base.py:280-288` — **任何异常自动降级到启发式重排器**，并在 trace 里记降级原因
- `rag/knowledge_base.py:302-313` — score threshold 在**重排和父扩展之后**才应用
- `rag/knowledge_base.py:315-322` — token 预算压缩：`min(1800, max(520, top_k*260))`
- 只重排 top `rag_rerank_top_n=24`，尾部原样追加

> 你 D3 答对了核心直觉（"rerank 可以和问题一起做匹配"）。补上术语：
> **检索用 bi-encoder**（query 和 doc 分别编码成向量，可预计算、可建索引、快但粗）；
> **重排用 cross-encoder**（query 和 doc **拼在一起**过模型，能捕捉交互、准但慢，无法预计算）。
> 所以工业界标准做法就是"粗筛召回 top-N → 精排 top-K"。本项目 N=24，K=top_k。

### 1.5 生成与答案门禁（45 min）

- `rag/answer_gate.py`（131 行，全读）★防幻觉第一道防线★
  - `:75-122` `evaluate_answer_gate` → 四种结论：`answer` / `insufficient_evidence` / `conflict` / `ask_clarification`
  - `:32-43` `_strong_query_signals`：query 里的强标识符（`foo_bar`、`#123`、`*Error`）**必须出现在证据文本里**，否则拒答
  - `:45-64` `_detect_conflict`：高分（≥0.6）证据之间做**极性冲突检测**（权限/结果/需求类词对）→ `conflict`
  - `:67-73` `_is_ambiguous_query`：出现"这个/它"这类指代词且命中多来源 → `ask_clarification`
- `rag/qa.py:19-26` `RAG_SYSTEM_PROMPT`：强制每个事实后标 `[1][2]`；把 `<knowledge>` 明确定性为**不可信数据**（prompt injection 防护）；资料不足必须说"资料不足"，禁止用模型记忆；禁止编造引用
- `rag/qa.py:44` `build_sources` — 分配连续引用编号
- `rag/qa.py:79` `_context_message` — 每条证据渲染成 `[资料 N] + 标题 + 类型 + 片段`，包在 `<knowledge>` 标签里
- `rag/qa.py:107` `extractive_answer` — **无 LLM 时的抽取式兜底**：句子切分 + 中文 bigram/trigram 词重叠打分 + 取 top-4（≥最高分 60%），每条带引用
- `rag/qa.py:184-195` — 门禁拒答时**直接短路，不调 LLM**，`generation_mode="no_evidence"`
- `rag/qa.py:216-285` `reindex_documents` — 重建索引五步（重跑 GitHub 文档索引 → 清非 RAG 残留 → 剥离过期 embedding 元数据 → 批量重嵌 → **严格删 Milvus 向量后重传**）

**五道防幻觉防线**（背下来，这是你 D4 的答案）：
```
1. source_policy  —— 从源头限定只索引可信来源，源码硬禁止
2. answer_gate    —— 生成前规则检查：证据不足/冲突/指代不清 → 不进 LLM
3. score_threshold—— 重排后按分数阈值过滤
4. RAG_SYSTEM_PROMPT —— 强制引用编号 + knowledge 定性为不可信 + 禁止用模型记忆
5. extractive_answer —— 无 LLM 时退回抽取式，只说证据里有的句子
```

---

## 2. 两个必须能讲清的架构问题

### Milvus 和 PostgreSQL 各存什么？
- **PostgreSQL `Document` 表**：chunk **全文**（权威副本）+ 元数据 + BM25 检索源
- **Milvus**：向量 + 一份**截断到 8192 字符**的 content 副本
- 为什么不能只用一个：只用 PG → 没有高效 ANN 向量检索；只用 Milvus → 没有精确字段查询、没有全文、且 Milvus 不适合当权威存储
- **两者没有事务耦合**，会漂移（embedding 配置变了 / Milvus 部分写失败 / policy 变了）→ 所以需要 `reindex` 端点做全量重建，且删向量是 **strict**（Milvus 挂了就让 reindex 大声失败，而不是留下孤儿向量）

### 降级路径（graceful degradation）
| 挂了什么 | 行为 | trace 标记 |
|---|---|---|
| Milvus 不可用（hybrid 模式） | 吞掉异常，只走关键词分支 | `vector_status="unavailable"` |
| Milvus 不可用（纯 vector 模式） | 重新抛出，不假装成功 | — |
| qwen3-rerank 失败 | 自动降级启发式重排 | 记录降级原因 |
| LLM 未配置 | 抽取式回答 | `generation_mode` |
| 无证据 / 门禁拒答 | 短路，不调 LLM | `generation_mode="no_evidence"` |

> 这个表本身就是很好的面试素材：**"每一层都有明确的降级行为，并且降级状态在 trace 里可见，不会把'降级成功'伪装成'完全成功'"**。README 里也强调了 `start_rag.ps1` 的降级模式会明确显示 `degraded`，不能用来证明完整 RAG 跑通。

---

## 3. 实操（45 min）

1. 启动完整版（需要 Milvus）：`.\scripts\start_full.ps1`
2. 前端 `/rag` 页面 → 建知识库 → 上传一个 Markdown 文档（可以用 `docs/examples/rag_employee_handbook.md`）
3. 观察文档状态机走完 `queued→parsing→splitting→embedding→completed`
4. 用**召回测试**功能查一个问题，展开看 `retrieval` trace：两路各自的原始分数、归一化后分数、融合分数、重排前后排名变化
5. 试一个证据不足的问题，看 answer_gate 是否拒答
6. 故意把 `.env` 里的 embedding 维度改错，看四层守卫在哪一层拦住你

---

## 4. 产出物

1. **双链路图**：ingestion（上传→解析→切片→嵌入→双写）+ retrieval（query→双路召回→归一化→加权融合→去重→重排→阈值→父扩展→预算压缩→门禁→生成→引用）
2. **五道防幻觉防线清单**（默写）
3. **D1-D6 口述稿**

---

## 5. 自测题

- **D1** 上传到可检索经过哪几步？为什么不能按固定字数切？本项目的解法是什么？
- **D2** "混合"指什么？两路分数怎么融合？**为什么不用 RRF？**
- **D3** 为什么检索完还要 rerank？bi-encoder 和 cross-encoder 的区别？
- **D4** 防幻觉有哪五道防线？
- **D5** 换 embedding 模型的风险是什么？项目在哪四层做了守卫？
- **D6** 你怎么证明 RAG 好用？（Day 5 会深入，这里先答思路）
- **新1** 什么是 small-to-big 检索？本项目靠什么实现（提示：`_stable_id`）？为什么需要版本守卫？
- **新2** BM25 每次查询在 Python 进程内全量扫描该 repo 所有文档，没有倒排索引。这个设计的代价是什么？你怎么改？
- **新3** CI 日志进 RAG 之前做了什么处理？为什么？（提示：`rag/indexing.py:18-45`，`sanitize_ci_log`）

---

## 6. 笔记区

### 五道防线（默写）

source type限定 RAG范围

score threshold 限定可信证据最低分

answer gate限定 证据不足、冲突、模糊问题

system prompt限定 证据不可信仅参考非命令 仅限根据证据回答 不靠模型记忆

llm未配置则走抽取式回答 兜底方案


### 为什么加权融合而非 RRF（口述稿）


### 我踩的坑 / 意外发现

### 还没搞懂的问题

假如有文档更新或者比如说 同步时 怎么自动更新sql和向量的 或者可能删掉重建 但有一个步骤的问题 

