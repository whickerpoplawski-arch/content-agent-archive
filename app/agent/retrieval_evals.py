"""Retrieval evals for the knowledge store (M0 baseline harness, 调研规划 M0).

Golden-query eval over a REAL knowledge.db — no LLM, no network: bge ONNX +
BM25 all run locally, so this is a true offline A/B harness for indexing
changes (contextual chunking, query expansion, recency weighting).

Two query styles per the Q2D-Web lesson (arXiv:2609.08887): agent-rewritten
queries distribute differently from human queries, so a human-only benchmark
misleads retriever selection. Cases are derived from the corpus itself:

- doc-level (human-style): query = the document title; expect that doc in top-k.
- goal-level (agent-style): query = the originating goal (planner-flavoured);
  expect ANY doc of that goal in top-k — mirrors how _knowledge_context is
  actually consumed (some relevant knowledge for the goal, not one doc).

Metrics: hit@k, MRR@k, and failure_rate@k = 1 - hit@k (Anthropic's
1-recall@20 failure-rate framing, at the k we actually inject).

Usage:
  python -m app.agent.retrieval_evals                 # eval real DB as-is (baseline)
  python -m app.agent.retrieval_evals --rebuild       # dump docs -> fresh DB re-ingested
                                                      # with CURRENT add() code (A/B)
  python -m app.agent.retrieval_evals --db PATH --json --verbose
"""

from __future__ import annotations

import argparse
import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.agent.knowledge import KnowledgeStore, DB_PATH

KS = (1, 3, 5)          # k values to report
RANK_WINDOW = 5         # MRR / failure-rate window


@dataclass
class GoldenCase:
    query: str
    style: str                       # "doc" (human-ish) | "goal" (agent-ish)
    doc_id: str = ""                 # doc-level expectation
    goal: str = ""                   # goal-level expectation (metadata match)


@dataclass
class CaseResult:
    query: str
    style: str
    hit_rank: int                    # 0 = miss within RANK_WINDOW

    @property
    def hit(self) -> bool:
        return self.hit_rank > 0


@dataclass
class RetrievalEvalSummary:
    n_cases: int
    hit_at: dict[int, float] = field(default_factory=dict)
    mrr: float = 0.0
    failure_rate: float = 0.0        # 1 - hit@RANK_WINDOW (Anthropic 口径)
    by_style: dict[str, dict[str, float]] = field(default_factory=dict)
    misses: list[CaseResult] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "n_cases": self.n_cases,
            "hit_at": {str(k): v for k, v in self.hit_at.items()},
            "mrr": round(self.mrr, 4),
            "failure_rate": round(self.failure_rate, 4),
            "by_style": {
                style: {key: round(val, 4) for key, val in stats.items()}
                for style, stats in self.by_style.items()
            },
        }


# --------------------------------------------------------------------------
# Corpus-derived golden cases
# --------------------------------------------------------------------------

def build_corpus_cases(store: KnowledgeStore) -> list[GoldenCase]:
    """Derive golden cases from the store's own documents table."""
    rows = store._conn.execute(
        "SELECT id, title, source, metadata FROM documents ORDER BY created_at"
    ).fetchall()
    cases: list[GoldenCase] = []
    seen_goals: set[str] = set()
    for doc_id, title, _source, metadata_json in rows:
        title = (title or "").strip()
        if title:
            cases.append(GoldenCase(query=title, style="doc", doc_id=doc_id))
        try:
            goal = (json.loads(metadata_json or "{}") or {}).get("goal", "")
        except Exception:  # noqa: BLE001
            goal = ""
        if goal and goal not in seen_goals:
            seen_goals.add(goal)
            cases.append(GoldenCase(query=goal, style="goal", goal=goal))
    return cases


# --------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------

def _hit_rank(hits: list[dict[str, Any]], case: GoldenCase) -> int:
    for rank, hit in enumerate(hits[:RANK_WINDOW], start=1):
        if case.style == "doc":
            if hit.get("doc_id") == case.doc_id:
                return rank
        else:
            if str((hit.get("metadata") or {}).get("goal", "")) == case.goal:
                return rank
    return 0


def evaluate_retrieval(
    store: KnowledgeStore, cases: list[GoldenCase]
) -> RetrievalEvalSummary:
    results = [
        CaseResult(query=c.query, style=c.style, hit_rank=_hit_rank(store.search(c.query, top_k=RANK_WINDOW), c))
        for c in cases
    ]
    return summarize(results)


def summarize(results: list[CaseResult]) -> RetrievalEvalSummary:
    n = len(results) or 1
    hit_at = {k: sum(1 for r in results if 0 < r.hit_rank <= k) / n for k in KS}
    mrr = sum(1.0 / r.hit_rank for r in results if r.hit) / n
    summary = RetrievalEvalSummary(
        n_cases=len(results),
        hit_at=hit_at,
        mrr=mrr,
        failure_rate=1.0 - hit_at[RANK_WINDOW],
        misses=[r for r in results if not r.hit],
    )
    for style in ("doc", "goal"):
        sub = [r for r in results if r.style == style]
        if not sub:
            continue
        m = len(sub) or 1
        summary.by_style[style] = {
            "n": m,
            "hit@5": sum(1 for r in sub if 0 < r.hit_rank <= 5) / m,
            "mrr": sum(1.0 / r.hit_rank for r in sub if r.hit) / m,
        }
    return summary


# --------------------------------------------------------------------------
# A/B helper: rebuild a corpus with the CURRENT add() implementation
# --------------------------------------------------------------------------

def rebuild_corpus(store: KnowledgeStore) -> KnowledgeStore:
    """Dump documents from ``store`` and re-ingest them into a fresh temp DB
    with the current indexing code, so indexing changes can be A/B'd against
    the same corpus without touching the real DB."""
    rows = store._conn.execute(
        "SELECT id, source, title, text, metadata FROM documents"
    ).fetchall()
    fresh = KnowledgeStore(path=Path(tempfile.mkdtemp()) / "knowledge_rebuilt.db")
    for doc_id, source, title, text, metadata_json in rows:
        try:
            metadata = json.loads(metadata_json or "{}")
        except Exception:  # noqa: BLE001
            metadata = {}
        fresh.add(text, title=title, source=source, metadata=metadata, doc_id=doc_id)
    return fresh


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Knowledge retrieval eval harness")
    parser.add_argument("--db", type=Path, default=DB_PATH, help="knowledge.db to eval")
    parser.add_argument("--rebuild", action="store_true",
                        help="re-ingest the corpus with current add() code into a temp DB first")
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    store = KnowledgeStore(path=args.db)
    if args.rebuild:
        store = rebuild_corpus(store)
    if store.count() == 0:
        print(f"knowledge.db empty at {args.db} — nothing to eval")
        return 1

    cases = build_corpus_cases(store)
    summary = evaluate_retrieval(store, cases)

    if args.as_json:
        print(json.dumps(summary.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(f"docs={store.count()} cases={summary.n_cases}")
        for k in KS:
            print(f"hit@{k}: {summary.hit_at[k]:.1%}")
        print(f"MRR@{RANK_WINDOW}: {summary.mrr:.3f}")
        print(f"failure_rate@{RANK_WINDOW}: {summary.failure_rate:.1%}")
        for style, stats in summary.by_style.items():
            print(f"  [{style}] n={int(stats['n'])} hit@5={stats['hit@5']:.1%} mrr={stats['mrr']:.3f}")
        if args.verbose:
            for miss in summary.misses:
                print(f"  MISS [{miss.style}] {miss.query[:60]}")
    if not args.as_json:
        print(f"\n(eval db: {store.path})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
