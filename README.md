# XHS Content Agent — 小红书 AI 内容助手

基于 FastAPI + LangChain + DeepSeek 构建的小红书内容挖掘、生成与发布系统，并内置一个**真正的自主 Agent 编排核心**（`app/agent/`）。既支持从爬取竞品数据、分析爆款规律、AI 生成文案与配图到一键发布小红书的确定性闭环，也支持「目标 → 规划 → 执行 → 反思 → 追踪」的自适应 Agent 循环。

## 项目简介（三大亮点）

1. **真正的自主 Agent，不是写死的三步流程**：`app/agent/` 实现目标 → 规划 → 执行 → 反思 → 追踪的自适应循环，内置评估-优化质量门（草稿 LLM 评分不达标自动重写）、跨轮次长期记忆（经验写入 `profile.json` 注入下次规划，越用越懂目标用户）、成本/步数预算护栏（超限自动停机，Agent 不会跑飞）。
2. **从数据到发布的端到端确定性闭环**：Playwright 爬取竞品 → 结构化爆款分析 → LLM 选题 → 文案生成 → gpt-image-1 配图 → 本地 MCP 服务一键发布小红书 → 飞书多维表格同步，一条流水线打通「内容挖掘到内容上线」全链路，并可封装为 MCP 工具供 Claude Desktop / Cursor 直接调用。
3. **可观察、可控制、可测试的工程化底座**：Web Agent 控制台 SSE 实时展示规划与工具调用轨迹；发布/同步等副作用需人工显式审批；checkpoint 断点续跑、运行痕迹持久化与 TTL 清理、150 个 pytest 测试覆盖核心行为，真机实测暴露的缺陷均有回归测试锁定。

> 本 README 与代码实况同步更新于 2026-09-15。近期完成的升级：**RAG 知识库（自研轻量混合检索）+ 爬取风控规避**（2026-09-10，详见 `docs/memory-rag-selection.md` / `docs/crawl-anti-detection.md`）；**Agentic RAG + 上下文感知检索落地**（2026-09-15，见下文「Agentic RAG 升级」小节与两份调研 `agentic_rag_research.md` / `context_aware_retrieval_research.md`）。

---

## 如何拉起本地 Agent

> LLM 配置会**自动读取 Codex**：`~/.codex/config.toml` 的 `model`/`base_url` + `~/.codex/auth.json` 的 `OPENAI_API_KEY`，与 Codex 完全一致，无需重复填写。环境变量 / `.env` 优先级更高，可覆盖。

### 前置：装依赖（仅首次）

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows；Linux/macOS 用 source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

**RAG 嵌入模型（91MB，未随仓库提供，需手动下载）**：知识库检索依赖本地 ONNX 模型，克隆后需下载 `bge-small-zh-v1.5` 放到 `data/models/`：

```
data/models/
├── bge-small-zh-v1.5.onnx    # 从 HuggingFace 下载 Xenova/bge-small-zh-v1.5 的 onnx/model.onnx 并改名
├── tokenizer.json
└── vocab.txt
```

下载地址：<https://huggingface.co/Xenova/bge-small-zh-v1.5/tree/main>（取 `onnx/model.onnx` → 重命名为 `bge-small-zh-v1.5.onnx`，以及 `tokenizer.json`、`vocab.txt`）。

> 未下载模型时：服务与确定性流水线仍可运行，仅 RAG 知识库相关功能会报 `FileNotFoundError`，`tests/test_embedder.py` 自动 skip。

### 方式一：Web 服务 + Agent 控制台（推荐）

```bash
# Windows 强烈建议先开 PYTHONUTF8=1，避免中文/特殊字符触发 gbk 控制台编码崩溃
set PYTHONUTF8=1
python run.py                    # 等价于 uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
# 长跑/实测建议关闭自动 reload：set XHS_RELOAD=0（避免改文件触发反复重启）
```

服务起来后：

- **Agent 控制台**（实时 SSE 流式观察 规划/执行/反思/追踪）：http://127.0.0.1:8000/static/agent_console.html
- Web UI：http://127.0.0.1:8000
- API 文档：http://127.0.0.1:8000/docs

在控制台里填目标（如「为西电学生生成 3 篇暑期实习分享文案」）、选 backend（`openai`=真实模型 / `mock`=离线），点运行即可看到完整 Agent 轨迹。

### 方式二：纯命令行 Agent 演示（不启动服务）

```bash
python run_agent_demo.py --backend mock "为大学生女生生成3篇小红书种草文案"   # 离线，无需 API key
python run_agent_demo.py --backend openai "为大学生女生生成3篇小红书种草文案" # 真实模型
```

### 方式三：MCP Server（给 Claude Desktop / Cursor 调用）

```bash
python mcp_server.py              # 把完整流水线 + Agent 封装为 MCP 工具
```

### 登录态（爬取 / 发布前必做，否则相应功能会快速失败）

- **爬取**：Playwright 读取 `data/raw/xhs_state.json`（storage_state）。首次在交互终端运行 `python test_crawler.py`，弹出浏览器手动登录后按 Enter，登录态自动落盘，后续启动自动复用、跳过登录。非交互环境（Agent 自主运行 / 后台任务）未登录时**不会阻塞等待**，而是快速失败并提示先完成登录。
- **风控规避**：爬取内置人类化节流（详情页间隔 3-6s、关键词冷却 8-15s、每 8 页长休息 15-30s）与指纹随机化（viewport/UA/locale/timezone），全部可经 `.env` 调整，详见 `docs/crawl-anti-detection.md`。
- **发布**：依赖本地小红书 MCP 服务（默认端口 `18060`，需自行下载 [xiaohongshu-mcp](https://github.com/xpzouying/xiaohongshu-mcp) 二进制放入 `tools/xiaohongshu-mcp/`）。`.env.example` 中 `XHS_MCP_BINARY` 已给出默认路径；可双击运行该二进制，或通过 `POST http://127.0.0.1:8000/xhs-service/start` 启动，再 `POST /xhs-service/login` 弹窗扫码登录。

---

## 功能概览

| 模块 | 说明 |
|------|------|
| 数据采集 | 通过 Playwright 爬取小红书搜索结果，支持按关键词、评论数、点赞数、收藏数过滤，自动跳过视频与广告 |
| 数据分析 | 提取高频关键词、热门标签、标题规律与用户洞察，输出结构化分析报告 |
| 话题生成 | 基于分析结果，调用 LLM 生成若干高质量选题建议（含标题与理由） |
| 内容生成 | 针对每个选题生成多条完整文案：正文、标题、话题标签、互动引导语、图片建议、内容类型 |
| 图片生成 | 调用 OpenAI `gpt-image-1` 生成符合小红书风格的配图，保存至本地 |
| 内容发布 | 支持 MCP 协议（推荐）或 REST API 两种模式发布至小红书 |
| 飞书同步 | 将爬取数据与 AI 生成内容分别同步至飞书多维表格，便于团队协作与审核 |
| RAG 知识库 | 本地中文混合检索（bge-small-zh 向量 + BM25 → RRF 融合，sqlite-vec 单文件存储）：爬取笔记/分析报告/生成文案自动入库，支持自然语言知识问答（`GET /agent/knowledge/search?q=`），相关历史知识自动注入规划 prompt，选型见 `docs/memory-rag-selection.md` |
| 自主 Agent | 目标驱动：规划器拆解目标 → 执行器调用工具 → 反思器决定继续/重规划/停止 |
| Agent 控制台 | Web 端实时观察规划与工具调用、审批发布类副作用、停止运行、回看历史 |
| MCP Server | 将完整流水线与 Agent 封装为 MCP 工具，可在 Claude Desktop / Cursor 等 AI 工具中直接调用 |

---

## 快速开始

### 1. 安装依赖

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt
playwright install chromium
```

### 2. 配置环境变量

复制 `.env.example` 为 `.env` 并填写必要字段（`.env` 已被 gitignore，不会误提交）：

```env
# OpenAI / LLM
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4o-mini
OPENAI_BASE_URL=                  # 可选，代理地址

# 图片生成
IMAGE_MODEL=gpt-image-1

# 飞书多维表格（可选）
FEISHU_APP_ID=
FEISHU_APP_SECRET=
FEISHU_APP_TOKEN=
FEISHU_TABLE_ID=                  # 爬虫数据表
FEISHU_PUBLISH_TABLE_ID=          # AI 生成笔记表

# 小红书 MCP 服务（本地）
XHS_MCP_URL=http://localhost:18060
XHS_MCP_ENDPOINT=http://localhost:18060/mcp

# Agent（可选）
AGENT_LLM_BACKEND=openai|mock     # 默认 LLM 后端，mock 用于离线
AGENT_MAX_RETRIES=3               # 单步重试次数上限
AGENT_MAX_LLM_CALLS=0             # LLM 调用次数上限（0=不限）
AGENT_MAX_EST_TOKENS=0            # 估算 token 上限（0=不限）
AGENT_MAX_STEPS=50               # 跨 replan 总执行步数预算（0=不限）
AGENT_HOUSEKEEPING_RETENTION_DAYS=30  # 运行痕迹保留天数
AGENT_HOUSEKEEPING_AUTO=false         # 是否概率自动清理旧痕迹
XHS_ADMIN_TOKEN=                     # 可选：设置后 stop/resume/housekeeping 需携带此值作为 X-Admin-Token

# 爬虫风控规避（可选，默认值见 docs/crawl-anti-detection.md）
CRAWL_DELAY_MIN=3.0                # 详情页访问间隔下限(秒)
CRAWL_DELAY_MAX=6.0                # 详情页访问间隔上限(秒)
CRAWL_SEARCH_DELAY_MIN=4.0         # 搜索页停留下限(秒)
CRAWL_SEARCH_DELAY_MAX=8.0         # 搜索页停留上限(秒)
CRAWL_KEYWORD_COOLDOWN_MIN=8.0     # 多关键词之间冷却下限(秒)
CRAWL_KEYWORD_COOLDOWN_MAX=15.0    # 多关键词之间冷却上限(秒)
CRAWL_BURST_SIZE=8                 # 连续访问 N 页后长休息(0=禁用)
CRAWL_BURST_REST_MIN=15.0          # 长休息下限(秒)
CRAWL_BURST_REST_MAX=30.0          # 长休息上限(秒)
CRAWL_HEADLESS=false               # 风控加强时可改 false 用有头窗口
```

> LLM 配置优先级：环境变量 / `.env` > Codex 默认值（`~/.codex/config.toml` 的模型与 `base_url`、`~/.codex/auth.json` 的 `OPENAI_API_KEY`）。

### 3. 启动服务

```bash
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

浏览器访问 `http://127.0.0.1:8000` 进入 Web UI；Agent 控制台访问 `http://127.0.0.1:8000/static/agent_console.html`。
FastAPI 交互文档：`http://127.0.0.1:8000/docs`

---

## 主要 API

| 路由 | 说明 |
|------|------|
| `GET  /health` | 健康检查 |
| `GET  /` | 重定向至 Web UI（`/static/index.html`） |
| `POST /analysis/analyze` | 分析笔记列表，返回关键词、标签、标题规律、洞察点 |
| `GET  /analysis/sample-analyze` | 用内置样例数据分析（需 `data/raw/sample_notes.json`） |
| `POST /topics/generate` | 根据分析结果生成话题建议 |
| `POST /content/generate` | 根据选题生成文案（含 `image_suggestion` 文本建议，不生成实际图片） |
| `POST /agent/run` | 一键运行完整确定性流水线（分析 → 话题 → 文案） |
| `POST /agent/autonomous` | 运行真正的自主 Agent（目标 → 规划 → 执行 → 反思 → 追踪） |
| `POST /agent/runs` | 创建异步 Agent 控制台运行（SSE 流式事件） |
| `GET  /agent/runs` | 列出最近最多 50 次 Agent 运行 |
| `GET  /agent/runs/{run_id}` | 获取单次运行详情 |
| `GET  /agent/runs/{run_id}/events` | SSE 订阅某次运行的实时事件 |
| `POST /agent/runs/{run_id}/stop` | 停止某次运行 |
| `POST /agent/runs/{run_id}/confirm` | 审批发布/飞书等副作用 |
| `POST /agent/runs/{run_id}/resume` | 从检查点续跑未完成运行 |
| `POST /agent/housekeeping` | 清理超过保留期的运行痕迹 |
| `POST /agent/knowledge` | 手动喂文档入 RAG 知识库（爬取/生成产物自动入库） |
| `GET  /agent/knowledge/search?q=` | 知识库混合检索（bge 向量 + BM25 → RRF），自然语言知识问答 |
| `GET  /agent/knowledge` | 知识库统计 |
| `POST /agent/schedule` | 创建定时/间隔 Agent 运行 |
| `GET  /agent/schedule` | 列出定时任务 |
| `DELETE /agent/schedule/{schedule_id}` | 删除定时任务 |
| `POST /local-crawl/search` | 按关键词爬取小红书图文笔记（未登录时快速失败，非阻塞） |
| `POST /publish/prepare` | 生成配图并组装发布 Payload（REST / MCP 格式） |
| `POST /publish/send` | 发送 Payload 到小红书 MCP 服务 |
| `POST /publish/run` | 一步完成：生成配图 → 组装 → 发布 |
| `POST /publish/run-from-agent` | 直接用 `/agent/run` 结果生成配图并发布 |
| `GET  /publish/tools` | 列出 MCP 服务端注册的所有工具（调试用） |
| `GET  /publish/login` | 检查小红书 MCP 登录状态 |
| `GET  /xhs-service/status` | 查询本地小红书 MCP 服务运行状态 |
| `POST /xhs-service/start` | 启动本地小红书 MCP 服务 |
| `POST /xhs-service/login` | 运行登录工具（弹出浏览器扫码） |
| `POST /xhs-service/stop` | 停止本地小红书 MCP 服务 |
| `POST /feishu/sync` | 将生成内容同步至飞书（直接传 MCP 参数） |
| `POST /feishu/sync-from-agent` | 从 `/agent/run` 结果同步至飞书（需图片路径） |
| `POST /feishu/sync-crawled` | 将爬取数据同步至飞书 |

---

## 自主 Agent（核心）

`app/agent/` 提供了一个真正的 Agent 编排核心：目标 → 规划 → 执行 → 反思 → 追踪 的自适应循环，而非写死的三步流程。设计依据为 Anthropic《Building Effective Agents》的 **Evaluator-Optimizer**、**Orchestrator-Workers** 模式，以及 LangGraph 的 StateGraph / 工具注册表思路。

### 核心组件

- `app/agent/orchestrator.py` —— 顶层编排对象 `XHSAgent`：串联 Planner/Reflector/Executor/Memory/Tracer，目标 -> 计划 -> 执行 -> 产物收集（`_collect_outputs`）-> 经验回写（`_remember_lesson`）-> 知识库自动入库（`_ingest_outputs_to_knowledge`）。
- `app/agent/tools.py` —— 类型化工具注册表：把爬取/分析/话题/文案/配图/发布/飞书同步封装成可被 Agent 选择的工具（名称 + 描述 + Pydantic 参数 schema）。
- `app/agent/llm.py` —— 统一 LLM 层：可注入后端（OpenAI 或 Mock）、结构化 JSON 输出、指数退避重试、token/成本统计。
- `app/agent/planner.py` —— 规划器与反思器：把目标拆成步骤；每步后决定继续/重规划/停止；失败时恢复。
- `app/agent/executor.py` —— 执行器：解析 `$latest.<tool>.<field>` 数据依赖、并行执行、重试、反思；爬取成功自动落盘并喂入 RAG 知识库。
- `app/agent/evaluator.py` —— 评估-优化质量门：每篇草稿经 LLM 评审（0-10 分），未达标重写，产出 `quality_report`。
- `app/agent/memory.py` —— 短期步骤记忆 + 长期用户画像（写入 `data/agent/profile.json`）。
- `app/agent/checkpoint.py` —— 步级 checkpoint：每执行批次原子落盘，崩溃/重启后可从最后成功步续跑（`POST /agent/runs/{id}/resume`）。
- `app/agent/consolidation.py` —— dreaming 后台记忆巩固：run 结束后由 LLM 提炼可迁移洞察写入 `procedural_insights`。
- `app/agent/embedder.py` —— 本地中文嵌入：bge-small-zh-v1.5 ONNX（CPU，512 维，无 torch 依赖）。
- `app/agent/knowledge.py` —— RAG 知识库：sqlite-vec 向量 KNN + jieba BM25 双路召回 → RRF 融合（单文件 `data/agent/knowledge.db`）。
- `app/agent/housekeeping.py` —— 运行痕迹/checkpoint/summary/派生产物的 TTL 清理。
- `app/agent/budget.py` —— 成本/调用/步数预算，超限自动停机，防止 Agent 跑飞。
- `app/agent/tracing.py` —— 每次运行的追踪与成本日志（`data/agent/runs/<run_id>.json`），批次边界增量 flush 防崩溃丢失。
- `app/agent/run_manager.py` —— 异步运行管理：SSE 流式事件、停止、审批、历史、resume 续跑、调度器。

### 使用

```bash
# 离线演示（Mock 后端，无需 API key）
python run_agent_demo.py --backend mock "为大学生女生生成3篇小红书种草文案并给出配图建议"

# 真实模型（需配置 LLM Key）
python run_agent_demo.py --backend openai "为大学生女生生成3篇小红书种草文案"

# 测试
python -m pytest tests/ -v
```

### API / MCP

- `POST /agent/autonomous`（body: `goal`、`audience`、`tone`、`backend`，backend 可为 `openai` 或 `mock`）。
- `mcp_server.py` 提供 `run_agent` 工具，可在 Claude Desktop / Cursor 中直接调用。

### 自主学习 / 迭代改进

- **评估-优化循环**（`evaluator.py`）：`generate_contents` 每篇草稿经 LLM 质量评审，未达标由优化器重写并再次评审，直到通过或达到轮次上限；产出带 `quality_report`（evaluated / optimized / passed）。
- **跨次学习**（`orchestrator.py::_remember_lesson`）：每次运行把目标、完成度、产出量与质量报告写入长期记忆 `profile.json` 的 `episodic`（镜像 `lessons`），下次规划 prompt 会带上历史经验，让 Agent 越用越懂目标用户。
- **经验上限与去重**：`episodic`/`procedural` 按 goal 归一化签名 + 主题指纹近似去重（`orchestrator._dedup_by_goal`），超出上限按 recency×hits×quality 加权淘汰，防止长期记忆无限膨胀；`orchestrator._profile_text` 按当前 goal 的主题相关度召回最相关经验（小集合须完全覆盖，防"女生≠研究生"误召回）注入规划 prompt。
- **后台记忆巩固（dreaming，`consolidation.py`）**：每次 run 结束后投递低优先级后台任务，由 LLM 回顾本次运行并提炼至多 1 条*可迁移* procedural 洞察（`procedural_insights`），按标题签名去重、失败不致命；`_profile_text` 会把近期洞察注入规划 prompt（对照 Letta "dreaming consolidates useful lessons"）。

### RAG 知识库（记忆体系升级，2026-09-10）

Agent 既能搜集信息又能作为知识问答助手。经对高星项目（Mem0 65k/Letta 24.7k/Graphiti 30.8k/LangGraph/CrewAI/Chroma/sqlite-vec）记忆架构的一手调研与评估（`docs/memory-rag-selection.md`），选定**自研轻量混合体系**（不引入重依赖记忆框架）：

- **数据源自动入库**：爬取笔记（executor 钩子，url 哈希幂等）、分析报告与生成文案（orchestrator 钩子，goal+内容哈希幂等）、`POST /agent/knowledge` 手动喂文档。mock 后端跳过写入，不污染真实库。
- **混合检索**：`GET /agent/knowledge/search?q=` —— bge-small-zh 向量 KNN + jieba BM25 双路召回，RRF(k=60) 融合（借鉴 Mem0 多信号检索），支持 source 过滤。
- **学习闭环**：planner 规划前自动检索 top-4 相关知识注入 prompt（`_knowledge_context`）——搜集 → 生成 → 回灌 → 再检索。
- **实战验证**（真实 LLM deepseek-v4-flash-ascend）：19 篇真实爬取笔记入库后自然语言问答全部命中；run 产物自动入库后问答第一命中即刚生成文案。

### Agentic RAG + 上下文感知检索落地（2026-09-15）

依据两份实时调研（`agentic_rag_research.md` / `context_aware_retrieval_research.md`）按 M0→M1→M2 分期落地，全部带评测与真机验证：

- **M0 检索评测基建**（`app/agent/retrieval_evals.py`）：语料自推导金标（doc 式人话查询 + goal 式 Agent 腔查询，Q2D-Web 结论：两种分布不同须分开评），hit@k / MRR / 失败率，`--rebuild` 支持同语料 A/B 重建（不动真库）。CLI：`python -m app.agent.retrieval_evals [--rebuild]`。
- **M1 上下文感知检索**（`app/agent/knowledge.py`）：入库自动拼情境前缀 `[知识|source|title|目标|url|日期]`（Anthropic Contextual Retrieval 的免 LLM 版，前缀随 chunk 同时进向量+BM25 双索引，切块窗口自适应收紧防 512-token 截断）；`search_multi` 多查询变体 RRF 融合（CompCQR 组合式改写思想，变体由 `_query_variants` 确定性产出零 LLM 成本）；crawl 来源 45 天指数时效衰减（analysis/output 知识资产不衰减）；`list_documents` 支撑小库 digest。注入 v2（`orchestrator._knowledge_context`）：小库（≤`KNOWLEDGE_DIGEST_MAX_DOCS`，默认 30 文档）全库 digest 直注 / 大库多变体检索，条目统一 K1..Kn 编号，`knowledge_context_budget_chars`（默认 800）预算封顶防 Context Rot。**A/B 实测（63 篇真库 74 查询）**：失败率@5 25.7%→8.1%（相对降 68.5%，与 Anthropic 发表值 67% 吻合），goal 式 hit@5 36.4%→100%。
- **M2 智能体化**：`knowledge_search` 注册为类型化工具（tools.py，schema 描述内置 source 意图路由，空结果带 crawl/load 兜底提示即 CRAG 思想，永不抛错）；planner 模板加 Adaptive-RAG 路由与 Read-Gate 规则；**结构化强制**（`orchestrator._inject_knowledge_steps`，仅真实后端）：K 知识可用而计划无检索步时链首确定性插入 `knowledge_search`（调用级），并对 generate_topics/generate_contents 步骤注入 `params.knowledge_hints`（消费级，K 线索经 schemas/prompts/services 物理进入生成 LLM 的 prompt）——两轮真机实证 prompt 措辞对抗不可靠后采用的机械保证。
- **run 级评估器**（`scripts/evaluate_run.py <run_id> --kb-before N`）：七维报告（终态/安全门/智能体路由/Read-Gate/质量门/相关性/成本+知识回灌），数据源 console record + trace；**浏览器驱动**（`scripts/console_live_run.py`，Playwright 走控制台真人路径全流程+截图存证）；**自动哨兵**（`scripts/auto_live_run.py`，探活网关→自动浏览器 run→自动评估）。
- **真机三轮战绩**：run1（API）与 run2（浏览器）均 PASS（4/4 步、3/3 质量门、零发布、知识回灌 +4/+3），且暴露 M2 缺口（planner 两轮均未自发调用检索工具）；run3 结构化强制版——`knowledge_search` 步已成功执行、K 线索已注入生成步骤（trace 有 `knowledge_injected` 事件），但服务进程被宿主回收致 run 中断于 generate_contents，待重跑收尾对比。

**当日配套修复**：LLM 客户端 connect 超时 5s→60s（llm.py，网关冷启动可达 230s）；`run_manager.DEFAULT_TOOLS` 与控制台侧栏（static/agent_console.html）收录 `knowledge_search`（此前控制台 enabledTools() 会把它过滤掉导致插步 Unknown tool）；api_key 经 config.toml `experimental_bearer_token` + 环境变量注入（本机 `~/.codex/auth.json` 缺失场景）。

### 爬取风控规避（2026-09-10）

人类化节流 + 指纹随机化，全部可经 `.env` 的 `CRAWL_*` 配置（详见 `docs/crawl-anti-detection.md`）：

- **节流**：详情页间隔 3–6s（每 8 页长休息 15–30s）、搜索页停留 4–8s、关键词冷却 8–15s、阅读停留 2–4s、滚动 1.5–3s；节流与采集成功数解耦（无效页同样停顿，消除机器节奏）。
- **指纹**：viewport 从 5 种分辨率随机、UA Chrome/131、locale=zh-CN、timezone=Asia/Shanghai、保留 `AutomationControlled` 抹除。
- 未做代理轮换/资源屏蔽的理由见文档（单账号场景反而更可疑）。

### 稳健性 / 可观测性 / 自约束

- **引用解析容错**（`executor.py::_best_effort_dig`）：planner 若生成不精确引用（如 `$latest.load_notes.items`），自动回退到最近可达祖先（整个列表），避免崩溃或取到空数据。
- **工具参数宽松收窄**（`tools.py::_coerce_params`）：字段期望 `str` 但 planner 传入整个对象时，自动取 `title/reason/text` 等标量，减少无效重试。
- **数据依赖兜底**（`executor.py::_ready_indices`）：planner 生成的 `generate_contents` 步骤若不带 `$latest` 引用（硬编码 topic），会与 `crawl_notes` / `analyze_notes` 同批并行、生成不消费真实数据。修复为：generate 无显式依赖且 plan 中排在其前的 analyze/crawl 存在时强制补依赖，保证 `crawl → analyze → generate` 串行、生成吃爬取数据（2026-08-13 实测）。
- **终态收尾**（`executor.py`）：反思主动 stop 是「目标达成收尾」，剩余 PENDING 步骤标 SKIPPED → `plan.is_complete=True` → 终态 `completed`。此前爬满 5/5 + 生成全过却因 analyze 未轮到被误报 `failed`「Plan incomplete」（2026-08-13 实测修复）；预算中断/失败路径不置 `_graceful_stop`，仍保持 `failed`，不虚报完成。
- **成本/调用预算**（`budget.py`）：在每次执行批次后检查 LLM 调用次数、估算 token 与总步数，超限自动停机，并把原因写入 `AgentResult.stopped_reason` 与 trace 的 `budget_stop` 事件。
- **用量统计真实化**：每次运行从 OpenAI Responses 响应 `usage` 取真实 `input_tokens` / `output_tokens` / `cache_read_tokens` 聚合进 `AgentResult.usage` 与 trace（后端不返回时回退 tiktoken 双端估算并标 `estimated: true`）。实测：真实 run `input 5440 / output 1956 / cache 2048`，旧版仅按输出估算 `833`，低估约 11 倍。

### Agent 运行时的登录交互注意

- `local_site_crawler_service.py::_ensure_logged_in` 通过 `sys.stdin.isatty()` 判断是否交互式终端：首次登录需用户在浏览器手动登录后按 Enter（内部调用 `input()`）。
- 当 Agent 自主运行 / 后台任务（非交互）且尚未登录时，**不会阻塞等待 `input()`**，而是快速失败并提示先在交互终端完成登录、保存登录态到 `data/raw/xhs_state.json` 再重试，避免 Agent 卡死。

---

## Agent 控制台

`static/agent_console.html` + `app/agent/run_manager.py` + `app/api/routes_agent.py` 共同实现一个 Web 控制台，让自主 Agent 的行为**可观察、可控制**：

- 输入目标与约束，观察规划与工具选择的实时执行轨迹（SSE 流式）。
- 检查反思与重规划过程、查看生成产物、回看历史运行。
- 发布到小红书 / 同步飞书等**副作用需显式审批**（`/agent/runs/{id}/confirm`）。
- 可随时停止运行（`/agent/runs/{id}/stop`）。
- 应用重启后，未完成的运行会被标记为 `failed`，避免残留悬挂状态。

> 详细设计见 `docs/superpowers/specs/2026-08-11-agent-console-design.md`。

---

## MCP Server 使用

将本项目封装为 MCP Server，可在支持 MCP 协议的 AI 工具（Claude Desktop、Cursor 等）中注册使用。

```bash
python mcp_server.py
```

| 工具 | 说明 |
|------|------|
| `run_content_pipeline` | 完整运行确定性内容生成流水线 |
| `generate_xhs_images` | 根据文案生成配图 |
| `publish_to_xhs` | 生成配图并一键发布至小红书 |
| `check_xhs_login` | 检查小红书登录状态 |
| `run_agent` | 驱动真正 Agent（目标 → 规划 → 执行 → 反思 → 追踪）完成内容生成任务 |

---

## 项目结构

```
xhs_content_agent/
├── app/
│   ├── api/                # FastAPI 路由层（含 /agent 路由与 knowledge 端点）
│   ├── agent/              # 自主 Agent 编排核心
│   │   ├── orchestrator.py / planner.py / executor.py   # 规划-执行-反思循环
│   │   ├── memory.py / consolidation.py                 # 分桶记忆 + dreaming 巩固
│   │   ├── embedder.py / knowledge.py                   # bge 本地嵌入 + RAG 知识库
│   │   ├── checkpoint.py / scheduler.py / housekeeping.py
│   │   └── evaluator.py / guardrails.py / budget.py / tracing.py
│   ├── core/               # 配置（Settings）
│   ├── models/             # Pydantic 数据模型
│   ├── prompts/            # LLM Prompt 模板
│   └── services/           # 业务逻辑层
│       ├── agent_service.py          # 确定性主流水线编排
│       ├── analysis_service.py       # 笔记数据分析
│       ├── topic_service.py          # 话题生成
│       ├── content_service.py        # 文案生成
│       ├── image_service.py          # 图片生成
│       ├── publish_service.py        # 小红书发布
│       ├── feishu_service.py         # 飞书同步
│       ├── local_site_crawler_service.py  # 小红书爬虫（含风控规避节流）
│       ├── crawl_job_manager.py      # 爬取后台任务管理
│       └── mcp_client_service.py     # MCP 客户端
├── static/                 # Web 前端（含 agent_console.html）
├── data/
│   ├── raw/                # 爬取数据 / 样本数据 / xhs_state.json 登录态
│   ├── output/images/      # 生成的图片
│   ├── models/             # bge-small-zh-v1.5 ONNX 嵌入模型（91MB）
│   └── agent/              # profile.json / knowledge.db / runs/ / console_runs/ / checkpoints/
├── docs/                   # 设计文档（memory-rag-selection / crawl-anti-detection 等）
├── tests/                  # 单元测试
├── mcp_server.py           # MCP Server 入口
├── run_agent_demo.py       # Agent 命令行演示
├── run.py                  # 本地启动入口（等价 uvicorn app.main:app，127.0.0.1:8000，XHS_RELOAD=0 关闭热重载）
├── test_crawler.py         # 交互式登录小红书并保存登录态到 data/raw/xhs_state.json
├── scripts/                # 运维与演示脚本（cleanup_runs / rebuild_profile / gzh_crawler / memory_upgrade_demo）
├── CrawlData_to_FeishiList.py  # 爬取数据导入飞书表格的工具脚本
├── GeneratedNote_to_FeishuList.py  # 生成笔记导入飞书表格的工具脚本
├── requirements.txt
└── .env.example            # 环境变量模板（复制为 .env 后填写）
```

---

## 技术栈

- **后端框架**：FastAPI + Uvicorn
- **LLM 调用**：LangChain + LangChain-OpenAI（GPT-4o-mini）；Agent 层统一 LLM（OpenAI Responses wire API，兼容 Codex 配置，实测 deepseek-v4-flash-ascend）
- **图片生成**：OpenAI gpt-image-1
- **爬虫**：Playwright（Chromium）+ 人类化节流与指纹随机化（`docs/crawl-anti-detection.md`）
- **RAG 知识库**：sqlite-vec（向量 KNN）+ rank_bm25 + jieba 分词 → RRF 融合；嵌入用 BAAI/bge-small-zh-v1.5 ONNX（onnxruntime CPU，本地推理，无 torch）
- **MCP 协议**：`mcp` SDK（FastMCP）
- **飞书 API**：飞书多维表格 Open API
- **中文处理**：jieba 分词
- **数据验证**：Pydantic v2

---

## 测试

```bash
python -m pytest tests/ -v
```

当前共 **179 个** pytest 测试（`test_agent.py` 34 + `test_agent_runs.py` 13 + `test_real_run_fixes.py` 10 + `test_fix_20260813.py` 10 + `test_content_drift_fixes.py` 9 + `test_memory_upgrade.py` 12 + `test_knowledge.py` 11 + `test_knowledge_wiring.py` 8 + `test_embedder.py` 7 + `test_governance.py` 6 + `test_scheduler.py` 5 + `test_guardrails.py` 5 + `test_consolidation.py` 6 + `test_crawler_login.py` 4 + `test_housekeeping.py` 4 + `test_crawler_throttle.py` 3 + `test_crawl_jobs.py` 2 + `test_agent_evals.py` 1 + `test_knowledge_upgrade.py` 28 + `test_llm_client_close.py` 1），覆盖规划/执行/反思、质量门、预算、记忆、运行管理与审批流，以及 checkpoint 续跑、护栏、TTL 清理（含 summary/tmp 冷区）、治理鉴权、端到端 evals、定时调度、登录态感知、dreaming 后台记忆巩固、RAG 知识库（bge 中文向量 + BM25 混合检索 + 幂等入库 + API 接线）、爬取节流、2026-08-13 真机实测回归（终态收尾、数据依赖兜底），以及 2026-09-15 Agentic RAG 升级（语境化前缀/多变体融合/时效衰减/digest 判别/注入预算/knowledge_search 工具与透传/结构化强制注入器）。`tests/check_agent_console_ui.py` 为控制台 UI 检查脚本（非 pytest 单元测试）。

---

## 注意事项

- 爬取功能需要提前完成小红书登录并将 Cookies 保存至 `data/raw/xhs_state.json`；非交互环境（Agent/后台）未登录时会快速失败而非阻塞等待。爬取内置人类化节流（默认详情页 3-6s 间隔），如需更慢/更快调整 `.env` 的 `CRAWL_*` 项。
- RAG 知识库首次使用前需确认 `data/models/bge-small-zh-v1.5.onnx` 存在（91MB，已随项目数据目录提供；缺失时 `get_embedder()` 抛 FileNotFoundError 并在文档 `docs/memory-rag-selection.md` 指引）。嵌入全程本地 CPU 推理，不调用任何 embedding API（当前 LLM 网关无 embedding 权限）。
- 知识库数据存于 `data/agent/knowledge.db`（单文件 SQLite），随 `purge_old_runs` 的 TTL 不清理（知识资产长期保留）；如需重建删除该文件后重新喂入即可。
- 发布功能依赖本地运行的小红书 MCP 服务（默认端口 `18060`），需完成扫码登录。
- 图片生成（`/publish/prepare`、`/publish/run`、`/publish/run-from-agent`、`/feishu/sync-from-agent`）需要 OpenAI API Key 且该 Key 有 `gpt-image-1` 的访问权限；`/content/generate` 仅产出图片建议文本，不调用图片接口。
- 飞书同步为可选功能，未配置时相关接口会返回提示信息而不会报错。

---

## 已知局限与演进路线

作为一个自主 Agent 产品，当前实现已具备「骨架」，距离「能长久持续运行、有充分自主性」的成品仍有短板，详见 `docs/optimization-plan.md`（持续更新）；2026-08-12 真实联调暴露的振荡/产物混杂/爬取长任务等问题与分析见 `docs/real-run-issues-20260812.md`：

- 自主性：已加入常驻调度器与 `POST /agent/schedule`（支持间隔/每日定时触发多轮运行，见 optimization-plan §1）；跨轮次状态经 checkpoint 延续。
- 长效记忆：结构化经验三分桶（episodic/procedural/insights）+ 主题相关度召回（小集合误召回兜底）+ dreaming 后台巩固均已落地；**RAG 知识库已落地**（2026-09-10，自研轻量混合检索，选型评估见 `docs/memory-rag-selection.md`；Mem0/Letta/Graphiti 等重依赖框架经评估未采用，理由见文档 §1/§2）。
- 护栏（guardrails）：已加入输入目标/敏感副作用工具/输出合规护栏；端到端 evals 仍待补。
- 断点续跑：已支持 checkpoint 持久化与 POST /agent/runs/{id}/resume（应用重启后可从最后成功步续跑；真实崩溃续跑已实战验证，含 resume 后仅补跑未完成步、池投递 dreaming 正常落地）。
- 外部依赖脆弱：爬虫依赖 Playwright+Cookies、发布依赖本地 MCP，长稳运行风险高；登录态失效已可感知（`needs_login` + 登录重定向检测）；爬取已内置风控规避节流与指纹随机化（`docs/crawl-anti-detection.md`）。
- 治理：管理端点（stop/resume/housekeeping）已支持可选 XHS_ADMIN_TOKEN 鉴权，并追加 data/agent/audit.log 审计日志。
- M0 质量修复（12.1–12.5）与爬取长任务化（12.6–12.8）已落地：目标域关键词注入对抗锚定漂移、质量门相关性维度、产物去重择优、振荡检测与追加补足、爬取后台任务 + DOM 兜底 + headless 可配（详见 real-run-issues §12 与 optimization-plan §0.1）。
- 2026-08-13 真机实测（真实模型 + 真实爬取西电研究生 5 篇）闭环两项缺陷：① 反思主动 stop 的终态误报 `failed` → 剩余 PENDING 收尾为 SKIPPED；② generate 与 crawl 并行先跑（不消费爬取数据）→ 数据依赖兜底强制串行。实测证据与缺陷分析见 `IMPROVEMENT.md` §8。
- 2026-09-10 真机实测（真实模型 deepseek-v4-flash-ascend）：IMPROVEMENT.md P0-P1 全部落地项完成实战验收（checkpoint 续跑 141s 恢复完成、池投递 dreaming 落盘、冷区 TTL、召回兜底）；RAG 知识库端到端闭环（真实爬取笔记入库 → 自然语言问答命中 → run 产物自动回灌 → planner 知识注入）。
- 短板仍在：LLM 网关（ustc）稳定性一般（冷启动可达 230s、偶发 429/连接抖动，2026-09-15 又两次整段不可达；connect 超时已放宽至 60s，长稳运行建议换更稳的商用端点或加重试预算）；知识库检索为纯本地词面+向量，无 rerank 与跨编码器精排（当前规模不需要）。
- **2026-09-16 收尾闭环（第三轮 + 卡死修复，详见 `三轮实战对比报告_20260916.md`）**：① 第三轮浏览器 run `363cdbc45966` 全门 PASS——Read-Gate 首次闭环（auto_search_step=True、hints 2 步、K1-K3 引用、实际检索 2 次），相关性 coverage 0.4→0.7 回升、关键词 7/7 命中，耗时 204s 反而最短，知识库 73→76，RAG 回灌飞轮实测闭环（第三轮检索命中前轮产物文案并引用）；② 定位并修复每轮必现的服务收尾卡死：py-spy 实锤 asyncio.run 的 loop.close() 在 Windows proactor 上等未关闭 httpx 客户端的挂起 I/O——LLMBackend 增 aclose()、run_manager finally 同 loop 关闭、巩固任务改独立客户端，测试 178→179 全绿；③ 两条僵尸记录（`a6b5e5c10b33`/`066c12fadc1d`）已按各自 trace 对账恢复终态；④ 遗留：服务进程宿主回收问题（长跑建议 Windows 服务/计划任务托管）、启动仍需带 OPENAI_API_KEY 环境变量。
