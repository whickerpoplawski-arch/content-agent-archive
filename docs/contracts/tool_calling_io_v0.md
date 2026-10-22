# 合同 · 工具调用 I/O v0

> 日期：2026-10-22（America/Martinique）  
> 对齐：`GOAL_CARD_工具调用_v0.md` · 仓库 `whickerpoplawski-arch/content-agent`  
> 源码锚点：`app/agent/tools.py`（`build_default_registry`）· `evals.py` · `topic_service.py`  
> 状态：**合同草稿，可开工实现**

---

## 0. 一句话

演示入口只做一件事：`theme` →（可选工具）→ 可解析的 `angles[]`；评测看的是**工具轨迹对不对**，不是端到端发帖。

---

## 1. 演示入口（薄路径）

### 1.1 输入

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `theme` | string | 是 | 一句话主题/人群/品类，如「大学生平价通勤穿搭」 |
| `audience` | string | 否 | 默认 `大学生女生` |
| `count` | int | 否 | 角度数，默认 4，范围 3–5 |
| `allow_tools` | string[] | 否 | 覆盖默认白名单；空则用 §2.1 |
| `mode` | `"live"` \| `"mock"` | 否 | 默认 `mock`（面试/Gate 可复现） |

**拒答前置**（不进工具循环）：
- `theme` 空 / 仅空白 → `status=refuse`，`reason=empty_theme`
- 明显违禁/医疗诊疗承诺等 → `status=refuse`，`reason=policy`（规则表另挂，v0 先关键词）
- 过泛且无受众（如只写「爆款」）→ `status=clarify`，附 1–2 个追问，不调选题工具

### 1.2 输出（成功）

```json
{
  "status": "ok",
  "angles": [
    {
      "angle": "痛点反转：面试前一天衣柜崩溃",
      "title": "研究生面试穿搭｜3套百元局救命",
      "why": "可执行 + 有场景时间点",
      "audience": "应届女研究生",
      "grounded_from": ["knowledge_search#0", "generate_topics#0"]
    }
  ],
  "trace": { "...见 §3..." },
  "meta": { "mode": "mock", "model": "..." }
}
```

| 字段 | 约束 |
|---|---|
| `angles` | 长度 ∈ [3,5]；每项必有 `angle`,`title`,`why`,`audience` |
| `grounded_from` | 至少 1 个可追溯的 tool step id（无工具成功路径可为 `generate_topics#0`） |
| `status` | `ok` \| `degraded` \| `refuse` \| `clarify` |

**与现仓差一档**：今日 `generate_topics` 返回 `{topics:[{title,reason}]}`。薄路径负责**映射**到 `angles[]`（`reason→why`，`angle` 可从 title 拆或由二次结构化补），合同以 `angles[]` 为对外 schema；仓内可先兼容再改 prompt。

### 1.3 输出（降级 / 拒答）

```json
{
  "status": "degraded",
  "angles": [],
  "message": "knowledge empty; refused to invent viral stats",
  "trace": { "..."}
}
```

---

## 2. 工具清单（相对 `build_default_registry`）

### 2.1 演示白名单（默认开启）

| 工具名 | 何时该调 | 关键参数 | 成功长什么样 | 失败怎么兜 |
|---|---|---|---|---|
| `knowledge_search` | 主题/人群可能已在知识库；**多数「需检索」case 的第一步** | `query`（必填，应用 theme 改写）；`source` 可选 `crawl\|analysis\|output\|manual`；`top_k` 1–8 | `{count, hits[{doc_id,title,source,score,text}], note?}` | `count=0` → 可改 `load_notes` 或直接 `generate_topics`（标 `degraded` 若无证据） |
| `load_notes` | KB 空且需要样本笔记；离线 demo | `items` 可空（走 sample） | `NoteItem[]` dump | 空列表 → 不得伪造互动数据 |
| `analyze_notes` | 刚拿到 notes，需要 summary/keywords 再选题 | `items: NoteItem[]` | 分析 dict（summary / top_keywords / …） | 失败则跳过，用 theme 直填 `generate_topics.summary` |
| `generate_topics` | **几乎总要**；最终选题工具 | `summary`；`audience`；`count`；`focus_keywords`（命中 theme）；`knowledge_hints`（来自检索） | `{topics:[{title,reason}]}` | 解析失败 → `status=degraded`，不编造 |

### 2.2 演示黑名单（默认禁用，`registry.filtered`）

| 工具名 | 原因 |
|---|---|
| `crawl_notes` | 需登录浏览器；不可复现 |
| `generate_contents` | 超出「选题+工具调用」切片 |
| `generate_images` | 同上 |
| `publish_to_xhs` | 副作用，需审批 |
| `sync_to_feishu` | 副作用 |
| `read_artifact` / `load_skill` | v0 不测；需要时再开 |

实现：`build_default_registry(...).filtered(DEMO_ALLOWLIST)`；Gate 的 `mock` 模式用已有 `build_mock_registry` 思路，但**只注册白名单**。

### 2.3 推荐轨迹模板（金标用）

**A. 需检索（默认多数）**

```
knowledge_search(query≈theme) → [可选 analyze 若先 load_notes] → generate_topics(...)
```

**B. KB 命中差 / 空**

```
knowledge_search → load_notes → analyze_notes → generate_topics
```

**C. 不该检索（主题自洽、测「不乱调」）**

```
generate_topics(summary=theme, focus_keywords=[...])   # 零检索工具
```

**D. 拒答**

```
(无工具) → status=refuse|clarify
```

**E. 工具空结果仍要诚实**

```
knowledge_search(count=0) → generate_topics 仅基于 theme；angles.why 不得引用「数据/爆款率」；或 status=degraded
```

---

## 3. 期望轨迹格式（评测用）

每条冻结 case 带 `expected_trace`：

```json
{
  "id": "tc_012",
  "theme": "宿舍党低成本护肤",
  "bucket": "must_retrieve",
  "expected_trace": {
    "must_call": ["knowledge_search", "generate_topics"],
    "must_not_call": ["crawl_notes", "publish_to_xhs", "generate_contents"],
    "order_soft": ["knowledge_search", "generate_topics"],
    "param_checks": [
      {"tool": "knowledge_search", "field": "query", "contains_any": ["护肤", "宿舍"]},
      {"tool": "generate_topics", "field": "focus_keywords", "intersects_theme": true}
    ],
    "grounding": "angles 的 grounded_from 至少引用一次 knowledge_search 或 generate_topics"
  },
  "checklist": ["可执行", "有人群", "非空话", "无伪造数据"]
}
```

运行后实际轨迹（Agent 必须落盘）：

```json
{
  "steps": [
    {
      "i": 0,
      "tool": "knowledge_search",
      "params": {"query": "...", "top_k": 5},
      "ok": true,
      "result_digest": {"count": 3, "titles": ["..."]}
    },
    {
      "i": 1,
      "tool": "generate_topics",
      "params": {"summary": "...", "focus_keywords": ["护肤"], "count": 4},
      "ok": true,
      "result_digest": {"n_topics": 4}
    }
  ],
  "forbidden_hits": []
}
```

---

## 4. Gate 断言（对应目标卡 G1–G5）

| Gate | 怎么验 |
|---|---|
| G1 调用正确 | `must_call` 全出现；`must_not_call` 为空；Pydantic 校验失败率 < 5% |
| G2 结果融入 | `grounded_from` 非空；或规则：title/why 与 `result_digest` 有词重叠（v0 可用简单 Jaccard） |
| G3 vs 裸 LLM | 同 model：无工具直出 angles vs 本路径；人工/规则分胜率≥60% 或均分Δ≥+0.3 |
| G4 失败可控 | bucket=`tool_empty` / `refuse` case 不出现伪造点赞/涨粉数字 |
| G5 可复现 | `mode=mock` + 冻结 jsonl + 一键脚本出报告 |

相对现有 `evals.py`：保留 goal completion，**新增** trace 断言与 bare baseline 对比；keyword_coverage 降为辅指标。

---

## 5. 建议落点（本仓内，不新建仓）

| 路径 | 作用 |
|---|---|
| `docs/contracts/tool_calling_io_v0.md` | 本文件（可 PR 进仓） |
| `scripts/run_topic_helper.py` 或 `POST /topics/helper` | 薄入口 |
| `data/evals/tool_calling_v0.jsonl` | 冻结 20–30 case |
| `scripts/eval_tool_calling.py` | Gate runner |
| `app/agent/tools.py` | 演示用 `filtered(DEMO_ALLOWLIST)`；必要时加 `theme→TopicsParams` 适配 |

---

## 6. 非目标（写进合同防膨胀）

- 不评真实爬取质量、不评发帖成功率  
- 不把医学 s1 / LoRA 叙事混进本 Gate  
- 不宣称「工具调用 SOTA」；只报冻结集上的相对裸 LLM 与轨迹指标  

---

## 7. 下一步实现顺序

1. 本合审查字（你点头即可当 v0 冻结）  
2. 实现 `DEMO_ALLOWLIST` + theme 薄入口（mock 可跑）  
3. 写 20–30 条带 `expected_trace` 的 jsonl  
4. Gate 脚本：trace + vs bare  
