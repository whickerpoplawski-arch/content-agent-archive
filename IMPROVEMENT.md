# XHS Content Agent — 缺陷补齐与优化方案

> 编写日期：2026-08-11。本文档基于上一轮「agent 面试官视角评估」定位的 5 项缺陷，结合**实时联网搜集的权威资料**与**本项目代码实情**，给出可落地的提升方案。
>
> **落地状态（2026-09-10 更新）**：P0（§1 checkpoint）、P1a（§2 阶一 记忆去重/分桶/召回）、P1b（§3 TTL 清理）均已实现并有回归测试；§2 阶三 dreaming 后台巩固已落地（`app/agent/consolidation.py`，提炼可迁移洞察写入 `procedural_insights`，run_manager 工作线程池投递，失败不致命）。§2 阶二 Mem0/LangGraph Store 接入**暂不实施**：现有实现（签名+指纹去重、加权淘汰、主题召回、洞察注入）已覆盖其核心收益，引入重依赖不划算。
>
> 资料来源（均经本地 VPN 代理 `http://127.0.0.1:7891` 用 curl 一手抓取，非二手转述）：
> - Anthropic《Building Effective Agents》https://www.anthropic.com/engineering/building-effective-agents
> - LangGraph 概念文档（jina reader 经代理）https://langchain-ai.github.io/langgraph/concepts/{persistence,memory,fault-tolerance,interrupts,checkpointers}/
> - Mem0 README https://github.com/mem0ai/mem0
> - Letta(MemGPT) 文档 https://docs.letta.com/letta-agent/memory
> - Playwright browser-contexts https://github.com/microsoft/playwright
> - MCP Authorization 规范 https://github.com/modelcontextprotocol/modelcontextprotocol
> - Temporal durable execution https://github.com/temporalio/temporal
> - Microsoft AutoGen / CrewAI README（见 search1.md）
>
> 外部基线总览见同目录 [search1.md](search1.md)。

---

## 0. 缺陷总览与优先级

| # | 缺陷 | 现状（代码实情） | 影响 | 优先级 |
|---|---|---|---|---|
| D4 | 无 per-step checkpoint / 崩溃续跑 | run_manager 重启把未完成 run 标 failed（[run_manager.py:295-310](app/agent/run_manager.py#L295-L310)）；trace 仅终态落盘 | 长任务崩溃=全量重跑 | **P0**（长时运行核心） |
| D5 | 长期记忆滑窗退化 | `lessons[-20:]` 全量覆盖写单 JSON（[orchestrator.py:130](app/agent/orchestrator.py#L130)），无去重/检索 | 跑久了同质化稀释信号 | **P1** |
| D7 | trace 文件无 TTL 清理 | `data/agent/runs/*.json` 与 `console_runs/*.json` 只增不删 | 磁盘膨胀 | **P1** |
| D8 | crawl_notes 登录与主循环耦合 | **已有缓解**：`sys.stdin.isatty()` 非交互快速失败 + `storage_state` 持久化（[local_site_crawler_service.py:315-337](app/services/local_site_crawler_service.py#L315-L337)）；但快速失败=该步直接失败走 recovery | agent 首次跑需人工预热登录 | **P2** |
| D9 | 安全/治理加固 | 凭据走 env✅；publish/feishu 有审批✅；但 MCP exe 信任假设、无凭据轮换/审计 | 自用可，多租户不够 | **P2** |

---

## 1. D4：per-step checkpoint 与崩溃续跑（P0）

### 1.1 外部基线（LangGraph / Temporal）
- **LangGraph Persistence** 原文：*"Persistence lets applications keep useful information beyond a single graph run. It matters when an agent needs to ... resume after an interruption, recover from a failure."*
- **两套互补系统**：
  - **Checkpointer**：持久化 *thread-scoped graph state* 快照，用于短期/会话连续性、human-in-the-loop、time travel、**fault tolerance**。
  - **Store**：持久化 *cross-thread* 的应用数据（用户偏好/事实/共享知识），用于长期记忆。
- **关键警告（直接命中本项目 D4 与 D7）**：
  > **MemorySaver does not persist between restarts** — MemorySaver/InMemorySaver 存在内存，进程重启即丢。生产应改用 **PostgresSaver** 或 **SqliteSaver**。
  > **Checkpoints growing unboundedly** — 长会话中 checkpoint 累积，增加延迟与存储成本。**Fix: 定期 prune 旧 checkpoint 或设保留策略（cron 删除 N 天前的）。**
- **fault-tolerance 原文**：*"Persistence: Build agents that persist through failures and can run for extended periods, **resuming from where they left off**."* 这正是本项目缺的能力。
- **Temporal** 把这一范式推到极致：整个工作流即 "durable execution platform"，每步自动 checkpoint、崩溃自动重放（replay）到上一步边界。本项目不必上 Temporal，但可借其思想：**把"步"作为持久化与重放的边界**。

### 1.2 本项目现状对照
- `Executor.execute` 的循环里，每步成功后 `memory.record(...)` 写入**内存** `AgentMemory`（[memory.py:72-75](app/agent/memory.py#L72-L75)），不落盘。
- `AgentTracer.save` 只在 run **结束**时一次性写 `data/agent/runs/<run_id>.json`（[tracing.py:47-58](app/agent/tracing.py#L47-L58)）。中途崩溃=trace 丢失。
- `run_manager._load_runs` 重启时只能把未完成 run 标 failed（[run_manager.py:300-304](app/agent/run_manager.py#L300-L304)），不能续跑。
- 结论：**短期记忆(steps)、plan 状态、trace 都是"内存态+终态落盘"，无中间 checkpoint**。

### 1.3 落地方案（最小侵入，不改架构）

**思路**：在 Executor 每步边界做"步级 checkpoint"——把 `{plan, memory.steps, usage}` 序列化追加到一个 run-scoped 持久文件；崩溃后 `run_manager._execute` 检测到已存在 checkpoint 即从断点续跑。等价于给现有循环装一个"SqliteSaver 的极简版"。

**新增 `app/agent/checkpoint.py`**（伪代码，约 80 行）：
```python
# 步级 checkpoint：每步边界原子追加，崩溃可续。
class StepCheckpointer:
    def __init__(self, run_id: str, dir_: Path = Path("data/agent/checkpoints")):
        self.path = dir_ / f"{run_id}.json"
    def save(self, plan: Plan, memory: AgentMemory, usage: dict): 
        # 原子写（tmp + os.replace，复用 run_manager._persist 的模式）
        write tmp -> os.replace
    def load(self) -> Optional[dict]: ...           # 无则 None
    def clear(self): self.path.unlink(missing_ok=True)
```

**Executor 改动**（2 处）：
1. `execute()` 入口：若 checkpointer.load() 命中，恢复 `plan/memory.steps/usage`，跳过已完成步（status 已 SUCCEEDED 的不重跑）。
2. `_run_step` 成功/失败分支末尾：`checkpointer.save(plan, memory, usage)`。

**run_manager 改动**：`_execute` 开头不再无条件 `run_started`，先 `load` 命中则 `emit resume_from_checkpoint` 并续跑。

**验收**：kill 进程后重启，未完成 run 能从最近一步继续，trace 连续不丢（对照 LangGraph "resuming from where they left off"）。

### 1.4 进阶（可选，P1 之后）
- 把 `AgentMemory.steps` 从 list 换成 SQLite 表，`record` 直接落库——等价 LangGraph `SqliteSaver`，免去整文件重写开销。
- 给 checkpoint 也设 TTL（见 D7）。

---

## 2. D5：长期记忆——从滑窗到「检索 + 去重 + 巩固」（P1）

### 2.1 外部基线（Mem0 / Letta / LangGraph Store）
- **LangGraph Store** 原文：长期记忆存于 *custom namespace*（不绑 thread_id），按 key 读写；与 checkpointer 配合（checkpointer 管短期、store 管长期）。
- **LangGraph 长期记忆四分法**（直接可用作本项目的记忆重构骨架）：
  - **Semantic memory**：事实/偏好（"目标用户是大学生女生"）。
  - **Episodic memory**：过往事件（"上次为 XX 目标生成了 2 篇通过质检的文案"）。
  - **Procedural memory**：如何做事（"load_notes→analyze→topics→contents 这条链路最稳"）。
  - **Profile**：用户画像聚合。
- **Mem0**（生产级记忆层，YC S24）：token-efficient 记忆算法，LoCoMo 基准 71.4→92.5；核心是 *抽取→去重→更新* 而非简单 append。README 明确其定位为"The Memory Layer for Personalized AI"。
- **Letta(MemGPT) dreaming**：用**后台子 agent** 回顾近期对话，**consolidate** 有用 lesson、更新记忆，不打断主循环；配 `/doctor` 审计去重与系统提示 token 占用、`/remember` 显式教学。这是"记忆不退化"的高级范式。

### 2.2 本项目现状对照
- `LongTermMemory` 是单文件 JSON，`set` 全量重写（[memory.py:51-53](app/agent/memory.py#L51-L53)）。
- lessons 用 `lessons[-20:]` 滑窗（[orchestrator.py:130](app/agent/orchestrator.py#L130)）——**有界但不去重、不检索**：20 条里若 18 条都是"为大学生女生生成文案"，信号被稀释；`_profile_text` 只回灌最近 5 条（[orchestrator.py:144](app/agent/orchestrator.py#L144)），更早的有效经验无法被召回。
- 无 semantic/episodic/procedural 区分。

### 2.3 落地方案（分三阶，按投入递增）

**阶一（低成本，先做）——去重 + 分桶 + 显式检索**：
- lessons 不再裸 append，改为"按 goal 归一化签名去重"：相同 goal 模式的 lesson 合并并保留最新 quality_report。
- 把 `lessons` 拆成 `semantic`（audience/tone）/`episodic`（每次 run 的 goal+results+quality）/`procedural`（成功链路模板）三个键。
- `_profile_text` 从"最近 5 条"改为"按当前 goal 做 substring/关键词匹配后召回最相关 5 条"——一个轻量 `if kw in lesson['goal']` 的召回即可，无需向量库。

**阶二（中成本）——接 Mem0 或换 LangGraph Store**：
- 把 `LongTermMemory` 抽成接口，实现 `Mem0Store`（`pip install mem0ai`）或 `LangGraphStore` 后端；profile.json 变成冷备。
- Mem0 自带 *抽取→去重→更新*，直接解决滑窗退化；Letta 式 `/doctor` 审计可做成一个 `agent.memory.audit()` 命令。

**阶三（高阶，自主性增强）——dreaming 式后台巩固**：
- 复用项目已有的 `ThreadPoolExecutor`（[run_manager.py:40](app/agent/run_manager.py#L40)），在 run 结束后投递一个低优先级"巩固任务"：让 LLM 回顾本次 run 的 trace + 历史 lessons，提炼出 1-2 条 *procedural* 记忆（"对美妆类选题，analyze 后直接 topics 效果更好"），去重后写入。对照 Letta "dreaming uses background subagents to consolidate useful lessons"。

**验收**：跑 50 次同质 goal 后，profile 里 procedural 记忆条数稳定在个位数且不重复；`_profile_text` 对新 goal 能召回到相关历史经验。

**落地补充（2026-09-10）**：阶三 dreaming 已实现于 `app/agent/consolidation.py`——run 结束后由 LLM 提炼至多 1 条*可迁移*洞察（`ProceduralInsight`，含 applies_to 适用域），按标题签名去重写入 `procedural_insights`（上限 20），`_profile_text` 注入规划 prompt；run_manager 以工作线程池 fire-and-forget 投递，CLI 路径同步跑且失败不致命。另修两处召回/写盘遗留：① 小 token 集（≤2）要求完全覆盖才算相关，杜绝「女生 vs 研究生」单字重叠 0.5 误召回；② `_remember_lesson` 的 episodic/lessons/procedural 三键合并单次落盘（`LongTermMemory.set_many`）。回归测试 `test_consolidation.py` 6 项 + `test_memory_upgrade.py::test_recall_rejects_small_set_partial_overlap`。

---

## 3. D7：trace 与 checkpoint 的 TTL 清理（P1）

### 3.1 外部基线
- LangGraph 明确给出该问题的 fix 原文：*"Consider adding a cron job to delete checkpoints older than N days."*
- 通用做法：保留窗口（hot=近 N 天全留 / cold=只留 summary）+ 定时清理。

### 3.2 本项目现状
- `data/agent/runs/*.json`、`data/agent/console_runs/*.json`、（D4 后）`data/agent/checkpoints/*.json` 三类只增不删。
- 当前文件量小（console_runs 约 20+），短期无压力，但"长久持续运行"必须有边界。

### 3.3 落地方案
**新增 `app/agent/housekeeping.py`**：
```python
def purge_old_runs(retention_days: int = 30, keep_summary: bool = True):
    for d in (RUNS_DIR, CONSOLE_RUNS_DIR, CHECKPOINTS_DIR):
        for f in d.glob("*.json"):
            if age_days(f) > retention_days:
                if keep_summary: archive_summary(f)  # 仅留 run_id/goal/status/usage
                f.unlink()
```
- 暴露为 `POST /agent/housekeeping`（需 admin token，对照 D9 治理）与一个 `scripts/cleanup_runs.py` 离线入口。
- 在 `run_manager.create` 末尾以概率（如每 50 次）触发一次清理，或挂 `APScheduler` 定时——后者更干净。
- 保留窗口默认 30 天，`keep_summary=True` 保证审计不丢"曾经跑过什么"。

**验收**：注入 40 天前的 run 文件后触发清理，hot 区只剩 30 天内全量 + 旧 run 的 summary。

**落地补充（2026-09-10）**：冷区边界补齐——`.summary.json` 改为双倍保留期后清除（保证 summary 始终活得比 run 文件久），崩溃残留 `.json.tmp` 达保留期即清（glob 扩为 `*.json*`）。测试 `test_purge_removes_stale_summary_and_tmp_files`。

---

## 4. D8：crawl_notes 登录与主循环解耦（P2，现状已有缓解）

### 4.1 外部基线
- **Playwright browser context**：每个 context 独立 cookies/localStorage/sessionStorage，可 `storage_state(path=...)` 持久化与复用登录态——不必每次交互登录。
- **MCP Authorization 规范**：STDIO 传输 *SHOULD NOT* 走 OAuth 交互，而应"从环境取凭据"；HTTP 传输才用 OAuth 2.1 流。含义：**本地工具的鉴权应离线预备，不应在 agent 运行时交互**。

### 4.2 本项目现状（如实评估：已有缓解，非裸缺陷）
- `_ensure_logged_in` 已正确做两件事（[local_site_crawler_service.py:315-337](app/services/local_site_crawler_service.py#L315-L337)）：
  1. `Path(STATE_FILE).exists()` 命中则跳过登录（复用 Playwright `storage_state`）。
  2. 非交互环境 `sys.stdin.isatty() == False` 时**快速失败**抛 RuntimeError，而非阻塞 `input()`。
- 即：**"阻塞 agent 主循环"这个最坏情况已被规避**。剩下的只是"首次需人工预热登录"的体验问题，以及快速失败后该步会走 recovery（Reflector.recover 决定 retry/replan/stop）。

### 4.3 落地方案（体验与健壮性增强，P2）
1. **登录预热解耦为独立前置服务**：新增 `scripts/xhs_login_warmup.py`，交互式启动 Playwright、完成扫码、落 `STATE_FILE`。Agent 运行时只读 state、不再有任何 `input()` 路径。README 与 `crawl_notes` 工具描述里写明"前置条件：已运行 warmup"。
2. **crawl_notes 失败转可恢复**：快速失败时，工具返回结构化错误（`{"needs_login": True}`）而非抛 RuntimeError，让 Reflector 的 recover 能产出一个更友好的 replan（如"改用 load_notes 的样本数据继续"），而不是整 run 失败。
3. **登录态健康度自检**：`crawl_notes` 调用前先 `GET /xhs-service/status`（项目已有该路由，见 server.stdout.log），失效则返回 `needs_login`，由上层决定是否中止或降级。

**验收**：未登录态下跑含 crawl_notes 的 goal，agent 不卡死、不整 run 崩，而是 replan 到样本链路或优雅停止并提示需 warmup。

---

## 5. D9：安全与治理加固（P2）

### 5.1 外部基线
- **MCP 安全警告（AutoGen README 原文）**：*"Only connect to trusted MCP servers as they may execute commands in your local environment or expose sensitive information."*
- **CrewAI AMP Suite**：把 *tracing & observability / governance / security / deployment* 做成独立控制面——工业级 agent 产品把这视为必备。
- **MCP Authorization**：OAuth 2.1 子集，STDIO 从环境取凭据。

### 5.2 本项目现状
- 凭据走 `settings`/`.env`✅；publish/feishu 经 approval_handler✅；这是好的基础。
- 待加固：本地 `xiaohongshu-mcp-*.exe` 是闭源二进制（信任假设）、`.env` 含 `XHS_PROXY` 等明文配置、无凭据轮换、`/agent/housekeeping` 这类管理动作若暴露需鉴权。

### 5.3 落地方案
1. **管理端点鉴权**：`/agent/housekeeping`、`/agent/runs/{id}/stop` 加一个 `X-Admin-Token` 头校验（token 从 env），避免本地服务被同机其他进程随意调用。
2. **凭据隔离**：`OPENAI_API_KEY` 等只进 settings，不进 trace/lesson/memory 任何落盘字段；加一个单测 `test_no_secret_in_trace` 扫 `data/agent/**/*.json` 不含 key 前缀。
3. **MCP server 信任清单**：在 `tools/xiaohongshu-mcp/` 加一个 `TRUST.md` 记录该二进制的来源、哈希、权限范围（仅本地、仅小红书域名），对应 AutoGen 的"trusted MCP server"要求。
4. **审计日志**：housekeeping 与 stop 动作额外写一行到 `data/agent/audit.log`（append-only），对照 CrewAI governance。

---

## 6. 实施路线图

| 阶段 | 任务 | 对应缺陷 | 预估工作量 | 依赖 |
|---|---|---|---|---|
| **P0** | `checkpoint.py` + Executor 续跑 + run_manager resume 事件 | D4 | 1-2 天 | 无 |
| **P1a** | lessons 去重 + 三分桶 + 关键词召回 | D5 | 0.5 天 | 无 |
| **P1b** | `housekeeping.py` + 定时清理 + summary 归档 | D7 | 0.5 天 | 与 D4 checkpoint 路径一致 |
| **P2a** | `xhs_login_warmup.py` + crawl_notes 结构化 needs_login | D8 | 0.5 天 | 无 |
| **P2b** | 管理端点鉴权 + 凭据隔离单测 + MCP TRUST.md + audit.log | D9 | 1 天 | 无 |
| **进阶** | Mem0/LangGraph Store 接入 + dreaming 巩固 | D5 阶二/三 | 2-3 天 | P1a 完成 |

### 路线图落地核对（2026-09-10）

| 阶段 | 状态 |
|---|---|
| P0 checkpoint + 续跑 | ✅ 已落地（checkpoint.py / executor / run_manager.resume + tracing.flush） |
| P1a 记忆去重/分桶/召回 | ✅ 已落地，且超出方案（指纹近似合并 + 加权淘汰 + 小集合误召回兜底 + 写盘合并） |
| P1b housekeeping | ✅ 已落地，且超出方案（覆盖 crawled_notes/images 产物 + summary/tmp 冷区 TTL） |
| P2a 登录预热 + needs_login | ✅ 已落地（test_crawler.py 预热 + 登录重定向检测） |
| P2b 治理加固 | ✅ 已落地（XHS_ADMIN_TOKEN + secrets 隔离 + audit.log） |
| 进阶 dreaming 巩固 | ✅ 已落地（consolidation.py）；Mem0/LangGraph Store 接入**暂缓**（现有实现已覆盖核心收益） |

完成后，本项目将从「合格原型」迈过「durable execution + 记忆不退化 + 可治理」三道门槛，具备"7×24 长久持续运行"的工业基础。

---

## 7. 与外部基线的对齐自检

| 工业基线要求（来自 search1.md 第 8 节） | 本方案对应项 |
|---|---|
| D1 真自主循环 | 已具备，方案不动核心循环 |
| D2 ground truth per step | 已具备 |
| D3 停止条件/预算 | 已具备 |
| **D4 持久化与可恢复** | **本方案 §1 落地 step checkpointer** |
| **D5 记忆（短期+长期，有界不退化）** | **本方案 §2 落地去重/检索/consolidation** |
| D6 人在环/审批 | 已具备 |
| **D7 可观测性（含 TTL）** | **本方案 §3 落地清理与归档** |
| **D8 安全/治理** | **本方案 §4 §5 落地** |
| D9 自学习/迭代改进 | 已具备（evaluator-optimizer + lessons） |

> 全部 8 个维度补齐后，对照 LangGraph "durable execution / persistence / memory / human-in-the-loop" 的核心卖点与 Anthropic "stopping conditions + ground truth per step" 的要求，本项目即达到"能长久持续运行且有自主性的合格 agent 产品"的工业基线。

---

## 8. 2026-08-13 真实链路实测补记（新缺陷）

> 来源：实战测试真实对话（backend=openai，真实模型 deepseek-v4-flash[1M]），爬取西电研究生 5 篇真实笔记 + 生成 3 篇文案，全程 API 轮询监控（未用截图）。发现 2 项新缺陷，1 项已修复 + 回归测试，1 项待修。

### 8.1 终态误报（已修复 ✅）

- **现象**：真实 run 爬取 5/5 成功、生成 3 篇全过质量门，但终态 `failed`，error「Plan incomplete」。plan 是 `[crawl → analyze → generate]`，反思 stop 时 analyze 因依赖 crawl 一直 PENDING 未轮到 → `plan.is_complete=False` → `_resolve_terminal` 误报 failed。
- **根因**：反思主动 stop 是「目标达成收尾」，但 executor 没有把剩余 PENDING 步骤收尾，导致 is_complete 永远为 False。
- **修复**（[executor.py](app/agent/executor.py)）：新增 `_graceful_stop` 标志，反思 stop（质量门 override 或 should_stop）置位；execute 循环返回前把剩余 PENDING 步骤标 SKIPPED → `is_complete=True` → completed。预算中断 / replan 拒绝 / 用户 stop 路径不置位，保持 failed / stopped，不虚报。
- **验证**：新增 `test_reflector_stop_skips_pending_steps_not_failed`；tests 全量 101 passed；真实 run C 终态 `completed`，analyze_notes 标 skipped。
- **遗留**：analyze 被跳过意味着生成的 3 篇文案未真正消费爬取的分析结果（见 8.2），不是理想行为，但已不再误报失败。

### 8.2 generate 与 crawl 并行先跑（已修复 ✅）

- **现象**：plan `[crawl → analyze → generate]`，但 generate_contents 与 crawl_notes 同批并行执行，生成时用的 topic 硬编码（「西电研究生」）而非爬取数据的分析结果。
- **根因**：[executor.py `_deps`](app/agent/executor.py) 依赖判定只从步骤 params 里 `$latest.<tool>` 引用推断。planner 生成的 generate 步骤 params 不带 `$latest.analyze_notes` 引用 → 判定无依赖 → 首轮 batch 即就绪并行。
- **修复**（[executor.py `_ready_indices`](app/agent/executor.py)）：generate_contents 步骤无显式依赖且 plan 中**排在其之前**的 analyze_notes / crawl_notes 存在时，强制补依赖（先 analyze 后 crawl），按索引保证只认「前置数据源」，排在 generate 之后的步骤不构成依赖。确定性兜底，不依赖 planner prompt。
- **验证**：新增 `test_generate_contents_waits_for_prior_analyze_even_without_ref`；tests 全量 102 passed；真实 run `77e116785e0d`：crawl 1/1 → analyze → generate 严格串行，generate 的 analyze 输入含爬取真实笔记（「西电研一｜读研日记」top_notes），终态 completed。
