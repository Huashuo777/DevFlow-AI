# Data Flow 数据流讲解

这份文档不重复罗列步骤（那些在代码里），只讲**数据流背后的判断依据**：一份数据进来时，系统是怎么决定它归哪一层、要不要进第二层、以及什么时候算"进入系统完成"。

---

## 1. 先建立三个判断，而不是记四条链路

DevFlow AI 的数据流看起来有四条，本质上只有三个反复出现的判断：

| 判断 | 问的问题 | 决定的事 |
|---|---|---|
| **① 这份数据有 schema 吗** | 能不能拆成字段去过滤、聚合、join | 进 Postgres 关系表，还是当文本处理 |
| **② 这份数据需要语义召回吗** | 用户会不会用"意思相近"而不是"字段相等"来找它 | 要不要额外写一份向量到 Milvus |
| **③ 这份数据会过时吗，过时了代价多大** | 拿旧数据回答会不会误导人 | 能不能缓存，还是每次现场读权威源 |

**第 ③ 个判断是理解整个项目的钥匙。** 代码之所以被主动排除在向量库之外，Issue 之所以只进 Postgres 而默认不进 Milvus，长期记忆之所以要人工批准才生效，全都是这同一个判断在不同数据上的不同答案。

---

## 2. 三层存储的分工

系统有三个存储层，它们不是冗余备份，而是**三种不同性质的副本**：

```
磁盘 checkout   ──  当前版本的权威视图     不可缓存（缓存就等于撒谎）
Postgres        ──  事实的权威副本         可查询、可回溯、可审计
Milvus          ──  事实的语义投影         可重建、允许过时、允许丢
```

三层的关系是**单向派生**：磁盘和外部 API 是源头，Postgres 是权威副本，Milvus 是从 Postgres 派生出来的可丢弃投影。

由此推出两条在代码里能反复验证的规律：

1. **Milvus 里的任何东西都可以删掉重建**，因为 `documents` 表保留了完整原文。所以 `POST /api/rag/{repo_id}/reindex` 敢于"删了重来"，`main.py` 启动时也敢直接 `delete_milvus_documents(["code_file"])`。
2. **Postgres 里的东西不能随便删**，因为它是唯一真相源。删仓库时的级联清理（`cascade="all, delete-orphan"`）之所以写得很谨慎，就是这个原因。

一个实操推论：**向量检索结果永远只是"候选证据"**。`README` 里那句"RAG 返回的是候选证据，涉及实时状态和当前代码时仍要回到权威数据源核验"不是客套话，是这套存储分层的直接结论。

---

## 3. 一份数据进来时，系统做的三个决策

把任意一条链路抽象出来，都是这三步：

```
外部数据 ──① 边界校验──> 内部对象 ──② 落权威层──> Postgres/磁盘 ──③ 选择性投影──> Milvus
              (只在这里做)              (必做，事务内)              (可选，可延迟，可人工闸门)
```

**① 边界校验只做一次，只在入口做。** 文件上传的后缀白名单、12MB 上限、SHA-256 查重，webhook 的 HMAC 签名校验，全部集中在路由层。进入内部之后各跳之间不再重复检查——这是"只在系统边界验证"的标准做法，也解释了为什么内部函数普遍不写防御性代码。

**② 落权威层是同步的、在事务里的。** `sync_repository` 一路攒到 `db.commit()` 才提交；文档上传则是先落一行 `status=queued` 的记录**再**返回 202【请求已接受处理，但处理尚未完成】，这样即使后台处理崩了，"这个文件来过"这个事实也不会丢。

这里有两个不同的存储职责：两者共同构成系统的权威数据层

- PostgreSQL：保存文件的元数据、状态、归属关系等。
- 文件系统：保存文件实际的二进制内容。

**③ 投影到 Milvus 永远是可延迟、可跳过、可加闸门的。** 这一层是四条链路差异最大的地方，也是下面要讲的。

---

## 4. 四条链路各自在回答什么问题

不要把它们记成四个流程图，记成**四个不同的设计答案**。

### 链路 A · GitHub 同步 —— 回答"外部事实怎么变成可查询的行"

它的核心矛盾是：**GitHub 的数据结构不由你决定，但你的查询需求由你决定。**

所以出现了三种不同的写入策略，对应三种不同的数据性质：

- Issue / PR / workflow run **状态会变但身份稳定** → 按自然键 `(repo_id, github_issue_id)` upsert【Update + Insert】。
- PR files / review comments **整体会随 diff 变化，没有稳定身份** → 先删后重插，不试图逐行匹配。
- `workflow_runs.jobs` **结构完全由 GitHub 决定，且只用于展示** → 直接塞 JSON 列，不拆表。

第四种情况是 CI 日志：**体积大且只有失败才有价值** → 只在 `conclusion == "failure"` 时才下载，并截断。这是整条链路里最典型的成本约束取舍。

> 值得注意的取舍代价：这里没有 `since` 游标，也没有数据库唯一约束，去重完全依赖应用层的"先查后写"。这在单进程下没问题，但四个触发入口（UI 连接、UI 同步、定时轮询、webhook）如果并发打同一个仓库，理论上存在竞态。

### 链路 B · 文档上传 —— 回答"非结构化知识怎么变成可检索且可追溯的证据"

它是唯一一条走完整 ETL【Extract（抽取）Transform（转换）Load（加载）】的链路，也是唯一一条**把处理进度本身当成数据**的链路。

`KnowledgeSourceDocument.status` 的 `queued → parsing → splitting → embedding → completed / failed` 不只是日志，它是**可恢复的断点**：失败留 `error_message`，前端"重试"复用同一个 `document_id`，重处理前先 `delete_document_group` 清掉半成品，因此整个流程是幂等的。

另外两个值得学的判断：

- **用内容指纹（SHA-256）而不是文件名判重**。文件名不可靠，内容指纹让"改名重传"也能被识别。
- **切片不是定长硬切，而是结构感知**。Markdown/PDF 走标题栈 + 代码围栏，CSV 打包行并重复表头，JSON 按 key 拆。目的是让每个 chunk **自身可读**——一个脱离了上下文的片段如果被检索出来，用户应该还能看懂它在说什么。配套的 `section_path` / `page` / `start_line` 元数据，就是为了让答案能给出可点击的引用编号。

### 链路 C · 源码 —— 回答"什么样的数据绝对不能缓存"

这条链路的重点不是它做了什么，而是它**刻意不做什么**。

源码被 clone 到磁盘、被正则扫出符号关系写进 `code_symbols` / `code_relations`，但**从不进向量库**——`main.py` 启动时主动删除 `code_file` 向量，`purge_legacy_code_documents()` 清理历史行。

理由用第 ③ 个判断就能推出来：代码的向量索引一旦滞后于 checkout，Agent 就会基于**已经不存在的代码**给出建议，而且这种错误看起来完全合理、极难发现。相比之下，Issue 状态滞后几小时通常只是"信息旧一点"。

所以源码的检索路径是：`search_checkout_code` 用 ripgrep 在磁盘上**现场**搜。代码的"检索新鲜度"因此等于 checkout 的同步时间，而不是索引时间。

> 一句话总结这条链路：**能现场读的，就不要缓存。**

### 链路 D · 运行时回流 —— 回答"怎么让系统越用越懂这个团队，同时不失控"

这条链路是唯一会**产生新数据源**的：对话本身变成了下一轮对话的输入。

它的关键设计是一道**人工闸门**。会话封存会沉淀出 `memory_candidates`，但状态是 `pending`，不自动生效；只有批准后 `memory_hub` 才调用 `add_document` 把它写进 RAG。

这和链路 A 那边的 `action_drafts`（只生成草稿、绝不直接写 GitHub）是同一种哲学：**模型可以决策、可以建议，但凡是产生持久副作用的写入，必须经过人确认。**

配套的两个埋点也服务于同一个目标——`recall_events` 记录每次检索的 query/mode/命中数/引用，`agent_runs` 记录完整工具轨迹。有了它们，"答得不好"才能被归因：是检索没召回，还是模型没用好召回的内容。没有这一层，评测就只能给出一个分数，无法指导改进。

---

## 5. 跟着一段 CI 日志走完全程

这是理解数据流最有效的方式：**选一份具体数据，看它在每一跳上长成什么样。**

以一段失败的 CI 日志为例：

```
GitHub 上的 zip 归档
   │  ① 只有 conclusion == "failure" 才下载
   ▼
解压拼接成纯文本，截断到 80,000 字符
   │  ② 原样写入，未脱敏
   ▼
workflow_runs.logs_text  ← 权威副本，落库完成
   │
   │  ③ 此时语义检索【搜不到】它。必须有人调 /reindex
   ▼
sanitize_ci_log()  ← 到这一步才脱敏：私钥块、ghp_/gho_/ghu_/ghs_/ghr_ 令牌、
   │                  AKIA 开头的 AWS key、JWT、Bearer 头、::add-mask::、
   │                  以及 key=value 形式的通用密钥模式
   ▼
按 8,000 字符切片，最多 6 片（上限 48,000，超出记 truncated 标记）
   │
   ▼
embed_knowledge_texts() 按 64 条一批向量化
   │
   ▼
Milvus 里的 source_type = "workflow_run" 文档  ← 可被语义召回
   │
   ▼
Agent 引用时带回编号，用户可回溯到原始 run 的 html_url
```

从这段演变能读出三个结论：

1. **"落库完成"和"可被检索"是两个不同的时间点**，中间隔着一个需要人显式触发的动作。
2. **<u>脱敏发生在投影层</u>，不在权威层。** Postgres 里的 `logs_text` 是未脱敏的原文，只有进 Milvus 的那份被清洗过。这意味着数据库导出、备份、以及任何直接读 `logs_text` 的接口，都不受 `sanitize_ci_log` 的保护。
3. **越往后形态越"小"**：80k → 48k → 6 片 → 向量。每一跳都在做取舍，取舍的依据都是"下游用得上多少"。

---

## 6. 三个最容易被误解的地方

**误解一：同步完就能被 RAG 搜到。**
不能。链路 A 只写 Postgres。Issue / PR / CI 要进向量库，必须显式调 `POST /api/rag/{repo_id}/reindex`（内部走 `index_repository_documents`）。刚同步完却搜不到内容，就是这个原因，不是 bug。

**误解二：`documents` 表和 Milvus 是两份等价的数据。**
不等价。Postgres 的 `documents` 是真相源，Milvus 里的 `content` 被截断到 8192 字符、`metadata_json` 截断到 4096。所以 Milvus 只负责"找到候选"，**要展示完整内容必须回 Postgres 取**。

**误解三：代码不在 RAG 里，等于 Agent 看不到代码。**
看不到的是"语义相似检索"这条路，Agent 仍然可以通过 `workspace.search_code`（ripgrep 现场搜）和 `workspace.read_file` 读真实代码，也可以通过 `code_symbols` / `code_relations` 拿到调用关系。代码不是被排除在系统外，而是被排除在**缓存**外。

---

## 7. 阅读代码时发现的几处真实不一致

这些不是学习障碍，反而是很好的练习材料：

1. **README 与代码不符**：README 里的 `POST /api/rag/knowledge-bases` 这组接口在代码中不存在，真实前缀是 `/api/rag/repositories/{repo_id}/...`。以 `backend/app/api/routes/rag.py` 和 `/docs` 为准。
2. **`workflow_runs.logs_text` 可能陈旧**：`repos_sync.py:157` 只在 `conclusion == "failure"` 时写入，没有 else 分支清空。一个 run 从失败重跑变成功后，旧日志仍然留在库里。
3. **自然键缺唯一约束**：`github_issue_id` / `github_pr_id` / `github_run_id` 都是 `nullable=True` 且没有唯一索引，去重完全依赖应用层"先查后写"。
4. **`.data/connect_repo.py:8` 硬编码了一个真实的 GitHub fine-grained PAT 明文**，建议吊销并改为读环境变量。

---

## 8. 怎么验证自己真的理解了

读完这份文档之后，用三个动作自检，每个都能证伪：

1. **同步一个真实仓库，然后立刻用 `/api/rag/{repo_id}/search` 搜 Issue 标题里的词。** 搜不到 → 说明你理解了误解一。再调 `/reindex`，搜到了 → 说明你理解了投影层是延迟的。
2. **上传同一个文件两次（第二次改个名）。** 第二次被 SHA-256 拦下 → 说明你理解了内容指纹判重。
3. **上传一个文件后立刻刷新页面看 `status`。** 能看到 `queued/parsing/splitting/embedding` 中间态 → 说明你理解了状态机是断点而不只是日志。

三个都通过，这份数据流就算真的理清了。

---

## 相关文档

- `docs/architecture/system_design.md` —— 整体分层
- `docs/architecture/database_schema.md` —— 表清单
- `docs/architecture/security.md` —— 安全边界（与第 5 节的脱敏时机相关）
- `backend/app/db/models.py` —— 所有落点的 ORM 定义，是"谁往哪张表写"的最快索引
