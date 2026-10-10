# 修复方案：`_coerce_params` 对真实 LLM planner 输出的 dict/list 包装抽取缺口

> 临时文档 · 2026-08-11 · 由真实 LLM 全量测试发现

## 1. 问题现象（真实 LLM trace 证据）

`run_agent_demo.py --backend openai`（deepseek-v4-flash[1M] @ Ark，与 Codex 配置一致）跑出：

| 工具 | 结果 |
|------|------|
| generate_contents | 前 2 次失败：`topic` 字段收到 `{'topics': [{'title': '...'}]}`（dict 包列表），期望 str |
| generate_images | 6/6 全失败，`elapsed=0.0ms`（参数校验阶段秒挂，未到工具处理器）|

`generate_images` 的多个标量/列表字段都拿到 dict 包装值：

```
topic_title      <- {'topics': [{'title': '...'}]}     (期望 str)
content_title    <- {'contents': [{'title': '...'}]}   (期望 str)
content_hashtags <- {'contents': [{'title': '...'}]}   (期望 list[str])
content_cta      <- {'contents': [{'title': '...'}]}   (期望 str)
...
```

触发 3 次重规划，浪费 ~20 次冗余 LLM 调用（31 calls / 5.7min），最终反思器放弃配图、以"文案已生成"停机。

## 2. 根因

`app/agent/tools.py::_coerce_params` 当前实现只做了一件事：当 str 字段收到 dict 且该 dict **顶层直接**含 `_SCALAR_KEYS`（title/reason/text/name/content/summary/value）时，取该标量。三个缺口：

1. **不处理 list[str] 字段**：`content_hashtags: list[str]` 完全跳过。
2. **不钻取 dict 包列表**：`{'topics': [{title:...}]}` 顶层键是 `topics`（列表），不在 `_SCALAR_KEYS`，不匹配 -> 原样留下 -> 校验失败。
3. **字段名前缀映射缺失**：`ImagesParams` 字段名为 `content_title`/`content_hashtags`/`topic_title`，记录里的键是 `title`/`hashtags`/`title`（去掉 `content_`/`topic_` 前缀），当前没有这层映射。

planner 写 `$latest.generate_topics`（不带 `.topics.0.title`）时，`executor._best_effort_dig` 回退到整个结果 dict `{topics:[...]}`，正好命中上述缺口。

## 3. 修复设计（通用，覆盖 generate_contents / generate_images / publish_to_xhs）

在 `app/agent/tools.py` 重写 `_coerce_params`，引入两个辅助函数：

### 3.1 `_unwrap_to_record(value)` - 把任意包装值降到首条记录 dict

处理顺序（最多递归 4 层防死循环）：
- `list` -> 取首元素，继续
- `dict` 且含已知列表键（`topics`/`contents`/`items`/`notes`/`results`/`top_notes`/`data`/`articles`）-> 取该列表首元素，继续
- `dict` 且无列表包装 -> 这就是记录本身，返回
- 其它 -> 返回 None

例：`{'topics':[{'title':'A'}]}` -> `{'title':'A'}`；`[{'title':'A'}]` -> `{'title':'A'}`；`{'title':'A'}` -> `{'title':'A'}`（向后兼容）。

### 3.2 `_candidate_keys(field_name)` - 字段名到源键的候选序列

`[字段名本身, 去掉前缀 content_/topic_ 的形式, ..._SCALAR_KEYS]`，去重保序。

例：`content_title` -> `['content_title','title',...]`；`content_hashtags` -> `['content_hashtags','hashtags',...]`；`content_type` -> `['content_type','type',...]`（exact 命中 `content_type`）。

### 3.3 新 `_coerce_params`

对每个字段：
- **str 字段**：已是 str -> 跳过；否则 `_unwrap_to_record` 后按候选键取首个 str。
- **list[str] 字段**（`get_origin(ann) is list` 且内层是标量类型）：已是 list -> 跳过；否则 `_unwrap_to_record` 后按候选键取首个 list（取到 str 则包成单元素 list）。
- **记录列表字段**（`list[NoteItem]` 等）：不动，保持原 list 完整到达 Pydantic。
- 其它类型字段不动。

核心原则：**只在值类型不匹配时介入**，已正确的不动，保证向后兼容。

## 4. 受影响文件

- `app/agent/tools.py` - 重写 `_coerce_params`，新增 `_unwrap_to_record` / `_candidate_keys`，新增 `_LIST_KEYS`。
- `tests/test_agent.py` - 新增 6 个用例：dict 包列表、裸 list、list[str] 字段、content_ 前缀映射、记录列表不降级、已正确值不动；不破坏现有 `test_tool_registry_coerces_dict_to_str`。

## 5. 验证计划

1. **单元测试**：`python -m pytest tests/ -v` 全绿（含新增 coerce 用例）。
2. **页面实测（真实 LLM）**：起服务，浏览器开 `http://127.0.0.1:8000/static/agent_console.html`，提交不含"配图建议"的目标（避免触发图片生成，本会话无法分析图片），观察 SSE 事件流里 generate_contents / generate_topics 等工具的 `tool_completed` 正常、最终产物正确渲染。

## 6. 不在本次范围

- 图片生成端到端测试（`gpt-image-1` 在 Ark 404 不可用，且本会话无法分析图片；`_coerce_params` 修复本身对 `generate_images` 参数同样生效，由单元测试覆盖）。
- 护栏未接入 HTTP 入口（另一独立缺口）。
