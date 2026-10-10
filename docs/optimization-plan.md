# XHS Content Agent 优化提升计划（Optimization Plan）

> 状态：持续更新 ｜ 依据：`README.md`「已知局限与演进路线」+ 实时联网调研（GitHub / Reddit / Anthropic / 主流框架文档）。
> 目标：把本项目从「单次目标触发的 Agent 编排骨架」提升为**能长久持续运行、有充分自主性**的合格 Agent 产品。

---

## 0. 结论速览

本项目 Agent 架构已具备 Planner / Executor / Reflector / ToolRegistry / Memory / Budget / Tracing / 审批 等核心组件，方向正确。当前主要短板集中在 **「持续性」与「自主深度」**，可用一句话概括：**引擎在，但缺"连续运转的调度、会自我生长的记忆、可证明质量的评估、防越界的护栏、可恢复的持久化"。**

五大缺陷与对应补齐方向（详见各节）：

| # | 缺陷 | 补齐方向 | 关键参照 |
|---|------|----------|----------|
| 1 | 仅单次目标内自主，无持续运行 | 增加调度器 / 事件驱动 / 常驻循环 | LangGraph 长时运行、OpenAI Sandbox Agent、deer-flow |
| 2 | 长效记忆过薄，无向量检索 | 引入记忆层（结构化 + 向量检索） | mem0、Letta/MemGPT |
| 3 | 缺端到端评估（evals） | 建立评估体系（goal 完成度 + 质量指标） | promptfoo、trulens、harbor、future-agi |
| 4 | 缺护栏（guardrails） | 发布/对外副作用前加护栏校验 | guardrails-ai、Portkey Gateway |
| 5 | 无断点续跑、外部依赖脆弱 | checkpoint 持久化 + 弹性重试 | LangGraph persistence/checkpoint、Playwright storage_state |

---

## 0.1 落地进度（2026-08-11 更新）

按附录 C 修订后的优先级，本轮已完成：

- **P0 checkpoint + resume**：新增 `app/agent/checkpoint.py::StepCheckpointer`，`Executor` 每批步后原子落盘 `{plan, memory.steps, usage}`，`XHSAgent.run` 支持从检查点恢复（跳过 `SUCCEEDED` 步、回灌 usage、发 `resume_from_checkpoint` 事件、完成即清理）；`AgentRunManager.resume` + `POST /agent/runs/{id}/resume` 续跑未完成运行。
- **P1-a guardrails**：新增 `app/agent/guardrails.py`（输入目标 / 敏感副作用工具 / 输出合规护栏），在 `Executor` 与 `XHSAgent.run` 挂钩，拦截写入 `guardrail_blocked` trace 事件。
- **P1-e 配置漂移修复（10.2）**：`config.py` 增 `agent_max_steps`，`Budget.from_settings` 接通 `max_steps`，`orchestrator` 传入 `max_retries_per_step=settings.agent_max_retries`，`Executor` 以 `budget.max_steps` 作为总步数上限（修复 replan 计数语义）。
- **P1-c 记忆升级（12.1 阶一）**：`episodic` / `procedural` 三分桶、按 goal 归一化签名去重、`_profile_text` 按当前 goal 关键词召回 Top-K（并回灌旧 `lessons` 键保持兼容）。
- **P1-d housekeeping（10.1）**：新增 `app/agent/housekeeping.py::purge_old_runs`、`POST /agent/housekeeping`、`scripts/cleanup_runs.py`，`run_manager` 提供可选自动触发（默认关闭）。
- **10.4 #2 trace 增量落盘**：`AgentTracer.flush()` 在每批步后写 `partial` trace，崩溃不丢中间状态。

- **P1-b 端到端 evals**：新增 app/agent/evals.py 与 tests/test_agent_evals.py，聚合 goal_completion_rate / 质量通过率 / 步数 / 成本指标（离线 mock 可跑）。
- **P2-c 治理加固（10.3）**：管理端点（stop/resume/housekeeping）加可选 X-Admin-Token 鉴权并追加 data/agent/audit.log；新增 app/agent/secrets.py 扫描 + test_no_secret_in_trace；新增 tools/xiaohongshu-mcp/TRUST.md。
- **P2-a 调度器（§1）**：新增 app/agent/scheduler.py（无第三方依赖，支持间隔/每日定时，持久化到 data/agent/schedules.json），新增 POST/GET/DELETE /agent/schedule，并在 app/main.py lifespan 常驻轮询触发 run_manager.create（复用审批与 SSE）。
- **P2-b 登录态解耦（§5）**：crawl 响应增加结构化 needs_login 字段；新增纯函数 login_state_from_url() 在 _ensure_logged_in 检测登录重定向并快速失败（可离线单测）。
待后续：P1-b 接入 CI/调度（当前仓库无 CI 流水线）、向量检索（§12.1 阶二）、登录态自动刷新与外部依赖熔断。

**2026-08-12 · real-run-issues §12 落地（M0 质量修复 + M1 爬取长任务化，回归 73 项 pytest 绿）**：
- **12.9 reload 开关**：`run.py` 支持 `XHS_RELOAD=0` 关闭自动 reload（README「方式一」已提示长跑/实测设置）。
- **12.1 focus_keywords 注入**：`TopicsParams`/`ContentsParams` + `TopicGenerateRequest`/`ContentGenerateRequest` 新增 `focus_keywords` 并透传 topic/content service 与 prompt 模板；`Planner.plan/replan` 从 goal 抽取目标域关键词（新增 `app/agent/focus.py`）并确定性注入 `generate_topics`/`generate_contents` 步骤，replan 携带不丢失。
- **12.2 判定口径统一**：`RUBRIC` 增第 8 项「目标相关性」；`Evaluator.evaluate`/`QualityGate.run` 接收 `focus_keywords`；单篇 `_score`/`_verdict` 透传（输出前剔除）；`Reflector.judge` 注入本轮 `quality_report`。
- **12.3 产物去重择优**：`_collect_outputs` 支持 `all/last_round/best_n`，按标题去重 + 按 focus 相关性排序截断（config `agent_collect_strategy`/`agent_top_n`）。
- **12.4 振荡检测与定向纠偏**：`Executor` 连续 3 次相同 reason 触发 `oscillation_break` 并强制注入 focus；`Budget` 增 `max_replans`（config `agent_max_replans`）。
- **12.5 反思计数客观化 + 追加执行**：`AgentMemory.count_results`；`Reflector.judge` 注入客观 `progress`；计划跑完仍 continue 时 `Planner.append_steps` 追加 `generate_contents` 补足缺额。
- **12.6 爬取后台任务化**：新增 `app/services/crawl_job_manager.py`（CrawlJob + 落盘 `data/raw/crawl_jobs/`）；`POST/GET /local-crawl/jobs` + SSE `/local-crawl/jobs/{id}/events`；`crawl(on_progress=...)` 进度回调；`POST /search` 保留兼容并标注 deprecated。
- **12.7 互动数据 DOM 兜底**：`_dom_count` + 按钮选择器兜底；`NoteItem` 增 `likes/comments/favorites_data_missing` 标记，下游可降权而非静默填 0。
- **12.8 headless 可配置 + 登录/爬取分离**：config `crawl_headless`（默认 True，爬取无头）；登录仍走有头 `test_crawler.py` 写 state。

## 1. 缺陷一：自主性仅限单次目标内，无持续运行

### 现状
- `XHSAgent.run()` 由用户/API 单次触发；`Executor.MAX_TOTAL_STEPS=50` 硬上限。
- 没有定时/事件/常驻循环，Agent 无法自我发起新目标或跨时段持续作业。

### 联网调研要点
- **LangGraph** 定位即「构建长时、有状态 Agent」，支持 checkpoint 让长任务在多次执行间保持状态、可中断/恢复。
- **OpenAI Agents SDK** 提供 Sandbox Agent，预配置容器以执行**长时间跨度**工作。
- **bytedance/deer-flow**（~80k⭐）：长时程 SuperAgent harness（研究→编码→产出），强调**多轮次、跨步骤**的持续目标达成。
- **ruflo / ruflo**：多智能体 swarm 元编排，面向「协调自主工作流」。
- 业界共识（Anthropic）：Agent 常以**终止条件（最大迭代/预算）**保持可控，但在该约束内应能**连续、自主**推进。

### 落地建议（结合本项目）
1. **新增 `Scheduler` / `AgentLoop` 常驻组件**：在 `run_manager.py` 之上加一个轮询循环，支持：
   - `cron` 表达式 / 间隔触发（如每天分析竞品→生成→待审批发布）。
   - 事件驱动（新爬取数据到达→自动触发内容生成）。
   - 队列（`AgentRunManager` 已有 `ThreadPoolExecutor`，可扩展为持久化任务队列）。
2. **放宽单次目标限制但保留预算护栏**：`MAX_TOTAL_STEPS` 仍作为单轮上限，但允许「多轮目标」由调度器编排（每轮受 `budget.py` 约束）。
3. **新增 `/agent/schedule` API**：创建/列出/删除定时任务，复用现有 `run_manager` 的审批与 SSE 事件。
4. 参照：LangGraph persistence + OpenAI Sandbox Agent 的「长时运行」思路，用 checkpoint 支持跨轮状态延续（见缺陷五）。

---

## 2. 缺陷二：长效记忆过薄，无向量检索与知识抽取

### 现状
- `memory.py` 长期层仅存 `audience/tone` + `lessons` 元数据（最近 20 条）。
- 无向量库、无检索、无「从运行中抽取可复用知识」的机制；"学习"接近装饰性。

### 联网调研要点
- **mem0**（~63k⭐）：Universal memory layer，自动从对话/运行中抽取并去重记忆，支持向量检索 + 图/键值存储，API 与自托管皆可。
- **Letta / MemGPT**：让 Agent 拥有**自我编辑、随时间学习**的记忆，把长期记忆分层（核心/归档）并动态换入换出。
- **Reddit r/AI_Agents** 反复出现的关键洞见：
  - "How do you decide what an agent remembers on its own?" —— 记忆**取舍标准**才是难点，不是"存多少"。
  - 多篇帖子结论：**"save everything + RAG" 是错误默认**；关键是**结构化保留经验/教训**，简单 Markdown/结构化笔记在 2176 任务评测中胜过多数重记忆产品。
  - 方向共识：**轻量、结构化、可检索** 优于「无差别全存」。

### 落地建议（结合本项目）
1. **把 `lessons` 从纯元数据升级为「经验条目」**：每条含 goal、action、result、quality、tags，字段化存储，便于检索与去重。
2. **引入轻量向量检索（渐进式）**：
   - 短期：为 `profile.json` 增加 `experience` 列表，planner 注入前用关键词/tag 过滤（无需向量库）。
   - 中期：接入 `mem0` 或自建向量索引（如 Chroma/FAISS + 本地 embedding），让 planner 检索"类似目标的成功/失败经验"。
3. **明确记忆取舍标准**（响应 Reddit 洞见）：仅保存「影响后续决策」的信息（完成度、质量分、失败原因、用户偏好），并设上限与归档策略。
4. 保持现有 `_remember_lesson` 的闭环，但让 `_profile_text` 由「最近 N 条」升级为「语义检索 Top-K」。

---

## 3. 缺陷三：缺端到端评估（evals）

### 现状
- 仅 26 个单元测试覆盖组件逻辑；**无**对"Agent 是否达成目标 / 内容质量是否达标"的端到端度量。

### 联网调研要点
- **promptfoo**（~46k⭐）：LLM evals & red teaming CLI/库，可用 `assert` 对输出做自动评分（正确性、格式、相似度、LLM 评判）。
- **trulens**（3.5k⭐）、**harbor-framework/harbor**（4.1k⭐）、**future-agi**（1.6k⭐）、**lmnr(Laminar)**（3.2k⭐）：专为 Agent 评估/可观测设计的平台，支持"目标完成度"类指标。
- **awesome-evals**（benchflow-ai）：agent evals 最佳资源合集（论文/博客/工具）。
- 业界共识：Agent 质量不能只看单步，需**端到端 goal-completion 指标**（步骤成功率、最终产出可用性、成本/步数效率、人类偏好）。

### 落地建议（结合本项目）
1. **新增 `tests/test_agent_evals.py`（或独立 `evals/` 目录）**：
   - 定义「目标→期望产出」用例集，如"生成 3 篇针对 X 受众的种草文案"。
   - 用 LLM 评判（复用 `evaluator.py` 的 0-10 分）对**最终产出**打分，聚合为 `goal_completion_rate`。
2. **关键指标**：步骤成功率、平均步数、LLM 调用/token（已有 `usage`）、`quality_report` 通过率、目标完成率。
3. **把 evals 接入 CI / 调度**：每次改动跑回归，防止规划/工具链路退化。
4. 可选接入 promptfoo/trulens 做可视化，但**先用本项目自有的 trace + quality_report 落地闭环**（最小侵入）。

---

## 4. 缺陷四：缺输入输出护栏（guardrails）

### 现状
- 仅对 `publish_to_xhs` / `sync_to_feishu` 做**人类审批**；无内容合规/敏感词/风格护栏。
- LLM 自由规划，缺乏对「不当目标、越权工具、危险输出」的拦截。

### 联网调研要点
- **guardrails-ai/guardrails**（7.3k⭐）：用 validator 对 LLM 输入/输出做声明式校验（敏感内容、PII、格式、事实性）。
- **Portkey-AI/gateway**（12.7k⭐）：网关内置 50+ AI Guardrails，可统一在请求层拦截。
- **litellm / BAML / NeMo Guardrails** 等为护栏生态主流。
- 业界共识：Agent 越自主，**护栏越重要**——既要拦「不该做的」（越权工具、敏感发布），也要拦「不该出的」（违规内容）。

### 落地建议（结合本项目）
1. **新增 `app/agent/guardrails.py`**：
   - 输入护栏：目标/提示词敏感检测（拒绝发布类/违规类目标）。
   - 工具护栏：`publish_to_xhs`/`sync_to_feishu` 之外的**敏感副作用工具白名单**校验（现有审批机制保留）。
   - 输出护栏：生成文案做敏感词/广告法禁用词/小红书合规过滤（可先用规则表 + LLM 二次判定）。
2. **在 `executor.py` 调用工具前后挂 `guardrails` 钩子**，拦截结果写入 trace（`guardrail_blocked` 事件），并在 `quality_report` 中体现。
3. 参照 guardrails-ai 的 validator 声明式风格，保持可插拔。

---

## 5. 缺陷五：无断点续跑、外部依赖脆弱

### 现状
- `run_manager._load_runs()`：应用重启后未完成运行直接标 `failed`，**不保留中间状态、不可续跑**。
- 爬虫依赖 Playwright + 持久化 cookies（`storage_state`），发布依赖本地 MCP（18060），长稳运行脆弱。

### 联网调研要点
- **LangGraph persistence/checkpoint**：把图状态存到存储（内存/SQLite/Postgres），节点可中断恢复，支持人类介入后继续。
- **Playwright `storage_state`**：持久化登录态避免每次重登（本项目已在用）；配合**指数退避重试 + 登录态失效快速失败**提升稳健性。
- Agent 产品"长久运行"必须解决：**进程重启不丢状态、外部服务抖动可恢复、登录态失效可感知**。

### 落地建议（结合本项目）
1. **Checkpoint 持久化（高优先级）**：在 `run_manager._persist` 已写 JSON 的基础上，把**执行中的 plan/step 状态/已产出**随事件一并落盘；重启后对 `running` 态运行提供 `resume` 端点，从最后成功 step 继续（而非一律 failed）。
2. **登录态失效感知**：在 `local_site_crawler_service` 增加登录态校验（如检测跳转登录页/401），失效时**重试+明确报错**，并把状态同步给调度器暂停相关任务。
3. **外部依赖熔断/降级**：MCP 发布服务不可用时，队列化待重试或转飞书同步；爬虫失败用重试+退避。
4. 参照 LangGraph checkpoint 的「存储中间状态 + 恢复」模式，落地到 `data/agent/checkpoints/`。

---

## 6. 优先级路线图

| 阶段 | 事项 | 影响 | 工作量 | 对应缺陷 |
|------|------|------|--------|----------|
| P0 | Checkpoint 持久化 + `resume` 端点 | 高（解决"长久运行"根基） | 中 | 五 |
| P0 | 输入输出护栏 `guardrails.py` | 高（自主化安全前提） | 中 | 四 |
| P1 | 端到端 evals（目标完成率指标） | 高（可证明质量） | 中 | 三 |
| P1 | 记忆层升级（经验字段化 + 向量检索） | 中高（"越用越懂"落地） | 中高 | 二 |
| P2 | 调度器 `Scheduler` + `/agent/schedule` | 中高（持续自主运行） | 高 | 一 |
| P2 | 登录态失效感知 + 外部依赖熔断 | 中（长稳运行） | 中 | 五 |

### 落地顺序建议
1. **先做 P0（checkpoint + guardrails）**：无此两者，自主化越深越危险、越难恢复。
2. **再做 P1（evals + 记忆）**：有了评估才有"改进的依据"，有了记忆才有"自主深度"。
3. **最后 P2（调度 + 弹性）**：持续性建立在前面稳固的基础上。

---

## 7. 关键参考（实时调研来源）

- LangGraph（长时/有状态/checkpoint）：https://github.com/langchain-ai/langgraph
- OpenAI Agents SDK（Sandbox Agent / 长时任务 / Tracing / guardrails）：https://github.com/openai/openai-agents-python
- bytedance/deer-flow（长时程 SuperAgent）：https://github.com/bytedance/deer-flow
- mem0（Universal memory layer）：https://github.com/mem0ai/mem0
- Letta / MemGPT（自我编辑记忆）：https://github.com/letta-ai/letta
- promptfoo（LLM evals & red teaming）：https://github.com/promptfoo/promptfoo
- trulens（Agent 评估/追踪）：https://github.com/truera/trulens
- harbor（评估与改进 Agent）：https://github.com/harbor-framework/harbor
- guardrails-ai（护栏）：https://github.com/guardrails-ai/guardrails
- Portkey Gateway（50+ Guardrails）：https://github.com/Portkey-AI/gateway
- Reddit r/AI_Agents（记忆取舍/自主 Agent 讨论）：https://www.reddit.com/r/AI_Agents/
- Anthropic《Building Effective Agents》：https://www.anthropic.com/research/building-effective-agents

---

## 8. 验收标准（本计划是否落地成功的可度量条件）

- [x] 存在可运行的长时调度器（/agent/schedule，间隔/每日定时），Agent 可定时触发多轮运行（缺陷一）。
- [ ] 长期记忆：已完成结构化经验三分桶 + 按目标关键词召回（§12.1 阶一，planning prompt 已用召回 Top-K）；语义/向量检索（阶二）仍待补（缺陷二）。
- [x] 有端到端 evals，输出 goal_completion_rate 指标（tests/test_agent_evals.py）；接入 CI/调度仍待补（缺陷三）。
- [x] 发布/对外副作用前有护栏拦截，且拦截事件可追踪（guardrail_blocked trace 事件）（缺陷四，P1-a 已落地）。
- [x] 应用重启后可 resume 未完成运行（checkpoint + POST /agent/runs/{id}/resume，P0 已落地）；登录态失效已可感知（needs_login + 登录重定向检测，P2-b 已落地）；自动刷新/优雅降级仍待补。

---

## 9. 附录 A：客观性校准（独立代码核实，2026-08-11）

> 本附录由独立工程评审对照**实际代码**与 **GitHub API 实测**核实，不轻信文档自述。

### 9.1 五大缺陷全部成立（代码锚点核实）

| 缺陷 | 核实结果 | 代码锚点 |
|---|---|---|
| 一·持续性 | ✅ 成立 | `app/` 全仓 grep `scheduler/cron` 无命中；`executor.py:26` `MAX_TOTAL_STEPS=50` 硬上限 |
| 二·记忆退化 | ✅ 成立（且证据确凿） | `data/agent/profile.json` 实测 **20 条 lessons 仅 7 个唯一 goal**，每个重复 2-3 次（如「为大学生女生生成小红书文案」×3、「Create sample content」×3）；`orchestrator.py:130` `lessons[-20:]` 滑窗不去重、`:144` 只回灌最近 5 条 |
| 三·缺 evals | ✅ 成立 | `tests/` 仅 26 个组件单测，grep `evals` 无命中，无 `goal_completion_rate` |
| 四·缺护栏 | ✅ 成立 | grep `guardrail` 无命中；`executor.py:185-197` 仅 `publish_to_xhs`/`sync_to_feishu` 有人工审批；`evaluator.py` 只评 0-10 质量、不评合规 |
| 五·无断点续跑 | ✅ 成立 | `run_manager.py:300-304` 重启把未完成 run 标 failed；`tracing.py:47-58` 仅终态落盘；grep `checkpoint/resume` 无命中 |

### 9.2 参考星标数修正（GitHub API 实测 2026-08-11）

| 来源 | 文档引用 | 实测 | 是否准确 |
|---|---|---|---|
| bytedance/deer-flow | ~80k⭐ | 79,733 | ✅ |
| mem0ai/mem0 | ~63k⭐ | 63,022 | ✅ |
| guardrails-ai | 7.3k⭐ | 7,272 | ✅ |
| Portkey/gateway | 12.7k⭐ | 12,686 | ✅ |
| truera/trulens | 3.5k⭐ | 3,500 | ✅ |
| **promptfoo/promptfoo** | **~46k⭐** | **24,124** | ❌ **夸大近一倍，应改为 ~24k⭐** |
| langchain-ai/langgraph | （未标数） | 39,445 | — |

> 结论：缺陷判定客观成立，可放心据此规划；星标数仅 promptfoo 一处需修正。

---

## 10. 附录 B：本计划遗漏的缺陷（需并入路线图）

### 10.1 遗漏一：trace / checkpoint 无 TTL 清理（D7）

- **实情**：`data/agent/console_runs/` 实测 **59 个**文件、`data/agent/runs/` **122 个**，共 181 个只增不删（比 §0 概述的规模更严重）。
- **影响**：长久持续运行磁盘必爆，且 trace 文件越多越难定位。
- **落地**：新增 `app/agent/housekeeping.py`，`purge_old_runs(retention_days=30, keep_summary=True)`；暴露 `POST /agent/housekeeping`（需鉴权，见 10.3）+ `scripts/cleanup_runs.py`；挂 APScheduler 定时或在 `run_manager.create` 末尾概率触发。
- **验收**：注入 40 天前 run 文件后触发，hot 区只剩 30 天内全量 + 旧 run 的 summary。

### 10.2 遗漏二：配置漂移（D8，新发现，小修但重要）

- **实情**：`config.py:72` 定义 `agent_max_retries: int = 3`，README 也文档化了 `AGENT_MAX_RETRIES` env；但 `orchestrator.py:76-86` 构造 `Executor` 时**未传** `max_retries_per_step`，Executor 用 `executor.py:101` 的默认值 3。**用户改 env 无效，是「文档说可配、实际不可配」的隐性 bug。**
- 同时 `executor.py:26` `MAX_TOTAL_STEPS=50` 是模块级硬编码常量，无 `agent_max_steps` 设置项；而 `budget.py:17` 明明有 `max_steps` 字段，`Budget.from_settings`（`budget.py:23-28`）却**只读了 `agent_max_llm_calls`/`agent_max_est_tokens`，未接通 `max_steps`**--字段在、设置项不在、接通也不在。
- **落地（P1 小修，0.2 天）**：
  1. `orchestrator.py` 构造 `Executor` 时传 `max_retries_per_step=settings.agent_max_retries`；
  2. `config.py` 增 `agent_max_steps: int = 50`；
  3. `budget.py::from_settings` 读入 `max_steps=settings.agent_max_steps`；
  4. `executor.py` 用 `self.budget.max_steps or MAX_TOTAL_STEPS` 替换裸常量。
- **验收**：`AGENT_MAX_RETRIES=5` / `AGENT_MAX_STEPS=10` 实测生效（加单测）。

### 10.3 遗漏三：管理端点无鉴权（D9 一部分）

- **实情**：`/agent/runs/{id}/stop`、未来的 `/agent/housekeeping`、`/agent/runs/{id}/resume` 任何同机进程可调。本地自用可忽略，但一旦上 §1 的 scheduler 做长稳运行，无鉴权 = 任意进程能停/清/续你的 agent。
- **落地（P2，1 天）**：管理端点加 `X-Admin-Token` 头校验（token 从 env）；`OPENAI_API_KEY` 等只进 settings 不进 trace（加 `test_no_secret_in_trace` 扫 `data/agent/**/*.json`）；`tools/xiaohongshu-mcp/TRUST.md` 记录二进制来源/哈希/权限范围；housekeeping 与 stop 动作 append 写 `data/agent/audit.log`。

### 10.4 遗漏四：两个隐性风险（建议随 P1 一并处理）

1. **replan 不重置步数计数器**：`executor.py:134-137` replan 时重置 `retries` 但不重置 `total`，多次 replan 会累积计入 `MAX_TOTAL_STEPS=50`，可能让「每段都短但 replan 多」的 run 被过早停机。建议在 10.2 的 `agent_max_steps` 一并明确「50 是跨 replan 总预算」或改为每段独立计数。
2. **trace 仅终态落盘**：`tracing.py:47-58` 只在 `run` 结束 `save` 一次。中途崩溃 = trace 丢失，无法事后排查「跑到哪一步崩的」。这其实是缺陷五 checkpoint 的同一面--P0 checkpoint 落地后顺手在每步 `event` 后 flush 一次即可解决。

---

## 11. 附录 C：优先级修订建议

> 原路线图把「checkpoint + guardrails」并列 P0。建议**仅 checkpoint 占 P0，guardrails 下沉 P1**。

**理由**：当前 Agent 只能按需触发、且副作用已有 `approval_handler` 人工审批兜底，即时安全风险是有界的；真正卡住「长久持续运行」的第一刀是「崩溃不可恢复」（缺陷五），不是「无护栏」。护栏是 P2 scheduler 的**前置条件**，但不是 P0 checkpoint 的前置条件。把 guardrails 与 checkpoint 并列 P0 会让 P0 工作量翻倍、推迟可恢复性落地。

**修订后顺序**：

| 顺序 | 任务 | 对应 | 依赖 | 工作量 |
|---|---|---|---|---|
| **P0** | checkpoint + resume 端点 | 五 | 无 | 1-2 天 |
| P1-a | guardrails（输入/工具/输出护栏） | 四 | 无（P2 scheduler 前置） | 中 |
| P1-b | 端到端 evals | 三 | 无 | 中 |
| P1-c | 记忆去重 + 三分桶 + 关键词召回（见 12.1） | 二 | 无 | 0.5 天 |
| P1-d | housekeeping TTL | 10.1 | 与 P0 路径一致 | 0.5 天 |
| P1-e | 配置漂移修复 | 10.2 | 无 | 0.2 天 |
| P2-a | scheduler + `/agent/schedule` | 一 | P0 + P1-a | 高 |
| P2-b | 登录解耦 + crawl_notes 结构化 needs_login | 五 | 无 | 0.5 天 |
| P2-c | 治理加固（鉴权 + 凭据隔离 + audit） | 10.3 | 无 | 1 天 |

> 与原路线图唯一实质差异：**guardrails 从 P0 下沉 P1**，并补入 D7/D8/D9 三项遗漏。

---

## 12. 附录 D：落地最小步补充（让建议更可执行）

### 12.1 记忆升级应分三阶，勿一上来背 mem0 依赖

原 §2 直接建议「引入 mem0」偏粗。建议渐进：

- **阶一（先做，0.5 天，无新依赖）**：
  - lessons 按 goal 归一化签名去重（同模式合并留最新 `quality_report`）--直接解决 profile.json 20 条仅 7 个唯一 goal 的退化；
  - 拆 `semantic`（audience/tone）/`episodic`（每次 run 的 goal+results+quality）/`procedural`（成功链路模板）三键；
  - `_profile_text` 由「最近 5 条」改为「按当前 goal 关键词匹配召回 Top-5」（`if kw in lesson['goal']`，无需向量库）。
- **阶二（中成本）**：接 `mem0` 或 `LangGraph Store`，profile.json 变冷备；mem0 自带「抽取->去重->更新」直接解决滑窗退化。
- **阶三（高阶）**：复用 `run_manager` 的 `ThreadPoolExecutor`（`run_manager.py:40`），run 结束后投递低优先级「巩固任务」让 LLM 提炼 procedural 记忆--对照 Letta dreaming。

### 12.2 checkpoint 落地锚点（采纳并校准）

新增 `app/agent/checkpoint.py::StepCheckpointer`，每步边界原子写 `{plan, memory.steps, usage}` 到 `data/agent/checkpoints/<run_id>.json`（复用 `run_manager._persist` 的 tmp+`os.replace` 模式）；`Executor.execute` 入口 `load()` 命中则恢复并跳过 `status==SUCCEEDED` 的步；`_run_step` 成功/失败分支末尾各 `save(...)`；`run_manager._execute` 开头先 `load` 命中则 `emit resume_from_checkpoint` 续跑；新增 `POST /agent/runs/{id}/resume`。

> 上述附录 A-D 已基于独立代码核实与 GitHub API 实测。完成 P0 + P1 + P2 后，本项目即迈过「durable execution + 记忆不退化 + 可拦截 + 可证明 + 可治理」五道门槛，达到「能长久持续运行且有充分自主性」的合格 Agent 产品基线。
