# M0 分数卡

backend=mock。本报告只验证 M0 流水线，不作正式 G3 结论。

- backend: `mock`
- model_id: `mock-topic`
- temperature: `0.7`
- max_tokens: `null`
- count: `4`
- prompt_sha256: `fadfcccb9ac067e8624187034a4f931f62e81afe1b9ccae0b46dbc0922c1159e`
- eval: `data/evals/tool_calling_v0.jsonl`
- eval_sha256: `52745dd1a19bdde7bdb068f34d17f0d7bf5cc2138105b357af4f49782486a053`
- n: 30
- 解析成功率: 30/30 (1.000)
- 主均分（must_retrieve + no_retrieve_ok + tool_empty，不含 refuse/clarify）: 5.00 （n=21）
- FAKE_METRIC_RE 命中: 0 处 / 0 条 case
- 零工具: 30/30（trace.steps 为空且 tool_calls_total=0）
- refuse 守规矩: 0/5（单独计数，不并入主均分）
- clarify 守规矩: 0/4（单独计数，不并入主均分）

## 分 bucket 平均 rubric（0–5）

| bucket | n | 均分 | 计入主均分 |
|---|---:|---:|---|
| must_retrieve | 10 | 5.00 | yes |
| no_retrieve_ok | 6 | 5.00 | yes |
| tool_empty | 5 | 5.00 | yes |
| refuse | 5 | 4.60 | no；守规矩 0/5 |
| clarify | 4 | 4.00 | no；守规矩 0/4 |

## Gates

- G1 调用正确: not_run
- G2 结果融入: not_run
- G3 相对基线: not_run（backend=mock，本轮只跑 M0，未做 Agent 对比，不作正式 G3 结论）
- G4 失败可控: not_run
- G5 可复现: not_run
