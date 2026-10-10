# 记忆层提升规划(Memory Layer Upgrade Plan)

> 触发场景:记忆内容多到 20 条摘要装不下用户画像时。
> 本文档基于本仓库记忆层**实测现状** + GitHub 高星 agent(Mem0 / MemGPT-Letta / Zep / CrewAI)记忆系统行为调研,产出分阶段落地规划。原则:先补行为,后补基础设施;不补的明确写「不做」。

---

## 0. 实测现状(2026-08-13,锚定代码与数据)

| 项 | 现状 | 证据 |
|----|------|------|
| 存储 | 单 JSON `data/agent/profile.json`,37KB | 实测 |
| 内容 | audience / tone + episodic(lessons 镜像)+ procedural | `data/agent/profile.json` |
| 写入 | 固定代码 `_remember_lesson`,run 尾自动 | [orchestrator.py:169](app/agent/orchestrator.py#L169) |
| 去重 | `_goal_signature`(去标点/空格/小写)全字符串匹配 | [orchestrator.py:253](app/agent/orchestrator.py#L253) |
| 有界 | episodic 留 20、procedural 留 20 | `_dedup_by_goal(keep=20)` / `procedural[-20:]` |
| 读取 | `_profile_text`:子串**双向包含**召回 episodic 5 / procedural 3 | [orchestrator.py:204](app/agent/orchestrator.py#L204) |
| 检索 | 无向量、无倒排,纯子串 | `_lesson_relevant` |
| 反馈 | **无**用户采纳/评分写入通道 | 全仓无 feedback 字段 |
| 事实提取 | **无**自动抽取用户偏好 | 记忆只存「系统做过什么」 |
| 遗忘 | **无**衰减,仅同签名覆盖 | `_dedup_by_goal` 取最新 |
| semantic | 键声明但 `_profile_text` 显式跳过 | [orchestrator.py:209](app/agent/orchestrator.py#L209) |

### 0.1 撞顶实证(为什么现在是规划时机)

`profile.json` 近 5 条 lessons 中 4 条为暑期实习主题,仅 goal 措辞不同:

```
goal: 挖掘小红书上西电实习相关笔记…生成3篇实习种草文案
goal: 为西电学生生成3篇关于暑期实习的小红书分享文案…
goal: 为西电学生生成 3 篇暑期实习文案
```

→ 措辞微调 → `_goal_signature` 不同 → **各占独立条目** → 20 条上限被同主题近似重复占满。后果:用户画像(多目标域、风格渐变)没位置,且 `quality` 数组嵌套进 lessons 让 JSON 膨胀至 37KB。

**结论:撞顶不是「迟早」,是已有雏形。** 提升记忆层刻不容缓,但按「先行为后基础设施」分阶段。

---

## 1. 目标架构:三层冷热记忆

```
热层(in-context)   audience / tone / 当前会话反馈     —— 每次注入 planner prompt(现状保留)
温层(profile.json) user_facts + episodic/procedural  —— 有界,升级召回与去重
冷层(可选,撞顶后) 全文/向量档案(本地,非服务)        —— 语义检索,突破表达力上限
```

分层动机(Mem0/Letta 同理):记忆总量与检索质量解耦 —— 热层保响应相关,冷层保容量。

---

## 2. 分阶段落地

### 阶段 1:反馈回路(行为补全,零依赖)—— 必做,最高优先级

**问题**:记忆只累积「系统做过什么」,不累积「用户要什么」。这是「越用越懂」的最致命缺口。

**落地**
- profile 新增 `user_preferences` 字段:`[{ "content_id", "goal_signature", "liked", "score", "note", "ts" }]`。
- 产物出口(现有 API 层)加「采纳/评分」写入通道:用户对已生成 results 点「喜欢 / 打 5 分 / 备注」→ 写 `user_preferences`。
- `_profile_text` 注入偏好摘要:把高采纳风格/主题加权拼进 profile 文本,planner 按偏好生成。
- `_remember_lesson` 不动;偏好与经验分离存储。

**验证**
- 测试:写入偏好 → 下次 `_profile_text` 含偏好加权 → 生成 topic/内容偏向偏好域。
- 行为验收:同一用户连跑 2 次,第二次生成明显偏向前次高评分产物的风格。

### 阶段 2:自动事实提取(一次 LLM 调用,零依赖)—— 必做

**问题**:现在不抽「用户是谁/喜欢什么」,只存 goal 摘要。

**落地**
- run 尾 `_remember_lesson` 旁新增 `_extract_user_facts(goal, outputs, backend)`:一次结构化 LLM 调用(Pydantic `UserFacts`),从本轮 goal + 产物抽 `{ target_domains[], preferred_tone, preferred_structure, avoid_topics[] }`。
- 存 `profile.user_facts`,按签名合并更新(新事实叠加,不覆盖历史)。
- Mock 后端注册 `UserFacts` 工厂,离线可测。

**验证**
- 测试:mock run 后 `user_facts` 含抽取结果,字段 schema 完整。
- 行为验收:2 个不同主题 run 后,`user_facts` 正确区分两个目标域,不互相污染。

### 阶段 3:召回升级(轻依赖,默认倒排;向量可选)—— 撞顶后再上

**问题**:子串双向包含召回,「语义近但措辞异」漏召回;同主题近似重复占满 20 条。

**落地**
- **3a(默认)**:`episodic` 去重键从「全文签名」升级为「签名 + 主题指纹」(抽取主题名词集合做归一化 key),近似重复合并计数而非各占位。
- **3b(默认)**:`_profile_text` 召回从子串匹配升级为**倒排索引**评分(词频 × 目标域词命中),相关召回 top-5。
- **3c(可选,仅当 3a/3b 后召回仍不够)**:本地向量(chromadb 内存模式 或 纯 numpy cosine),`semantic` 键启用,存全文 embedding;无外部服务依赖。

**验证**
- 测试:构造「语义近措辞异」两 goal,3b 命中而子串漏;近似重复不占双条目。
- 行为验收:档案 50 条规模下,召回 top-5 相关度人工抽查 ≥4/5。

### 阶段 4:遗忘 / 衰减(轻依赖)—— 防「越用越厚但越不准」

**问题**:只增不减(除同签名覆盖),旧经验与新偏好权重相同。

**落地**
- 条目加 `updated_at` / `hit_count`。
- `_dedup_by_goal` 与召回排序改 `recency × hit_count × quality` 加权,冷条目自然降权/归档。
- 超期(如 90 天未命中)条目移入冷层或清除。

**验证**
- 测试:冷条目降权后不被召回;新偏好覆盖旧偏好。
- 行为验收:长时间未用主题不再出现在 profile 注入文本。

### 阶段 5:档案治理(独立于记忆,但堵真实溢出)—— 必做

**问题**(已实测):`runs/`、`console_runs/`、`raw/crawled_notes_*`、`crawl_jobs/`、`checkpoints/` 无限累积;housekeeping `agent_housekeeping_auto=False` **默认关** + 概率触发,实际不清理。

**落地**
- 默认开启 housekeeping:`agent_housekeeping_auto` 默认改 `True`(或改为每次 run 后必触发,去掉 `random.random()<0.01` 概率)。
- 产物轮转:`data/raw/crawled_notes_*`、`data/output/images/` 加入 TTL 清理。
- checkpoint 完成即清(现状 `is_complete` 才 clear,失败残留需补兜底)。

**验证**
- 测试:构造超龄文件 → purge 命中;默认配置下清理真实执行(而非概率跳过)。
- 行为验收:连续 10 次 run 后档案目录总量有上限。

---

## 3. 明确不做(Simplicity First)

- ❌ 不引入外部向量库服务 / 图数据库服务(阶段 3c 仅本地内存向量,且撞顶后才上)。
- ❌ 不做多用户隔离 / 多 agent 记忆分区(单用户单机场景,无需求)。
- ❌ 不做记忆可视化面板 / 记忆浏览 UI(未提出需求前不做)。
- ❌ 不迁移现有 profile.json(保持字段兼容,旧数据可读)。

---

## 4. 里程碑与验收总表

| 阶段 | 优先级 | 依赖 | 验收 | 状态(2026-08-13) |
|------|--------|------|------|------|
| 1 反馈回路 | P0 | 无 | 偏好写入 → 下次生成偏向偏好 | ✅ 已落地(`POST /agent/runs/{id}/feedback` + `_preferences_text` 注入) |
| 2 事实提取 | P0 | LLM 后端(现有) | user_facts 抽取正确,多域不污染 | ✅ 已落地(`_extract_user_facts`/`_store_user_facts`,run 尾自动,mock 工厂注册) |
| 3a/3b 召回升级 | P1 | 阶段 2 后 | 语义近召回、近似重复去重 | ✅ 已落地(主题指纹去重 + 倒排评分召回,阈值 0.85/子集规则) |
| 3c 向量(可选) | P2 | 3a/3b 不足才触发 | 档案 50 条召回 top-5 ≥4/5 | ⏸ 未触发(见 §5 量化信号) |
| 4 遗忘衰减 | P1 | 阶段 3 后 | 冷条目降权、旧偏好可被覆盖 | ✅ 已落地(`updated_at`/`hits` + recency×hits×quality 加权裁剪) |
| 5 档案治理 | P0 | 无(独立) | 清理真实执行,档案有上限 | ✅ 已落地(`agent_housekeeping_auto=True` 每次 run 后必触发 + 产物 TTL) |

> 建议实施顺序:阶段 5(堵溢出,半小时内可完成)→ 阶段 1 → 阶段 2 → 阶段 3a/3b → 阶段 4。3c 留作撞顶后的正式触发点。

### 落地记录(2026-08-13)

- **验证**:`tests/test_memory_upgrade.py`(11 例)+ 全量 113 通过;`scripts/memory_upgrade_demo.py` 端到端演示五阶段交互(近似重复合并 hits=2、考研主题分开、偏好 avg_score=5.0 注入、同主题召回/异主题过滤)。
- **测试隔离**:新增 `tests/conftest.py` 会话级把默认 profile 指到 tmp,测试不再污染真实 `data/agent/profile.json`。
- **真实 profile 重建**:升级期间测试曾误写真实 profile(audience/tone 被覆盖、episodic 折叠)。`scripts/rebuild_profile.py` 从 `console_runs` 真实历史重建(西电研究生/真实克制,13 次真实 run 合并为 7 条 lesson,hits 计数),派生键 `user_facts`/`user_preferences` 置空待真实 run 再生。
- **已知权衡**:近似重复去重基于「去通用词后的字符集 + 子集/重合 0.85」启发式;用户反馈写入走 `user_preferences`(有界 200 条),偏好聚合按 goal 签名。

---

## 5. 触发 3c(向量)的量化信号

当前「撞顶」用以下任一信号判定,达到即进入阶段 3c:

1. `episodic` 中「同主题近似重复」占比 > 50%(当前 4/5,已超)。
2. 阶段 3b 倒排召回 top-5 人工相关度 < 4/5 持续 3 轮。
3. `profile.json` 增长到含 quality 数组的 lessons 超 20 条后,用户画像(非重复主题)仍被挤出。
