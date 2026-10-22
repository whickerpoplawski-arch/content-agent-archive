#!/usr/bin/env python3
"""Build a deterministic KB snapshot from a raw JSONL seed.

    python scripts/build_kb_snapshot.py --raw data/raw/kb_seed_v0.jsonl --out data/interim/kb_snapshot_v0.json

Each input line is ``{"doc_id","title","text","source"}``. Output is
``{"version","docs":[...]}`` sorted by ``doc_id``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def build_snapshot(raw_path: Path, out_path: Path, *, version: str = "kb_snapshot_v0") -> dict:
    docs: list[dict[str, str]] = []
    for lineno, line in enumerate(raw_path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        item = json.loads(line)
        for key in ("doc_id", "title", "text"):
            if not str(item.get(key, "")).strip():
                raise SystemExit(f"{raw_path}:{lineno} missing {key}")
        docs.append(
            {
                "doc_id": str(item["doc_id"]),
                "title": str(item["title"]),
                "text": str(item["text"]),
                "source": str(item.get("source") or "manual"),
            }
        )
    docs.sort(key=lambda doc: doc["doc_id"])
    ids = [doc["doc_id"] for doc in docs]
    if len(ids) != len(set(ids)):
        raise SystemExit("duplicate doc_id in seed")
    if not 20 <= len(docs) <= 40:
        raise SystemExit(f"seed size {len(docs)} is outside 20–40")
    payload = {"version": version, "docs": docs}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Build kb_snapshot JSON from a raw JSONL seed")
    parser.add_argument("--raw", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--version", default="kb_snapshot_v0")
    args = parser.parse_args()
    raw_path = Path(args.raw)
    out_path = Path(args.out)
    if not raw_path.is_absolute():
        raw_path = PROJECT_ROOT / raw_path
    if not out_path.is_absolute():
        out_path = PROJECT_ROOT / out_path
    payload = build_snapshot(raw_path, out_path, version=args.version)
    print(f"wrote {out_path} docs={len(payload['docs'])} version={payload['version']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
