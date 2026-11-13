# Day 4 — 多 Agent 编排 / Skill Runtime / 安全写回

**目标**：讲清 DAG 调度与失败语义；讲清"为什么编排层不用 LLM"；补上 E5（Skill Runtime）的空白。
**权重**：★★★★★（这是本项目的差异化卖点，也是 Agent 岗最爱问的"你怎么设计多 Agent"）

---

## 0. 一句话抓住全局

> **LLM 只出现在叶子 Agent 里；Planner、Observer、Synthesis 全是确定性规则代码。**

这是本项目最重要的架构决策，也是你 E2/E3/E4 所有答案的总纲。
好处：编排层**可单测、可复现、零额外 token 成本、行为可预测**。
代价：规则是硬编码的，覆盖不到的场景就规划不出来（`planner_agent.py` 里有大量关键词匹配，中英文双语，比较脆）。

---

## 1. 精读清单

### 1.1 数据契约（20 min，先读这个）

`app/schemas/workflow.py`（87 行，**全读**）：

```python
WorkflowSpec:      workflow_id / goal / trigger_message / acceptance_criteria
                   tasks / max_parallel_tasks(默认3, 夹在1-8)
                   requires_human_confirmation / created_at

WorkflowTask:      id / agent_name / task_type / objective
                   dependencies: list[str]     # ← 就是任务 id 列表，天然构成 DAG
                   claim: WorkflowClaim
                   input_hint / critical: bool

WorkflowClaim:     target_type / target_id / allowed_sources / write_intent / reason
```

★**`WorkflowClaim` 是最值得讲的设计**：每个任务带一份"作用域契约"，声明它要处理哪个目标、允许读哪些来源、是否有写意图。
runner 执行时会**硬校验**目标是否匹配，不匹配直接失败，而不是静默降级去处理别的东西（`api/routes/chat.py:627-643`）。
→ 这解决了多 Agent 系统最阴险的 bug：**Agent 悄悄处理了错误的对象，还返回了一份看起来很合理的结果。**

### 1.2 Planner（40 min）

`services/agents/planner_agent.py`（342 行）：
- `:14-157` `plan()` — **无 LLM**。读上下文标志（`has_issue/has_pr/has_ci_run/requires_safety`）+ 关键词匹配（`:310-342` 的 `_mentions_issue/_pr/_ci/_merge/_history`、`_is_broad_engineering_question`）
- `:27-41` — `repo_health` **永远是第一个任务**
- `:51/69/88/107` — issue/pr/ci/rag 任务都声明 `dependencies=["repo_health"]`
- `:117-133` — 有写意图时加 safety 任务，带 `write_intent=True` + `critical=True`
- `:135-141` — 兜底：如果只规划出 repo_health，补一个 PR/CI/issue 任务
- `:146-153` — 固定的 acceptance_criteria
- `:159-236` `replan()` — 由 Observer 的 finding 类型驱动，追加 `safety_review` / `rag_search` / `ci_debug` 任务
- `:282` `_unique_task_id`、`:291-292` — replan 时只在 `repo_health` 成功的前提下才依赖它

### 1.3 Orchestrator（60 min）★核心★

`services/agents/workflow_orchestrator.py`（483 行）：

- `:154-219` `_run_task_graph` — **拓扑波次调度**：
  ```
  while 还有未完成任务:
      ready = [t for t in 剩余 if t.dependencies 全部在 completed 里]     # :194-198
      if not ready:  → 全部标 dependency_cycle_or_missing，退出           # :199-204
      batch = ready[:spec.max_parallel_tasks]                            # :206
      results = await asyncio.gather(*[run(t) for t in batch])           # :207-210  ← 真并行
      失败的进 blocked_dependencies                                       # :215
  ```
- `:176-190` + `:278-288` — ★**失败语义：skip-downstream，不重试、不中止**★
  失败/报错的任务进 `blocked_dependencies`；任何依赖被阻塞的任务直接标 `skipped`，`error_kind="dependency_failed"`，并列出是哪些依赖挂了
- `:240-254` — 每任务 `asyncio.wait_for` 超时 → `error_kind="timeout"`
- `:229-238` — 找不到对应 runner → `skipped`
- `:57-106` — **replan 循环**：每轮跑完 → Observer 检查 → 判断要不要 replan
- `:306-330` `_replan_reason` — 触发条件：`task_error / evidence_gap / low_confidence / missing_safety_review / owner_needs_review / team_profile_missing`，**或任何成功任务的 confidence < 0.45**
- `:297-304` `_max_replans` — **上限 2 轮**
- `:351-355` `_same_task_graph` — 任务图签名比对，**打破无效 replan 死循环**
- `:332-349` `_normalize_replanned_spec` — 钉住 `workflow_id` / `max_parallel_tasks`；`requires_human_confirmation` 做 **OR 合并（sticky，一旦为真不可回退）**
- `:117-137` — 最终 `WorkflowReport`：metrics + 去重引用（`:391-405`）+ 合成 tool_calls + `agent_events` 时间线（`:407-483`）

> **纠正你 E3 的答案**：不是"记录情况并重试"。是 **skip-downstream，零重试**。
> 重试只存在于**工具层**（`chat_agent.py:855`），而且 safety/report/workflow 三类永不自动重试。
> 这个分层要讲清：**工作流层不重试（因为任务有副作用、且失败往往是数据问题不是瞬时问题），工具层有条件重试（只针对瞬时错误）。**

> **纠正你 E4 的答案**：PR 说可以合、CI 说阻塞，**不是交给 Planner replan**，而是 Observer 直接产出一个 `cross_agent_conflict` 的 **blocker** finding（见下），Synthesis 据此硬编码输出"暂缓合并"。replan 处理的是"可修复的缺陷"（证据不足、置信度低、缺安全审查），不是"结论冲突"。

### 1.4 Observer（40 min）

`services/agents/observer_agent.py`（209 行，**全读**，无 LLM）。六类 finding，统一类型 `WorkflowFinding(finding_type, severity, message, task_ids, recommendation)`：

| 方法 | 位置 | 检查什么 | 产出 |
|---|---|---|---|
| `_status_findings` | `:40` | error / skipped | blocker `task_error` / warning `task_skipped` |
| `_evidence_findings` | `:65` | 成功但 `evidence_count<=0` 且无引用（排除 repo_health/safety） | warning `evidence_gap` |
| `_confidence_findings` | `:82` | 成功但 confidence < 0.5 | warning `low_confidence` |
| `_merge_gate_findings` | `:97` | CI `is_merge_blocking` / PR `blocking_issues` / ★**跨 Agent 冲突** | blocker `ci_merge_gate` / `pr_blocking_issues` / `cross_agent_conflict` |
| `_ownership_findings` | `:134` | 推荐负责人未分配或不在 team_members / 没配团队 | `owner_needs_review` / `team_profile_missing` |
| `_safety_findings` | `:170` | 要求人工确认但没有 safety 任务 / safety 输出 requires_confirmation | blocker `missing_safety_review` / `human_confirmation_required` |

- `:196` `_looks_like_merge_approval` — **带否定词识别**的判断（避免把"不建议合并"误判成"建议合并"）
- `:26-30` 总体 confidence = 成功任务 confidence 均值（默认 0.55）− 0.2/blocker − 0.05/warning，夹在 [0.1, 0.95]
- `:35` `human_review_required = spec.requires_human_confirmation OR 存在任何 blocker`

> ★**跨 Agent 冲突检测是一等公民**★ —— PR Agent 说"建议合入"、CI Agent 说"阻塞"，这种矛盾单 Agent 系统根本发现不了。这是"为什么需要多 Agent"的最有力论据，面试重点讲。

### 1.5 Synthesis（20 min）

`services/agents/synthesis_agent.py`（120 行，**全读**，无 LLM，模板拼装）：
- `:21-56` `_build_answer` — Markdown 决策备忘录：目标+总体决策+Observer 置信度 → 任务图（含依赖）→ 各 Agent 状态与摘要 → Observer findings → 决策依据（每 Agent 摘要+证据数+置信度，≤8 条，`:106-117`）→ 下一步（≤6 条去重，`:77-104`）
- `:58-75` `_decision` — ★**硬编码门禁优先级**：
  ```
  CI/冲突 blocker        → "暂缓合并"
  人工确认 blocker        → "只生成草稿"
  否则                   → 听 PR Agent 的 merge_recommendation
  ```

### 1.6 专用 Agent 与结构化输出（45 min）

读 `services/agents/pr_review_agent.py`（249 行）作为样板，理解**所有专用 Agent 的统一模式**：

```
单次结构化 LLM 调用（llm.chat_json）
  → Pydantic model_validate
  → 失败则走启发式兜底
  → 成功也要过领域守卫，与兜底结果 merge
```

- `pr_review_agent.py:18-27` — 输入：PR + 分块 diff（`chunk_pr_files`）+ 评论 + CI 摘要
- `schemas/analysis.py:50-64` `PRReviewOutput` — `merge_recommendation` 是 **Literal**（建议合入/修改后合入/暂缓/拒绝）；`review_findings` 带 P1/P2/P3 和 `blocking` 标志
- `pr_review_agent.py:138-140` ★**领域守卫**：有 blocking issue 时，把"建议合入"强制降级为"修改后合入"

其余专用 Agent 快速过一遍（各 15 min）：

| Agent | 文件 | 用 LLM? | 输出 schema | 关键点 |
|---|---|---|---|---|
| IssueAgent | `issue_agent.py:18-27` | ✅ | `analysis.py:25-39` | `conclusion` 是 Literal；P0-P3 优先级；S-XL 复杂度；`:73-97` **否决 LLM 推荐的、不在 assignees/team 里的负责人**；`:465-475` 启发式 confidence 0.35-0.86 |
| CIDebugAgent | `ci_debug_agent.py:18-26` | ✅ | `analysis.py:67-80` | 7 种 `failure_type` Literal；`:118-128` 提取 first_error；`:185-188` `is_merge_blocking` |
| RecommendationAgent | `recommendation_agent.py:15-48` | ❌ 纯启发式 | — | 从 issues/PRs/CI 排序出 4-5 条聊天提示建议 |
| SafetyAgent | `safety_agent.py:12-40` | ❌ 纯关键词分类 | `SafetyOutput` | `requires_confirmation / risk_level / reason / draft` |
| ReportAgent | `report_agent.py` | ✅ | — | 周报，生成后**还会索引进向量库** |

> ★**"LLM 输出永远要过领域守卫"是 Agent 工程的核心纪律**★。
> 面试金句：*"我不会让 LLM 的输出直接成为系统行为。所有结构化输出都先过 Pydantic 校验，再和业务规则做 merge——比如 LLM 说'建议合入'但存在 blocking finding，代码会强制降级。这样即使模型出错，系统行为仍然可控。"*

### 1.7 Skill Runtime（60 min）★补你的 E5 空白★

**它解决什么问题**：把所有领域的指令全塞进 system prompt → ① token 爆炸 ② 指令互相干扰 ③ 无法审计"到底用了哪套指令" ④ 无法限制工具权限。
Skill Runtime 的做法：**按需加载**。只在当前任务需要时，把对应 `SKILL.md` 的完整指令和受限资源注入，并记录版本与激活原因。

- `services/skills/registry.py:282-288` — 发现：构造时 glob `backend/app/skills/*/SKILL.md`；`:377-380` 缓存单例
- `:290-299` — 强制 `name` 和 `entrypoint` 全局唯一
- `:309-374` — ★**手写 frontmatter 解析器，没用 YAML 库**（零依赖）
- `:15-27` `SkillManifest` — `name / title / version / description / category / entrypoint / tools[] / input_modes[] / triggers[] / workflow_steps[] / output_contract / safety_level`
- `:142-166` `activate` — **显式优先**：传了 `requested_name`，或消息里含 "skill/技能"+名称，或匹配 `use X skill` 正则（`:192-217`；显式指名但不存在 → 抛 `SkillRegistryError`）
- `:230-243` — 否则自动匹配：trigger 打分，ASCII trigger 要求词边界匹配，`score = min(len(trigger), 8)`，最多激活 `max_auto_skills=3` 个
- `:168` `activate_entrypoint` — 把 skill 绑定到专用 Agent 路由（`chat.py:649-672`）
- `:188` — `instruction_digest = sha256(instructions)[:16]`
- `:245-280` — 引用资源（`` `references/...` ``）**懒加载**，带路径逃逸防护（`resolve().relative_to`，`:262-266`），≤8 个引用、≤64 KiB，各自 sha256 摘要
- `chat_agent.py:1752-1769` — ★**工具白名单强制**：仅对**显式模式**的 skill 生效，调用未声明的工具在执行时被拒（返回错误字符串）
- `chat_agent.py:1771-1846` `_record_skill_validation` — 事后审计，每个激活的 skill 发一条 `skill_validation` 事件：
  `status`(completed/failed/not_executed/violated) / `entrypoint_called` / `entrypoint_succeeded` / `observed_tools` / `delegated_task_ids` / `tool_contract_violations` / `step_verification` 枚举

6 个 skill：`ci-debug` / `issue-triage` / `pr-review`（带 references 子目录）/ `project-investigation` / `safety-draft`（`safety_level: guarded_write_draft`）/ `weekly-report`（其余都是 `read_only`）。

> ★**Skill Runtime 的精髓是闭环审计**★：不只是"注入了哪段 prompt"，还要验证"entrypoint 到底跑没跑成"。
> `step_verification` 那个枚举（`entrypoint_completed` / `delegated_entrypoint_completed` / `supporting_tools_observed` / `skill_tool_failed` / `no_skill_tool_observed`）就是在回答这个问题。
> 面试金句：*"很多 Agent 系统只记录'我给了模型什么指令'，但不验证'模型有没有照做'。我们把指令版本、sha256 摘要、激活原因、实际观测到的工具调用和 entrypoint 执行结果都写进 trace，形成闭环。"*

### 1.8 安全写回（30 min）★补你的 E6★

**两层门禁**：

1. **Chat 路由层** `api/routes/chat.py:1229-1284` `check_safety`
   - `_risky_action(message)` 检测写意图 → SafetyAgent 分类风险
   - **代码绝不执行写操作**：只创建 `ActionDraft` 行（`models.py:514-533`，默认 `pending_confirmation`，`confirmed_by`/`executed_at` 为 null）+ 写 `AuditLog`
2. **独立确认端点** `api/routes/action_drafts.py:158-197` `confirm_action_draft`
   - 需要 `draft:approve` 权限
   - 拒绝任何非 `pending_confirmation` 状态的草稿（防重放）
   - 执行类型：comment / create_issue / close_issue / label；`send_report` 是 no-op stub（`:35-62`）
   - 记录 executed/failed 状态、确认人、结果、审计条目
   - cancel 只在 pending 时允许
3. **工作流层强制**：Observer 把"缺安全审查"或"安全审查未确认"变成 **blocker**，Synthesis 的门禁优先级强制输出"只生成草稿"

> 你 E6 的直觉完全对（"有持续副作用，需要人把关，方便追溯"）。补上实现：**它不是一个约定，而是数据库状态机 + 独立权限端点 + Observer blocker 三重强制。** Agent 代码里根本没有执行写操作的路径。

### 1.9 持久化（20 min）

`db/models.py:472-502`，写入点 `chat.py:889-935`：
- `AgentWorkflowRun` — repo/conversation FK、goal、`spec_json`（完整 spec）、`observation_json`、final_answer、metrics、status（任何任务 error 则整体 error，`chat.py:909`）
- `AgentTaskRun` — task_id、agent_name、task_type、`claim_json`（作用域契约）、`input_json`（objective+dependencies）、`result_json`（完整 `WorkflowTaskResult`，**含 `output._skill_runtime`**）、status、error
- skill trace 注入：`chat.py:674-676` `with_skill_runtime` 给任务输出包一层 `_skill_runtime = {skill_name/title/version/steps, activation_mode, activation_reason, instruction_digest, entrypoint, output_contract, safety_level, instructions_loaded, validation}`；orchestrator 在 `:450-468` 原样复制进 `workflow_task_result` 事件

---

## 2. 实操（45 min）

1. 前端发一个跨领域问题触发工作流，例如"这个 PR 现在能合吗？CI 挂了，帮我全面评估一下"
2. 看 SSE 里的 `agent_events` 时间线
3. 查 `AgentWorkflowRun` 表，把 `spec_json` 拿出来看任务图；查 `AgentTaskRun` 看每个任务的 claim 和 result
4. 在 `result_json` 里找 `_skill_runtime`，看 instruction_digest 和 activation_reason
5. 故意让一个任务失败（比如给个不存在的 PR 号），验证下游是不是 `skipped` + `dependency_failed`
6. 试一次写操作意图（"帮我在这个 issue 下评论 xxx"），确认只生成了 `ActionDraft`，然后走 `/action-drafts` 页面确认

---

## 3. 产出物

1. **工作流时序图**：ChatAgent → `workflow.run_engineering_review` → Planner → 波次并行 → Observer → (replan ≤2) → Synthesis → 双表落库
2. **"确定性编排 vs LLM 编排"论证**（本项目的核心取舍）
3. **Skill Runtime 价值陈述**（≤200 字）
4. **E1-E6 口述稿**

---

## 4. 自测题

- **E1** 什么时候单 Agent、什么时候多 Agent 工作流？
- **E2** 四个角色各自职责？为什么必须有 Observer？
- **E3** 有依赖怎么调度？能真并行吗？任务失败下游怎么办？（**skip-downstream，不重试**）
- **E4** PR 说可合、CI 说阻塞，怎么处理？（**Observer 产 `cross_agent_conflict` blocker，Synthesis 硬编码"暂缓合并"**）
- **E5** Skill Runtime 解决什么问题？和全塞 system prompt 的区别？工具白名单怎么强制？怎么审计"照没照做"？
- **E6** 为什么只生成草稿？三重强制分别在哪？
- **新1** `WorkflowClaim` 解决什么问题？没有它会出什么阴险 bug？
- **新2** replan 为什么上限 2 轮？靠什么防止无效 replan 死循环？
- **新3** `requires_human_confirmation` 为什么是 OR 合并（sticky）而不是覆盖？
- **新4** 为什么 Planner 用关键词匹配而不是让 LLM 规划？这个选择的代价是什么？
- **新5** Observer 算总体 confidence 的公式是什么？为什么要夹在 [0.1, 0.95]？

---

## 5. 笔记区

### 确定性编排 vs LLM 编排（论证）


### Skill Runtime 价值陈述


### 我踩的坑 / 意外发现


### 还没搞懂的问题

