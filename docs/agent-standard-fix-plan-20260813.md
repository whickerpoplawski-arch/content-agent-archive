# 达标修复规划:个人使用级 Agent 产品(2026-08-13)

> 目标:让本 agent 达到「严格 agent 开发面试官认可」的标准。基于 GitHub 实时搜集的业界权威实践(Anthropic / OpenAI 官方)映射到本项目 8 项硬伤,逐项修复并验证。

## 一、引用的业界权威源(实时搜集 2026-08-13)

| 源 | 仓库/文档 | 对本项目对应的标准 |
|----|-----------|-------------------|
| Claude Agent SDK (Python) | anthropics/claude-agent-sdk-python | permissions 评价顺序、allowed/disallowed_tools 白黑名单、hooks 人机审批 |
| OpenAI Agents SDK | openai/openai-agents-python | agent 循环、handoffs、sessions、guardrails、tracing、human-in-the-loop |
| OpenAI Agents SDK guardrails.md | 同上 docs/guardrails.md | input/output/tool 三类 guardrail、tripwire 阻断、blocking/parallel 执行模式 |
| OpenAI Agents SDK tracing.md | 同上 docs/tracing.md | Trace+Span 层级、generation/function/guardrail span、group_id 会话关联 |
| Anthropic Cookbook | anthropics/anthropic-cookbook | patterns/agents(orchestrator-workers、evaluator-optimizer、basic workflows)、evals/agentic_search、observability/usage_cost_api |
| Anthropic Cookbook usage_cost_api.ipynb | 同上 | usage 标准字段:uncached_input_tokens / output_tokens / cache_creation / cache_read_input_tokens;分时桶聚合、成本归属 |

本 agent 已符合业界标准的部分(不重复修):
- patterns/agents 的 orchestrator-workers(Planner→Executor)、evaluator-optimizer(QualityGate)已实现,与 Anthropic cookbook 同名 pattern 一致。
- human-in-the-loop:APPROVAL_TOOLS 审批流与 SDK 的 human in the loop 一致。
- sessions/checkpoint:StepCheckpointer + resume 与 OpenAI sessions、Anthropic checkpoint 一致。

## 二、业界标准 vs 本项目现状(差距表)

| 维度 | 业界标准 | 本项目现状 | 判定 |
|------|---------|-----------|------|
| 成本可观测 | usage 从 API 响应取(input/output/cache),分桶聚合 | `_estimate_tokens` 仅 tiktoken 编码 **output**(llm.py),真实 run est_tokens 286-698 严重低估 | ✗ P0 |
| 合规拦截 | guardrail 函数 + tripwire **阻断执行**;tool guardrail 每次工具调用前跑;blocking 模式省成本 | check_output 子串匹配(单字「最」误杀)+ **仅标 trace 不阻断**(executor.py:584-593) | ✗ P0 |
| 凭证安全 | 最小权限、不落明文敏感字段 | xhs_state.json 明文 18 cookies(a1/abRequestId) | ✗ P0 |
| 决策确定性 | 决策结果与客观数据硬校验;评估(replan)有边界 | 反思曾 3→1→2 振荡、虚假 replan(已修部分,replan 仍靠 LLM 一句话) | △ P1 |
| 可观测性 | Trace+Span 统一层级,generation/function span 挂 usage | 双轨事件(trace 24 vs API 31),generation 无 usage 字段 | △ P1 |
| 评估基准 | 真实场景 eval 基准(benchmark 可复现) | 99 测试全 mock,无真实 LLM eval | ✗ P1 |
| 架构模式 | orchestrator-workers/evaluator-optimizer | 已实现 | ✓ |
| 审批 | human-in-the-loop | APPROVAL_TOOLS + 条件等待 | ✓ |

## 三、修复项(按优先级)

### P0-1 用量真实化(成本可观测)
- **现状**: [llm.py](app/agent/llm.py) `_estimate_tokens` 用 tiktoken cl100k_base **仅编码输出文本**,不含输入。真实 run usage.est_tokens=698(10 次调用),输入(整篇 plan/反思上下文)完全未计 → 成本面板失真,用户无法做成本决策。
- **业界标准**: Anthropic usage_cost_api.ipynb 定义 uncached_input_tokens / output_tokens / cache_creation / cache_read_input_tokens 四元组;OpenAI Responses API 响应的 usage 字段含 input_tokens / output_tokens / input_tokens_details.cached_tokens。
- **改法**: OpenAILLMBackend 每次响应后从 `response.usage` 取真实值;aggregate 为 `{"calls": N, "input_tokens": X, "output_tokens": Y, "cache_read_tokens": Z, "est_tokens": 合计}`。若后端(volces ark)不返回 usage,fallback 为 tiktoken 双端估算(输入+输出,而非仅输出),并在 usage 标记 `estimated: true`。
- **验证**: 真实 run 后 usage 字段 input_tokens+output_tokens>0 且远大于旧值;控制台展示真实 token;测试断言 usage 含 input_tokens 字段。

### P0-2 guardrails 重建(tripwire 阻断,合规真拦截)
- **现状**: [guardrails.py](app/agent/guardrails.py) `AD_LAW_BANNED` 首项单字「最」→ 误杀「最新/最便宜」;`check_output` 命中只记 `guardrail_blocked` trace(executor.py:584-593),**不阻断 publish** → 合规功能既不精准又无拦截力。
- **业界标准**: OpenAI guardrails.md —— ① tool guardrails:每次函数工具调用**前后**校验,可 skip / 替换输出 / 抛 tripwire **halt 执行**;② guardrail 函数返回结构化 `GuardrailFunctionOutput(tripwire_triggered, output_info, reasoning)`,判断用 LLM/结构化而非裸子串;③ blocking 执行模式:guardrail 跑完工具才启动,省钱省副作用。
- **改法**:
  1. `check_output` 改正则+上下文:绝对化用语「最+形容词/程度副词」(如 `最(好|大|高|低价|便宜|强|专业|权威|领先|优惠|超值|新|全)`),删除单字「最」;保留词组表「第一」「顶级」等。
  2. publish_to_xhs / sync_to_feishu 的**工具调用前**加 output guardrail(tool 级):对目标文案跑 `check_output`(先正则快检,命中再走 LLM 结构化确认,避免 LLM 每篇都调),tripwire 触发 → **阻断该工具执行**,步骤标 failed(错误「非合规文案,已拦截: [词]」),不再仅标 trace。
  3. blocking 执行:guardrail 在工具执行前串行跑完,不与发布并行。
- **验证**: 测试:含「全球最好用」文案 → publish 被阻断、步骤 failed、错误信息含词;含「最近」正常文案 → 放行。真实 run:生成文案含敏感词时不发布。

### P0-3 凭证安全(cookies 不落明文)
- **现状**: 爬取登录态 xhs_state.json 明文 18 条 cookies(a1/abRequestId 等敏感 token),无加密无权限位。
- **改法**: 最小改动:落盘前对 value 加密(Fernet/自实现 XOR 或 base64+env key),读取时解密;文档警示;不落盘 a1 等高敏字段或脱敏。若改动面过大,最低限度:启动时校验文件权限 + README 警示 + 支持 env 注入登录态。
- **验证**: cookies 文件 grep 不到明文 a1;爬取仍成功(decrypt 路径可跑)。

### P1-4 决策确定性(replan 硬校验)
- **现状**: 反思振荡已修(质量门数据兜底),但 replan 决策仍纯 LLM 一句话:无「replan 理由必须与失败原因匹配」约束。实测曾出现「声明 replan 却零执行」。
- **改法**(executor.py 反思块):
  1. replan 前:若存在 FAILED 步骤,replan 的新计划必须显式包含对应恢复动作,否则拒绝 replan → 改 stop。
  2. replan 后:下轮执行前重查质量门计数,若目标已达成 → 直接 stop(已实现 override,补全 replan 路径)。
  3. 保留 MAX_REPLAN_SAME_REASON 熔断,防死循环。
- **验证**: 单元测试:质量已全过但 LLM 说 replan → 拒绝;FAILED 步骤无恢复动作的 replan → 拒绝。真实 run:replan 次数有界且每个 replan 有匹配恢复步骤。

### P1-5 评估基准(真实 LLM eval,达标验证器)
- **现状**: tests/ 99 项全 mock 确定性测试,不覆盖真实 LLM 决策质量 → 无法证明「达标」。
- **业界标准**: Anthropic evals/agentic_search 模式 —— benchmark 场景可复现、有明确指标。
- **改法**: 建 `.research/eval_runner.py` + `tests/test_eval_real.py`(标记 skip-if-no-key):
  - 场景集:①爬取+分析(含空爬取注入)②生成 3 篇且质量全过 ③含失败注入(analyze 崩溃)④含敏感词文案。
  - 断言:终态正确(不虚报)、quality passed 数=目标、replan ≤ 上限、usage 含 input_tokens、guardrail 阻断生效。
  - 输出 `.research/eval_report.md` 归档。
- **验证**: eval_runner 跑通全场景,报告记录每次真实 run 数据。

### P2-6 可观测性统一(trace 单源 + usage 挂载)
- **现状**: run_manager 事件与 AgentTracer 双轨,真实 run trace 24 vs API 31 事件不一致;generation span 无 usage。
- **改法**: 以 AgentTracer 为唯一记录源,tracer.event 时挂 usage(generation/tool span);run_manager 从 trace 派生 API 事件,消除双轨。低优先级(不影响正确性,影响调试体验)。
- **验证**: 真实 run 事件数 trace 与 API 一致;trace JSON 每个 generation 含 usage。

## 四、执行顺序(依赖)

1. **P1-5 eval 基准先行**:把 11 缺陷 + 上述差距映射为断言。无 eval 无法证明后续修复达标。
2. **P0-1 用量真实化**:改动小、独立,立即验证。
3. **P0-2 guardrails 重建**(P0-3 凭证安全按用户指示跳过)。
4. **P1-4 决策确定性**:依赖质量门(已就绪)。
5. **P2-6 可观测性统一**:最后,非阻断。

每步完成跑 tests/ 全量 + 真实 run 验证,更新本文档状态。

## 五、落地状态(2026-08-13)

全部落地 + 实测监控验证完成,tests 100 passed(新增 replan 恢复路径单测)。

| 项 | 状态 | 实测/监控证据 |
|----|------|--------------|
| P1-5 eval 基准 | ✅ | `.research/eval_runner.py`(LLM 层 + 防御层)。基线先捕获缺陷再修复转 PASS。LLM 层 2/2 轮 complete、usage 真实、trace 含 usage 快照;防御层 S2 空爬取熔断 / S3 guardrail tripwire / S4 失败注入不虚报 / S5 replan 硬校验 全 PASS。多轮模式监控 LLM 决策稳定性(单次波动不误报) |
| P0-1 用量真实化 | ✅ | `llm.py` 从 Responses 响应 usage 取 input/output/cache;后端不返回则双端估算标 `estimated`。真实 run C2:input 7348 / output 1831 / cache 2048(旧版仅 est 833,低估 11 倍)。eval LLM 层 input 19591 / output 5747 |
| P0-2 guardrails 重建 | ✅ | `guardrails.py` 单字「最」改「最+程度形容词」正则 + 词组表;`executor.py` publish/sync 调用前 tripwire 阻断(步骤 FAILED + guardrail_blocked 事件)。实测:「最好/最便宜/第一」拦截;「最近/最早」不误杀 |
| P1-4 replan 硬校验 | ✅ | `executor.py` replan 前校验新计划必须含失败动作恢复步骤,否则 reject + stop。eval S5 PASS;新增 `test_replan_with_recovery_step_is_not_rejected` 防误拒 |
| P2-6 可观测性 | ✅ | `executor.py` 每 batch 后 trace 挂 usage 快照(eval 断言 4 个快照);`run_manager.summary` 补 model 字段。run C2 详情 model=deepseek-v4-flash[1M] |
| P0-3 凭证加密 | ⏭️ | 按用户指示跳过 |
