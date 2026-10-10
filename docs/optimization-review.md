# XHS Content Agent 优化计划评审与修订（代码实情校准版）

> 评审日期：2026-08-11 ｜ 评审对象：`docs/optimization-plan.md`、`IMPROVEMENT.md`、`app/agent/*` 实际代码
> 立场：独立工程评审，以**实际代码**为准绳，不轻信文档自述。
> 方法：逐文件读完 `app/agent/`（orchestrator/planner/executor/evaluator/tools/llm/memory/budget/run_manager/tracing/schemas/mock_factories）+ `app/core/config.py` + `app/api/routes_agent.py` + `tests/`，并 grep 验证「scheduler/guardrail/evals/checkpoint/resume」在代码中是否存在。

---

## 一、独立结论：本项目是否算「合格 / 可持续自主 Agent」

分两层下结论，因为这两层差距很大：

- **作为「单次目标内的自主 Agent」：合格。** 它有真正的 `目标→规划→执行→反思→追踪` 自适应循环（`orchestrator.py::XHSAgent.run` → `executor.py::Executor.execute` 的 while 循环），而非写死三步流程；具备并行执行（`executor.py:143-147` `max_parallel`）、失败恢复（`planner.py::Reflector.recover`）、质量门（`evaluator.py::QualityGate`）、预算自停（`budget.py`）、审批副作用（`executor.py:185-197`）、追踪落盘（`tracing.py`）。26 个离线测试可跑通。作为原型 / 演示，骨架是扎实的。
- **作为「能长久持续运行、有充分自主性」的产品：未达。** README 自己也诚实承认了这一点。硬伤是：① 崩溃即全量重跑、不可续（无 checkpoint）；② 只能按需触发、不能定时 / 事件持续自主（无 scheduler）；③ 没有 goal 级评估证据（无 evals）；④ 副作用前只有「人工审批」、无合规护栏（无 guardrails）；⑤ 长期记忆会随运行同质化退化（`lessons[-20:]` 滑窗不去重不检索）。

**一句话定性：引擎合格，尚未达到「7×24 无人自主」；距离该目标还差 P0（可恢复）+ P1（可证明 / 可拦截 / 记忆不退化）两层。**

---

## 二、与现有两份计划文档的关系（先厘清，避免叠床架屋）

项目里已存在**两份**优化文档，本评审是第三份，必须说清和它们的关系：

| 文档 | 优点 | 缺点 |
|------|------|------|
| `docs/optimization-plan.md` | 方向对、覆盖广（持续性/记忆/evals/护栏/断点续跑五项全），参照了 LangGraph/OpenAI/mem0 等 | 高层、**几乎无代码锚点**；优先级偏激进（把 guardrails 与 checkpoint 并列 P0） |
| `IMPROVEMENT.md`（较新） | 代码锚点准（file:line）、落地伪代码可执行；正确识别 D4/D5/D7 | **错误地把 guardrails / evals / scheduler 当作「已具备」而整段删去**（见 §7 自检表标 D1/D6/D9「已具备」），导致覆盖面收缩 |

**本评审定位**：取两者之长（IMPROVEMENT 的代码锚点 + optimization-plan 的覆盖广度），纠正两者的优先级与遗漏，并补一个**两份都没发现的真实配置漂移 bug**（见 §六）。

---

## 三、缺陷清单（逐条，代码实情 + 是否成立）

| ID | 缺陷 | 代码实情（锚点） | 成立？ | 两份文档态度 |
|----|------|------------------|--------|----------------|
| D1 | 仅单次目标内自主，无持续运行 | `XHSAgent.run` 仅由 API/demo 触发；`executor.py:26` `MAX_TOTAL_STEPS=50` 硬上限；grep 代码无 `schedule/cron` | ✅ 成立 | optimization-plan 列为 P2；**IMPROVEMENT 错误标「已具备」** |
| D2 | 长效记忆过薄、无检索 | `orchestrator.py:130` `lessons[-20:]` 全量覆盖；`orchestrator.py:144` 只回灌最近 5 条；`memory.py:51-53` `set` 整文件重写 | ✅ 成立（profile.json 实测 4+ 条近乎重复 lesson，已证实退化） | 两份均列 P1 ✅ |
| D3 | 缺端到端 evals | `tests/` 仅 26 个组件单测；无 `goal_completion_rate` / evals 目录；grep 无 `evals` | ✅ 成立 | optimization-plan 列 P1；**IMPROVEMENT 整段删除** |
| D4 | 无 checkpoint / 续跑 | `run_manager.py:300-304` 重启把未完成 run 标 failed；`tracing.py:47-58` 仅终态落盘；`memory.py:72-75` 仅内存 | ✅ 成立 | 两份均列 P0 ✅ |
| D5 | 缺护栏 | `executor.py:185-197` 只对 `publish_to_xhs`/`sync_to_feishu` 做审批；`evaluator.py` 只评 0-10 质量、**不评合规**；grep 无 `guardrail` | ✅ 成立 | optimization-plan 列 P0；**IMPROVEMENT 整段删除** |
| D6 | 外部依赖脆弱（爬虫登录 / MCP） | `local_site_crawler_service` 已有 `sys.stdin.isatty()` 快速失败 + `storage_state`（部分缓解） | ✅ 成立（已缓解一半） | IMPROVEMENT 列 P2 ✅ |
| D7 | trace 无 TTL 清理 | `data/agent/console_runs/` 实测 16+ 文件只增不删；代码无清理 | ✅ 成立 | **仅 IMPROVEMENT 列出（P1），optimization-plan 遗漏** |
| D8 | 配置漂移（两份均漏） | `config.py:72` 定义 `agent_max_retries=3`，但 `orchestrator.py:76-86` 构造 `Executor` 时**未传** `max_retries_per_step`，env 变量是死配置；`MAX_TOTAL_STEPS=50` 硬编码，无 `agent_max_steps` 设置项 | ✅ 成立（**新发现**） | **两份均遗漏** |
| D9 | 治理 / 安全加固 | 凭据走 env ✅、审批 ✅；但 `/agent/runs/{id}/stop`、`/agent/housekeeping` 等管理端点无鉴权；无 secret-in-trace 单测 | ✅ 成立 | IMPROVEMENT 列 P2；optimization-plan 遗漏 |

---

## 四、对 `docs/optimization-plan.md` 的逐条评审

> 回应评审 prompt 第 2 题：缺陷分析是否准确、优先级是否恰当、落地建议是否贴合、有无遗漏。

1. **§1 持续性（P2）—— 成立，优先级恰当。** 落地建议（Scheduler + `/agent/schedule` + 事件驱动）方向对，且正确地放在 P2：scheduler 必须建立在 checkpoint + guardrails 之上，否则无人值守调度 = 无人值守闯祸。
2. **§2 记忆（P1）—— 成立，优先级恰当，但落地建议偏粗。** 「引入 mem0」是中期方案，缺了 IMPROVEMENT.md §2 的「阶一：先做去重 + 三分桶 + 关键词召回」最小步。建议采纳 IMPROVEMENT 的三阶递进，避免一上来背 mem0 依赖。
3. **§3 evals（P1）—— 成立，优先级恰当。** 「先用本项目自有 trace + quality_report 落地闭环，再可选接 promptfoo」的克制是对的，赞同。
4. **§4 护栏（标 P0）—— 缺陷成立，但优先级偏高，应降为 P1。** 理由：当前 Agent 只能按需触发、且副作用已有人工审批兜底，即时安全风险是有界的；真正卡住「长久运行」的第一刀是「崩溃不可恢复」（D4），不是「无护栏」。护栏是 P2 scheduler 的**前置条件**，但不是 P0 checkpoint 的前置条件。把 guardrails 与 checkpoint 并列 P0 会让 P0 工作量翻倍、推迟可恢复性的落地。
5. **§5 断点续跑（P0）—— 成立，优先级恰当。** 与 IMPROVEMENT.md §1 一致，赞同。
6. **§6 路线图问题：** 「先 P0（checkpoint + guardrails）」应改为「先 P0（仅 checkpoint）」，guardrails 下沉 P1。其余顺序合理。
7. **遗漏项：** ① trace TTL（D7，只增不删，长稳运行磁盘必爆）；② 配置漂移（D8，`AGENT_MAX_RETRIES` 死配置 + `MAX_TOTAL_STEPS` 硬编码）；③ 管理端点鉴权（D9 一部分）。

---

## 五、修订后的 P0 / P1 / P2（更合理的顺序）

> 原则：P0 只放「可恢复」这一根基；P1 放「可证明质量 / 可拦截越界 / 记忆不退化 / 可观测有界」四件事（彼此大多独立，可并行）；P2 放「持续自主 + 弹性 + 治理」（必须在 P0+P1 之上）。

### P0 — 可恢复（长时运行根基，1-2 天，无依赖）

**D4：步级 checkpoint + resume 端点**

- 现状锚点：`run_manager.py:300-304`（重启即 failed）、`tracing.py:47-58`（仅终态落盘）、`memory.py:72-75`（仅内存）。
- 落地（采纳 IMPROVEMENT.md §1，已校准）：
  1. 新增 `app/agent/checkpoint.py::StepCheckpointer`，每步边界原子写 `{plan, memory.steps, usage}` 到 `data/agent/checkpoints/<run_id>.json`（复用 `run_manager._persist` 的 tmp+`os.replace` 模式）。
  2. `executor.py::Executor.execute` 入口：`checkpointer.load()` 命中则恢复 `plan/memory.steps/usage`，跳过 `status==SUCCEEDED` 的步（不重跑）。
  3. `executor.py::_run_step` 成功 / 失败分支末尾各加一次 `checkpointer.save(...)`。
  4. `run_manager.py::_execute` 开头先 `load`，命中则 `emit resume_from_checkpoint` 续跑，而非无条件 `run_started`。
  5. 新增 `POST /agent/runs/{id}/resume` 路由（`routes_agent.py`）。
- 验收：kill 进程后重启，未完成 run 从最近一步继续；trace 连续不丢；`data/agent/checkpoints/<run_id>.json` 在 run 终态后被 `clear()`。

### P1 — 可证明 / 可拦截 / 记忆不退化 / 可观测有界（并行推进，各 0.5-1 天）

**P1-a 护栏 `app/agent/guardrails.py`（前置 P2 scheduler）**
- 现状：`executor.py:185-197` 仅审批 2 个副作用工具；`evaluator.py` 只评质量不评合规。
- 落地：输入护栏（目标敏感 / 越权检测）、工具护栏（副作用工具白名单，保留现有审批）、输出护栏（敏感词 / 广告法禁用词 / 小红书合规，规则表 + LLM 二次判定）。在 `executor.py` 调用工具前后挂钩子，拦截写 `guardrail_blocked` 事件进 trace。
- 验收：注入含禁用词的草稿，输出护栏拦截并记 trace；恶意 goal 被输入护栏拒。

**P1-b 端到端 evals `tests/test_agent_evals.py`**
- 现状：无 goal 级度量。
- 落地：定义「目标→期望产出」用例集，复用 `evaluator.py` 0-10 分对**最终产出**打分，聚合 `goal_completion_rate`；关键指标 = 步骤成功率 / 平均步数 / `quality_report` 通过率 / 目标完成率。接入 CI 防回归。
- 验收：`pytest tests/test_agent_evals.py` 输出 `goal_completion_rate` 数值，且 mock 后端可离线跑。

**P1-c 记忆：去重 + 三分桶 + 关键词召回（IMPROVEMENT.md §2 阶一）**
- 现状：`orchestrator.py:130` 滑窗 + `:144` 最近 5 条。
- 落地：lessons 按 goal 归一化签名去重（同模式合并留最新 quality）；拆 `semantic/episodic/procedural` 三键；`_profile_text` 由「最近 5 条」改为「按当前 goal 关键词匹配召回 Top-5」（`if kw in lesson['goal']`，无需向量库）。
- 验收：跑 50 次同质 goal 后，profile 条数稳定个位数且不重复；新 goal 能召回到相关历史经验。
- 进阶（P1 完成后可选）：接 mem0 / LangGraph Store + Letta 式后台巩固（dreaming）。

**P1-d trace / checkpoint TTL `app/agent/housekeeping.py`（IMPROVEMENT.md §3）**
- 现状：`console_runs/` 16+ 文件只增不删。
- 落地：`purge_old_runs(retention_days=30, keep_summary=True)`；暴露 `POST /agent/housekeeping`（需鉴权，见 P2-c）+ `scripts/cleanup_runs.py`；挂 APScheduler 定时或在 `run_manager.create` 末尾概率触发。
- 验收：注入 40 天前 run 文件后触发，hot 区只剩 30 天内全量 + 旧 run 的 summary。

**P1-e 配置漂移修复（新发现，小修）**
- 现状：`config.py:72` `agent_max_retries` 死配置；`executor.py:26` `MAX_TOTAL_STEPS=50` 硬编码。
- 落地：① `orchestrator.py` 构造 `Executor` 时传 `max_retries_per_step=settings.agent_max_retries`；② `config.py` 增 `agent_max_steps: int = 50`，`budget.py::from_settings` 读入，`executor.py` 用 `budget.max_steps or 50` 替换硬编码常量。
- 验收：`AGENT_MAX_RETRIES=5` / `AGENT_MAX_STEPS=10` 实测生效（加一个单测）。

### P2 — 持续自主 + 弹性 + 治理（必须在 P0+P1 之上）

**P2-a 调度器 `app/agent/scheduler.py` + `/agent/schedule`（optimization-plan §1）**
- 落地：在 `run_manager` 之上加常驻循环，支持 cron / 间隔 / 事件驱动；复用现有审批与 SSE。**只有 P1-a 护栏落地后才允许调度 publish 类 goal。**
- 验收：定时任务能跨天触发多轮 run，每轮受 `budget` 约束。

**P2-b 登录解耦 + crawl_notes 结构化 needs_login（IMPROVEMENT.md §4）**
- 落地：`scripts/xhs_login_warmup.py` 独立预热；`crawl_notes` 快速失败时返回 `{"needs_login": True}` 而非抛 RuntimeError，让 Reflector replan 到样本链路。
- 验收：未登录跑含 crawl_notes 的 goal，agent 不卡死、不整 run 崩，replan 到 `load_notes` 或优雅停止。

**P2-c 治理加固（IMPROVEMENT.md §5）**
- 落地：`/agent/housekeeping`、`/agent/runs/{id}/stop` 加 `X-Admin-Token` 头校验；`OPENAI_API_KEY` 等只进 settings 不进 trace（加 `test_no_secret_in_trace` 扫 `data/agent/**/*.json`）；`tools/xiaohongshu-mcp/TRUST.md` 记录二进制来源 / 哈希 / 权限范围；housekeeping 与 stop 动作 append 写 `data/agent/audit.log`。
- 验收：无 token 调管理端点被 401；trace 文件不含 key 前缀。

**P2-d（并发容量，次要）** `run_manager._pool` 仅 4 worker，审批等待期间也占用 worker。并发上限=4，多审批并发会顶满。建议审批等待改用独立线程池或显式排队，避免新 run 被饿死。

---

## 六、两份计划都遗漏的关键风险（评审 prompt 第 4 题）

1. **配置漂移（D8，新发现）**：`config.py:72` 的 `agent_max_retries` 与 README 文档化的 `AGENT_MAX_RETRIES` env 变量是**死配置**——`orchestrator.py:76-86` 构造 `Executor` 时没传，Executor 用默认 3。用户改 env 无效，是「文档说可配、实际不可配」的隐性 bug。同时 `MAX_TOTAL_STEPS=50` 硬编码、无 `agent_max_steps` 设置项。**建议并入 P1-e 小修。**
2. **trace 仅终态落盘（可观测性打折）**：`tracing.py:47-58` 只在 `run` 结束 `save` 一次。中途崩溃 = trace 丢失，无法事后排查「跑到哪一步崩的」。这其实是 D4 checkpoint 的同一面——P0 落地后顺手在每步 `event` 后 flush 一次即可解决，但两份文档都没点明「trace 也会丢」。
3. **管理端点无鉴权**：`/agent/runs/{id}/stop`、未来的 `/agent/housekeeping` 任何同机进程可调。本地自用可忽略，但一旦上 P2-a 调度器做长稳运行，无鉴权 = 任意进程能停 / 清你的 agent。IMPROVEMENT.md §5 提了，optimization-plan.md 漏了。
4. **replan 不重置 `total` 步数计数器**：`executor.py:134-137` replan 时重置 `retries` 但不重置 `total`，多次 replan 会累积计入 `MAX_TOTAL_STEPS=50`，可能让一个「每段都短但 replan 多」的 run 被过早停机。属合理性边界，建议在 P1-e 一并暴露为可配置或明确「50 是跨 replan 总预算」。

---

## 七、落地顺序总览

| 顺序 | 任务 | 对应 | 依赖 | 工作量 |
|------|------|------|------|--------|
| **P0** | checkpoint + resume 端点 | D4 | 无 | 1-2 天 |
| **P1-a** | guardrails | D5 | 无（P2-a 前置） | 中 |
| **P1-b** | evals | D3 | 无 | 中 |
| **P1-c** | 记忆去重+三分桶+召回 | D2 | 无 | 0.5 天 |
| **P1-d** | housekeeping TTL | D7 | 与 D4 路径一致 | 0.5 天 |
| **P1-e** | 配置漂移修复 | D8 | 无 | 0.2 天 |
| **P2-a** | scheduler | D1 | P0 + P1-a | 高 |
| **P2-b** | 登录解耦 | D6 | 无 | 0.5 天 |
| **P2-c** | 治理加固 | D9 | 无 | 1 天 |

> 与 `optimization-plan.md` 原路线图的唯一实质差异：**guardrails 从 P0 下沉 P1**（理由见 §四-4），并补入 D7/D8/D9 三项遗漏。完成后即迈过「durable execution + 记忆不退化 + 可拦截 + 可证明 + 可治理」五道门槛。
