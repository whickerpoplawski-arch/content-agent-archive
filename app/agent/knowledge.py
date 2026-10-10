"""Knowledge store: local RAG over crawled notes / analysis reports / fed docs.

sqlite-vec virtual table for dense KNN (bge-small-zh 512-dim vectors, computed
locally by app.agent.embedder) + jieba BM25 for lexical recall, fused with
Reciprocal Rank Fusion — the multi-signal retrieval pattern from Mem0's 2026
algorithm, reimplemented without its heavy dependency chain
(docs/memory-rag-selection.md R2).

Single-file store at data/agent/knowledge.db; embeds share process-wide
embedder; all writes are synchronous SQLite (safe for the run pool).
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import numpy as np

from app.agent.embedder import get_embedder

logger = logging.getLogger("agent.knowledge")

DB_PATH = Path("data") / "agent" / "knowledge.db"
RRF_K = 60  # standard RRF constant
TOP_CHUNK = 400       # chunk max chars
CHUNK_OVERLAP = 60    # sliding overlap so sentences don't split mid-thought
DEFAULT_TOP_K = 5
# 情境前缀 (Anthropic Contextual Retrieval 的免 LLM 版): 入库时把
# 来源/标题/目标/url/日期拼成一行前置进每个 chunk —— 向量嵌入与 BM25
# 用的是同一份 chunks.text, 双索引自动同时语境化。
CONTEXT_PREFIX_MAX_CHARS = 120
# crawl 来源 chunk 的指数时效衰减 tau (天): 小红书爆款半衰期短, 竞品笔记
# 越旧检索加权越低 (TRACE/时序 RAG 的最简落地); analysis/output 知识资产不衰减。
CRAWL_RECENCY_TAU_DAYS = 45.0


_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,          -- 'crawl' | 'analysis' | 'output' | 'manual'
    title TEXT NOT NULL,
    text TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_documents_source ON documents(source);
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    text TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec USING vec0(
    chunk_id INTEGER PRIMARY KEY,
    embedding float[512]
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _context_prefix(
    title: str, source: str, metadata: dict[str, Any], created_at: float
) -> str:
    """Build the situating prefix line prepended to every chunk at ingest.

    Answers "这段文本属于谁/什么事/什么时间" without an LLM call — XHS 笔记
    天然带 title/tags/日期元数据, 直接利用 (调研规划 v2 M1: 免费语境化)。
    """
    bits = [f"知识|{source}"]
    if title:
        bits.append(str(title)[:40])
    goal = str((metadata or {}).get("goal", "") or "")
    url = str((metadata or {}).get("url", "") or "")
    if goal:
        bits.append(f"目标:{goal[:40]}")
    if url:
        bits.append(f"url:{url[:40]}")
    if created_at:
        try:
            bits.append(datetime.fromtimestamp(created_at).strftime("%Y-%m-%d"))
        except (ValueError, OSError, OverflowError):
            pass
    line = "[" + "|".join(bits) + "]"
    return line[:CONTEXT_PREFIX_MAX_CHARS]


def chunk_text(text: str, max_chars: int = TOP_CHUNK, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Slide a window over text on whitespace-friendly boundaries."""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        # prefer breaking at a sentence-ish boundary inside the tail
        if end < len(text):
            for sep in ("。", "！", "？", "\n", "；", ". "):
                cut = text.rfind(sep, start + max_chars - 120, end)
                if cut > start:
                    end = cut + len(sep)
                    break
        chunks.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return [c for c in chunks if c]


class KnowledgeStore:
    """Dense + lexical hybrid retrieval over a single-file SQLite store."""

    def __init__(self, path: Path = DB_PATH) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._load_vec_extension()
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def _load_vec_extension(self) -> None:
        import sqlite_vec

        self._conn.enable_load_extension(True)
        sqlite_vec.load(self._conn)
        self._conn.enable_load_extension(False)

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    def add(
        self,
        text: str,
        *,
        title: str = "",
        source: str = "manual",
        metadata: Optional[dict[str, Any]] = None,
        doc_id: Optional[str] = None,
    ) -> str:
        """Chunk, embed, and store one document. Returns its id.

        Idempotent on ``doc_id``: re-adding an existing id is a no-op, so
        checkpoint-resume replays and repeated run outputs don't duplicate
        entries (callers should derive deterministic ids from content).
        """
        text = (text or "").strip()
        if not text:
            raise ValueError("cannot add empty document")
        doc_id = doc_id or uuid.uuid4().hex[:12]
        now = time.time()
        metadata = metadata or {}
        final_title = title or text[:50]
        prefix = _context_prefix(final_title, source, metadata, now)
        if prefix:
            # 前缀占掉窗口后收紧切块上限, 防 bge 512-token 截断; 前缀随
            # chunk 一起进嵌入与 BM25 (双索引同时语境化)。
            chunks = chunk_text(text, max_chars=max(120, TOP_CHUNK - len(prefix)))
            chunks = [f"{prefix} {c}" for c in chunks]
        else:
            chunks = chunk_text(text)
        vectors = get_embedder().encode(chunks)
        with self._lock:
            try:
                cur = self._conn.cursor()
                if cur.execute(
                    "SELECT 1 FROM documents WHERE id = ?", (doc_id,)
                ).fetchone():
                    return doc_id  # already ingested
                cur.execute(
                    "INSERT INTO documents (id, source, title, text, metadata, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        doc_id,
                        source,
                        final_title,
                        text,
                        json.dumps(metadata, ensure_ascii=False),
                        now,
                    ),
                )
                for seq, (chunk, vec) in enumerate(zip(chunks, vectors)):
                    cur.execute(
                        "INSERT INTO chunks (doc_id, seq, text) VALUES (?, ?, ?)",
                        (doc_id, seq, chunk),
                    )
                    chunk_id = cur.lastrowid
                    cur.execute(
                        "INSERT INTO chunks_vec (chunk_id, embedding) VALUES (?, ?)",
                        (chunk_id, vec.tobytes()),
                    )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return doc_id

    def delete(self, doc_id: str) -> bool:
        with self._lock:
            vec_ids = [
                r[0]
                for r in self._conn.execute(
                    "SELECT chunk_id FROM chunks WHERE doc_id = ?", (doc_id,)
                )
            ]
            for cid in vec_ids:
                self._conn.execute(
                    "DELETE FROM chunks_vec WHERE chunk_id = ?", (cid,)
                )
            cur = self._conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
            self._conn.commit()
            return cur.rowcount > 0

    def count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0])

    def list_documents(
        self, *, limit: int = 100, snippet_chars: int = 100
    ) -> list[dict[str, Any]]:
        """Compact per-doc listing (title/source/snippet/date), newest first.

        小库 digest 直注模式的数据源 (Anthropic: <20万 token 整库入 prompt)。
        """
        rows = self._conn.execute(
            "SELECT id, source, title, text, created_at FROM documents"
            " ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        docs: list[dict[str, Any]] = []
        for doc_id, source, title, text, created_at in rows:
            docs.append(
                {
                    "doc_id": doc_id,
                    "source": source,
                    "title": title or "",
                    "snippet": (text or "")[:snippet_chars],
                    "created_at": created_at,
                }
            )
        return docs

    # ------------------------------------------------------------------
    # Read path
    # ------------------------------------------------------------------

    def _dense_rank(self, query_vec: np.ndarray, top_k: int) -> list[tuple[int, float]]:
        rows = self._conn.execute(
            "SELECT chunk_id, distance FROM chunks_vec"
            " WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
            (query_vec.tobytes(), top_k),
        ).fetchall()
        return [(int(cid), float(dist)) for cid, dist in rows]

    def _lexical_rank(self, query: str, top_k: int) -> list[tuple[int, float]]:
        import jieba

        from rank_bm25 import BM25Okapi

        rows = self._conn.execute("SELECT chunk_id, text FROM chunks").fetchall()
        if not rows:
            return []
        corpus_tokens = [list(jieba.cut(text)) for _, text in rows]
        bm25 = BM25Okapi(corpus_tokens)
        scores = bm25.get_scores(list(jieba.cut(query)))
        order = np.argsort(scores)[::-1][:top_k]
        return [(int(rows[i][0]), float(scores[i])) for i in order if scores[i] > 0]

    def search(
        self,
        query: str,
        *,
        top_k: int = DEFAULT_TOP_K,
        source: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """RRF-fused dense + lexical retrieval; returns ranked chunk hits."""
        return self.search_multi([query], top_k=top_k, source=source)

    def search_multi(
        self,
        queries: list[str],
        *,
        top_k: int = DEFAULT_TOP_K,
        source: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Multi-variant RRF-fused retrieval with crawl-recency decay.

        每个查询变体各出 dense+lexical 两路排名, 全部 2N 路按 RRF 融合
        (CompCQR 组合式查询扩展的落点: 变体由调用方确定性产出, 不依赖 LLM);
        crawl 来源 chunk 按 created_at 做指数时效衰减 (tau=45 天), 爆款旧闻
        自然沉底。返回 ranked chunk hits (score 为加权后 RRF 分)。
        """
        clean = [q.strip() for q in (queries or []) if isinstance(q, str) and q.strip()]
        if not clean:
            return []
        query_vecs = get_embedder().encode(clean, is_query=True)
        with self._lock:
            keep: Optional[set[int]] = None
            if source:
                keep = {
                    int(r[0])
                    for r in self._conn.execute(
                        "SELECT c.chunk_id FROM chunks c JOIN documents d ON d.id = c.doc_id"
                        " WHERE d.source = ?",
                        (source,),
                    )
                }
                if not keep:
                    return []
            lists: list[list[tuple[int, float]]] = []
            for query, query_vec in zip(clean, query_vecs):
                dense = self._dense_rank(query_vec, top_k * 3)
                lexical = self._lexical_rank(query, top_k * 3)
                if keep is not None:
                    dense = [(cid, s) for cid, s in dense if cid in keep]
                    lexical = [(cid, s) for cid, s in lexical if cid in keep]
                lists.append(dense)
                lists.append(lexical)

            rrf: dict[int, float] = {}
            for lst in lists:
                for rank, (cid, _) in enumerate(lst):
                    rrf[cid] = rrf.get(cid, 0.0) + 1.0 / (RRF_K + rank + 1)
            if not rrf:
                return []

            # 时效衰减: 一次取回候选 chunk 的 (source, created_at)
            info: dict[int, tuple[str, Optional[float]]] = {}
            cids = list(rrf)
            for i in range(0, len(cids), 500):
                batch = cids[i : i + 500]
                ph = ",".join("?" * len(batch))
                for cid, src, created in self._conn.execute(
                    "SELECT c.chunk_id, d.source, d.created_at"
                    f" FROM chunks c JOIN documents d ON d.id = c.doc_id"
                    f" WHERE c.chunk_id IN ({ph})",
                    batch,
                ):
                    info[int(cid)] = (src, created)
            now = time.time()
            weighted: dict[int, float] = {}
            for cid, score in rrf.items():
                src, created = info.get(cid, ("", None))
                weight = 1.0
                if src == "crawl" and created:
                    age_days = max(0.0, now - float(created)) / 86400.0
                    weight = math.exp(-age_days / CRAWL_RECENCY_TAU_DAYS)
                weighted[cid] = score * weight
            ranked = sorted(weighted.items(), key=lambda x: x[1], reverse=True)[:top_k]

        hits: list[dict[str, Any]] = []
        for cid, score in ranked:
            row = self._conn.execute(
                "SELECT c.text, d.id, d.title, d.source, d.metadata"
                " FROM chunks c JOIN documents d ON d.id = c.doc_id WHERE c.chunk_id = ?",
                (cid,),
            ).fetchone()
            if not row:
                continue
            text, doc_id, title, src, metadata = row
            hits.append(
                {
                    "chunk_id": cid,
                    "score": round(score, 6),
                    "text": text,
                    "doc_id": doc_id,
                    "title": title,
                    "source": src,
                    "metadata": json.loads(metadata or "{}"),
                }
            )
        return hits

    def close(self) -> None:
        self._conn.close()


_store: Optional[KnowledgeStore] = None
_store_lock = threading.Lock()


def get_knowledge_store() -> KnowledgeStore:
    """Process-wide shared store."""
    global _store
    with _store_lock:
        if _store is None:
            _store = KnowledgeStore()
        return _store
