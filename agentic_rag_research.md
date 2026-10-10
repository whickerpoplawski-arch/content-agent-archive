# 智能体化 RAG 框架（Agentic RAG）实时调研报告

> 调研日期：2026-09-15｜方法：arXiv 实时检索 + GitHub 开源生态 + 官方文档，多轮交叉验证
> 信息新鲜度：主要素材集中于 2025-09 ~ 2026-09，重点前沿论文来自 2026 年 8–9 月

---

## 1. 定义与背景

**Agentic RAG（智能体化 RAG）** = RAG 的检索/生成流水线由 LLM Agent 自主编排：不再是"一次检索 → 一次生成"的固定管线，而是把 `检索 → 评估 → 再检索 → 推理 → 回答` 变成可控的循环决策过程。LLM 自主决定：什么时候检索、检索什么、检索结果是否够用、要不要多跳追问、要不要调用工具、何时停止并给出答案。

驱动因素：
- 复杂多跳问题需要多轮检索，单步 RAG 覆盖率不足；
- 固定 pipeline 无法判断"证据是否充分"，容易出现次优召回或幻觉；
- Agent 技术（ReAct、tool calling、记忆）成熟后，检索被自然纳入 Agent 的行为空间。

---

## 2. 演进脉络（Naive → Advanced → Modular → Agentic）

| 阶段 | 特征 | 代表 |
|---|---|---|
| Naive RAG | 固定切块、一次向量检索、拼接生成 | 早期 E5/BGE 方案 |
| Advanced RAG | 预检索优化、混合检索、重排（rerank） | BM25+向量、Cohere/Voyage reranker |
| Modular RAG | 检索模块可插拔、可组合（路由/融合/重写） | [RAG Survey, arXiv:2312.10997](https://arxiv.org/abs/2312.10997) |
| **Agentic RAG** | LLM 作为控制中枢，决策式多步检索、自我反思、工具调用 | Self-RAG / CRAG / Adaptive-RAG → 2025-2026 系统 |

**关键里程碑论文（已逐一核对 arXiv 原文）**：
- **Self-RAG**（arXiv:2310.11511，2023-10）：学习"检索-生成-自我批判"的反思令牌（reflect tokens），模型自主决定是否检索、检索结果是否相关/被支持。
- **CRAG / Corrective RAG**（arXiv:2401.15884，2024-01）：检索质量差时触发纠正动作——如 Web 搜索兜底、知识库精炼。
- **Adaptive-RAG**（arXiv:2403.14403，2024-03）：按问题复杂度自适应路由——简单问题跳过检索、中等问题单轮检索、复杂问题多轮迭代。
- **RAG 全景综述**（arXiv:2312.10997，2023-12）：提出 Naive/Advanced/Modular 三范式图谱，是 Modular RAG 概念的主要出处。
- **From RAG to Agentic RAG**（arXiv:2506.15911、arXiv:2601.07528，2025-2026）：直接印证"从 RAG 走向 Agentic RAG"的演进主线（伊斯兰问答/医学验证领域）。

---

## 3. 核心技术机制（2026 最新论文支撑）

以下论文均为 2026 年新作，摘要已在 arXiv 实时核对：

### 3.1 检索决策与"何时停止"
- **MetaRAG**（arXiv:2608.24214，2026-08）：把"是否继续搜索 vs 直接回答"建模为 belief-action aligned 策略优化；核心创新是显式建模 Agent 内部"证据是否充分"的信念，摆脱纯外部监督。
- **Adaptive-RAG**（上文）：按复杂度自适应检索预算。

### 3.2 多跳推理与轨迹学习
- **GTA-RAG**（arXiv:2608.22479，2026-08）：Graph-Trajectory-Augmented RL——用图结构扩充多轮检索轨迹，强化学习训练多轮检索-推理 Agent。

### 3.3 记忆结构
- **AgenticRag-R1**（arXiv:2608.29622，2026-08）：引入 **Stack Memory（栈式记忆）**，支持多步推理、检索与记忆的循环、以及奖励驱动的 RL 训练。
- **GraMRAG**（arXiv:2609.14066，2026-09，最新）：多智能体 + 图记忆（Graph Memory）+ 强化学习，用记忆图解决多模态复杂推理中的"状态盲区"。
- **MimirRAG**（arXiv:2605.25030，2026-05）：多智能体 RAG + 元数据集成，面向金融数据检索。

### 3.4 强化学习驱动的 Agentic RAG（2026 主流趋势）
多篇 2026 论文把 **RL（常配 GRPO/策略优化类算法）** 用于端到端训练检索 Agent：
- AgenticRag-R1、GTA-RAG、MetaRAG、GraMRAG 均属此路线；
- 趋势判断：2026 年"检索策略本身可被 RL 训练"成为 Agentic RAG 最重要的研究主线之一。

### 3.5 多智能体协作
- **MA-RAG**（arXiv:2608.28624，2026-07）：多智能体纵向时序摘要（帕金森临床评估），角色分工 + 校验。
- **SpecMind**（arXiv:2609.00427，2026-09）：多智能体混合检索做频谱智能。
- **多智能体 RAG for Telecom SNOC**（arXiv:2609.01618，2026-09）：云知识库多智能体检索（运营商生产环境案例）。

### 3.6 可靠性：失败归因与信任
- **AgenticRag-FP / Causal Failure Attribution**（arXiv:2608.20627，2026-08）：干预式基准，研究"第 1 跳检索错误如何传染到第 3 跳答案"的因果归因——这是 Agentic RAG 与单体 RAG 的本质差异（错误跨跳传播）。
- **Trustworthy Graph-Agentic RAG for Social Good**（arXiv:2609.06391，2026-09）：图智能体 RAG 的架构、失败传播与"assurance by construction"保证。

### 3.7 工具/数据源路由
- **SchemaRouter**（arXiv:2608.21375，2026-08）：异构 Agentic RAG（外部 API / 内部库 / 向量库 / 图库）的 field-aware 工具路由，缓解 over-fetching 与 token 浪费。

### 3.8 低延迟/高效化
- **LatentRAG**（arXiv:2605.06285，2026-05）：潜在空间推理+检索，压缩 Agentic RAG 的推理开销。

---

## 4. 代表性框架与平台（GitHub 实时星数 @2026-09-15）

| 框架 | 星数 | 定位 |
|---|---|---|
| [LangChain / langchain](https://github.com/langchain-ai/langchain) | 146k | Agent 工程平台，RAG 组件最全 |
| [LangGraph](https://github.com/langchain-ai/langgraph) | 41.7k | 图状态机编排 Agentic 检索循环 |
| [LlamaIndex](https://github.com/run-llama/llama_index) | 52.2k | 文档处理平台，提供 `AgentWorkflow` / `FunctionAgent` 预置 Agentic RAG |
| [Dify](https://github.com/langgenius/dify) | 155.8k | Agentic workflows + RAG 一体化的低代码平台 |
| [RAGFlow](https://github.com/infiniflow/ragflow) | 90.7k | 深度文档理解的开源 RAG 引擎 |
| [Haystack](https://github.com/deepset-ai/haystack) | 26.5k | "context-engineered" 的生产级编排框架 |
| [LightRAG](https://github.com/HKUDS/LightRAG) | 39.7k | EMNLP2025，简单快速的图增强 RAG |
| [GraphRAG (Microsoft)](https://github.com/microsoft/graphrag) | 36.0k | 模块化图结构 RAG |
| [mem0](https://github.com/mem0ai/mem0) | 65.3k | Agent 记忆层（RAG+记忆结合） |
| [claude-mem](https://github.com/thedotmack/claude-mem) | 93.9k | Agent 跨会话持久上下文 |
| [anything-llm](https://github.com/Mintplex-Labs/anything-llm) | 66.0k | 本地知识库 + Agent |
| [awesome-llm-apps](https://github.com/Shubhamsaboo/awesome-llm-apps) | 138.3k | 100+ Agent/RAG 开源应用合集 |

**框架层判断（2026-09）**：
- 事实标准组合 = **LangGraph（编排）+ 向量库（Milvus/Qdrant/pgvector）+ Reranker**；
- LlamaIndex 官方把 Agentic RAG 收敛为 `AgentWorkflow` 预置工作流（FunctionAgent / Multi-agent 两种形态）；
- Dify/RAGFlow 走"低代码 + 企业知识库"路线，覆盖非工程师用户；
- Haystack 强调 context engineering（上下文工程），定位生产级 LLM 应用。

---

## 5. 评估与基准

- **Q2D-Web**（arXiv:2609.08887，2026-09，Perplexity 团队）：首个大规模 Agentic RAG 检索基准——190M 文档网页语料 + 7 万条 Agent 改写搜索查询（10 种语言）+ 3 套关联判定（agent 引用 / 生产排序 / 联合+LLM 标注），基准测试 13 种检索器（lexical / dense / late-interaction）。在线榜单：huggingface.co/spaces/perplexity-ai/q2d-web-leaderboard。
- 核心结论：**Agent 改写查询的分布与人类查询不同**，传统基准会误导 Agentic 系统的检索器选型。

---

## 6. 在 Agent 开发中的应用（重点）

1. **工具型检索 Agent**：把检索器封装为 tool，由 ReAct/Function-Calling Agent 按需多次调用（LangGraph `create_retriever_tool` 模式）。
2. **记忆型 Agent**：检索过去会话/事实作为上下文——Agentic RAG + 记忆（mem0、Stack Memory、图记忆）。
3. **多跳问答与代码定位**：BLAgent（arXiv:2605.17965，2026-05）用 Agentic RAG 做文件级 Bug 定位。
4. **企业数据集成**：Naïve → Agentic RAG 的信任+成本权衡（arXiv:2607.22319，2026-07）；MimirRAG 金融数据；跨库异构工具路由（SchemaRouter）。
5. **可靠性治理**：用 AgenticRag-FP 式因果归因做故障注入测试；用 Graph-Agentic RAG 保证可审计（合规：EU Cyber Resilience Act 工业案例 arXiv:2608.19509）。
6. **自动评估**：Agent 驱动的评测框架（RAGAS 思路 + Q2D-Web 级基准），把检索 Agent 的轨迹纳入评测。

---

## 7. 前沿趋势（截至 2026-09）

- **RL 训练检索策略**：检索 Agent 从"提示词驱动"走向"强化学习驱动"（AgenticRag-R1 / GTA-RAG / MetaRAG / GraMRAG）；
- **多智能体 RAG 进入垂直行业**：金融、医疗、运营商、频谱、网络安全均有 2026 年系统论文；
- **记忆结构丰富化**：栈式 / 图式 / 层级时序记忆成为标配；
- **可靠性成为一等公民**：因果失败归因、assurance by construction、评估基准（Q2D-Web）同步演进；
- **检索器为 Agent 重塑**：面向"机器产生查询"优化首段检索（Q2D-Web 揭示的分布差异）。

---

## 8. 参考资源

- RAG Survey：https://arxiv.org/abs/2312.10997
- Self-RAG：https://arxiv.org/abs/2310.11511
- CRAG：https://arxiv.org/abs/2401.15884
- Adaptive-RAG：https://arxiv.org/abs/2403.14403
- 2026-08/09 前沿论文（均可在 arxiv.org/abs/<ID> 查证）：MetaRAG `2608.24214`、GTA-RAG `2608.22479`、AgenticRag-R1 `2608.29622`、GraMRAG `2609.14066`、Q2D-Web `2609.08887`、SchemaRouter `2608.21375`、AgenticRag-FP `2608.20627`、LatentRAG `2605.06285`、BLAgent `2605.17965`、MimirRAG `2605.25030`
