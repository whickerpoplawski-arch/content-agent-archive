# Formal G3 (M2) — blocked

blocked: need OPENAI_API_KEY (or Codex)

This file is not a formal G3 result. No live model was called. There is no win rate, no tie rate, no loss rate, and no mean Δ. Do not treat any earlier mock G3 number as this result. Mock G3 only checked the pipeline.

Checked on this run: no `/workspace/.env`, no `~/.codex/config.toml`, no `~/.codex/auth.json`, and `OPENAI_API_KEY`, `OPENAI_MODEL`, `OPENAI_BASE_URL`, and `CODEX_HOME` were unset. With those unset, `settings.openai_model` is `deepseek-v4-flash` and `settings.openai_temperature` is `0.7`. A live run records that model id (or `OPENAI_MODEL` / Codex `model` if you set one). Pass the same id to `--model`. Frozen eval sha stays `52745dd1a19bdde7bdb068f34d17f0d7bf5cc2138105b357af4f49782486a053`. Do not start training.

Set `OPENAI_API_KEY` in the environment or `.env`, or provide `~/.codex/auth.json` (`OPENAI_API_KEY`) and `~/.codex/config.toml` (`model`, `base_url`). Optional overrides: `OPENAI_BASE_URL`, `OPENAI_MODEL`.

```bash
python scripts/compare_m0.py --stage m0 \
    --eval data/evals/tool_calling_v0.jsonl \
    --backend live --model deepseek-v4-flash \
    --out experiments/baselines/m0_bare_llm/deepseek-v4-flash/2026-10-22

python scripts/eval_tool_calling.py --mode live \
    --eval data/evals/tool_calling_v0.jsonl \
    --repeat 1 --out experiments/runs --report reports/gate \
    --m0 experiments/baselines/m0_bare_llm/deepseek-v4-flash/2026-10-22/cases.jsonl \
    --latch-meta experiments/runs/run_2026-10-22_51e8496_mock.meta.json

python scripts/compare_m0.py --stage g3 --backend live \
    --agent-run experiments/runs/run_<date>_<commit7>_live.jsonl \
    --m0 experiments/baselines/m0_bare_llm/deepseek-v4-flash/2026-10-22/cases.jsonl \
    --latch-meta experiments/runs/run_2026-10-22_51e8496_mock.meta.json \
    --out reports/gate/<date>_<commit7>_live_g3.json
```

The live eval writes `reports/gate/<date>_<commit7>_live_gate.md` and labels `backend=live`. That report is the formal G3 when the M0 card used the same model id, the same temperature, and the same per-case count. G1/G2/G5 come from the mock latch above; this live run uses `--repeat 1` because temperature 0.7 is not deterministic. The third command reprints the same compare. Replace `<date>` and `<commit7>` with the stem the eval script prints.
