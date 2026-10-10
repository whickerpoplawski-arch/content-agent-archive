# 实战运行问题分析报告（2026-08-12）

> 本文档记录 XHS Content Agent 在 **2026-08-12 真实联调**中暴露的问题。测试方式：真实后端（openai，读取 Codex 配置 `deepseek-v4-flash[1M]` + volces ark），通过真实 Chromium 驱动 Agent 控制台完成一次真实对话，并用 API 补测真实爬取流程；跳过图片生成、发布、飞书环节。
>
> 运行对象：`run_id = 2b26442ae14f`（状态 completed），爬取测试：2 次。
>
> 二次实测（2026-08-12 晚间，目标「西电研究生」）：run_id = `41127b9e8303`(stopped)、`0777eb16c17f`(failed，爬取振荡)、`af61e1a62643`(completed 但数据被编码污染、产物作废)、`3c824659e25f`(failed，服务旧代码 bug)、`a390fce14b30`(stopped)。新增问题见第 13 节；爬取 0 条与旧代码问题另见 4.5 / 13.x。

---

## 1. 运行摘要

| 项目 | 值 |
|------|-----|
| 目标 | 为西电（西安电子科技大学）学生生成 3 篇关于暑期实习的小红书分享文案，主题围绕西电实习经历，不要发布 |
| 后端 | openai（真实模型，读取 Codex 配置） |
| 启用工具 | `crawl_notes`、`analyze_notes`、`generate_topics`、`generate_contents`、`load_notes`（图片/发布/飞书按要求排除） |
| 最终状态 | completed |
| 模型调用 | 24 次 LLM，估算约 2079 tokens |
| 工具调用 | `load_notes`×3、`analyze_notes`×3、`generate_topics`×3、`generate_contents`×3、`crawl_notes`×0 |
| 质量评估 | 3 轮，每轮 `evaluated=3, optimized=0, passed=3` |
| 最终产物 | 9 篇（3 轮×3 累积），其中实习主题 3 篇、宿舍收纳类 6 篇 |

### 1.1 事件时间线（关键节点，本地时间）

| 时间 | 事件 | 说明 |
|------|------|------|
| 17:23:18 | run_started | 任务开始 |
| 17:24:01 | plan_created | 生成计划：load → analyze → topics → contents |
| 17:24:01–17:24:53 | 第 1 轮执行 | 4 个工具全部成功 |
| 17:25:03 / 17:25:08 | quality / reflection+replan | 反思判定「宿舍改造而非暑期实习」，重规划 |
| 17:25:25–17:26:02 | 第 2 轮执行 | 4 个工具全部成功 |
| 17:26:05–17:26:06 | reflection+replan ×3 | 连续 3 次「内容与暑期实习无关」 |
| 17:26:14 / 17:26:24 | quality / reflection+replan | 第 2 轮结束，再次重规划 |
| 17:27:10–17:27:44 | 第 3 轮执行 | 4 个工具全部成功 |
| 17:27:48–17:28:04 | reflection×4 / quality | 前 3 次判定「目标达成」，最后 1 次判定「还需两篇」 |
| 17:28:04 | run_completed | 任务结束 |
### 1.2 二次实测（2026-08-12 晚间）摘要

| run_id | 状态 | 关键结果 |
|------|------|------|
| `41127b9e8303` | stopped | 6 关键词（西电研究生/西安电子科技大学研究生/西电科研/西电求职/西电读研/西电保研）target=20 → 爬取 82 分钟未完成，手动 stop |
| `0777eb16c17f` | failed | 1 关键词 target=5、低门槛（赞≥20/藏≥10/评≥0）→ 爬取 25.6 分钟返回 0 条 → 反射 replan → 重爬 → 振荡 |
| `af61e1a62643` | completed(作废) | load_notes 因样本文件带 BOM 失败 → 反射尝试无效参数 → 最终以被污染（中文→`?`）数据跑完，产物不可用 |
| `3c824659e25f` | failed | 干净数据下 load→analyze 成功；generate_topics 因服务运行旧代码缺 `focus_keywords` 必失败 → 重试振荡 |
| `a390fce14b30` | stopped | 重启服务（加载新代码）后重跑，用户要求停止 |

---

## 2. 问题一：反思-重规划循环 + 产物跨轮次累积（核心问题）

### 2.1 现象

- 3 轮执行中，反思器连续给出 `next_action=replan`，理由集中在「生成的内容是宿舍改造/收纳而非暑期实习，与目标不符」。
- 每轮重规划后都重新执行 `load → analyze → topics → contents`（后两个为 LLM 调用，单轮约 40–90 秒），导致总耗时约 4 分 46 秒（17:23:18→17:28:04）。
- **质量门与反思器结论矛盾**：每轮质量评估都是 `passed=3/3`，但同一轮反思器仍判「偏离目标需重规划」。
- **产物累积不清理**：最终 `outputs.results` 包含 9 篇（3 轮 × 3），既含 3 篇实习主题，也含 6 篇与目标无关的宿舍收纳类文案，无去重、无筛选。

### 2.2 根因

1. **样例数据锚定偏差**：`load_notes` 加载的是内置 `sample_notes.json`，内容全部是「平价好物 / 宿舍收纳 / 护肤避雷」类。`analyze_notes` 基于这些样本提取高频词（学生、必入、平价、好物、宿舍、收纳、避雷…），LLM 在选题/写文时被反复锚定到「宿舍收纳」，与「暑期实习」目标域不匹配。
2. **反思器与质量门是两套独立判定，标准不一致**：质量门（`QualityGate`）只校验「文案结构/是否产出」这类表面质量，全部通过；反思器（`Reflector.judge`）按「是否达成用户目标」判定，认为跑题 → 两个模块结论互相矛盾，系统无所适从，只能反复重规划。
3. **重规划不携带/不坚持目标域约束**：`replan` 重新生成计划时没有把「目标域关键词（实习/求职）」显式注入 `generate_topics` / `generate_contents` 的参数，导致每轮都重新掉进同一个锚定陷阱。
4. **无振荡检测与去重**：executor 没有「同一失败原因连续 N 次 → 换策略/停止」的保护；`_collect_outputs` 把 `memory.results_for("generate_contents")` 全量拼进最终输出，历史轮次产物不清理、不择优。

### 2.3 影响

- 单目标耗时膨胀（一轮无效重规划 ≈ 1–1.5 分钟与 4 次 LLM 调用），token 成本随之上升。
- 最终交付物混入大量不相关内容，用户需人工二次筛选，Agent 的「自闭环」价值被削弱。
- 长时间无进展时只能依赖全局 step 预算停机，缺乏「定向纠偏」能力。

### 2.4 建议（P1）

- 重规划/选题阶段把目标域关键词显式注入 `generate_topics` 参数（如 `topic_words` / `focus_keywords`），从源头对抗锚定漂移。
- 质量门与反思器统一判定口径：让 `Reflector` 复用质量门结果，或在质量门增加「目标相关性」维度（LLM 核对标题/正文是否命中目标关键词）。
- `_collect_outputs` 对 `generate_contents` 结果做去重 + 相关性排序（保留命中目标关键词且质量分最高的 N 篇），或只保留最后一轮产物。
- 增加重规划振荡保护：同一 replan 原因连续出现 2–3 次时，自动切换策略（如强制换源数据、强制注入目标域、或停止等待审批）。

---

## 3. 问题二：反思器最终判定与执行器结束条件不一致

### 3.1 现象

第 3 轮结束时，反思器连续 3 次判定「3 篇文案已生成、目标达成」，随后第 4 次却判定「已生成一篇实习主题文案，还需生成另外两篇以完成目标」（`next_action=continue, should_stop=false`），紧接着 1 秒后 `run_completed`。

### 3.2 根因

- 执行器（executor）的结束条件是「计划步骤全部成功 / 无就绪步骤」，与反思器最新判定（continue）脱节；反思器说「还要继续」时，计划已经跑完、无步骤可执行，系统直接收尾。
- 反思器本身对「已产出几篇合格文案」的计数不稳定（同一轮内 3 次说达成、1 次说还差 2 篇），说明其判定纯靠 LLM 记忆/自述，没有把「本轮实际产出的 results」作为输入做客观计数。

### 3.3 影响

- 运行状态显示 completed，但最后一条反思内容与状态矛盾，用户会困惑「到底达没达成」。
- 潜在漏产：若反思器真认为还差 2 篇，系统直接结束，目标实际未完成。

### 3.4 建议（P2）

- 把「当前已达标篇数」作为结构化数据（如 memory 里统计命中目标关键词的 results 数）喂给反思器，避免 LLM 自述计数。
- 反思器判定 `continue` 且计划已满时，应触发一次追加执行（生成补足缺额）而不是静默收尾；或显式给出「计划已结束，未完全达标」的状态。

---

## 4. 问题三：真实爬取慢、接口非任务化、互动数据全为 0

### 4.1 现象

- 首次爬取（`西电实习`，target_count=8）：约 8–10 分钟；HTTP 客户端 360 秒超时断开，但服务端任务继续跑完，**结果随连接丢失**。
- 二次爬取（target_count=4）：299 秒（约 5 分钟）完成，`status_code=200`，`needs_login=false`（登录态有效），成功返回 4 条真实笔记。
- 4 条笔记的 `likes`、`comments`、`favorites` **全部为 0**。

### 4.2 根因

1. **同步长任务接口**：`POST /local-crawl/search` 直接 `await` 完整爬取，无后台任务/进度接口。客户端超时即丢结果，也无法观察中间进度。
2. **页面节奏慢**：爬虫对每次 `page.goto` 设 30 秒超时，搜索页再随机停 2–3 秒、详情页随机停 0.8–1.8 秒，小红书页面加载慢时整体时间线性膨胀。
3. **互动数据提取不可靠**：`_meta_content` 只读 `meta[name='og:xhs:note_like'|note_comment|note_collect']` 的 content 属性；实测详情页这些 OG meta 缺失，回退成默认值 0。

### 4.3 影响

- 长爬取易触发 HTTP 网关/客户端超时，用户拿不到结果。
- 0 互动数据会让分析/选题阶段丢失「热度排序」信息，影响后续质量。

### 4.4 建议（P1）

- 将爬取改为后台任务：`POST /local-crawl/jobs` 创建任务 → `GET /local-crawl/jobs/{id}` 轮询/SSE 进度 → 结果落盘 `data/raw/`，客户端不再长连接。
- 缩短/参数化页面超时与随机停顿，增加「已收集 N/target」进度事件。
- 互动数据增加兜底：优先 OG meta，缺失时读详情页 DOM（点赞/收藏/评论按钮旁的文本），或标记 `data_missing=true` 而非静默填 0。
### 4.5 二次实测（晚间）：DOM 兜底落地后仍 0 条、耗时更极端

- 现象：
  - 6 关键词 × target=20（赞≥100/藏≥50/评≥10/一年内图文）→ 爬取 82+ 分钟仍未完成，手动 stop。
  - 1 关键词「西电研究生」× target=5、低门槛（赞≥20/藏≥10/评≥0）→ `elapsed_ms=1535139`（约 25.6 分钟）返回 `count=0`、`needs_login=false`。
  - 探针（独立 Playwright 会话、复用同一 storage_state）：搜索页返回 44 张卡片、登录有效、无登录重定向；单详情页 OG meta 全缺失，但 DOM 兜底 `.engage-bar .count` 可读到互动数 85。
- 根因：12.7 的 DOM 兜底对**部分**页面有效，但绝大多数详情页三项互动数仍取不到（或 `#detail-desc` 内容为空）→ `_is_valid`（内容非空 + 三项互动门槛 + 一年内 + 非广告）全部拒绝 → 0 有效笔记；即使门槛放最低仍无法收集。
- 影响：目标域（西电/校园类）本身互动量低，叠加提取失败 → 真实爬取管线在当前网络/页面结构下无法产出数据，整条「爬取→分析→选题→文案」链路被卡死。
- 建议（P1）：用真实页面校准详情页选择器（点赞/评论/收藏按钮 DOM）；对 `*_data_missing=true` 的笔记**降门槛放行**（仅要求内容+时间+相关性，互动缺失不拒绝）；对低互动目标域，Agent 应主动降级用 `load_notes` 注入并标注数据来源。

---

## 5. 问题四：爬虫强制有头浏览器（交互/CI 不友好）

### 5.1 现象

`XHSCrawler.crawl()` 使用 `p.chromium.launch(headless=False)`，每次爬取都会在用户桌面弹出真实 Chromium 窗口。Agent 自主运行/后台任务/CI 场景下同样如此。

### 5.2 影响

- 无人值守场景下弹窗干扰；CI/无 GUI 环境直接不可用（`headless=False` 会失败）。
- 与「发布」所需的扫码登录窗口语义混在一起，用户无法区分哪个窗口是干嘛的。

### 5.3 建议（P2）

- 增加 `headless` 配置（默认 false 用于登录，爬取阶段默认 true）；登录与爬取分离：登录用有头窗口写 storage_state，爬取复用 state 全程无头。

---

## 6. 问题五：开发期 uvicorn `--reload` 反复重启

### 6.1 现象

README 推荐的 `python run.py` 自带 `--reload`（WatchFiles）。实测期间因 `tests/check_agent_console_ui.py`、`app/agent/run_manager.py` 等文件被改动/触碰，服务自动重启多次，出现瞬时的 `/health` 超时与连接中断；若在 Agent 运行中途重启会直接打断任务。

### 6.2 建议（P3）

- 生产/实测用无 reload 启动（本次改用 `_server_launcher.py`，`reload=False` 稳定运行）；`run.py` 可加环境变量开关（如 `XHS_RELOAD=0`）。
- reload 模式下避免在项目目录内新增/修改文件，防止误触发。

---

## 7. 观测侧问题（测试工具，非产品缺陷）

- **SSE 心跳噪声**：`/agent/runs/{id}/events` 在无事件时每 0.2 秒发一次 `: heartbeat`，监控端需过滤空事件，否则日志被刷屏。
- **控制台产物面板读取时序**：任务完成瞬间（`run_completed` → 前端 `loadRun` 异步渲染 `#artifacts`）直接读 DOM 可能拿到空面板；应以 `GET /agent/runs/{id}` 的 `outputs` 为准（本次最终数据来自 `final_run.json`）。
- **内置浏览器不可用**：本会话未暴露浏览器控制工具，控制台测试改用项目自带 Playwright 真实 Chromium 完成，功能等价；内置浏览器能力受限属运行环境问题，与项目无关。

---

## 8. 改进清单汇总

| 级别 | 改进项 | 归属 |
|------|--------|------|
| P1 | 反思器/选题注入目标域关键词，对抗锚定漂移 | agent |
| P1 | 统一质量门与反思器判定口径，增加目标相关性维度 | agent |
| P1 | 最终产物去重 + 相关性筛选 | agent |
| P1 | 爬取改后台任务化 + 结果落盘 + 进度接口 | crawler/API |
| P2 | 反思器计数客观化（以实际 results 为准） | agent |
| P2 | `continue` 且计划已满时追加执行或显式报未达标 | executor |
| P2 | 爬取互动数据 DOM 兜底 + `data_missing` 标记 | crawler |
| P2 | 爬取 headless 可配置，登录/爬取分离 | crawler |
| P3 | 运行/实测默认无 reload，增加开关 | infra |
| P1 | Agent 工具层 `crawl_notes` 接入后台任务 + 进度 + 可取消 | crawler/agent |
| P2 | 反思器：`crawl_notes` 返回 0 条时限制重爬次数，超限切 `load_notes` 或停止 | agent |
| P2 | 详情页互动/内容提取选择器实测校准；`data_missing` 笔记降门槛放行 | crawler |
| P3 | 运行版本指纹 + 改代码提醒重启（防幽灵旧代码） | infra |
| P3 | 工具错误信息附参数 schema；反射对同一错误连续失败即停 | agent |

---

## 9. 复现方式

```bash
# 1) 启动服务（无 reload）
set PYTHONUTF8=1
python _server_launcher.py            # 或 uvicorn app.main:app --host 127.0.0.1 --port 8000

# 2) 打开控制台
http://127.0.0.1:8000/static/agent_console.html
# 目标：为西电（西安电子科技大学）学生生成3篇关于暑期实习的小红书分享文案，主题围绕西电实习经历，不要发布。
# 后端：openai；观察 规划→执行→反思→重规划 循环

# 3) 复现爬取慢/超时/0 互动数据
#    target_count=8 时客户端 360s 超时；target_count=4 时约 300s 完成、互动数据全 0

# 4) 复现二次实测（2026-08-12 晚间）问题
#    爬取 0 条：目标限定 1 关键词「西电研究生」target=5 低门槛 → 约 25 分钟返回 count=0
#    振荡：crawl 空 → replan → 重爬（每轮 25 分钟+），直至手动 stop
#    stop 不中断：运行中 POST /agent/runs/{id}/stop → status=stopping 但爬取线程继续跑
#    旧代码：服务启动后改 topic_service.py / 提示词模板且不重启 → generate_topics 必失败
#    提示：改代码后必须重启服务；样本/脚本文件勿经带 GBK 的控制台管道写入（会破坏中文）
```

本次运行监控产物：`_shots/test2/`（`final_run.json`、`run_events.jsonl`、`snapshots.txt`、`crawl_result.json`）。
---


## 10. 能力盘点与总体判断

> 本节只做**事实盘点**与**明确标注的主观判断**；所有问题细节已在第 2–6 节给出，此处不重复。

### 10.1 已具备的能力（事实）

| 类别 | 能力 | 落点 |
|------|------|------|
| 自主性 | 规划→执行→反思→重规划/停止 自适应循环 | `app/agent/planner.py`、`executor.py` |
| 自主性 | 工具自主选择（类型化注册表） | `tools.py` |
| 自主性 | 质量门 + 优化器 | `evaluator.py` |
| 自主性 | 护栏（敏感目标/副作用审批/广告法/内容词表） | `guardrails.py` |
| 自主性 | 预算自停（LLM 次数/token/步数） | `budget.py` |
| 自主性 | 长短记忆 + 跨运行经验（episodic/procedural） | `memory.py`、`orchestrator._remember_lesson` |
| 持续运行 | 运行持久化（原子写，重启可恢复） | `run_manager._persist/_load_runs` |
| 持续运行 | 检查点续跑 | `checkpoint.py` + `POST /agent/runs/{id}/resume` |
| 持续运行 | 定时调度（interval/每日，重启恢复） | `scheduler.py` |
| 持续运行 | TTL 清理 / 审计 / 管理鉴权 | `housekeeping.py`、`audit.py`、`admin_token` |
| 持续运行 | SSE 实时事件 + 控制台 | `/agent/runs/{id}/events`、`agent_console.html` |

### 10.2 总体判断（主观，非客观评分）

基于上述事实与本次实测：该 Agent **具备完整的产品骨架与自保护机制**，方向正确；但**尚处原型/骨架级，未达成品级**。主要差距不是缺机制，而是「机制间的判定一致性」（质量门 vs 反思器）与「决策可靠性」（振荡、产物混杂），详见第 2、3 节。是否要补齐到成品级，取决于产品目标（单机个人工具 / 面向公众），见第 11 节。

## 11. 单人消费级产品化评估与路线图

> 评估口径：**“普通人装好就能单人上手使用”的标准产品级 agent**（个人产品，非多租户 SaaS）。

### 11.1 事实：当前形态与个人产品目标的契合度

- **架构契合**：单用户画像（`profile.json`）、单人登录态、单机部署——与个人产品形态一致，无需改多租户。
- **场景完整**：爬取→分析→选题→文案→配图→发布 已垂直打通，具备一个完整 Use Case。
- **自保护齐全**：护栏、预算、审批、审计（见 10.1）。
- **当前定位事实**：仍是开发者工具（需 Python/venv/Playwright/手动配 `.env`；控制台为英文工具名 + 原始 JSON 事件 + 技术性报错），且本次实测存在振荡、产物混杂、长任务无进度等问题（第 2–6 节）。

### 11.2 事实：普通人上手即用所需的差距清单

| 类别 | 差距 | 对应现有问题 |
|------|------|--------------|
| 打包部署 | 无一键安装/桌面版/Docker；需手工装依赖 | 环境依赖 |
| 引导配置 | LLM 密钥、小红书扫码登录、图片 key 全手动 | 登录态流程（README） |
| 界面产品化 | 控制台为开发者工具，无向导/模板/进度条/人话报错 | 前端现状 |
| 可靠性 | 振荡、无关产物混杂、长任务无进度、登录过期快速失败 | 第 2/3/4 节 |
| 安全合规 | 爬取真实数据 + 对外发布，缺内容审核/广告法/隐私处理 | 第 4/5 节 |
| 成本控制 | 默认 token/调用不设上限，无用户可见预算 | budget 默认值 |
| 质量保障 | 无验收评测与产物相关性硬校验 | 第 2 节 |

### 11.3 判断（主观，明确标注）

- 今天它属于「**面向熟悉技术个人的 Prosumer Agent 工具**」，不是「大众通用产品」。
- 按“每个个体都能单人上手”的方向看：**架构方向是对的**，缺的是整整一轮产品化工程；难的部分不是 UI/打包，而是 **决策稳定性、外部依赖可靠性、安全合规**（正是本次实测暴露的核心）。
- 因此：**当前不可作为“标准产品级”直接交付给普通人；修复质量与可靠性后，这个目标是可达到的。**

### 11.4 落地路线图

| 阶段 | 目标 | 关键交付 |
|------|------|----------|
| M0 · 质量修复 | 决策可靠、产物干净 | 反思器/选题注入目标域关键词；质量门与反思器口径统一；产物去重择优；振荡检测（第 2 节建议） |
| M1 · 长任务化 | 慢流程可感知可恢复 | 爬取改后台任务 + 进度 + 结果落盘；登录过期自动重登/清晰引导（第 4 节建议） |
| M2 · 打包与引导 | 普通人装得上、配得好 | 一键安装包/桌面壳/Docker；初始化向导（扫码登录、自动识别密钥、默认安全配置） |
| M3 · 消费级 UI | 上手即用 | 聊天式界面、模板、进度条、人话报错、发布前预览确认、预算/配额控件 |
| M4 · 安全合规 | 可对外提供 | 内容安全审核、广告法/版权合规、隐私与数据删除、密钥加密存储、错误上报与更新机制 |

### 11.5 验收标准（示例）

- 一个零技术背景用户：下载 → 扫码登录 → 选模板/填一句目标 → 10 分钟内得到 3 篇可用的图文文案（或明确、可理解的失败原因）。
- 连续 10 次不同主题运行：无反思振荡死循环、产物无目标外混杂、全部在预算内完成。
- 任意一次失败（登录过期/网络/接口异常）：有用户可读提示与一键重试/降级，而非技术报错。

---

## 12. 优化架构规划与具体实现方式

> 本节把第 8 节改进清单与第 11.4 路线图落到**文件 / 函数 / 数据结构 / 代码片段**层面，可直接据此实施。所有引用的符号名均与当前源码核对一致。每个子系统的改动按「改动点 → 数据结构 → 关键逻辑 → 验证」给出。

> 状态（2026-08-12 落地）：**M0 质量修复 12.1–12.5 与爬取侧 12.6–12.9 已全部实现**并补对应单测，全量 `python -m pytest tests/ -v` 73 项保持绿；12.10 建议节奏里 M1 其余长任务化（登录过期自动重登、外部依赖熔断）与 M2+ 待后续。

### 12.1 目标域约束注入（P1，对抗锚定漂移）

**根因回顾**：`TopicsParams` / `ContentsParams` 无目标域字段；`PLANNER_TEMPLATE`（[planner.py:28-52](app/agent/planner.py#L28-L52)）只把 goal 原样传 LLM，且第 47-50 行明确「PREFER chaining sample data」，导致每轮被样例锚定到「宿舍收纳」。

**改动点**：

1. `app/agent/tools.py` 给两个参数模型加字段：
   ```python
   class TopicsParams(BaseModel):
       ...
       focus_keywords: list[str] = Field(default_factory=list,
           description="目标域关键词，选题必须命中至少一个（由 goal 抽取/注入）")

   class ContentsParams(BaseModel):
       ...
       focus_keywords: list[str] = Field(default_factory=list,
           description="目标域关键词，正文/标题必须体现")
   ```
   `app/models/schemas.py` 的 `TopicGenerateRequest` / `ContentGenerateRequest` 同步加同名字段；`_topics` / `_contents` handler（[tools.py:181-206](app/agent/tools.py#L181-L206)）透传到 `topic_service` / `content_service` 的 prompt 模板。

2. `app/agent/planner.py` 在 `PLANNER_TEMPLATE` 的 Rules 追加一条：
   ```
   - 若目标含明确主题域（如"暑期实习""考研"），必须在 generate_topics / generate_contents 的 params.focus_keywords 里列出该域关键词；选题与正文须命中至少一个。
   ```

3. `Planner.replan`（[planner.py:100](app/agent/planner.py#L100)）签名加 `focus_keywords: list[str] | None = None`，非空时拼进 prompt 并要求新 plan 沿用，避免每轮重规划丢失目标域。

**验证**：`tests/test_agent.py::test_focus_keywords_injected`——给定 goal「为西电学生生成 3 篇暑期实习文案」，断言 plan 中 `generate_contents` 步骤 `params.focus_keywords` 非空且含「实习」。

---

### 12.2 判定口径统一 + 目标相关性维度（P1）

**根因回顾**：`RUBRIC`（[evaluator.py:20-28](app/agent/evaluator.py#L20-L28)）7 项全是内容质量，无相关性；`Reflector.judge`（[planner.py:116-122](app/agent/planner.py#L116-L122)）独立判 goal satisfied，两者结论可矛盾。

**改动点**：

1. `evaluator.py::RUBRIC` 增加第 8 项：
   ```
   8. 目标相关性：标题/正文命中 focus_keywords 至少 1 个，未跑题
   ```
   `Evaluator.evaluate` 签名加 `focus_keywords: list[str]`，拼进 `EVALUATE_TEMPLATE` 上下文；`QualityGate.run`（[evaluator.py:95](app/agent/evaluator.py#L95)）签名加 `focus_keywords`，下传给 `evaluate`。

2. `executor.py::_refine_contents`（[executor.py:294](app/agent/executor.py#L294)）调用处改为 `self.quality_gate.run(items, focus_keywords=focus)`，`focus` 从当前 plan 的 `generate_contents` 步骤 `params.focus_keywords` 取（由 12.1 注入）。

3. **统一口径**：让反思器复用质量门结果。`_refine_contents` 已把 `quality_report` 写进 `result["quality_report"]` 并经 `memory.record` 落地；修改 `REFLECT_TEMPLATE`（[planner.py:54-64](app/agent/planner.py#L54-L64)）增加一段：
   ```
   本轮质量报告（客观，以此为准）：
   {quality_report}
   ```
   `Reflector.judge` 从 `memory.latest("generate_contents")` 取 `quality_report` 填入，避免 LLM 自述。

**验证**：`tests/test_agent.py::test_relevance_gate_blocks_off_topic`——构造 `focus_keywords=["实习"]`、正文为「宿舍收纳好物」的草稿，断言 `QualityGate` 判 `verdict=revise` 或 `score<7`。

---

### 12.3 产物去重 + 相关性择优（P1）

**根因回顾**：`_collect_outputs`（[orchestrator.py:286-295](app/agent/orchestrator.py#L286-L295)）`results.extend(c.get("contents", []))` 全量累积，跨轮 9 篇含无关内容、无去重。

**改动点**：重写 `app/agent/orchestrator.py::_collect_outputs`：

```python
def _collect_outputs(memory: AgentMemory, focus_keywords: list[str] | None = None,
                     strategy: str = "best_n", top_n: int = 3) -> dict[str, Any]:
    ...
    contents = memory.results_for("generate_contents")
    if contents:
        results, quality_reports = [], []
        for c in contents:
            if isinstance(c, dict):
                results.extend(c.get("contents", []))
                if c.get("quality_report"):
                    quality_reports.append(c["quality_report"])
        if strategy == "last_round":
            results = results[-top_n:]                 # 仅末轮
        else:                                          # best_n（默认）
            results = _dedupe_by_title(results)
            if focus_keywords:
                results = _rank_by_relevance(results, focus_keywords)
            results = results[:top_n]
        outputs["results"] = results
        ...
```

新增辅助函数（同文件）：

- `_dedupe_by_title(items)`：按 `title` + `body[:60]` 归并，重复时保留 `quality_report.score` 高者（需把单篇 score 透传到 item，可写入 `item["_score"]` 临时字段，输出前剔除）。
- `_rank_by_relevance(items, focus_keywords)`：命中 `focus_keywords` 数降序，同命中按 score 降序。

`orchestrator.run` 调用处传入 `focus_keywords`（从 plan 抽取）与 `settings.agent_collect_strategy` / `settings.agent_top_n`。

**配置**：`app/core/config.py::Settings` 加 `agent_collect_strategy: str = "best_n"`（可选 `all` / `last_round` / `best_n`，`all` 兼容旧行为）、`agent_top_n: int = 3`。

**验证**：`tests/test_agent.py::test_collect_outputs_dedupes_and_ranks`——造 3 轮 9 篇（含标题重复、含「宿舍收纳」跑题、含「实习」命中），断言 `outputs["results"]` 去重后按相关性排序且长度 ≤ top_n。

---

### 12.4 振荡检测与定向纠偏（P1）

**根因回顾**：`executor.execute`（[executor.py:134-180](app/agent/executor.py#L134-L180)）每次 `_replan_requested` 即直接 `replan`，无连续相同原因计数；`Budget`（[budget.py](app/agent/budget.py)）只计 LLM/token/步数。

**改动点**：

1. `Executor.__init__` 加状态：
   ```python
   self._replan_history: list[str] = []     # 每次 replan 的 reason
   self._force_focus: list[str] | None = None
   MAX_REPLAN_SAME_REASON = 3
   ```

2. `_run_step` 中反思器判 `replan` 时（[executor.py:289-290](app/agent/executor.py#L289-L290)）：
   ```python
   if verdict.next_action == "replan":
       self._replan_history.append(verdict.reason)
       if _same_reason_streak(self._replan_history) >= MAX_REPLAN_SAME_REASON:
           self._force_focus = _extract_focus(goal)   # 强制注入目标域
           self.tracer.event("oscillation_break",
                             streak=MAX_REPLAN_SAME_REASON, reason=verdict.reason)
       self._replan_requested = True
   ```

3. `executor.execute` 调 `replan` 处（[executor.py:148](app/agent/executor.py#L148)）传入 `focus_keywords=self._force_focus`；`Planner.replan` 收到非空 `focus_keywords` 时，在 prompt 标注「前 N 轮因相同原因失败，禁止重复同一计划形态，必须换源数据或在 params.focus_keywords 注入目标域」。

4. `Budget` 加 `max_replans: int = 0`（0=不限），`exceeded` / `reason` 增加对 `replans` 计数的判定；`from_settings` 读 `settings.agent_max_replans`。达到上限时 `tracer.event("budget_stop", reason="replan_oscillation")`，`AgentResult.stopped_reason` 带原因。

**新增辅助**：
- `_same_reason_streak(history)`：从末尾向前数连续相同 reason（可用 `_goal_signature` 同款归一化）。
- `_extract_focus(goal)` / `_extract_target_count(goal)`：从 goal 文本抽目标域关键词与篇数（正则 `\d+\s*篇`）。

**验证**：`tests/test_agent.py::test_oscillation_break`——mock 反思器连续 3 次返回相同 reason 的 `replan`，断言第 3 次触发 `oscillation_break` 事件且后续 `replan` 调用收到非空 `focus_keywords`。

---

### 12.5 反思器计数客观化 + 计划已满追加执行（P2）

**根因回顾**：`Reflector.judge` 输入仅 goal/plan/`memory.observations()`（最近 5 步摘要），无 results 篇数，导致同一轮内 3 次说达成、1 次说还差 2 篇（见第 3 节）。

**改动点**：

1. `app/agent/memory.py::AgentMemory` 加便捷方法：
   ```python
   def count_results(self, action: str, predicate=None) -> int:
       n = 0
       for r in self.results_for(action):
           if isinstance(r, dict):
               for item in r.get("contents", []):
                   if predicate is None or predicate(item):
                       n += 1
       return n
   ```

2. `Reflector.judge` 签名加 `progress: dict`，`REFLECT_TEMPLATE` 增加：
   ```
   客观进度（以实际 results 为准，勿自述）：
   - 已产出文案数：{progress["total"]}
   - 命中目标域且质量合格数：{progress["on_target_passed"]}
   - 目标要求篇数：{progress["target"]}
   ```

3. `executor._run_step` 反思前组装 `progress`：
   ```python
   focus = _step_focus(plan, "generate_contents")
   progress = {
       "total": memory.count_results("generate_contents"),
       "on_target_passed": memory.count_results("generate_contents", _on_target(focus)),
       "target": _extract_target_count(goal),
   }
   verdict = await self.reflector.judge(goal, plan, memory, progress=progress)
   ```
   `_on_target(focus)` 返回谓词：标题或正文命中任一 focus 关键词且 `quality_report` 标 passed。

4. **计划已满但 continue 的追加执行**：`executor.execute` 循环末尾，若 `verdict.next_action=="continue"` 且 `not ready`（计划跑完、无就绪步骤），则调用 `planner.append_steps(goal, deficit)` 生成补足步骤（仅 `generate_contents`，`deficit = target - on_target_passed`），而非 `break`。需在 `Planner` 加 `append_steps` 方法。

**验证**：`tests/test_agent.py::test_reflector_uses_objective_count`——memory 里 3 篇合格、目标 3 篇，断言反思器 prompt 含「已产出 3 / 目标 3」且不再返回「还差 2 篇」；`test_append_steps_when_continue_and_full`——反思器返 continue 且计划跑完时，断言新增 generate_contents 步骤。

---

### 12.6 爬取后台任务化 + 进度 + 结果落盘（P1）

**根因回顾**：`POST /local-crawl/search`（[routes_local_site_crawler.py:11-19](app/api/routes_local_site_crawler.py#L11-L19)）同步 `await crawl_local_site_notes(request)`，无后台任务、无进度；客户端 360s 超时即丢结果。

**架构**：拆为 create → poll/SSE → 结果落盘 `data/raw/crawl_jobs/<job_id>.json`。

**改动点**：

1. 新增 `app/services/crawl_job_manager.py`（内存任务表 + 文件持久化）：
   ```python
   @dataclass
   class CrawlJob:
       job_id: str
       status: str = "pending"          # pending | running | done | failed
       progress: dict = field(default_factory=dict)  # {collected, target, current_keyword}
       result: Optional[dict] = None    # SearchCrawlResponse.model_dump()
       error: Optional[str] = None
       created_at: float
   ```
   `CrawlJobManager` 提供 `create / get / update_progress / save_result`，结果落盘到 `data/raw/crawl_jobs/<job_id>.json`。

2. `app/api/routes_local_site_crawler.py` 新增三个端点：
   ```python
   @router.post("/jobs")
   async def create_crawl_job(request: SearchCrawlRequest) -> dict:
       job = crawl_job_manager.create(request)
       asyncio.create_task(_run_crawl_job(job.job_id, request))  # 后台执行
       return {"job_id": job.job_id, "status": job.status}

   @router.get("/jobs/{job_id}")
   async def get_crawl_job(job_id: str) -> dict: ...

   @router.get("/jobs/{job_id}/events")   # SSE 进度
   async def crawl_job_events(job_id: str) -> StreamingResponse: ...
   ```
   保留 `POST /search` 作为同步兼容入口（内部 create + 轮询至 done，或直接调旧路径），并在文档标注 deprecated。

3. `app/services/local_site_crawler_service.py::XHSCrawler.crawl`（[local_site_crawler_service.py:89](app/services/local_site_crawler_service.py#L89)）加进度回调：
   ```python
   async def crawl(self, on_progress=None) -> SearchCrawlResponse:
       ...
       # 每次 _add_note 后：
       if on_progress:
           await on_progress({"collected": len(self.collected),
                              "target": self.request.target_count,
                              "keyword": keyword})
   ```
   后台任务 `_run_crawl_job` 把 `on_progress` 写入 `CrawlJob.progress` 并经 SSE 推送。

**验证**：新建 `tests/test_crawl_jobs.py`——create job → 轮询 status 由 running→done → 结果从落盘文件可读 → SSE 收到至少一条 progress 事件。

> **★ 二次实测（晚间）**：`/local-crawl/jobs` 后台任务已落地，但 Agent 工具层 `crawl_notes`（tools.py `_crawl`）仍同步 `await crawl_local_site_notes(req)` 且未传 `on_progress` → 控制台/SSE 无进度事件、stop 无法中断进行中的爬取（见 13.2）。

---

### 12.7 互动数据 DOM 兜底 + data_missing 标记（P2）

**根因回顾**：`_meta_content`（[local_site_crawler_service.py:375-382](app/services/local_site_crawler_service.py#L375-L382)）仅读 OG meta，缺失返回 None，`_parse_number(None)=0`（[:422-424](app/services/local_site_crawler_service.py#L422-L424)），无 DOM 兜底。

**改动点**：`_fetch_note_detail`（[local_site_crawler_service.py:219-263](app/services/local_site_crawler_service.py#L219-L263)）：

```python
like_text = await self._meta_content(page, self.META_LIKE)
like_missing = like_text is None
if like_missing:
    like_text = await self._dom_count(page, self.LIKE_BTN_SELECTORS)

return NoteItem(
    ...
    likes=self._parse_number(like_text),
    likes_data_missing=like_missing and not like_text,   # 新字段
    ...
)
```

新增：
- 类常量 `LIKE_BTN_SELECTORS / COMMENT_BTN_SELECTORS / COLLECT_BTN_SELECTORS`：详情页点赞/评论/收藏按钮旁文本选择器（需实测确定，如 `".engage-bar .like .count"`、`".count"` 等）。
- `_dom_count(page, selectors)`：依次尝试选择器，取按钮旁文本解析数字（复用 `_parse_number`）。

`app/models/schemas.py::NoteItem` 加 `likes_data_missing: bool = False`（同理 `comments_data_missing` / `favorites_data_missing`），下游 `analyze_notes` 可据此降权或标注。

**验证**：`tests/test_crawler_login.py::test_interaction_dom_fallback`——mock OG meta 缺失、DOM 有「1.2万」，断言 `likes==12000` 且 `likes_data_missing=False`；两者都缺时 `likes==0` 且 `likes_data_missing=True`。

> **★ 二次实测（晚间）**：DOM 兜底对真实页面效果有限——单详情页可读到 85，但绝大多数页面互动数/内容仍取不到，低门槛 target=5 仍 0 条（见 4.5）。

---

### 12.8 headless 可配置 + 登录/爬取分离（P2）

**根因回顾**：`XHSCrawler.crawl` 硬编码 `headless=False`（[local_site_crawler_service.py:94-97](app/services/local_site_crawler_service.py#L94-L97)）。

**改动点**：

1. `app/core/config.py::Settings` 加 `crawl_headless: bool = True`（爬取默认无头；登录仍用有头）。
2. `XHSCrawler.__init__` 接收 `headless: bool = True`；`crawl()` 用 `self.headless` 替换硬编码。`crawl_local_site_notes`（[local_site_crawler_service.py:481-483](app/services/local_site_crawler_service.py#L481-L483)）从 `settings.crawl_headless` 读取并传入。
3. 登录/爬取分离：`test_crawler.py`（首次登录，有头）写 `xhs_state.json` 不变；爬取路径（`crawl_notes` 工具 / 后台任务 / Agent）复用 state 且 `headless=True`。`_ensure_logged_in` 在 headless 且无 state 时已有 `sys.stdin.isatty()` 快速失败逻辑（[:347-352](app/services/local_site_crawler_service.py#L347-L352)），无需改动。

**验证**：`tests/test_crawler_login.py::test_headless_config`——断言 `settings.crawl_headless=True` 时传给 `p.chromium.launch` 的 `headless=True`。

---

### 12.9 reload 开关（P3）

**根因回顾**：`run.py` 硬编码 `reload=True`（[run.py:10](run.py#L10)），实测期间文件触碰致反复重启。

**改动点**：`run.py`：
```python
import os
...
reload = os.environ.get("XHS_RELOAD", "1") != "0"
uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=reload)
```
`_server_launcher.py` 已是 `reload=False`，保持作为生产/实测入口；README「方式一」补一句「长跑/实测设 `set XHS_RELOAD=0`」。

**验证**：手动 `set XHS_RELOAD=0 && python run.py`，改动项目内文件观察不再触发 reload。

---

### 12.10 实施顺序与依赖

```
12.9 (reload 开关) ── 独立，先做，稳住测试环境
   │
12.1 (focus_keywords 注入) ── 12.2 / 12.3 / 12.4 / 12.5 的共同前置
   ├── 12.2 (相关性维度 + 口径统一) ── 依赖 12.1 的 focus_keywords
   ├── 12.3 (产物去重择优)       ── 依赖 12.1 做相关性排序
   ├── 12.4 (振荡检测)           ── 依赖 12.1 的「换策略=强制注入 focus」
   └── 12.5 (计数客观化)         ── 依赖 12.1 判定 on_target_passed
12.6 / 12.7 / 12.8 (爬取侧) ── 相互独立，可并行
```

**建议节奏**：M0 先做 `12.9 + 12.1 + 12.2 + 12.3`（质量修复闭环，消除振荡与产物混杂）；验证通过后做 `12.4 + 12.5`（稳定性，定向纠偏与计数客观化）；爬取侧 `12.6 / 12.7 / 12.8` 进入 M1 长任务化。

**回归基线**：每完成一个子系统补对应单测（见各节验证项），全量 `python -m pytest tests/ -v` 保持绿；之后用第 9 节复现方式做一次真实联调，对比 `final_run.json` 中「反思 replan 次数 / 产物篇数与相关性 / 爬取是否超时」三项指标是否改善。

---

## 13. 二次实测新增问题（2026-08-12 晚间）

> 本节为晚间二次真实联调补充：目标「围绕西电研究生写 3 篇文案」，真实后端 openai（Codex 配置 `deepseek-v4-flash[1M]` + volces ark），真实 Chromium 驱动控制台 + SSE 全程监控；排除图片/发布/飞书。登录态有效（`needs_login=false`），无需重新登录。

### 13.1 问题六：爬取空数据 → Agent 振荡空转（每轮 25 分钟+）

- 现象：`0777eb16c17f` 中 crawl 返回 0 条 → reflection 判定 `replan`（「爬取结果为空，需调整关键词或放宽条件重新爬取」）→ 再次 crawl（同参数）→ 又空 → analyze 空 → 再 replan，形成无限循环。
- 根因：反思器对「爬取 0 条」只做 replan 重爬，缺少「判定当前环境无法产出有效数据 → 停止或切换数据源（`load_notes`）」的决策；12.4 振荡检测未覆盖「工具级稳定失败模式」。
- 影响：单轮爬取 25–82 分钟，振荡代价极高；多 run 并行时互相争抢带宽，进一步拖慢。
- 建议（P2）：反思器对 `crawl_notes` 返回 0 条时最多允许 1 次重爬，之后强制切 `load_notes` 或停止；振荡检测对「同一工具同参数连续失败」直接熔断并报告。

### 13.2 问题七：stop 无法中断进行中的爬取 + 无进度事件

- 现象：
  - `POST /agent/runs/{id}/stop` 仅置 `stop_requested`，orchestrator 在工具返回后才检查 → 进行中的 `crawl_notes` 无法被中断。`41127b9e8303` 标记 `stopping` 后仍空转，直至爬虫浏览器意外关闭（`Page.goto: Target page, context or browser has been closed`）才 `step_failed` → `run_stopped`。
  - `tools.py::_crawl` 调用 `crawl_local_site_notes(req)` 未传 `on_progress` → SSE/控制台全程只有 `tool_started`，无「已收集 N/target」中间事件；服务端 `print` 在 stdout 重定向/缓冲下不可见。
- 影响：用户无法取消长任务；无法区分「爬取中」与「卡死」。
- 建议（P1）：Agent 工具层接入 12.6 的后台任务（`/local-crawl/jobs` + SSE 进度），`crawl_notes` 支持取消（asyncio 协程取消 / 检查 `stop_requested`），控制台展示进度。

### 13.3 问题八：reload=False 长跑服务运行「幽灵旧代码」导致 generate_topics 必失败

- 现象：服务 17:15 启动（`reload=False`），而 `topic_service.py`/`schemas.py`/`tools.py`/提示词模板在 19:24–19:43 才更新。磁盘新模板要求 `{focus_keywords}`，运行中的旧 `generate_topics` 不传 → LangChain 报 `missing variables {'focus_keywords'}`。Agent 反射重试并在 params 补上 `focus_keywords`，但工具层（旧代码）仍丢弃 → 重试同样失败 → 振荡。重启服务（加载新代码）后该问题消除。
- 根因：`reload=False` 时人工改代码不自动生效；进程长期运行后「内存代码 ≠ 磁盘代码」且无任何版本提示。
- 建议（P3）：启动日志打印运行时间 + 代码指纹（关键文件 hash）；提供 `/health/version`；开发期明确「改代码必须重启」，或默认开 reload。

### 13.4 问题九：Agent 反射用工具不支持的参数「修复」输入，无效振荡

- 现象：`load_notes` 因样本文件带 BOM 失败后，Agent 反射连续尝试 `encoding: "utf-8-sig"`、`file_encoding: "utf-8-sig"`、`file_encoding: "gbk"` 等参数，但 `LoadNotesParams` / `load_sample_notes` 不支持这些参数 → 每次以相同错误失败 → 振荡。
- 根因：LLM 反射基于错误信息猜测参数，但工具 schema 不认 → 重试永远无效；错误信息未提示「可用参数列表」。
- 建议（P3）：工具抛错时在 summary 附「支持参数 schema」；反射器对「同一工具连续 N 次失败且错误相同」直接停止并报告，避免无效重试。

### 13.5 二次实测中验证通过的环节

- 规划→执行→反思→追踪 机制与 SSE 事件流（`run_started`/`plan_created`/`step_started`/`tool_started`/`tool_completed`/`step_failed`/`reflection`/`replan`/`run_stopped`）正常。
- 数据干净时 `load_notes` → `analyze_notes` 正确（关键词：西电/研究生/科研/秋招 等）。
- 登录态感知正常（`needs_login=false`），爬取无需重新登录。
- 观测注意：PowerShell 管道/控制台会破坏中文（GBK→`?`），写样本/脚本须走 UTF-8 文件；服务 stdout 重定向时 print 缓冲不可见。

### 13.6 实测结论：产品定位判断与「三刀」修复路径

> 本节记录二次实测后的总体判断（主观结论 + 可执行修复路径），供决策参考。

**判断（主观）**

- Agent 编排骨架可用：规划→执行→反思→追踪、SSE 事件流、护栏/预算/记忆/续跑等机制实测正常，不是「假 agent」。
- **当前最大短板 = 真实爬取不可用**：`crawl_notes` 在真实小红书页面上互动数/内容提取失败 → 0 条 → 整条「爬取→分析→选题→文案」链路被卡死。爬取不修复，产品对真实数据场景「基本不可用」。

**三刀修复路径（P1，按依赖排序）**

1. 详情页数据提取校准：真实页面校准点赞/评论/收藏 DOM 选择器；对 `*_data_missing=true` 的笔记降门槛放行（仅要求内容+时间+相关性，互动缺失不拒绝），避免 0 条。
2. `crawl_notes` 长任务化：复用已落地的 `/local-crawl/jobs` + SSE 进度，Agent 工具层接入后台任务，支持取消（`stop_requested` / asyncio 取消），控制台显示进度。
3. 空数据熔断与降级：反思器对 `crawl_notes` 返回 0 条限 1 次重爬，超限自动切 `load_notes` 或停止并报告；振荡检测覆盖「同一工具同参数连续失败」。

**验收标准**：修完后用「西电研究生」目标重测，要求 1 关键词 target=5 在 ≤5 分钟内返回 ≥5 条有效笔记（互动数据允许缺失，但内容/时间/相关性齐全），且 Agent 能走完 爬取→分析→选题→文案。


---

## 14. 修复落地记录（2026-08-12，当日二次联调后）

> 本节记录 §13 各问题对应的编码修复（均已落地并有单测，`python -m pytest tests/` 83 绿）。

### 14.1 三刀 #1 — 互动缺失降门槛 + 内容兜底（13.6）

- `_is_valid`（local_site_crawler_service.py）：`*_data_missing=true` 的笔记跳过对应互动阈值检查，仅保留「内容+相关性+时间」校验；`publish_time` 缺失时放行（有日期且过期仍拒绝）。真实页面互动数/日期常缺失，此前整批 0 条。
- 新增 `DETAIL_CONTENT_SELECTORS`（`#detail-desc` 兜底 `.note-content` 等），正文提取不到主选择器时有次选。
- 验证：`tests/test_real_run_fixes.py::test_is_valid_*`（缺失放行 / 非缺失仍拒 / 时间缺失放行 / 内容与相关性仍必须）。

### 14.2 三刀 #2 — crawl_notes 取消 + 进度（13.2）

- `XHSCrawler.crawl(..., should_cancel=None)`：关键字/卡片循环边界检查，中断则置 `cancelled=True` 返回已收集部分（`SearchCrawlResponse.cancelled` 新字段），不再跑到底。
- Executor 用模块级 `_crawl_ctx`（ContextVar）在调用 `crawl_notes` 时注入 `stop_check` + tracer sink；tools.py `_crawl` 读取后转成 `should_cancel` 与 `on_progress`（每收集一条发 `crawl_progress`）。
- `run_manager._from_agent_event` 新增 `crawl_progress` → SSE；前端 agent_console.html 映射 `采集进度` 并在采集按钮显示「已收集 N/target」。
- 停止链路闭环：`POST /agent/runs/{id}/stop` 置 `stop_requested` → 爬取协程轮询到 → 优雅返回部分结果 → run 以 `stopped` 收尾。
- 验证：`test_crawl_cancelled_via_should_cancel` / `test_crawl_cancelled_result_stops_run` / `test_crawl_progress_piped_through_executor_ctx`。

### 14.3 三刀 #3 — 空爬取熔断（13.1）

- Executor `_track_crawl_result`：`crawl_notes` 返回 `count==0` 连续 2 次（首次放行交给反思，第 2 次）熔断 `_stop_requested` 并发 `crawl_stalled` 事件；`cancelled=true` 直接停。消除「每轮 25 分钟空爬振荡」。
- 验证：`test_crawl_empty_circuit_breaker_stops_after_second_empty`。

### 14.4 无效重试熔断 + 错误附可用参数（13.4）

- Executor 按「工具名 + 错误字符串」记录连续失败；同一工具同错误连续 2 次即停止重试并发 `tool_stall`（此前 LLM 靠错误文本乱猜 `encoding=gbk` 等不支持参数，振荡）。
- 失败错误附加 `[工具 X 可用参数]: ...`（取 params schema 字段名），让反思基于真实 schema 修正而非猜测。
- 验证：`test_identical_error_stops_retries`。

### 14.5 幽灵旧代码防护（13.3）

- `/health` 扩展 `code_fingerprint` + `uptime_seconds`；新增 `GET /health/version`。指纹 = 关键文件（app/agent|services|prompts|api|models|core）SHA-256 前 12 位，改码即变。
- 启动日志（main.py lifespan）打印 `version + code_fingerprint` 与「reload=False 长跑须重启」提示。
- 验证：`test_health_reports_code_fingerprint` / `test_health_version_endpoint`。

### 14.6 待真机实测项

- 详情页互动 DOM 选择器（`LIKE_BTN_SELECTORS` 等）为启发式值，需真实页面截图核对；内容兜底选择器同理。
- 验收：用「西电研究生」目标重测，1 关键词 target=5 ≤5 分钟内 ≥5 条有效笔记（互动允许缺失，内容/时间/相关性齐全），Agent 走完 爬取→分析→选题→文案。
