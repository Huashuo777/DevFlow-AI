# Day 1 — 一条消息的一生 / Agent 推理循环

**目标**：能画出端到端时序图；能对着白板把 tool calling 机制讲 5 分钟不出错。
**权重**：★★★★★（Agent 岗最核心，也是你当前最大的洞）

---

## 0. 前置：FastAPI 最小认知（30 分钟）

只需要看懂 `chat.py` 用到的这几个概念，不用系统学：

| 概念 | 一句话 | 在本项目哪里 |
|---|---|---|
| 路由装饰器 | `@router.post("/chat/stream")` 把 URL 映射到函数 | `api/routes/chat.py:1791` |
| `Depends` | 依赖注入，FastAPI 自动帮你创建/关闭资源（如 DB session） | `db: Session = Depends(get_db)` |
| Pydantic Model | 请求体/响应体的类型校验，不合法直接 422 | `schemas/chat.py` |
| `StreamingResponse` | 返回一个异步生成器，边算边推给客户端（SSE 的基础） | `chat.py:1791` |
| `BackgroundTasks` | 先返回响应，任务在后台跑（RAG 文档处理用） | `api/routes/rag.py:241` |

**关键澄清**：FastAPI 只在"前端 ↔ 后端"这一层。它和 LLM、和工具执行**都没有关系**。

---

## 1. 精读清单

按顺序读，每段都有明确的"看什么"。总计约 600 行有效代码。

### 1.1 启动与配置（30 min）
- `app/main.py`（199 行，全读）— 看：注册了哪些 router、启动时做了什么（建表、ad-hoc ALTER、定时同步）
- `app/core/config.py`（93 行，全读）— 看：有哪些配置组，特别是 `CONTEXT_*` 那一批 token 预算旋钮

### 1.2 全流程骨架 ★最重要★（60 min）
- `app/api/routes/chat.py:1670-1769` — `_conversation_sse`，**这 100 行是整个项目的脊柱**

逐行对照这个顺序（背下来）：

```
1. _repo_or_404 / ensure_conversation      定位仓库和会话
2. ProgressiveContextManager.ensure_headroom  预压缩检查（还没调 LLM 就先腾空间）
3. MessageStore.append                      用户消息落库（durable transcript）
4. EvidenceStore.rebuild_repo               重建证据表（issue/PR/CI日志/文档/记忆）
5. ContextAssembler.assemble                组装 server_context + server_messages + evidence
6. _build_conversation_tools                构造 14 个工具闭包
7. ChatAgent().run_stream                   进推理循环
8. 收到 final 事件 → 助手消息落库 + AgentRun + SessionSealer.seal
9. encode_sse                               每个事件转成 SSE 帧推给前端
```

> **注意第 2 步在第 7 步之前**：压缩不是"超了才压"，是每次请求前先检查余量。这是个设计选择，面试可讲。

### 1.3 推理循环（90 min）
- `app/services/agents/chat_agent.py:38-59` — `GenericToolArgs`（一个 `extra="allow"` 的宽松 schema，所有工具共用）
- `:106-108` — 三个关键常量：
  ```
  max_steps = 12                  # 循环上限
  max_repeated_tool_calls = 2     # 同一工具+同一参数指纹的重复上限
  tool_retry_attempts = 1         # 瞬时错误重试次数
  ```
- `:394-414` — `_build_agent_graph`：LangGraph `StateGraph`，三个节点
  ```
  model_decision ──有tool_calls──> tool_action ──还有pending──> tool_action（自环）
        │                              │
        │无tool_calls                   │pending清空
        ↓                              ↓
      final                      model_decision
                                       │步数耗尽
                                       ↓
                                 max_steps_stop
  ```
- `:416-440` — `_stream_agent_graph`：`recursion_limit = max_steps*8+20`，用 `stream_mode="debug"` 拦截 LangGraph 内部事件转成自己的 SSE 事件
- `:449-500` — `model_decision` 节点：**每步只发 2 条消息（System + Human）**，看它怎么把观测结果塞进 Human
- `:502-570` — `tool_action` 节点：一次访问执行一个 tool call，然后自环
- `:610-626` — 循环守卫：`工具名+参数` 做指纹，重复次数 > 2 就阻断
- `:855-943` — `_invoke_tool_with_recovery` + `_classify_tool_error`：7 类 `ToolErrorKind`，只有 `timeout/rate_limited/transient_network` 才重试，且 **safety/report/workflow 三类工具永不重试**

### 1.4 工具注册（45 min）
- `app/api/routes/chat.py:935-1312` — `_build_conversation_tools`，14 个 async 闭包。**不用逐个细读**，看结构和注册表（`:1297-1312`）
- `app/services/agents/chat_agent.py:1280-1329` — `_build_langchain_tools`：闭包 → `StructuredTool` → `model.bind_tools`

### 1.5 观测结果投影 ★B4 的答案★（45 min）
- `chat_agent.py:1474-1582` — `_runtime_tool_context`：读它的 docstring（`:1480-1486`），明确写了这是模仿 Claude Code 的 durable transcript / active context 分离
- `chat_agent.py:1905` — `_summarize_observation`：单个观测怎么被裁剪
- `chat_agent.py:849` — **看这里建了 `ToolMessage` 然后没用**。这不是 bug，是 vestigial code，可以主动讲

---

## 2. 核心机制：tool calling 到底是什么

**你必须能不看稿子讲清这段。**

```
┌─────────┐   HTTP #1: messages + tools(JSON Schema)   ┌─────────┐
│  后端    │ ─────────────────────────────────────────> │   LLM   │
│ (你的    │                                            │  (远端)  │
│  Python) │ <───────────────────────────────────────── │         │
└─────────┘   HTTP #1 响应: tool_calls=[{name, args}]   └─────────┘
     │
     │  ← 这里没有任何网络！本进程直接 await 那个 Python 函数
     ↓
  workspace.read_file(path="x")  →  返回文件内容字符串
     │
     ↓
┌─────────┐   HTTP #2: messages（含观测结果投影）        ┌─────────┐
│  后端    │ ─────────────────────────────────────────> │   LLM   │
│         │ <───────────────────────────────────────── │         │
└─────────┘   HTTP #2 响应: content="最终回答"           └─────────┘
```

四个必须说对的点：

1. **工具定义随请求发给 LLM**，形式是 JSON Schema（名字/描述/参数类型）。LLM 靠"描述"决定用哪个工具——所以工具描述写得好不好直接影响 Agent 效果。
2. **LLM 不执行任何东西**。它只是生成了一段结构化文本表达"我想调这个"。执行永远在你的进程里。这就是"调用边界"。
3. **网络请求只有 后端 ↔ LLM 这一条线**。工具执行是本地函数调用。
4. **循环终止**靠四种机制：模型不再返回 tool_calls（正常结束）/ `max_steps=12`（预算耗尽）/ 重复指纹 > 2（死循环守卫）/ LangGraph `recursion_limit`（框架级兜底）。

### MCP 是什么关系（澄清你之前的混淆）

MCP（Model Context Protocol）是**工具提供方的一种标准化封装**，不是 tool calling 的替代。
本项目手写了 stdio MCP server（`app/mcp/memory_server.py`，JSON-RPC 2.0，没用官方 SDK），暴露 3 个记忆工具：
`devflow_search_evidence` / `devflow_get_thread_context` / `devflow_read_session_events`。
后端用 `services/mcp_client.py` 把它当**子进程**拉起来通信。

**14 个工具里只有这 3 个走 MCP，其余 11 个是纯本地闭包。**
对 LLM 来说这 14 个长得一模一样——它不知道也不需要知道背后是本地函数还是子进程。
面试可讲的点：*为什么记忆工具要单独走 MCP？* → 解耦、可被其他 MCP 客户端复用、进程隔离（记忆服务崩了不拖垮主进程）。

---

## 3. 实操（60 min，别跳过）

理解只有看到真实数据才会"通"。

1. 启动：`.\scripts\start_full.ps1`（完整版）或 `.\scripts\start_rag.ps1`（降级版，无需 Milvus）
2. 前端发一条会触发工具调用的消息，例如"看一下这个仓库的 README 讲了什么"
3. **打开浏览器 DevTools → Network → 找到 `chat/stream` 请求 → 看 EventStream 标签**，你会看到真实的事件序列：
   `thought` → `tool_use` → `tool_result` → `observation` → `delta` → `final`
4. 在 `chat_agent.py` 的 `model_decision` 节点里临时加一行 print，打出模型返回的原始 `tool_calls`。**亲眼看一次那段 JSON，B1 的误解就彻底消失了。**
5. 故意问一个需要多步的问题，数一下 SSE 里 `tool_use` 出现了几次，对照 `max_steps=12`。

---

## 4. 产出物

1. **手绘端到端时序图**：前端 → route → ensure_headroom → EvidenceStore → ContextAssembler → LangGraph 循环 → 工具 → SSE → 落库。画完拍照存进 `docs/course/`。
2. **tool calling 口述稿**（≤300 字，写在下面笔记区）：必须包含"JSON Schema / LLM 只生成文本 / 本地执行 / 四种终止条件"。
3. **14 个工具的分组清单**（凭记忆写，再对答案）。

---

## 5. 自测题（全部答到 3 分才算过关）

- **B1** tool calling 完整机制？（4 个点）
- **B2** 循环靠什么终止？死循环怎么防？
- **B3** 工具报错怎么处理？所有工具同一套重试策略吗？为什么 safety 类不重试？
- **B4** "每步重建 prompt" vs "累积 message 历史"，本项目选哪个、代价是什么、怎么补救？
- **新1** `GenericToolArgs` 用了一个 `extra="allow"` 的宽松 schema 给所有工具共用。这样做的好处和坏处分别是什么？
  
  > 提示：好处是注册简单、模型自由度高；坏处是**参数校验被削弱**，非法参数要等到函数执行时才炸。这是个可以主动承认的取舍。
- **新2** 为什么最终回答是"伪流式"（LLM 用非流式 `ainvoke`，然后把答案切成 36 字符的 delta 推出去，见 `chat_agent.py:2052`）？工具进度却是真流式？
- **新3** 如果 `max_steps=12` 用完了模型还没给出答案，返回给用户的是什么？（看 `max_steps_stop` 节点）

---

## 6. 笔记区

> 用自己的话写。写不出来说明没懂，回去重读。

​	我似乎可以看懂关于main文件的脉络，首先是启动时向app注册了各router所定义的接口，其次是进行了postgresql库表的初始化创建和补充统一，并且创建也只会创建不存在的表，而已存在的表不会自动按新的字段定义进行更新删除增加，为了让历史数据库逐步兼容当前代码定义的新结构，故手动进行数据库表的迁移，其中额外对documents表的嵌入embedding列做了删除，在获取milvus状态后接着对milvus库的代码文件做了删除，符合设计思路，代码文件统一从工作区实时获取；最后配置了仓库同步定时任务；关闭app时则取消后台同步任务，即整个组装完成。

### tool calling 口述稿

### 我踩的坑 / 意外发现

1. 启动时的 `ALTER TABLE` 则是为了让过去已经落地的旧结构，逐步适应现在演进后的业务抽象。数据模型的演进过程中持续维护！
1. Pydantic 主要用于数据及配置的定义、解析、检验、转换
1. 一个step并非一次工具调用，而是一次模型调用

### 还没搞懂的问题

gpt说向量存储完全交给了milvus，所以删了document的embedding字段，有待细看

fastapi所处位置

