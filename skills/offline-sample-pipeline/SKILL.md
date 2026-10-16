---
name: offline-sample-pipeline
description: "当目标可以用离线样本完成、不必新鲜爬取时，使用 load_notes 然后 analyze_notes、generate_topics、generate_contents。Use when the goal mentions 演示, 离线, or 样本数据, or when crawl cost is too high."
metadata:
  applies_to: 离线, 演示, 样本
---

# offline-sample-pipeline

## Steps
1. load_notes
2. analyze_notes
3. generate_topics（带 focus_keywords）
4. generate_contents（count=目标篇数）

## Avoid
- 不要默认 crawl_notes
- 不要把本技能正文塞进规划 prompt；需要时调用 load_skill
