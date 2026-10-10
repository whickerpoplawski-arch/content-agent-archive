# 2026-08-13 控制台真实对话实测：西电研究生（少样本）

> 浏览器(本地 Chrome 独立实例)打开 Agent 控制台 → 填写目标 → 真实模型跑完整对话 → API/SSE 实时监控每一步 → 记录问题。
> 范围：除「飞书同步」「发布」外的全流程（爬取/载入样本 → 分析 → 选题 → 文案生成 → 质量门 → 反思）。
> 证据文件：`.research/run_67a17b0e86fe.json`（API run 详情，31 事件）、`.research/sse_67a17b0e86fe.log`（SSE 原始流）、`data/agent/runs/67a17b0e86fe.json`（磁盘 trace，24 事件）。

## 一、测试信息

| 项 | 值 |
|----|----|
| 时间 | 2026-08-13（UTC 08-12 17:07:17 创建） |
| run_id | `67a17b0e86fe` |
| 目标 | 分析已有样本，为西电研究生生成 3 篇差异化小红书笔记文案，不要发布，不要同步飞书。 |
| audience / tone | 西电研究生 / 真实克制 |
| backend | openai（实际经 Codex 配置走 `deepseek-v4-flash[1M]` + volces ark） |
| enabled_tools | crawl_notes, analyze_notes, generate_topics, generate_contents, generate_images, load_notes（发布/飞书默认禁用） |
| 总耗时 | **73.1s**（17:07:18 → 17:08:31） |
| 结果 | status=completed，10 次 LLM 调用，6 次工具调用，31 个运行事件 |

## 二、实际执行轨迹（SSE 实时监控确认）

```
plan_created   [load_notes, analyze_notes, generate_topics,
                generate_contents, generate_contents, generate_contents]
 1. load_notes        ok  0.9ms   载入 5 条样本
 2. analyze_notes     ok  842ms   输出 top_keywords/tags/title_patterns/insight_points
 3. generate_topics   ok  7.0s    产出 3 个差异化选题（西电研究生向）
 4-6. generate_contents ok 6.6s / 9.0s / 8.7s  产出 3 篇文案
      quality_evaluation ×3：evaluated=1, optimized=0, passed=1（3/3 全过）
 反思振荡（见缺陷 1）：on_target_passed 3 → 1 → 2
replan（见缺陷 2）：声明后未执行新步骤
run_completed
```

Agent 自主选择 `load_notes`（目标含「分析已有样本」，未触发需登录的 `crawl_notes`，未使用 `generate_images`）——符合预期，无副作用。

## 三、产物质量（本次未复现历史「格式漂移」）

3 篇文案差异化明显（content_type = 教程 / 清单 / 避雷），标题、正文、hashtags、image_suggestion 齐全，内容贴合「西电研究生」：

1. 西电研究生宿舍收纳大法｜3平米书桌也能装下全部实验资料（教程）
2. 西电研究生平价好物推荐｜这些学习神器不到50元，宿舍党必入不踩雷（清单）
3. 西电研究生避雷指南｜新生入学这5类东西千万别买，省钱又省心（避雷）

控制台「最终产物」「运行数据」面板渲染正确；SSE 事件在「执行轨迹」面板逐条展示。浏览器端与 API 数据一致。

---

## 四、问题与缺陷（按严重度排序）

### 缺陷 1（高）：反思器计数振荡，与质量门结果矛盾，白白消耗 LLM 调用

run 完成前 3 条反思（API 事件 27/28/30，磁盘 trace 尾部 3 条 reflection）互相矛盾：

| 事件 | 内容 | next_action | on_target_passed |
|------|------|-------------|------------------|
| 27 | 已生成3篇差异化文案且全部达标，目标达成 | stop | 3 |
| 28/29 | 已有3篇但仅1篇达标，需调整方向重新生成 | replan | 1 |
| 30 | 已生成3篇但仅2篇通过，需再生成或优化1篇 | continue | 2 |

而质量门实际结果（事件 24-26 与 outputs.quality_report）是 **3/3 全 passed**。反思器没有采用 quality_report 的实测数据，而是让 LLM 自行「数」达标题数，得出 1、2 的幻觉计数。振荡（3→1→2）未触发已有的振荡检测抑制（该机制似乎只针对工具级产出，未覆盖反思计数），多消耗约 4 次 LLM 调用且无任何实际执行。

### 缺陷 2（中）：replan 声明「需调整方向重新生成」，却未执行任何新步骤即 run_completed

事件 29 replan 明确「需调整方向重新生成」，但此后无任何 `step_started`/`tool_started`，直接事件 31 run_completed。最终状态（3 篇达标）虽正确，但轨迹与反思逻辑不一致，用户看控制台会困惑：为什么重规划了却什么都没做就结束？根因与缺陷 1 同源——反思输出不可靠，`_collect_outputs` 判定达标后直接收尾，replan 形同虚设。

### 缺陷 3（中）：usage.est_tokens=1084 明显低估，成本观测失真

10 次真实模型调用（规划 1 + 选题 1 + 文案生成 3 + 质量评审 3 + 反思若干），仅计 1084 估算 tokens，对长文案生成显然偏低，成本/用量面板数值无参考价值。另：usage.calls=10 与 6 条 reflection 事件的调用口径对不上（若每步反思都计 1 次 LLM，应 >10）。建议核对 `OpenAILLMBackend` 的 token 估算公式与反思的调用计数。

### 缺陷 4（低）：双轨事件模型割裂，磁盘 trace 无法独立还原运行结果

- API run 详情 31 个高电平事件（含 `replan`、`quality_evaluation`）；磁盘 `data/agent/runs/67a17b0e86fe.json` 为 24 条低电平 trace（kind=goal/plan/tool/reflect/quality/done），**无 replan kind**。
- trace 文件 `result` 只存产物 dict（analysis_summary/results/quality_report），**不含 status、usage、stopped_reason**，单独打开文件无法判断运行成败与用量。
- 建议 trace 落盘补 status/usage/stopped_reason，并统一事件模型或明确映射。

### 缺陷 5（低）：planner 将 3 篇文案拆成 3 个独立 generate_contents 步骤

计划为 `generate_contents ×3`（各 count=1），触发 3 次独立质量评审 + 3 次步骤反思，增加 LLM 调用与轨迹噪音。本可一次 `generate_contents(count=3)`。轻微低效，非错误。

### 缺陷 6（低）：控制台模型标签写死「OpenAI」

页面 `modelLabel` 固定显示「OpenAI」，实际经 Codex 配置走的是 `deepseek-v4-flash[1M]`（volces ark）。展示标签与真实模型不一致，易误导。

### 备注（非缺陷）

- `crawl_notes` 默认启用：目标含「分析已有样本」时 Agent 正确选择 `load_notes` 跳过爬取；但若目标含糊，Agent 可能选择触发需登录态的爬取（非交互环境会快速失败）。控制台无工具预选 UI，依赖 LLM 判断，存在不确定性。
- 未复现历史「内容生成格式漂移」与「追加补足锚定漂移」，3 篇文案结构完整。

## 五、改进建议

1. 反思器直接消费 `quality_report`（evaluated/passed/optimized 实测数），禁止 LLM 自行估算 `on_target_passed`；对计数突变（如 3→1→2）做一致性校验，否则以质量门数据为准并触发振荡抑制。
2. replan 必须产出并执行新计划；若 `_collect_outputs` 已判定达标，则应直接 stop 而非先发 replan。
3. 校正 token 估算与 usage.calls 口径；trace 落盘补 status/usage/stopped_reason。
4. planner 聚合同类步骤（`generate_contents` 合并为 count=3）；控制台模型标签改为显示实际模型名。

---

# 补充：实时爬取实测（5 篇西电研究生笔记）

> 时间 2026-08-13（UTC 08-12 17:16:26 创建），run_id `085081e748c6`。
> 目标：爬取 5 篇关于西电研究生的小红书图文笔记并做简要分析，不要发布，不要同步飞书。
> 登录态：`data/raw/xhs_state.json`（18 条 cookies，a1/abRequestId 等过期时间 2026-2027，有效）。
> 证据：`.research/run_085081e748c6.json`、`.research/sse_085081e748c6.log`、`data/agent/runs/085081e748c6.json`。

## 爬取执行轨迹（SSE 实时监控）

```
plan_created   [crawl_notes, analyze_notes]
 1. crawl_notes     执行中，crawl_progress 逐条推进：1/5 → 2/5 → 3/5 → 4/5 → 5/5（kw=西电研究生）
                    tool_completed ok=True：target_count=5, count=5, used_keywords=["西电研究生"], items=[5条]
 2. analyze_notes   step_failed ×2（相同 Pydantic 校验错误，触发 13.4 相同错误早停）
run_completed      ← 实际 analyze 未成功、产出为空，但状态标 completed
```

爬取本身成功（5/5，约 1 分钟），随后分析失败，且 **5 篇爬取结果未持久化到任何文件**（`data/raw/` 无新文件，仅在内存/事件流中存在，被丢弃）。

## 新增缺陷

### 缺陷 7（高）：步骤失败被吞，run 虚报「completed」

[run_manager.py:215-220](app/agent/run_manager.py#L215-L220) 在 `agent.run()` 正常返回后**无条件** `_set_status("completed")`：

```python
self._emit(run_id, "run_stopped" if stopped else "run_completed", {"outputs": result.outputs})
self._set_status(run_id, "stopped" if stopped else "completed")
```

不检查 `result.plan.is_complete`（orchestrator 的 tracer "done" 事件实际给出 `ok/incomplete`）、不看步骤是否 FAILED、不看 outputs 是否为空。本次 plan 为 `[crawl_notes succeeded, analyze_notes failed]`，`is_complete=False`，产出 0 篇，但控制台仍显示「已完成」。用户看到绿色「已完成」+ 空产物，误以为任务成功。这是比缺陷 1 更严重的正确性问题：**失败被静默吞掉**。

### 缺陷 8（高）：爬取数据丢失，analyze 失败后无人接手

5 篇笔记只在 `crawl_notes` 返回与事件流中存在，analyze 失败后 orchestrator 直接收尾，`outputs=[]`、`stopped_reason=None`，数据既未落盘也未进入记忆。爬取成功 = 白爬。

### 缺陷 9（高）：`_coerce_params` 对 record-list 字段不做兜底，工具返回形状不同即崩

[tools.py:420-435](app/agent/tools.py#L420-L435) 对 list 字段只处理**标量列表**（`list[str/int/...]`）；record-list 字段（如 `AnalyzeParams.items: list[NoteItem]`）被注释为「交给 Pydantic」，遇 planner 内联的整个工具结果 dict 时**直接透传** → Pydantic 报 `items Input should be a valid list`。

对照第一轮（load_notes 路径）：`load_notes` 返回**裸 list**，planner 引用后收窄成功；本轮 `crawl_notes` 返回 **dict `{"target_count":..., "items":[...]}`**，planner 把整个 dict 内联为 `items` 参数，无兜底 → 2 次失败。**同一管线对工具返回形状敏感**，与 README 宣称的「宽松收窄/引用容错」不符。

### 缺陷 10（中）：planner 内联工具结果 dict，而非 `$latest.crawl_notes.items` 引用

planner 直接把 crawl 完整结果（含 target_count/count/used_keywords）作为 `items` 传参，而非用引用解析。若用 `$latest.crawl_notes.items`，`_best_effort_dig` 能正确取到列表。planner prompt 对多字段工具返回的引用教学不足。（缺陷 9 修复后可兜住此问题。）

### 缺陷 11（低）：失败信息对用户零透出

analyze 失败后，run 无 `error`、无 `stopped_reason`，控制台轨迹里仅两条 `step_failed`（`run_manager` 转译为 step_failed 事件，但最终状态 completed 会掩盖它）。建议失败步骤的信息至少进入 `AgentResult.stopped_reason` / run.error 并展示。

## 爬取实测改进建议

1. `run_manager` 收尾时依据 `result.plan.is_complete` 与 outputs 判终态：`incomplete`/有 FAILED 步骤 → 状态 `failed`（或新增 `partial`），绝不无条件 completed。
2. `_coerce_params` 扩展：record-list 字段遇 dict 时，取 dict 内首个 list 值（`value["items"]`/`value["notes"]`）作为列表，兜住爬取结果这种包装形状。
3. 爬取成功后若后续步骤失败，将 `crawl_notes` 结果写入 `memory` 或落盘 `data/raw/`，避免数据丢失并支持重试复用。
4. 失败信息透出：FAILED 步骤 error 汇入 run.error / stopped_reason，控制台显示失败原因。

---

# 修复记录（2026-08-13，按优先级从高到低，边实测边修）

> 依据本报告缺陷按优先级修复完成，`pytest tests/` 99 项全过（原 92 + 新增回归 7），并两次真实模型实测（Run A 爬取路径、Run B 生成路径）验证修复方向正确。

## 修复 1（缺陷7/11，高）：终态判定 + 失败透出

- **文件**：[run_manager.py](app/agent/run_manager.py)（`_execute` + 新增纯函数 `_resolve_terminal`）
- **改法**：终态由 `_resolve_terminal(stopped, plan.is_complete)` 决定 —— 用户停止 → `stopped`；计划全 succeeded/skipped → `completed`；否则 → `failed`，并把 FAILED 步骤的 error 汇入 `run.error`、发 `run_failed` 事件。不再无条件 `completed`。
- **实测**：Run A 爬取 5/5 + analyze 成功后状态 `completed`（真实成功正确标绿）；Run A 的失败场景此前标绿属虚报，现逻辑下 analyze failed 会标 `failed` 并带 error（单测 `_resolve_terminal` 覆盖三种分支）。

## 修复 2（缺陷9，高）：`_coerce_params` 对 record-list 字段兜底

- **文件**：[tools.py](app/agent/tools.py)（`_coerce_params`）
- **改法**：`list[NoteItem]` 这类 record-list 字段，遇整个工具结果 dict（如 crawl 返回 `{"target_count":...,"items":[...]}`）时，取 dict 内首个列表值（`items`/`notes`/`results`… 依 `_candidate_keys`+`_LIST_KEYS`）作为列表，不再把 dict 整包透传给 Pydantic。标量 list 分支原逻辑不变。
- **实测**：Run A `analyze_notes` 由「连续 2 次 Pydantic 校验失败」变为 `succeeded`，outputs 产出 analysis_summary/top_keywords/top_tags/title_patterns/insight_points 全有。

## 修复 3（缺陷1/2，高/中）：反思计数对齐质量门，质量全过不再 replan

- **文件**：[executor.py](app/agent/executor.py)（`_quality_passed_count` 新 helper + 反思块 + `_run_step` 时序）、[planner.py](app/agent/planner.py)（REFLECT prompt 已含权威 progress）
- **改法**：
  1. `generate_contents` 成功路径改为**先跑质量门再 `memory.record`**（此前 record 在前，memory 里的结果缺 quality_report/_verdict，反思拿不到实测数据只能靠 LLM 数数 → 3→1→2 振荡根因之一）。
  2. `progress.on_target_passed` 以 `_quality_passed_count`（所有质量门报告 passed 汇总）为准，仅在无质量报告时回退到 `_on_target` 计数。
  3. 防御兜底：质量门已全过（passed ≥ target）而反思仍要 `continue`/`replan` → 强制 `stop` 并发 `reflection_override` 事件，杜绝「3/3 全过还重规划」。
- **实测**：Run B（同一目标，3 篇差异化文案）反思序列 `continue×3 → stop(passed=3)` 单调，**0 次 replan**（修复前 1 次虚假 replan），事件 31→20，LLM 调用 10→8，quality_report `3/3` 与反思 passed=3 一致。override 本次未触发（LLM 已有权威数据直接正确 stop），由单测 `test_executor_overrides_reflector_when_quality_already_passes` 覆盖兜底。

## 修复 4（缺陷8，高）：爬取结果落盘，analyze 失败不丢数据

- **文件**：[executor.py](app/agent/executor.py)（`_persist_crawl_result` 新方法，`_track_crawl_result` 成功后调用）
- **改法**：`crawl_notes` 收集到 ≥1 条即写 `data/raw/crawled_notes_<run_id>.json`，落盘失败仅告警不中断。
- **实测**：Run A 结束后 `data/raw/crawled_notes_f593b2cb5847.json` 存在，含 5 条 items + used_keywords。此前该数据只在内存/事件流中、analyze 失败即丢。

## 修复 5（缺陷4，低）：trace 落盘补 status/usage/stopped_reason

- **文件**：[tracing.py](app/agent/tracing.py)（`save()`）、[orchestrator.py](app/agent/orchestrator.py)（调用传参）
- **改法**：`save()` 从 `done` 事件提取 `status`（ok/incomplete），新增顶层 `usage`/`stopped_reason` 字段。
- **实测**：`data/agent/runs/f593b2cb5847.json` 现含 `status: ok`、`usage`、`stopped_reason`，单独打开即可判断成败与用量。

## 修复 6（缺陷5，低）：planner 聚合同类步骤

- **文件**：[planner.py](app/agent/planner.py)（PLANNER_TEMPLATE 新增规则）
- **改法**：提示词明确「不要为同一目标拆多个同工具步骤，一次 `generate_contents(count=N)` 优于 N 个 count=1」。
- **实测**：Run B planner 只拆 1 个 `generate_contents` 步骤（修复前拆 3 个独立步骤 + 3 次独立质量评审）。

## 修复 7（缺陷6，低）：控制台模型标签显示真实模型

- **文件**：[run_manager.py](app/agent/run_manager.py)（run dict 新增 `model` 字段）、[agent_console.html](static/agent_console.html)（createRun/loadRun 设置 `#modelLabel`）
- **改法**：run 记录 `model = "mock" | settings.openai_model`，控制台不再写死「OpenAI」。
- **实测**：Run B 显示 `model: deepseek-v4-flash[1M]`。

## 回归测试（新增 [tests/test_fix_20260813.py](tests/test_fix_20260813.py)）

覆盖：缺陷7 终态三分支、缺陷9 record-list unwrap + 裸 list 回归、缺陷1 `_quality_passed_count` 汇总/无报告回退、缺陷8 落盘、缺陷1/2 质量全过 override stop。全套 99 passed。

## 遗留（未修，低优先级）

- 缺陷3：`usage.est_tokens` 低估（10 次调用仅计 ~698-1084 tokens），需核对 `OpenAILLMBackend` token 估算公式，本次未动。
- 缺陷10：planner 内联整包 dict 而非 `$latest.crawl_notes.items` 引用 —— 已被缺陷9 兜底，仅剩提示词教学层面，可后续增强。
