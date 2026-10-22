# content-agent · 工具调用可评测 Agent 完整项目 Spec

> Status: **DRAFT v0**
> Owner: 项目作者（研究生 / Agent 入门）；本项目不设团队
> 创建日期：2026-10-22
> 对齐参考仓：`jerry-ai-dev/post-training-slot-extractor` @ `e652df8`（2026-09-07），主要参照其 `finetune-spec.md`（约 1.3k 行、11 节）、`docs/project-structure.md`、Phase 05 tool-loop 对比页实现计划（`docs/superpowers/plans/2026-08-12-phase05-tool-loop-app.md`）与 `data/eval/DATASET_CARD.md`
> 落点仓：`whickerpoplawski-arch/content-agent`（private，`main`）；已有切片见 PR #2 `cursor/topic-helper-demo-943b`
> 合同：`docs/contracts/tool_calling_io_v0.md`（PR #2）；目标卡：`GOAL_CARD_工具调用_v0.md`
> 建议落库路径：`docs/FULL_PROJECT_SPEC_tool_calling_v0.md`

---

## 前言：这份文档怎么用

参考仓的 `finetune-spec.md` 的价值不在于「结论」，而在于它是**一份能照着施工的工程规格**：先定任务合同（输入/输出/唯一表示），再做选型（基座/后训练/压缩/框架/推理），再定评测与数据（两套严格隔离的数据集），最后排施工顺序（Phase 01–07 + 里程碑 M0–M4），每一步都有**产物路径 + 验收线**。

本项目把同一套方法论**整体搬到「工具调用」任务上**：

- 参考仓训练的是「从多轮对话抽取预约槽位 JSON + 判断是否调 `find_technicians`」；
- 本项目评测/训练的是「给定 theme，判断调哪些工具、填什么参数、把工具结果融入 `angles[]`，失败时诚实降级」；
- 参考仓的「预约业务」→ 本项目的「小红书选题」只是**垂域外壳**；主线永远是 **tool calling 的中间过程是否可评测**。

三处与参考仓的**结构性差异**，全文反复体现：

| 差异 | 参考仓 | 本项目 | 原因 |
|---|---|---|---|
| 训练是否必做 | 必做（项目目标就是后训练） | **可选**，Gate 之前禁止开训 | 用户目标是简历对准「评测/数据/Agent 应用」实习，先证明尺子正确 |
| 部署目标 | 本地 CPU（离线推理） | **本地演示可离线（mock）**；训练在 Kaggle 双 T4 | 无稳定本地 GPU；面试要能一键复现 |
| 工具循环 | 单工具、最多三轮 | **最多 2 轮、4 个白名单工具**，确定性编排优先 | 小模型实验矩阵不必要；先把轨迹断言做对 |

阅读顺序建议：A（问题与成功标准）→ B（目录）→ D（评测设计）→ C（阶段）→ 其余按需。**D 是规格核心**，C 依赖 D 的产出。

---

## A. 问题定义与成功标准

### A.1 一句话目标

> 在内容-agent 仓内，实现并冻结评测一条**可陈述、可复现的工具调用路径**：`theme →（判断是否检索）→ 调用白名单工具并填对参数 → 把工具结果融入 3–5 个选题角度 → 工具失败时降级/拒答**；用冻结评测集证明该路径在「轨迹正确率 + 相对裸 LLM 的选题质量」上稳定过关。

### A.2 非目标（写死，防止范围膨胀）

1. 不做完整笔记正文、排期、配图、自动发帖、飞书同步的**评测**（这些工具仅存在于仓内，演示与 Gate 默认禁用）。
2. 不评真实爬取质量、不评发帖成功率、不评多 Agent 协作。
3. 不宣称「涨粉/爆款率/平台官方数据/已上线产品」。
4. 不设第二个主仓，不 fork `slot-extractor` 当产品仓——所有代码长在 `content-agent` 内。
5. 不并入、不续训任何既有历史微调线（本规格**全文只此一处提及**）。
6. 不追求「工具调用 SOTA」；只报冻结集上的相对指标与轨迹指标。

### A.3 简历可写边界

**可以写**：实现了带白名单工具注册表的 Agent 路径；构建了 N 条带 `expected_trace` 的冻结评测集；用确定性断言评测工具选择/参数/结果融入；测出相对同模型裸生成的选题质量 Δ；实现了 mock/live 双模式与一键 Gate 脚本。

**禁止写**：
- 「上线/落地/服务真实用户」
- 「爆款率提升 X%」「涨粉 X」等平台数据
- 「训练了工具调用 SOTA 模型」（未做训练矩阵时）
- 任何**未跑出**的 Gate 数字（数字位一律先用 `X` 占位，出报告后回填并附报告路径）。

### A.4 与 slot-extractor 的任务映射

| 维度 | slot-extractor（预约） | 本项目（工具调用 + 选题） |
|---|---|---|
| 输入 | `history` + `user_input` + `current_time` + 工具结果 | `theme` + `audience` + `count` + 文件内 `KB_SNAPSHOT` + 白名单工具描述 |
| 中间产物 | `action=tool_call` + `tool_name` + `arguments` | `trace.steps[]`：`tool` + `params` + `ok` + `result_digest` |
| 终态产物 | Final JSON（槽位） | `status` + `angles[{angle,title,why,audience,grounded_from}]` |
| 唯一事实来源 | 技师库 YAML fixture（固定日期） | `KB_SNAPSHOT`（冻结知识库快照，逐条带 `doc_id`） |
| 工具 | `find_technicians`（1 个，5 参） | `knowledge_search` / `load_notes` / `analyze_notes` / `generate_topics`（白名单 4 个） |
| 反例 / 幻觉 | 编造列表外技师 | 编造互动数据、无视空检索结果瞎写、调用黑名单工具 |
| 基线 M0 | 未微调基座 | **同模型、同解码、无工具裸生成 `angles[]`** |
| 冻结集 | `data/eval/test.jsonl` | `data/evals/tool_calling_v0.jsonl` |
| 对比页 | Phase 05 双模型 tool-loop | P4 双路径（裸 LLM \| Agent+工具，可扩展第三栏微调） |
| 训练 | LoRA SFT→DPO，Kaggle/4090 | 可选：从轨迹蒸馏 tool-call SFT，Kaggle 双 T4 |
| 评测纪律 | eval 冻结、与训练零重叠 | 同（**逐字保留**） |

### A.5 可量化成功标准（G1–G5 的可测断言）

以下断言均须**可由脚本判定**，不接受人工口头结论。`Gate runner` 输出一份 `reports/gate/<date>_<commit>.md`，逐项填实测值。

| # | 名称 | 可测断言（v0 阈值） | 判定脚本 |
|---|---|---|---|
| **G1** | 调用正确 | ①`must_call` 全集在本条实际 `trace.steps[].tool` 中出现；②实际工具 ∩ `must_not_call` = ∅；③`param_checks` 全过；④合法参数率 = 通过 Pydantic 的工具调用数 / 总调用数 ≥ **95%**；⑤`no_retrieve_ok` bucket 零检索工具调用率 = 100% | `scripts/eval_tool_calling.py` |
| **G2** | 结果融入 | ①每个 angle 的 `grounded_from` 非空，且至少一个 id 指向实际轨迹中 `ok=true` 的 step（形如 `<tool>#<i>`）；②`grounded_from` 引用的 step 在该 angle **之前**发生；③对有 `must_ground_in` 的 case，抽取式引文检查：angle 中任一字段包含 `KB_SNAPSHOT` 中指定义项的 ≥10 字连续子串 | 同上 |
| **G3** | 相对基线 | 同一模型、同一解码参数：`Agent+工具` vs `M0 裸生成`，在全部 bucket=`ok 类` case（`must_retrieve ∪ no_retrieve_ok ∪ tool_empty`）上：**胜率 ≥ 60%** 或 **均分 Δ ≥ +0.3**（1–5 分）。评分口径见 D.6 | `scripts/compare_m0.py` |
| **G4** | 失败可控 | ①`tool_empty`/`refuse`/`clarify` 全部 case 中，输出文本**零命中** `FAKE_METRIC_RE`（`爆款率|涨粉\d|点赞过万|阅读量\d+万|收藏率|互动率|GMV`）；②`refuse`/`clarify` case 的 `trace.steps` 为空、`status` 正确；③`tool_empty` case 要么 `status=degraded` 要么 angles 不引用任何数据性 claim | `scripts/eval_tool_calling.py` |
| **G5** | 可复现 | ①`mode=mock` 下同一 commit 连跑 3 次，逐 case `status + trace.steps[].tool 序列 + assertions 结果` 完全一致；②冻结集文件有 `sha256` 且被 runner 校验；③任意机器跑 `python scripts/eval_tool_calling.py --mode mock` 产出同结构报告 | `scripts/eval_tool_calling.py --repeat 3` |

> **G3 的执行门闩**：G3 只允许在 G1、G2、G5 的 mock 版本先绿之后开跑，且 M0 与 Agent 路径必须用**同一个模型 id + 同一 `temperature` + 同一 `count`**。任何一项不同，G3 结果作废。

### A.6 里程碑

| 里程碑 | 含义 | 达成条件 |
|---|---|---|
| **M0** | 基线 | 冻结集 FROZEN + 同模型裸生成 angles 的分数与样例落盘 |
| **M1** | 工具路径 | G1/G2/G5(mock) 全绿 |
| **M2** | Gate 通过 | G4 绿 + G3 达标（win ≥60% 或 Δ ≥ +0.3） |
| **M3** | 对比页 | P4 双路径页可在 mock 下一键起、左右两栏都展示完整 tool trace |
| **M4** | 训练（可选） | 微调模型在冻结集上 G1/G2 ≥ M1，且 G3 相对 M0 有额外 Δ |
| **M5** | 量化/本地服务（可选） | GGUF 模型注册进对比页并灰/亮正确 |

**训练门闩（硬约束）**：M1、M2 未达成，**禁止开始 P6 训练**；未完成 P5 去污染检查，禁止训练。

---

## B. 仓库目录规范

### B.1 设计原则

1. **长在现仓里**：`content-agent` 已经是「能跑的 Agent 底座 + 大能力面」；本项目是**收紧切片**，不是新工程。
2. **app / data / experiments / reports 四分**：应用代码（`app/`）只管跑；数据（`data/`）只进不改（冻结）；实验结果（`experiments/`）只追加；报告（`reports/`）对外可读。
3. **`scripts/` 是薄 CLI**：只做参数解析 + 调用，不承载业务逻辑；业务逻辑进 `app/`，便于 pytest。
4. **冻结集单点定义**：`data/evals/` 下的文件 + `sha256` + `DATASET_CARD.md`，任何脚本只读不写。
5. **mock 与 live 同级可跑**：mock 不依赖任何外部服务、登录态或网络。

### B.2 目标目录树（`content-agent` 内）

```text
content-agent/
├── app/                               # 应用代码（已有，小改）
│   ├── agent/
│   │   ├── tools.py                   # [改] DEMO_ALLOWLIST / DEMO_DENYLIST（PR#2 已有）
│   │   ├── mock_factories.py          # [改] 增加 theme-aware mock topics；mock KB 命中
│   │   ├── llm.py                     # [改] MockLLMBackend.register(TopicsDraft)
│   │   ├── evals.py                   # [不动] 现有 goal-completion evals，保留
│   │   └── tool_eval/                 # [新] 本项目的评测内核（纯函数，无 IO）
│   │       ├── __init__.py
│   │       ├── case.py                # ToolEvalCase pydantic 模型 + 加载 + sha256 校验
│   │       ├── assertions.py          # must_call / must_not_call / param_checks / grounding / honesty
│   │       ├── rubric.py              # 选题质量规则分（0–5）与 win/tie/loss 计算
│   │       ├── baseline.py            # M0 裸生成 runner（同一模型，无工具）
│   │       └── report.py              # 分数卡 + markdown/json 报告渲染
│   ├── services/
│   │   ├── topic_helper.py            # [已有 PR#2] theme → angles + trace
│   │   └── compare_service.py         # [新 P4] 双路径（bare | agent）并排执行
│   ├── api/
│   │   ├── routes_topics.py           # [可选] POST /topics/helper
│   │   └── routes_compare.py          # [新 P4] GET /api/models, POST /api/compare, /api/cases
│   └── prompts/
│       └── topic_generation_prompt.txt# [已有] 裸生成与工具路径共用同一底层 prompt
├── static/
│   ├── topic_helper.html              # [新 P0] 极简演示页
│   └── compare/                       # [新 P4] index.html + app.js + styles.css
├── data/
│   ├── raw/
│   │   └── kb_seed_v0.jsonl           # 手写种子笔记 20–40 条（已有 sample_notes.json 可扩充）
│   ├── interim/
│   │   └── kb_snapshot_v0.json        # 由 raw 确定性生成：{version, docs[{doc_id,title,text,source}]}
│   ├── processed/
│   │   └── training/v0.1/{train,val}.jsonl   # [P5] SFT 样本，gitignore 可选
│   └── evals/
│       ├── tool_calling_v0.jsonl      # 冻结评测集（FROZEN）
│       ├── tool_calling_v0.sha256     # 冻结校验
│       └── DATASET_CARD.md            # 规模/分层/字段/冻结流程/禁止事项
├── experiments/
│   ├── baselines/m0_bare_llm/         # M0：per-case 输出 + 分数卡 + 样例
│   ├── runs/                          # P3/P4 每次 Gate 运行的原始 trace
│   └── rounds/R001/                   # P8 迭代账本
├── reports/
│   ├── gate/                          # 每次 Gate 的 md 报告
│   └── figures/                       # 对比页截图、分数卡表
├── scripts/
│   ├── run_topic_helper.py            # [已有 PR#2] CLI demo
│   ├── build_kb_snapshot.py           # raw → interim 快照（确定性）
│   ├── freeze_evals.py                # 写 sha256、加 FROZEN 标记
│   ├── eval_tool_calling.py           # P3 Gate runner
│   ├── compare_m0.py                  # G3 对比
│   └── run_compare_app.py             # P4 起对比页
├── models/                            # [gitignore] adapters/ merged/ gguf/（仅 P6/P7）
├── docs/
│   ├── contracts/tool_calling_io_v0.md
│   ├── FULL_PROJECT_SPEC_tool_calling_v0.md   # 本文件
│   └── phases/P0..P8.md                       # 每阶段一份日志（做了什么/命令/结果/遗留）
└── tests/
    └── test_tool_eval_*.py            # 断言/评分/runner 单测
```

### B.3 每个目录：放什么 / 不放什么 / 命名规则

| 目录 | 放什么 | **不放**什么 | 命名规则 |
|---|---|---|---|
| `app/agent/tool_eval/` | 评测内核纯函数（case 加载、断言、评分、报告） | 网络/LLM 调用（由 runner 注入） | 小写下划线模块名 |
| `app/services/` | 编排（`topic_helper`、`compare_service`） | 评测阈值常量（进 `tool_eval/`） | `<域>_helper.py` / `<域>_service.py` |
| `app/api/` | FastAPI 路由，薄 | 业务实现 | `routes_<资源>.py` |
| `static/` | 原生 HTML/CSS/JS，无构建工具 | React/Vue 构建产物 | 页面 `kebab.html`；对比页独立子目录 |
| `data/raw/` | 手写/半自动的原始种子（笔记、政策词表） | 任何带 `expected`/`assertions` 的评测字段 | `<源>_<版本>.jsonl` |
| `data/interim/` | 可重复生成的中转产物（KB 快照） | 手工编辑结果 | `<名>_v<N>.json` |
| `data/processed/` | 训练样本（messages 格式） | 评测专用字段 | `training/v<x.y>/train.jsonl` |
| `data/evals/` | **冻结**评测集 + sha256 + DATASET_CARD | 训练样本、实验输出、临时调试 case | `tool_calling_<版本>.jsonl`；版本只增不改 |
| `experiments/baselines/` | M0 结果（每 case 输出 + 分数卡） | 人工手填数字 | `m0_<model_slug>_<date>/` |
| `experiments/runs/` | 每次 Gate 的原始 trace（jsonl） | 汇总结论（进 reports） | `run_<date>_<commit7>_<mode>.jsonl` |
| `experiments/rounds/` | 迭代账本每轮目录 | 代码实验的中间产物 | `R<三位序号>/` |
| `reports/gate/` | 人类可读报告 | 机器原始数据 | `<date>_<commit7>_gate.md` |
| `scripts/` | 薄 CLI（argparse + 调用） | 业务逻辑、阈值 | `<动词>_<对象>.py` |
| `models/` | LoRA 适配器 / merged / GGUF | 基座全量权重（体积大，不入仓） | `<base>-<method>-<round>` |
| `docs/phases/` | 每阶段施工日志（含踩坑） | 最终规格（进 `docs/` 顶层） | `PN_<名称>.md` |

### B.4 **评测与训练隔离规则（写死）**

1. `data/evals/` 下的任何文件**只读**：训练脚本、数据构建脚本不得读取该目录。CI/本地检查脚本 `scripts/check_isolation.py` 用 grep 断言 `scripts/build_sft_data.py`、`app/agent/tool_eval/` 之外的任何训练相关代码中不出现 `data/evals`。
2. **零重叠**：P5 构建训练集后必须跑 `python scripts/check_contamination.py --train data/processed/training/v0.1 --eval data/evals/tool_calling_v0.jsonl`；命中规则见 F.4，**命中数必须为 0** 才能训练。
3. 评测集之外的 `experiments/runs/` 轨迹**可以**用于训练（这是 P5 的主来源），但从中抽取的 `theme` 必须先过去污染检查。
4. 冻结流程：`scripts/freeze_evals.py` 写 `sha256` 并在 DATASET_CARD 标记 `FROZEN at <date> @ <commit>`；runner 启动时校验 sha256，不一致直接退出码 2。
5. 改评测集的唯一合法方式：**新建版本** `tool_calling_v1.jsonl` 并重新冻结；不得修改 v0。旧版本保留，报告中注明所用版本。

---

## C. 分阶段施工计划（P0–P8）

每阶段格式：**目标 / 输入 / 输出产物 / 验收清单 / 命令草案 / 风险**。阶段间依赖如下：

```text
P0(切片+页) → P1(冻结集) → P2(M0) → P3(Gate) → P4(对比页) → P5(数据) → P6(训练) → P7(量化)
                                  ↘ P8 随每轮迭代记录
```

### C.1 P0 · 演示切片 + 极简页

**目标**：`theme` 一条命令 → `{status, angles, trace, meta}`；mock 可离线；黑名单工具调不到；页面能看见 trace。

**输入**：PR #2 的 `DEMO_ALLOWLIST`、`app/services/topic_helper.py`、`scripts/run_topic_helper.py`。

**输出**：
- `static/topic_helper.html`：输入框（theme/audience/count/mode）+ 结果区（status 徽标、angles 卡片、可折叠 `trace.steps` 表格、`meta.trace_path`）。
- `app/api/routes_topics.py` 增加 `POST /topics/helper`（与现有路由风格一致，`APIRouter` + pydantic request/response），失败返回合同 §1.3 结构而非 500。
- `GET /static/topic_helper.html` 静态页（挂已有 StaticFiles）。

**验收清单**
- [ ] `python scripts/run_topic_helper.py --theme "宿舍党低成本护肤" --mode mock` 退出码 0，stdout 含 `status=ok` 且 `angles ≥ 3`、`trace.steps` 非空。
- [ ] `--theme ""` → `status=refuse, reason=empty_theme`，`trace.steps == []`。
- [ ] `--theme "爆款"` → `status=clarify`，`trace.steps == []`。
- [ ] registry 中不存在 `crawl_notes/generate_contents/generate_images/publish_to_xhs/sync_to_feishu`（单测断言 `set(registry.names()) == set(DEMO_ALLOWLIST)`）。
- [ ] `allow_tools=["publish_to_xhs"]` 被静默降级为白名单子集，不抛异常、不注册黑名单。
- [ ] 页面在 mock 模式输入 theme 后可见 `trace.steps` 每一行（tool / 参数摘要 / ok / result_digest）。
- [ ] `pytest tests/test_topic_helper.py tests/test_tool_helper_api.py -q` 全绿；既有 pytest 不回归。

**命令草案**
```bash
set PYTHONUTF8=1
python scripts/run_topic_helper.py --theme "研究生面试穿搭" --mode mock --count 4
uvicorn app.main:app --port 8000     # 打开 http://127.0.0.1:8000/static/topic_helper.html
pytest tests/test_topic_helper.py tests/test_tool_helper_api.py -q
```

**风险**：①薄入口若走完整 `XHSAgent`，mock planner 会调度 `generate_contents` → 保持 PR #2 的「确定性编排 + `registry.call`」更稳；②`generate_topics` 现返回 `{topics:[{title,reason}]}`，`angle` 只能从 title 拆——v0 接受 `angle = title 前半段`，在合同标注为已知妥协，P3 起在断言中只校验 `title/why/audience`。

### C.2 P1 · 冻结评测集

**目标**：产出 20–50 条带 `expected_trace` 的分层 case，冻结并校验。

**输入**：合同 §3 轨迹格式；`CARD_tool_calling_eval_v0.md` 的 24 条草案；`KB_SNAPSHOT`。

**输出**：
- `data/evals/tool_calling_v0.jsonl`（目标 N=30，最少 20；草案 24 可扩充）
- `data/evals/tool_calling_v0.sha256`
- `data/evals/DATASET_CARD.md`：规模 / 分层配额 / 字段表 / 冻结日期与 commit / 「本文件永不进训练」声明

**配额（P1 定稿）**：

| bucket | n（建议） | 用意 |
|---|---:|---|
| `must_retrieve` | 10 | 默认 `knowledge_search → generate_topics` |
| `no_retrieve_ok` | 6 | `allow_tools=[generate_topics]`，测不乱调 |
| `tool_empty` | 5 | 检索可为空，禁止伪造数据 |
| `refuse` | 5 | 空主题 / 政策违规 → `refuse`，零工具 |
| `clarify` | 4 | 过泛 → `clarify`，零工具 |
| **合计** | **30** | |

**验收清单**
- [ ] 每行通过 `ToolEvalCase` pydantic 校验；`bucket` ∈ 上述枚举；`id` 唯一且形如 `tc_001`。
- [ ] 每条至少 1 个 `must_call` 或显式 `expect_no_tools=true`。
- [ ] `must_retrieve` 与 `tool_empty` 各至少 3 条带 `must_ground_in`。
- [ ] `sha256` 生成，`--check` 通过；`DATASET_CARD.md` 标 `FROZEN`。
- [ ] 评审记录：每条 case 至少人工过一遍 `expected_trace` 是否与合同 §2.3 轨迹模板一致。

**命令草案**
```bash
python scripts/build_kb_snapshot.py --raw data/raw/kb_seed_v0.jsonl --out data/interim/kb_snapshot_v0.json
python -m pytest tests/test_tool_eval_case.py -q          # schema 校验
python scripts/freeze_evals.py --file data/evals/tool_calling_v0.jsonl
```

**风险**：①人工写 30 条 `expected_trace` 容易自相矛盾 → 用 schema + 单测兜住；②`clarify`/`refuse` 边界靠关键词表，写进 DATASET_CARD 的「判定依据」列，后续版本不追溯修改。

### C.3 P2 · M0 基线

**目标**：同一模型、同一解码，**不注册任何工具**，直接生成 `angles[]`，作为所有后续对比的尺子。

**输入**：冻结集；`app/services/topic_service.py` 的 `generate_topics`（底层 prompt 与工具路径共用）；`mode=mock`（mock backend 需注册 `TopicsDraft`）或 `live`。

**输出**：
- `experiments/baselines/m0_bare_llm/<model_slug>/<date>/cases.jsonl`：每 case 的 raw 输出、解析后 angles、rubric 分数。
- 同目录 `scorecard.md`（模板见 D.7）。

**验收清单**
- [ ] M0 路径**不调用** `registry.call`（runner 断言 `trace.steps == []` 且 `tool_calls_total == 0`）。
- [ ] 对 `refuse`/`clarify` case，M0 是否守规矩单独计数（作为 G4 对照，而不是 M0 分数）。
- [ ] `scorecard.md` 含：解析成功率、平均 rubric 分、分 bucket 平均分、`FAKE_METRIC_RE` 命中数。
- [ ] 记录 `model_id`、`temperature`、`max_tokens`、`count`、`prompt_sha256`。

**命令草案**
```bash
python scripts/compare_m0.py --stage m0 --eval data/evals/tool_calling_v0.jsonl \
    --backend mock --model mock-topic --out experiments/baselines/m0_bare_llm/mock-topic
```

**风险**：①mock backend 的 angles 若与工具路径同源，G3 会变成「自己跟自己比」——mock 下 G3 **只验证流水线，不作为结论**；G3 结论必须由 live 或微调模型给出，并在报告里注明 backend。②裸生成 prompt 与工具路径的 prompt 若不共享前缀，Δ 会混入 prompt 差异，因此两者必須共用 `topic_generation_prompt.txt`。

### C.4 P3 · Agent + 工具 Gate

**目标**：跑白名单路径，判 G1/G2/G4/G5；与 M0 做 G3。

**输入**：P1 冻结集、P0 薄入口、P2 M0 结果。

**输出**：
- `scripts/eval_tool_calling.py`（runner）
- `experiments/runs/run_<date>_<commit7>_<mode>.jsonl`（每 case 一行：输入、`status`、`angles`、`trace`、逐断言结果）
- `reports/gate/<date>_<commit7>_gate.md`（分数卡 + G1–G5 判定 + 失败样例 Top 5）
- 失败样例诊断：`reports/gate/<date>_diag.md`（每条失败归因到：选错工具 / 漏调 / 参数错 / 未融入 / 伪造数据 / 格式坏）

**验收清单**
- [ ] `--mode mock --repeat 3` 三次逐 case 结果一致（G5）。
- [ ] runner 启动前校验 `sha256`，不一致退出码 2。
- [ ] 报告逐项写实测值 + 阈值 + PASS/FAIL；**空值写 `not_run`，不得写估计值**。
- [ ] 任一 G 失败时，报告必须列出 3 条失败样例并给出归因。
- [ ] `pytest tests/test_tool_eval_assertions.py -q` 绿（断言函数单测：正例必过、负例必挂）。

**命令草案**
```bash
python scripts/eval_tool_calling.py --mode mock --eval data/evals/tool_calling_v0.jsonl \
    --repeat 3 --out experiments/runs --report reports/gate
python scripts/compare_m0.py --stage g3 --agent-run experiments/runs/<run>.jsonl \
    --m0 experiments/baselines/m0_bare_llm/mock-topic/cases.jsonl
```

**风险**：①断言过严 → 每个断言函数先写单测（正负例）；②`grounded_from` 的抽取式引文检查在 `generate_topics` 输出改写较多时容易失效 → 设「至少一事不过」的 fallback：`grounded_from` 指向的 step 的 `result_digest` 与 angle 文本 Jaccard ≥ 0.15 也算过（阈值在 DATASET_CARD 写死）。

### C.5 P4 · 双路径对比页（对标 slot-extractor Phase 05 tool-loop UI）

**目标**：一页左右栏 **裸 LLM | Agent+工具**，共享同一 `theme`，各自渲染 angles + 完整 tool trace + 该 case 的断言通过情况；模型/后端不可用时灰掉不可选。

**输入**：PR #2 薄入口；P2 bare runner；P3 断言模块；`KB_SNAPSHOT`。

**输出**：
- `app/services/compare_service.py`：`run_both(theme, args) -> {left, right}`，两侧独立、共享输入。
- `app/api/routes_compare.py`：
  - `GET /api/compare/models` → `[{model_id, backend, kind: "bare"|"agent"|"finetuned", available, unavailable_reason?}]`
  - `GET /api/compare/models/{id}/health` → `{ok, latency_ms, reason?}`（P4 起左侧裸生成走 `topic_service`，右侧走 `topic_helper`）
  - `POST /api/compare` → `application/x-ndjson`，事件 envelope：`{type, side, seq, case_id?, payload}`；`type ∈ {start, step, angles, score, error, done}`，`side ∈ {left, right}`。
  - `GET /api/compare/cases?bucket=` → 冻结 case 列表（仅 `id/theme/audience/bucket`，**不下发 expected_trace**）。
- `static/compare/{index.html,app.js,styles.css}`：原生三件套，无构建工具。
- 扩展点：第三栏 `kind="finetuned"` 由 P7 注册，P4 先留 selector 占位但 `available=false`。

**UI 信息架构**
```text
┌ 顶部：theme 输入 · audience · count · mode(mock/live) · [Compare]
├ 案例选择：bucket 过滤 → 下拉选取冻结 case（不显示答案）
├ 模型选择： 左侧 [bare 模型 ▾(灰)]   右侧 [agent 后端 ▾(灰)]
├ 左右双栏（共享同一输入，独立渲染）
│  ┌ 左：裸 LLM ─────────────┐  ┌ 右：Agent + 工具 ──────────┐
│  │ status 徽标               │  │ status 徽标                │
│  │ angles 卡片 ×N            │  │ angles 卡片 ×N             │
│  │ (无 trace → 灰字「无工具」) │  │ ▸ trace.steps（默认展开）   │
│  │                          │  │   step0 knowledge_search   │
│  │                          │  │   step1 generate_topics    │
│  │ rubric 分（若选 case）     │  │ 断言：G1 ✓ G2 ✗ …           │
│  └──────────────────────────┘  └────────────────────────────┘
└ 底部：导出 NDJSON / 导出 JSON；「mock 结果仅验证流水线」提示条
```

**交互与错误态**
- 事件按 `side` 路由；`step` 事件到达即渲染一行 trace（增量，非 token streaming）。
- 模型 `available=false` → `<option disabled>` + 悬浮显示 `unavailable_reason`；两侧不得都选同一不可用模型。
- 后端异常 → `type=error` 事件，该栏显示错误卡片，另一栏继续；不 500 全页。
- `mode=mock` 时页面顶部常驻提示：「mock 只验证链路，G3 结论需 live」。

**验收清单**
- [ ] `python scripts/run_compare_app.py --port 8010` 后可访问 `/`、`/static/compare/app.js`。
- [ ] `POST /api/compare` 返回 NDJSON，`{side}` 集合 = `{left,right}`，两侧各至少 1 个 `angles` 事件。
- [ ] 右侧至少 1 个 `step` 事件且 `tool ∈ DEMO_ALLOWLIST`；左侧 `step` 事件数 = 0。
- [ ] 静态测试断言页面含 `id="left-model" id="right-model" id="left-panel" id="right-panel" id="trace-left" id="trace-right" id="case-select" id="export-ndjson"`。
- [ ] 将某个 model 配置为不可用后，`/api/compare/models` 返回 `available=false` 且 UI 中对应 option 为 disabled。
- [ ] `pytest tests/test_compare_api.py tests/test_compare_ui.py -q` 绿。

**命令草案**
```bash
python scripts/run_compare_app.py --port 8010 --mode mock
curl -s -X POST localhost:8010/api/compare -H 'content-type: application/json' \
  -d '{"theme":"宿舍党低成本护肤","mode":"mock"}'
pytest tests/test_compare_api.py tests/test_compare_ui.py -q
```

**风险**：①左右两侧若共用 mock 数据源，画面「一样」→ 在 UI 明确标注「mock 下两侧同源，仅验证链路」；②事件模型不要复制 slot-extractor 的 token streaming（它本身也明确只发完成事件）；③静态页禁止引入构建工具，保持与现仓 `static/*.html` 风格一致。

### C.6 P5 · 数据构建（仅工具调用轨迹）

**目标**：把成功轨迹蒸馏成 SFT 样本，并用负例构造错误样本；与冻结集严格隔离。

**输入**：`experiments/runs/*.jsonl` 中 `status=ok` 且 G1/G2 断言全过的轨迹；`KB_SNAPSHOT`；冻结集（**只用于去污染比对，不取样**）。

**输出**：
- `data/processed/training/v0.1/train.jsonl` / `val.jsonl`（9:1 分层切分）
- 同目录 `manifest.json`：来源 run 文件 sha、条数、bucket 分布、去污染结果、生成脚本 commit
- `reports/data/v0.1_contamination.md`

**验收清单**
- [ ] 每条样本通过 `messages` schema 校验，最后一条 `assistant` 内容可 JSON 解析为 `{status, angles}` 或 tool-call 形态。
- [ ] 负例占比 ≥ 30%（类型见 F.3）。
- [ ] `check_contamination.py` 命中 = 0。
- [ ] `manifest.json` 记录配比与版本 commit。

**命令草案**
```bash
python scripts/build_sft_data.py --runs experiments/runs --out data/processed/training/v0.1
python scripts/check_contamination.py --train data/processed/training/v0.1 \
    --eval data/evals/tool_calling_v0.jsonl
```

**风险**：轨迹蒸馏天然偏向「成功简单样本」→ 必须显式构造负例与 `tool_empty/refuse` 样本；详见 F。

### C.7 P6 · 训练（可选，但规格写全）

**目标**：在 Kaggle 双 T4 上做 LoRA SFT（可选再 DPO），产出可注册进对比页的 adapter。

**门闩**：M1 与 M2 均已达成；P5 去污染为 0；冻结集 sha256 未变。

**输入**：`data/processed/training/v0.1/*`；基座候选（见 F.6）。

**输出**：
- `configs/train/sft_<model>_<round>.yaml`（LLaMA-Factory 格式，参考仓同构）
- `models/adapters/<round>/`（gitignore）
- `experiments/runs/run_<date>_<commit7>_live_ft.jsonl`（用同一 Gate runner 跑）

**验收清单**
- [ ] 本地 `max_steps=2` 冒烟通过（格式/路径/chat template 无低级错误）后再上 Kaggle。
- [ ] 训练日志含 loss 曲线、val loss；无 NaN。
- [ ] 微调模型在冻结集上 G1、G2 ≥ M1 对应值；G3 相对 M0 的 Δ 单独报（不得把 Δ 与 M2 混报）。
- [ ] 报告注明：模型 id、LoRA rank、数据版本、训练时长、GPU 类型。

**命令草案（Kaggle 双 T4）**
```bash
# 本地冒烟
llamafactory-cli train configs/train/smoke.yaml            # max_steps=2, 0.5B 级模型
# Kaggle Notebook（GPU T4 x2）
pip install -q llamafactory==0.9.5
llamafactory-cli train configs/train/sft_qwen3-1.7b_r001.yaml
# 回归
python scripts/eval_tool_calling.py --mode mock --backend adapter:models/adapters/r001 \
    --eval data/evals/tool_calling_v0.jsonl
```

**风险**：①双 T4 无 NVLink，1.7B 以上模型用 `device_map` 需设 DS/ZeRO 或逐卡训练两份配置→首轮优先单卡可跑规模（0.6B/1.7B）；②Kaggle 会话有时长/磁盘限制 → 产物只保存 LoRA + 配置；③超参占位见 F.7，第一轮不改超参只改数据。

### C.8 P7 · 量化 / 本地服务（可选）

**目标**：仅当对比页需要「本地双开」或想演示 GGUF 时启用。

**输出**
- `models/gguf/<name>-Q4_K_M.gguf`（gitignore）
- `configs/models.local.yaml`：`[{model_id, backend:"llama_cpp", endpoint, kind:"finetuned", available}]`
- 对比页 `GET /api/compare/models/{id}/health` 对该 endpoint 探活，失败则 `available=false` + 原因。

**验收清单**
- [ ] `llama-server -m ... --port 8080` 起服务后 health 返回 `ok:true`。
- [ ] 注册的 GGUF 模型在冻结集上跑一遍，质量回退（vs LoRA fp16）在阈值内（rubric 均分回退 ≤ 0.3），记录于 `reports/gate/`。
- [ ] 服务不可用时 UI 灰掉而非报错。

**命令草案**
```bash
llama-quantize models/merged/r001/fp16.gguf models/gguf/r001-Q4_K_M.gguf Q4_K_M
llama-server -m models/gguf/r001-Q4_K_M.gguf --port 8080 -c 4096
python scripts/run_compare_app.py --port 8010 --config configs/models.local.yaml
```

**风险**：llama.cpp 对 Qwen 系列的 tool-call chat template 支持需版本对齐；若 `--jinja` 不支持则回退到纯文本 JSON 输出合同（本项目的输出本就是 JSON 合同，不依赖原生 function-calling token）。

### C.9 P8 · 迭代账本

**目标**：每轮改动可追溯，防止「改了就说不清楚」。

**输出**：`experiments/rounds/R001/README.md`，模板：

```markdown
# R001 · <一句话假设>
- 日期 / commit：
- 假设：（如「knowledge_hints 注入后 G2 提升」）
- 改动：（文件 + 关键 diff 摘要）
- 数据/评测版本：eval v0 (sha256 前 8)，train v0.1
- Gate 数字：G1 x% | G2 x% | G3 win x% / Δ x | G4 hits x | G5 pass/fail
- 对比上一轮：R000 → R001 的变化
- 是否合并：是/否 + 理由
- 失败归因 Top3：
```

**验收**：每轮 README 必须填满 Gate 数字字段，`not_run` 明确标注。

### C.10 阶段 × 里程碑汇总

| 阶段 | 核心产物 | 前置 | 里程碑 | 环境 |
|---|---|---|---|---|
| P0 | 薄入口 + 极简页 + API | PR #2 | — | 本地 mock |
| P1 | 冻结集 + DATASET_CARD + sha256 | P0 | — | 本地 |
| P2 | M0 基线分数卡 | P1 | **M0** | 本地 mock/live |
| P3 | Gate runner + 报告 | P1,P2 | **M1/M2** | 本地 mock 默认 |
| P4 | 双路径对比页 | P2,P3 | **M3** | 本地 |
| P5 | SFT 数据 + 去污染 | P3 | — | 本地 |
| P6 | LoRA + 回归 | P5 + M2 门闩 | **M4** | Kaggle T4×2 |
| P7 | GGUF + 本地服务 | P6 | **M5** | 本地 CPU |
| P8 | 迭代账本 | 全程 | — | — |

---

## D. 评测设计（规格核心）

### D.1 评测对象与「拿什么比」

每条 case 评估**两类产出**：

1. **中间过程**：`trace.steps[]`（选没选对工具、参数对不对、顺序对不对）——来自 contract §3 的轨迹。
2. **终态**：`status` + `angles[]`（格式合法、有依据、不伪造）。

> 关键认知：只评终态会漏掉「绕远路/乱调工具但答案碰巧对」；只评轨迹会漏掉「调对了但没用好结果」。两者都要。

### D.2 case schema（`data/evals/tool_calling_v0.jsonl` 每行）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `id` | str | ✓ | `tc_001`，稳定不复用 |
| `bucket` | enum | ✓ | `must_retrieve` / `no_retrieve_ok` / `tool_empty` / `refuse` / `clarify` |
| `theme` | str | ✓ | 与 CLI 输入一致（评测时原样传给入口） |
| `audience` | str\|null | | 缺省用合同默认值 |
| `count` | int\|null | | 缺省 4 |
| `allow_tools` | list[str]\|null | | 用于 `no_retrieve_ok` 收窄；不得出现黑名单 |
| `kb` | str | ✓ | 引用快照版本，如 `kb_snapshot_v0` |
| `expect_status` | enum | ✓ | `ok` / `degraded` / `refuse` / `clarify` |
| `expected_trace` | object | ✓ | 见 D.3；`expect_no_tools=true` 时其它字段可为空 |
| `expect_angles` | object | ✓ | `min`/`max` 数量，`require_fields: ["angle","title","why","audience","grounded_from"]` |
| `must_ground_in` | list[{doc_id, span}] | | 抽取式引文检查（G2 ③）。span 为必须出现在 angle 文本中的连续片段 |
| `forbid_regex` | list[str] | | 追加的禁止模式（默认已含 `FAKE_METRIC_RE`） |
| `tags` | list[str] | | `需检索`/`自洽`/`空结果`/`政策`/`过泛`/`多工具`/`单工具` |
| `notes` | str | | 判定依据（人工解释，供 reviewer） |

**示例（`must_retrieve`）**
```json
{
  "id": "tc_003",
  "bucket": "must_retrieve",
  "theme": "宿舍党低成本护肤",
  "audience": "大学生女生",
  "count": 4,
  "allow_tools": null,
  "kb": "kb_snapshot_v0",
  "expect_status": "ok",
  "expected_trace": {
    "must_call": ["knowledge_search", "generate_topics"],
    "must_not_call": ["crawl_notes", "generate_contents", "generate_images", "publish_to_xhs", "sync_to_feishu"],
    "order_soft": ["knowledge_search", "generate_topics"],
    "max_tool_calls": 4,
    "param_checks": [
      {"tool": "knowledge_search", "field": "query", "contains_any": ["护肤", "宿舍"], "forbid_any": []},
      {"tool": "generate_topics", "field": "count", "equals": 4},
      {"tool": "generate_topics", "field": "focus_keywords", "intersects": ["护肤", "宿舍", "平价"]}
    ],
    "grounding": "each_angle_grounded",
    "honesty": "no_fake_metrics"
  },
  "expect_angles": {"min": 3, "max": 5, "require_fields": ["angle", "title", "why", "audience", "grounded_from"]},
  "must_ground_in": [{"doc_id": "kb_007", "span": "宿舍 三件套 百元内"}],
  "forbid_regex": [],
  "tags": ["需检索", "多工具"],
  "notes": "知识库有「宿舍护肤」命中，必须先检索再选题"
}
```

**示例（`tool_empty`，期望降级）**
```json
{
  "id": "tc_021",
  "bucket": "tool_empty",
  "theme": "北欧极简风小户型收纳（宿舍场景）",
  "expect_status": "degraded",
  "expected_trace": {
    "must_call": ["knowledge_search", "generate_topics"],
    "must_not_call": ["crawl_notes"],
    "param_checks": [{"tool": "knowledge_search", "field": "query", "contains_any": ["收纳"]}],
    "grounding": "none_required",
    "honesty": "no_fake_metrics"
  },
  "expect_angles": {"min": 0, "max": 5, "require_fields": []},
  "tags": ["空结果"],
  "notes": "快照中无收纳类笔记，检索 count=0；允许 degraded 空 angles 或 angles 不引用数据"
}
```

### D.3 `expected_trace` 断言算法

断言器 `app/agent/tool_eval/assertions.py`，输入 `(case, actual_trace, actual_output)`，输出逐项结果：

| 断言 | 判定算法 | 归属 |
|---|---|---|
| `must_call` | `set(must_call) ⊆ set(steps.tool)`；`expect_no_tools=true` 时 `steps == []` | G1 |
| `must_not_call` | `set(steps.tool) ∩ set(must_not_call) == ∅`（实际注册表本身就不含黑名单，故此项多为兜底） | G1 |
| `order_soft` | 按出现顺序取 `order_soft` 中出现的工具，其相对顺序与列表一致（允许夹杂其它工具） | G1 |
| `max_tool_calls` | `len(steps) ≤ max_tool_calls`（默认 4） | G1 |
| `param_checks[].equals` | `params[field] == value`（数值/字符串严格；字符串先 NFKC + strip） | G1 |
| `param_checks[].contains_any` | 归一化后 `any(k in params[field] for k in contains_any)`；`field` 为 list 时任一元素命中 | G1 |
| `param_checks[].forbid_any` | `not any(k in ... for k in forbid_any)` | G1 |
| `param_checks[].intersects` | 集合交非空；`intersects_theme=true` 时以 theme 分词（2-gram + 词典词）作为集合 | G1 |
| `schema_valid` | 每个 step 的 `params` 在运行时已过 Pydantic（`registry.call` 保证）；`ok=false` 的 step 计入失败 | G1 |
| `grounding: each_angle_grounded` | 每个 angle 的 `grounded_from` 含 ≥1 个 id，且该 id 指向的 step 满足 `ok=true && i < angle 产生步`（v0 用「angle 生成 step 的 index」近似为一个常量：全部 angle 视作在末步产生） | G2 |
| `grounding: none_required` | 不检查引用；若 angles 非空则仍检查「不伪造数据」 | G2/G4 |
| 抽取式引文 | 对 `must_ground_in` 每一项：`span` 去空白后作为连续子串出现在 angle 任一字段中；或 step `result_digest` 与 angle 文本 Jaccard ≥ 0.15 视为过 | G2 |
| `honesty: no_fake_metrics` | 全部输出文本（message/reason/angles 全字段）不命中 `FAKE_METRIC_RE ∪ forbid_regex` | G4 |
| `status_match` | `actual.status == expect_status`；对 `tool_empty` 允许 `ok` 或 `degraded`（在 case 内用 `expect_status_any` 覆盖） | G1/G4 |

**约定**：
- 字符串归一化：NFKC → 去首尾空白 → 大小写折叠（中文不影响）。
- Jaccard 分词：中文 2-gram + 英文/数字按 `\w+`；交集/并集在去停用词后计算。
- 每个断言写单测：正例、负例、边界（空 trace、空 angles、`grounded_from` 引用不存在 step）。

### D.4 bucket 定义

| bucket | 定义 | 期望行为 | 主要考的事 |
|---|---|---|---|
| `must_retrieve` | 快照中有相关 doc（`must_ground_in` 常见） | `knowledge_search` → `generate_topics`，`status=ok` | 「该调就调」+ 结果融入 |
| `no_retrieve_ok` | 主题自洽（如「面试自我介绍怎么写」但快照无相关 doc），`allow_tools=[generate_topics]` | 零检索，`generate_topics` 直出 | 「不该调就不调」 |
| `tool_empty` | 快照无命中（检索 `count=0`） | `degraded` 或 ok 但不引用数据 | 空结果诚实 |
| `refuse` | 空 theme / 政策词（诊疗承诺等） | `status=refuse`，零工具 | 拒答前置 |
| `clarify` | 过泛（如仅「爆款」） | `status=clarify`，零工具 | 不硬猜 |

> `refuse` / `clarify` 的判定依据写在 case `notes`，由 P0 的 `_POLICY_TERMS` / `_VAGUE_THEMES` 关键词表保证可复现；v1 若更换规则表，新建评测集版本。

### D.5 M0 定义

- **模型**：与 Agent 路径**完全相同**的 `model_id`（mock 下都是 mock backend；live 下同一个 API 模型）。
- **解码**：同 `temperature`（默认 `0.7`）/ `max_tokens` / `count`。
- **路径**：不注册工具，直接调用 `topic_service.generate_topics`，把 `{title, reason}` 映射为 `angles`（`angle=title 前半`、`why=reason`、`audience=输入`、`grounded_from=[]`）。
- **落盘**：`experiments/baselines/m0_bare_llm/<model_slug>/<date>/cases.jsonl` + `scorecard.md`。
- **不评**：M0 的 trace（无工具）；M0 在 `refuse/clarify` case 的表现只记录，不计入 M0 质量分。

### D.6 选题质量评分（G3 用）与对比指标

不使用 LLM judge，采用**规则 + 人工抽检复核**的两层：

**Rubric（每条 case 每个输出，0–5 分，规则版）**

| 维度 | 分值 | 判定 |
|---|---:|---|
| 可解析 | 1 | `status` 合法且 angles 数量在 `[3,5]`（`degraded` case 除外） |
| 字段完整 | 1 | 每个 angle 有非空 `angle/title/why/audience` |
| 切题 | 1 | 每个 angle 文本与 theme 的 Jaccard ≥ 0.1 的比例 ≥ 0.75 |
| 可执行 | 1 | `why` 或 `title` 中含动作/场景词（「如何/怎么/清单/避雷/公式/步骤/天/元/分钟」等）的比例 ≥ 0.75 |
| 无空话/无伪造 | 1 | 不命中 `FAKE_METRIC_RE`；角度两两标题去重后相似度 < 0.9 |

**对比计算**
```text
F(c) = tokens(c) ∩ tokens(theme)；J = |F| / |tokens(c) ∪ tokens(theme)|
score(output, case) = Σ rubric_i            ∈ [0,5]
win(c)  = score(agent) >  score(m0)
tie(c)  = score(agent) == score(m0)
loss(c) = score(agent) <  score(m0)
win_rate = (win + 0.5*tie) / N_ok_cases      # 平局按半分
delta    = mean(score(agent) - score(m0))
```
- **通过条件**：`win_rate ≥ 0.60` **或** `delta ≥ +0.3`。
- **人工抽检**：从 win/loss 各抽 5 条，由作者按同一 rubric 盲评（只看输出不看 side），记录与规则分的一致率；一致率 < 0.8 时须修订 rubric 并重跑 M0。
- **报告模板**见 D.7。

### D.7 分数卡与报告模板

```text
===== content-agent · Tool-Calling Gate  (eval=v0 sha=1a2b3c4d / agent=mock / n=30) =====
G1 调用正确         PASS    must_call 30/30 | 黑名单命中 0 | 参数失败 0/41 (0.0%) | no_retrieve 零检索 6/6
G2 结果融入         PASS    每个 angle 有 step 引用 118/120 | 引用先于产出 118/118 | 抽取引文   9/10
G3 相对基线         PASS    win 0.63 | tie 0.10 | loss 0.27 | Δ +0.41   (backend=mock，仅流水线)
G4 失败可控         PASS    refuse/clarify 守规矩 8/8 | tool_empty 降级 4/5 | 伪造指标命中 0
G5 可复现           PASS    repeat=3 逐 case 一致 | sha256 校验通过
质量（分 bucket 平均 rubric，0–5）
  must_retrieve 4.10 | no_retrieve_ok 4.33 | tool_empty 3.20
失败样例 Top5
  tc_017 G2 失败：angles[1].grounded_from 指向 step#5（不存在）
  ...
备注：mock 下 G3 只验证流水线；正式 G3 需 live 或微调模型。
```

报告文件：`reports/gate/<date>_<commit7>_gate.md`，**只填实测值**；未跑项写 `not_run`。

### D.8 与现有 `app/agent/evals.py` 的扩展关系

| 现有 evals.py | 处理 | 理由 |
|---|---|---|
| `EvalCase(goal, expected_keywords)` | **保留** | 用于旧目标级冒烟，不删 |
| `run_evals / EvalSummary`（goal_completion、steps、tool_calls、quality_pass_rate、keyword_coverage） | **保留**，`keyword_coverage` 降级为辅指标 | 不破坏既有 pytest 与历史报告 |
| mock backend / mock registry | **复用**，但 tool-eval 必须用 `build_demo_registry()`（只含白名单） | 避免评测路径能调到黑名单 |
| 新增 | `app/agent/tool_eval/`（case/assertions/rubric/baseline/report）+ `scripts/eval_tool_calling.py` | 轨迹断言与相对基线是现 evals 缺失的 |
| 不新增 | LLM judge | 确定性、离线、可复现 |

**测试迁移**：现有 `tests/test_agent_evals.py` 不动；新增 `tests/test_tool_eval_case.py`、`tests/test_tool_eval_assertions.py`、`tests/test_tool_eval_rubric.py`、`tests/test_gate_runner.py`。

---

## E. 对比页 / Demo 产品规格

### E.1 P0 极简页（当前交付）

- 单栏：theme/audience/count/mode → 提交 → 结果。
- 显示：status 徽标（ok/degraded/refuse/clarify 四色）、angles 卡片、`<details>` 折叠的 trace 表（`i/tool/params/ok/result_digest`）、`meta.trace_path`。
- 失败态：refuse/clarify 显示 reason 与追问列表；degraded 显示 message。
- 无模型选择、无对比。
- 目的：证明链路 + trace 可见，供 PR #2 review 与面试前自测。

### E.2 P4 双模型对比页（演进）

| 维度 | P0 极简页 | P4 对比页 |
|---|---|---|
| 布局 | 单栏 | 左右双栏 + 顶部控制条 |
| 输入 | 自由 theme | 自由 theme **或** 从冻结 case 下拉（不显示答案） |
| 路径 | Agent+工具 | 左：裸 LLM；右：Agent+工具（可选第三栏微调） |
| trace | 折叠表 | 默认展开，增量追加，每行可展开 params/result_digest |
| 断言 | 无 | 选 case 后显示该 case 的 G1/G2/G4 判定徽标 |
| 模型可用性 | 无 | `/api/models` 驱动，不可用灰掉 |
| 导出 | 无 | NDJSON + JSON 摘要 |
| 默认 | mock | mock；live 需显式选择 |

**演进路径**：P0 页面保留（作为最小演示）；P4 独立 `static/compare/`，复用 `topic_helper` 与新增 `compare_service`，不改动 P0 页。

### E.3 API 契约（P4）

```http
GET /api/compare/models
→ 200 [{"model_id":"mock-bare","kind":"bare","backend":"mock","available":true},
       {"model_id":"mock-agent","kind":"agent","backend":"mock","available":true},
       {"model_id":"qwen3-1.7b-sft-r001","kind":"finetuned","backend":"llama_cpp","available":false,
        "unavailable_reason":"endpoint not configured"}]

GET /api/compare/cases?bucket=must_retrieve
→ 200 [{"id":"tc_003","theme":"宿舍党低成本护肤","audience":"大学生女生","bucket":"must_retrieve"}]   # 无 expected_trace

POST /api/compare
  {"theme":"...", "audience":null, "count":4, "mode":"mock",
   "left_model_id":"mock-bare", "right_model_id":"mock-agent", "case_id":null}
→ 200 application/x-ndjson
   {"type":"start","side":"left","seq":0,...}
   {"type":"angles","side":"right","seq":3,"payload":{"status":"ok","angles":[...]}}
   {"type":"step","side":"right","seq":2,"payload":{"i":0,"tool":"knowledge_search",...}}
   {"type":"score","side":"right","seq":5,"payload":{"rubric":4.0}}
   {"type":"error","side":"left","seq":1,"payload":{"message":"...","code":"backend_error"}}
   {"type":"done","side":"right","seq":6}
```

**错误码**：`422` 未知模型 / 非法 body；`409` 两侧同模型（前端已禁用，后端兜底）；单侧内部错误 → `type=error` 事件，不回 5xx 中断另一侧。

### E.4 trace 展示（硬要求）

- 右侧必须**默认展开** trace，每行：`#i` 工具名 · 参数摘要（`query="宿舍护肤" top_k=5`）· `ok ✓/✗` · `result_digest`（`count=3`、`n_topics=4`）。
- 左侧显示「本条路径不调用工具」灰字，**不得**伪装成空 trace 表。
- mock 模式顶部常驻黄色提示条。

---

## F. 数据与训练规格

### F.1 轨迹日志 schema → SFT 样本

**来源**：`experiments/runs/*.jsonl`（Gate 每次运行写入，每 case 一行，含 `args`、`trace`、`angles`、`assertions`）。筛选条件：`status ∈ {ok, degraded}` 且 `assertions.g1 == pass && assertions.g2 == pass`；`degraded` 样本仅取 `honesty` 过的。

**SFT 样本格式（ShareGPT，与参考仓同构，便于 LLaMA-Factory 直接注册）**

```json
{
  "system": "你是小红书选题 Agent。只输出 JSON。可用工具：knowledge_search / load_notes / analyze_notes / generate_topics。规则：…（与 tool_calling_io_v0.md 输出合同一致）",
  "tools": "[{\"name\":\"knowledge_search\",\"description\":\"…\",\"parameters\":{…}}, …]",
  "conversations": [
    {"from": "human", "value": "{\"theme\":\"宿舍党低成本护肤\",\"audience\":\"大学生女生\",\"count\":4}"},
    {"from": "function_call", "value": "{\"name\":\"knowledge_search\",\"arguments\":{\"query\":\"宿舍护肤 平价\",\"top_k\":5}}"},
    {"from": "observation", "value": "{\"count\":3,\"hits\":[{\"doc_id\":\"kb_007\",\"title\":\"…\",\"score\":0.71}]}"},
    {"from": "function_call", "value": "{\"name\":\"generate_topics\",\"arguments\":{\"summary\":\"…\",\"focus_keywords\":[\"护肤\",\"宿舍\"],\"knowledge_hints\":[\"百元内三件套\"],\"count\":4}}"},
    {"from": "observation", "value": "{\"topics\":[{\"title\":\"…\",\"reason\":\"…\"}]}"},
    {"from": "gpt", "value": "{\"status\":\"ok\",\"angles\":[{\"angle\":\"…\",\"title\":\"…\",\"why\":\"…\",\"audience\":\"大学生女生\",\"grounded_from\":[\"knowledge_search#0\",\"generate_topics#1\"]}]}"}
  ]
}
```

约定：
- 奇数位为 `human/observation`，偶数位为 `function_call/gpt`（与 LLaMA-Factory 约定一致），由构建脚本断言。
- `function_call` 只表示**已发生**的工具事件；最后决策统一写 JSON（本项目的输出合同就是 JSON，不改成原生 function-call label）。
- system 与 tools 与线上 `topic_helper` 使用**同一份**模板常量（从 `app/agent/tool_eval/prompt_contract.py` 导入），避免训练/推理分布不一致。

### F.2 两层样本

| 层 | 输入 | 标签 | 占比建议 |
|---|---|---|---:|
| **中间过程**（工具选择） | `human` 输入 + 空历史 | 单步 `function_call`（含参数） | 40% |
| **终态**（angles 输出） | `human` + 完整工具事件历史 | 最终 `gpt` JSON | 60% |

同一条真实轨迹切成多条样本：每个工具调用点一条中间过程样本 + 末端一条终态样本。

### F.3 负例构造（程序化，不靠人工写）

| 负例类型 | 构造方式 | 对应痛点 |
|---|---|---|
| 乱调工具 | 在 `no_retrieve_ok` 样本前插 `knowledge_search`；或在自洽主题上调 `crawl_notes`（→ 用白名单内最不相关工具模拟） | 不该调却调 |
| 无视工具结果 | 终态 angles 的 `grounded_from` 指向不存在的 step，或改写为与 `result_digest` 无关的内容 | 结果融入失败 |
| 伪造数据 | 在 `why` 中写「爆款率 12%」「点赞过万」「互动率提升」 | 幻觉 |
| 格式坏 | 去掉 `audience` 字段、angles 数量 = 2 或 6、`status` 写「成功」 | 合同不遵守 |
| 空结果硬编 | `tool_empty` 样本中仍写「根据检索到的 N 篇笔记」 | 幻觉 |

负例用途：SFT 中以「拒答/降级正确样本」形式训练（`degraded` 标签），DPO 中作为 `rejected`。**负例不少于训练集 30%**。

### F.4 去污染规则（相对冻结 eval）

`scripts/check_contamination.py` 逐条比对：

1. **精确命中**：训练样本 `theme` 归一化后与任一 eval `theme` 完全相同 → 命中。
2. **高相似**：字符 3-gram Jaccard ≥ **0.7** → 命中。
3. **同源 doc 泄漏**：训练样本的 `observation.hits[].doc_id` 集合与任一 eval 的 `must_ground_in.doc_id` 相同，**且**训练样本 `theme` 与该 eval `theme` 的 2-gram Jaccard ≥ 0.4 → 命中（允许同一 KB 被多条不同主题使用，只是禁止近同题）。
4. 命中数 **必须为 0**；脚本以非零退出码阻断训练入口。

### F.5 规模与配比（首版）

| 项 | 首版 | 依据 |
|---|---:|---|
| SFT 总条数 | ~600（中间过程 ~240 / 终态 ~360） | 窄任务、输出结构固定；OpenAI 经验「50–100 可见效」，本项目留 5–10× 余量覆盖 5 个 bucket |
| 负例占比 | ≥ 30% | 空结果/拒答/不调工具是易错边界 |
| bucket 配比 | 按 eval bucket 比例过采样低频（refuse/clarify 各 ≥ 15%） | 天然分布里拒答样本近乎为零 |
| DPO 偏好对（可选） | ~150–300 对 | 只针对 SFT 后残留的「乱调 / 伪造数据」错例 |
| train/val | 9:1 分层 | val 只看 loss，不等于 eval |

**来源策略**：以「**自身轨迹 + 程序化负例**」为唯一主力；不依赖真实日志、不爬公开数据集；不对冻结 eval 做任何取样。

### F.6 训练配置占位（Kaggle 双 T4）

| 项 | 首版选择 | 备选 | 说明 |
|---|---|---|---|
| 框架 | LLaMA-Factory `0.9.5`（与参考仓一致） | TRL | 标准 LoRA SFT 足够，省工程量 |
| 基座候选 | `Qwen3-0.6B` / `Qwen3-1.7B` | `Qwen2.5-1.5B-Instruct` | 双 T4 单卡可跑；1.7B 为质量参考档 |
| 方法 | LoRA；`train_on_prompt=false`、`mask_history=true` | QLoRA(NF4) | 只学输出段 |
| 超参占位 | `lora_rank=16, alpha=32, lr=1e-4, epochs=3, batch=2×grad_accum=8, cutoff_len=4096, bf16=true` | — | 第一轮**不改超参**，只改数据 |
| 多卡 | `torchrun --nproc_per_node=2` 或 HF accelerate | 单卡 | 双 T4 无 NVLink，优先数据并行或逐卡跑两份配置 |
| 评估 | 用**同一个** `eval_tool_calling.py` | — | 禁止另写评估 |

### F.7 「何时值得训」决策树

```text
M1/M2 达成？
├─ 否 → 不训（先把尺子做对）
└─ 是 → live API + 工具路径 G3 已达标？
         ├─ 是 → 训练只做「简历加分项」：训一个 ≤1.7B 的小模型，证明「小模型 + 少量数据也能守住 G1/G2」
         └─ 否 → 先做 R 轮 prompt/参数/工具描述迭代（P8）；2 轮失败后再训
训练后：
├─ 微调模型 G1/G2 < M1 → 训崩了：查数据负例占比、chat template、mask
├─ G1/G2 ≥ M1 但 G3 Δ 无额外提升 → 停（数据边际收益为 0）
└─ G1/G2 ≥ M1 且离线/成本有优势 → 进 P7，写进报告
```

**与异构项目线隔离**：训练目录只允许 `data/processed/training/` 与 `configs/train/`；`models/` 下只存本项目 adapter，不混用任何既有适配器与历史权重。

---

## G. 运行手册

### G.1 环境

```bash
set PYTHONUTF8=1                  # Windows 中文控制台
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt
# 可选（仅知识库 live 检索需要）：
# 下载 bge-small-zh-v1.5 onnx 到 data/models/（见仓 README）
```

### G.2 本地 mock 演示

```bash
# CLI
python scripts/run_topic_helper.py --theme "研究生面试穿搭" --mode mock

# Web（P0 页）。主路径是 /demo/topic-helper；静态挂载同样有效。
uvicorn app.main:app --port 8000
# → http://127.0.0.1:8000/demo/topic-helper
# 同样有效：http://127.0.0.1:8000/static/topic_helper.html

# 对比页（P4）
python scripts/run_compare_app.py --port 8010 --mode mock
# → http://127.0.0.1:8010/
```

### G.3 Gate 一键

```bash
# 冻结集校验 → 跑 Agent 路径（3 次）→ 对比 M0 → 出报告
python scripts/eval_tool_calling.py --mode mock --eval data/evals/tool_calling_v0.jsonl \
    --repeat 3 --report reports/gate
python scripts/compare_m0.py --stage g3 \
    --agent-run experiments/runs/latest.jsonl \
    --m0 experiments/baselines/m0_bare_llm/mock-topic/cases.jsonl \
    --report reports/gate
```

### G.4 环境变量与密钥

| 变量 | 用途 | 注意 |
|---|---|---|
| `OPENAI_API_KEY` / `OPENAI_BASE_URL` | live 模式 LLM | 只放 `.env`（已 gitignore）；**不写进本规格、不进 PR** |
| `XHS_RELOAD=0` | 关 uvicorn reload | 长跑/debug |
| `TOOL_EVAL_MODE` | `mock`/`live` 默认值 | 默认 `mock` |
| `COMPARE_MODELS_CONFIG` | 对比页模型注册表 yaml | 缺省只用 mock 两个模型 |

> 面试演示：一律 `mode=mock`，不依赖网络与账号；live 只在本地临时验证。

---

## H. 简历叙事模板（诚实）

**中文（3 条）**
1. 在可运行的 FastAPI 内容 Agent 上实现**白名单工具调用切片**（`knowledge_search → generate_topics`），每次运行落盘结构化 `trace.steps`，支持 mock/live 双模式，demo 可离线一键复现。
2. 构建 **N=X 条带 `expected_trace` 的冻结工具调用评测集**（分层：需检索 / 不乱调 / 空结果 / 拒答 / 追问），用确定性断言评「工具选择 / 参数 / 结果融入 / 伪造数据」，覆盖 G1–G5 五项 Gate。
3. 在同一模型与解码下对比**裸生成 vs Agent+工具**：冻结集上胜率 **X%** / 均分 **+X**（1–5 分）；工具失败场景伪造指标命中 **0** 次。（数字只填实测值，附报告路径。）
4. （若做训练）在 Kaggle 双 T4 上对小模型做 LoRA SFT（~X 条轨迹 + 负例，与评测集零重叠），微调后 G1/G2 达 X%，相较 API 基线降低推理成本。

**英文（1 条，可选）**
> Built an evaluable tool-calling agent slice (allow-listed registry, persisted traces) with a frozen N-case eval set; deterministic assertions on tool choice/params/grounding/refusal; +X win-rate over a no-tool baseline under identical decoding.

**明确禁止写的话术**：已上线 / 服务真实用户 / 爆款率提升 X% / 涨粉 X / SOTA / 平台官方合作 / 任何诊疗或健康功效宣称。

---

## I. 附录

### I.1 与 slot-extractor 的目录/阶段对照

| slot-extractor | 本项目 | 说明 |
|---|---|---|
| `src/slot_extractor/evaluation/` | `app/agent/tool_eval/` | 打分内核，纯函数 |
| `data/eval/test.jsonl` + `test.sha256` + `DATASET_CARD.md` | `data/evals/tool_calling_v0.jsonl` + `.sha256` + `DATASET_CARD.md` | 冻结集 |
| `data/fixtures/technicians/phase05-v1.yaml` | `data/interim/kb_snapshot_v0.json` | 唯一事实来源 |
| `data/raw/v0.x/samples.jsonl` | `data/raw/kb_seed_v0.jsonl` | 原始种子 |
| `data/processed/sft/v0.x/` | `data/processed/training/v0.1/` | 训练样本 |
| `configs/evaluation/phase0x.yaml` | `configs/eval/tool_calling_v0.yaml` | runner 配置 |
| `configs/training/llamafactory/sft/*.yaml` | `configs/train/sft_*.yaml` | 训练 |
| `configs/quantization/phase05.yaml` 注册表 | `configs/models.local.yaml` | 推理后端注册 |
| `src/slot_extractor/tool_loop/` | `app/api/routes_compare.py` + `app/services/compare_service.py` + `static/compare/` | 对比页 |
| Phase 01 脚手架 + harness | P0 | |
| Phase 02 评估集 + M0 | P1 + P2 | |
| Phase 03 训练数据 | P5 | |
| Phase 04 训练矩阵 | P6 | |
| Phase 05 量化 + tool-loop app | P7 + P4（本项目提前：对比页先于训练） | 顺序调整：先有尺子与展示，再决定是否训 |
| Phase 06 迭代 | P8 | |

### I.2 已有 content-agent 文件锚点

| 文件 | 用途 | 本项目关系 |
|---|---|---|
| `app/agent/tools.py` | `ToolRegistry` / `filtered()` / Pydantic params / `build_default_registry` | PR #2 新增 `DEMO_ALLOWLIST`、`DEMO_DENYLIST` |
| `app/agent/mock_factories.py` | `build_mock_backend()` / `build_mock_registry()` / `_stub_topics` | P2/P3 复用；需让 mock topics 对 theme 有区分度 |
| `app/agent/llm.py` | `MockLLMBackend.register(cls, factory)` | bare 路径注册 `TopicsDraft` |
| `app/agent/evals.py` | goal-completion evals | 保留，不删；新评测另建 |
| `app/agent/orchestrator.py` | `XHSAgent` 主循环 | **不用于**工具切片（mock planner 会调度 `generate_contents`） |
| `app/services/topic_service.py` | `generate_topics` 底层 LLM 路径 | bare 与工具路径共用 |
| `app/prompts/topic_generation_prompt.txt` | 选题 prompt | 两侧共用，保证 G3 公平 |
| `app/api/routes_topics.py` | 现有选题路由 | 风格参考 |
| `app/main.py` | FastAPI 装配 + `StaticFiles` | P0/P4 页面挂载 |
| `static/*.html` | 原生静态页 | 风格参考 |
| `app/agent/knowledge.py` | RAG 检索（`search_multi`） | live 模式 `knowledge_search` 后端 |
| `data/raw/sample_notes.json` | 样笔记 | `kb_seed_v0` 的起点 |
| `tests/test_topic_helper.py` | PR #2 切片测试 | P0 基线 |

### I.3 开放问题（需拍板，单项列选项）

1. **P2 M0 与 P3 Agent 的 live 后端**
   - A（推荐）：DeepSeek / OpenAI 兼容 API，一个模型跑两侧，G3 只体现「有无工具」差异；
   - B：两个不同规模模型对比（如 0.6B vs API），混淆「工具」与「模型能力」，需额外控制实验；
   - C：一侧 mock、一侧 live（不可比，不推荐）。
2. **冻结集规模**：A. 30 条（推荐）；B. 24 条（沿用 CARD 草案，更快）；C. 50 条（更稳但人工成本高）。
3. **`angle` 字段来源**：A. 先从 title 拆（v0 妥协，推荐，已在 PR #2）；B. 改 prompt 让模型同时输出 `angle`（需重跑 P0 测试与 PR #2 回归）；C. 二次结构化补全（额外一次 LLM 调用，影响成本与可复现性）。
4. **P4 对比页是否进主应用**
   - A（推荐）：独立小 app（`scripts/run_compare_app.py`），不污染 `app/main.py` 启动链；
   - B：挂进 `app/main.py`（多一个路由组，但演示地址统一）。
5. **训练目标模型**：A. `Qwen3-0.6B`（稳、快，推荐首轮）；B. `Qwen3-1.7B`（效果参考）；C. 两者都跑成小程序矩阵（时间成本 ×2）。
6. **DPO 是否进入首版**
   - A（推荐）：SFT 先行，DPO 仅当 SFT 后仍存在「乱调 / 伪造」错例；
   - B：SFT + DPO 一起做（简历叙事更完整，但负例质量要求更高）。
7. **私有仓公开策略**：A. 保持 private + 面试 live demo（现状）；B. PR #2 合入后拆一个 `content-agent-tool-eval` 公开精简镜像（只含切片 + 评测；需确认无敏感文件）。
8. **政策/过泛词表的维护者**：A. 作者手工维护（v0 推荐）；B. 引入外部安全词表（增加依赖与合规面）。

### I.4 下一步 PR 拆分（建议）

| PR | 范围 | 依赖 | 验收 |
|---|---|---|---|
| PR-A | 本规格入仓 `docs/FULL_PROJECT_SPEC_tool_calling_v0.md` + 在 README 加一行索引 | 无 | 文档可读、无密钥 |
| PR-B（P0 UI） | `static/topic_helper.html` + `POST /topics/helper` + `tests/test_tool_helper_api.py` | PR #2 合入 | G.2 命令可跑；页面可见 trace |
| PR-C（P1） | `data/evals/tool_calling_v0.jsonl`（30 条）+ `.sha256` + `DATASET_CARD.md` + `scripts/freeze_evals.py` + `app/agent/tool_eval/case.py` + 单测 | PR-B（入口字段稳定） | schema 全过；sha 校验；标注 FROZEN |
| PR-D（P2–P3） | `assertions.py` / `rubric.py` / `baseline.py` / `report.py` + `scripts/eval_tool_calling.py` + `scripts/compare_m0.py` + 单测 | PR-C | mock 下 Gate 报告生成；`not_run` 明确 |
| PR-E（P4） | `compare_service.py` + `routes_compare.py` + `static/compare/*` + 测试 | PR-D | 左右栏 + trace + 灰掉不可用模型 |
| PR-F（P5+，按需） | `scripts/build_sft_data.py` + `check_contamination.py` + 数据 v0.1 | PR-D | 去污染 0 命中 |
| PR-G（P6+，按需） | `configs/train/*.yaml` + Kaggle notebook + 回归报告 | PR-F + M2 | M4 判据 |

---

> **结束语**：本规格的主线只有一句——**先把「工具调用做没做对」变成可测的断言，再谈选题好不好，最后才谈训不训。** 任何新增工作若不能落回 B.2 的目录与 C 的阶段验收框，就是范围膨胀。
