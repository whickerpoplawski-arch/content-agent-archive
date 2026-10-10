# 记忆体系与 RAG 知识库选型评估（2026-09-10）

> 目标：agent 既能搜集信息又能作为知识问答助手，要有自己的 RAG 库。
> 方法：高星开源项目记忆架构调研 → 全部一手来源独立校验（GitHub API / README 原文 / 本机实测），不采信二手转述 → 评估矩阵 → 选型 → 落地规划。

## 1. 候选调研结果（一手校验）

### 1.1 专用记忆层

| 项目 | stars* | 记忆架构核心 | 必需依赖 | 嵌入式可行 | 结论 |
|---|---|---|---|---|---|
| **Mem0** | 65,054 | 2026-04 新算法：单遍 ADD-only 抽取（一次 LLM 调用，无 UPDATE/DELETE）+ 实体抽取链接 + **多信号检索（semantic + BM25 + entity 并行融合）** + 时序推理 | LLM 必经；向量库；`[nlp]` 还要 spacy | 库模式可，但依赖链极重 | ❌ 不引入。**借鉴其多信号融合检索** |
| **Letta (MemGPT)** | 24,689 | core/archival/recall 分层 + agent 自主记忆编辑 | 独立 server / npm CLI | ❌ 已产品化，V1 server 归档停止维护 | ❌ 排除 |
| **Graphiti (Zep)** | 30,758 | 时序知识图谱（bi-temporal） | Neo4j 5.26 / FalkorDB（图库进程） | ❌ 需图服务 | ❌ 排除 |

*stars 为 2026-09-10 GitHub API 实测。

Mem0 README 原文（关键借鉴点）：
> "Single-pass ADD-only extraction — one LLM call, no UPDATE/DELETE."
> "Multi-signal retrieval — semantic, BM25 keyword, and entity matching scored in parallel and fused."
> 基准：LoCoMo 71.4→92.5（其托管平台数据，开源版方向相近）。

### 1.2 框架级记忆

| 框架 | 模式 | 可借鉴点 |
|---|---|---|
| **LangGraph** | semantic/episodic/procedural 三分桶；**profile（单文档持续更新）vs collection（文档集合）** 权衡：collection 召回更高但更新复杂、profile 上下文全但易错 | 本项目 profile.json 已是三分桶形态，验证了方向；新增知识库应走 **collection** 路线（"a document collection tends to lead to higher recall downstream"） |
| **Agno** | memory 与 knowledge(RAG) 分离；knowledge 由工具结果/文档写入再检索 | ❌ 已平台化（Docker/Postgres），只借分离思想 |
| **CrewAI** | 单一 Memory API：写入时 LLM 推断 scope/importance；**composite 评分召回（semantic + recency + importance 加权，recency 半衰期可调）** | 借鉴 composite 评分：本项目已有 recency×hits×quality 加权（`_lesson_weight`），方向一致 |

### 1.3 向量存储 + 嵌入（本机实测）

| 组件 | 实测结果 | 结论 |
|---|---|---|
| **sqlite-vec** 0.1.9 | `pip install sqlite-vec` 后本机 KNN 虚拟表跑通（3 维验证 + 版本确认） | ✅ 选定。零服务、零进程、SQLite 单文件，与现有 data/ 目录形态一致 |
| Chroma 1.5.9 | 嵌入式可用但**默认嵌入函数 = all-MiniLM-L6-v2（英文）**，中文必须自定义 EF；依赖 31 个（onnxruntime/grpcio/kubernetes 等） | ❌ 不引入。依赖收益比差 |
| LanceDB 11.4k | Rust 嵌入式，本机未测 | 备选，sqlite-vec 已满足 |
| **bge-small-zh-v1.5**（Xenova ONNX 导出，91MB） | 本机 CPU（onnxruntime 1.27）推理：**512 维，4 条/120ms**；中文语义检索 sanity 4/4 命中（"大学生开学要买哪些平价好物"→"学生党必入的5款平价好物推荐" sim=0.811） | ✅ 选定。模型已落盘 `data/models/` |
| ustc 网关 embedding API | **/v1/embeddings 全部 403**（key 无 embedding 模型权限，21 个可用模型无一 embedding 类） | 嵌入必须本地算，API 路线不可行 |
| jieba + rank_bm25 | 中文 BM25 分词+评分跑通 | ✅ 混合检索关键词路 |

## 2. 选型结论

**自研轻量混合记忆体系**（不引入任何重依赖记忆框架）：

- **向量存储**：sqlite-vec 虚拟表（单文件 `data/agent/knowledge.db`）
- **嵌入**：bge-small-zh-v1.5 本地 ONNX（onnxruntime CPU，已有 91MB 模型）
- **检索**：向量 KNN + BM25 双路召回，RRF（Reciprocal Rank Fusion）融合 —— 抄 Mem0 多信号融合
- **记忆分桶**：沿用现有 episodic/procedural/procedural_insights（LangGraph 三分桶思想已落地），新增 **knowledge** 桶 = RAG 知识库（collection 路线，LangGraph 结论：召回优于 profile）
- **评分**：召回结果带 recency 半衰期加权（CrewAI/现有 `_lesson_weight` 一致）

架构定位（两级）：
```
┌─ 记忆层（结构化，现有 profile.json，保持不变）
│   episodic / procedural / procedural_insights / user_facts
│   检索: 主题 overlap 召回（已有）
└─ 知识层（非结构化 RAG，新增 knowledge.db）
    来源: ① 爬取的笔记(自动) ② 分析报告/生成产物(自动) ③ 手动喂文档(API)
    检索: bge 向量 KNN + BM25 → RRF 融合 → top-k 注入 prompt 或问答接口
```

## 3. 落地规划

| # | 任务 | 验收标准 | 预估 |
|---|---|---|---|
| **R1** | `app/agent/embedder.py`：bge ONNX 封装（CLS pooling + L2 norm，批处理） | 单测：单条/批量嵌入 512 维、归一化正确、确定性一致；CPU 10 条 <500ms | 0.5 天 |
| **R2** | `app/agent/knowledge.py`：sqlite-vec 知识库（add/search/delete，chunk 切分，RRF 融合向量+BM25） | 单测：写入 20 条中文文档后语义+关键词双路都能召回正确文档；RRF 排序合理；删除生效 | 1 天 |
| **R3** | 数据源接线：① crawl_notes 结果自动入知识库（executor 钩子，复用 `_persist_crawl_result` 时机）② analyze/outputs 自动入 ③ `POST /agent/knowledge` 手动喂文档 API | 单测：mock run 完成后 knowledge.db 有对应条目；API 喂入可检索 | 0.5 天 |
| **R4** | 知识问答：`GET /agent/knowledge/search?q=` 检索端点 + planner/reflect prompt 注入相关历史知识（复用 `_profile_text` 模式） | 实战：喂入 5 篇真实爬取笔记后，用自然语言问"宿舍收纳有什么技巧"能返回对应笔记 | 0.5 天 |
| **R5** | 实战端到端验收 + 文档 | 真实 LLM run：爬取→自动入库→知识问答闭环；README/IMPROVEMENT 更新；全量 pytest 过 | 0.5 天 |

总计约 3 天（文档估）。每步落地后按验收标准实测（单测 + 真实运行），不通过不进下一项。

---

## 4. 落地结果（2026-09-10 全部完成）

| # | 状态 | 实现 | 实战验收证据 |
|---|---|---|---|
| R1 | ✅ | [embedder.py](../app/agent/embedder.py)：bge-small-zh ONNX、固定 512 grid 批处理（batch 无关确定性）、query instruction 前缀 | 7 单测过；修掉两个真 bug：tokenizer `enable_padding` 与手写 mask 叠加致 pad token 泄漏（batch 组合影响向量）、fixed grid 解决 |
| R2 | ✅ | [knowledge.py](../app/agent/knowledge.py)：documents+chunks+chunks_vec 三表、滑动窗口 chunk（句界回退）、RRF(k=60) 融合、source 过滤 | 11 单测过（语义召回/关键词召回/RRF 双信号优先/删除/元数据）；本机 19 篇真实爬取笔记入库后 3 组自然语言问答全部命中正确笔记 |
| R3 | ✅ | executor crawl 钩子（url 哈希 doc_id 幂等）+ orchestrator outputs 钩子（goal+内容哈希幂等）+ `POST /agent/knowledge`、`GET /agent/knowledge/search`、`GET /agent/knowledge` | 实测：重复 ingest 计数不增（幂等 ✓）；mock 后端跳过写入（全量 pytest 后真实库计数不变 ✓，此前曾污染 6 条已清理）；真实 LLM run 后 knowledge 47→50 自动入库 |
| R4 | ✅ | `_knowledge_context` 检索 top-4 注入 planner prompt（store 异常时静默跳过） | 实测：goal「开学宿舍收纳好物」注入 695 字符相关知识（含已生成文案+历史爬取笔记） |
| R5 | ✅ | 见下方端到端实录 | |

### 端到端实战实录（真实 LLM deepseek-v4-flash-ascend）

1. 真实爬取的 19 篇西电研究生笔记经 API 喂入 → `GET /agent/knowledge` = 19
2. 问答「西电研究生秋招怎么准备」「宿舍收纳有什么技巧」「研究生科研生活是什么样的」→ 全部召回对应笔记（`[crawl]` 命中）
3. 真实 LLM run（开学宿舍好物 goal）→ 2 篇文案 + 分析摘要**自动入库**（47→50）
4. 问答「桌面很乱怎么收纳」「备考自习室要买什么」→ **第一命中均为刚自动入库的 output 文案**（`★AUTO-INGESTED`）
5. 下次 run 的 planner prompt 已含上批产物知识（注入实测 695 字符）→ 检索→生成→回灌→再检索 学习闭环成立

最终测试规模：147 pytest 全过（embedder 7 + knowledge 11 + wiring 8，共 +26）。
