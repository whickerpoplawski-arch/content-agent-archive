"""SQLite profile store: learnings + episodic run index.

This is a local, single-tenant memory lane (``user_id=local``). It does not
replace KnowledgeStore and it does not talk to CortexDB / TinyMemory.

Writes stamp ``user_id``. Explicit learn/edit/forget go through the scrubber.
Import from ``profile.json`` is idempotent and will not revive a row the user
has forgotten.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from app.agent.memory_scrub import require_clean_text, scrub_memory_text

logger = logging.getLogger("agent.memory_store")

DEFAULT_DB_PATH = Path("data") / "agent" / "memory.db"
USER_ID = "local"

KINDS = ("preference", "fact", "procedure", "correction", "veto")
LANES = ("general", "situational")
STATUSES = ("active", "superseded", "forgotten", "candidate")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS learnings (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL DEFAULT 'local',
    text TEXT NOT NULL,
    norm TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL,
    lane TEXT NOT NULL DEFAULT 'general',
    topics_json TEXT NOT NULL DEFAULT '[]',
    confidence REAL NOT NULL DEFAULT 0.6,
    pinned INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',
    source TEXT NOT NULL DEFAULT 'user',
    evidence TEXT NOT NULL DEFAULT '',
    superseded_by TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_learnings_status ON learnings(status, kind);
CREATE INDEX IF NOT EXISTS idx_learnings_norm ON learnings(norm, kind);

CREATE TABLE IF NOT EXISTS episodic_runs (
    run_id TEXT PRIMARY KEY,
    goal TEXT NOT NULL,
    signature TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT '',
    titles_json TEXT NOT NULL DEFAULT '[]',
    topics_json TEXT NOT NULL DEFAULT '[]',
    snippet TEXT NOT NULL DEFAULT '',
    result_count INTEGER NOT NULL DEFAULT 0,
    quality_json TEXT NOT NULL DEFAULT '',
    knowledge_doc_ids TEXT NOT NULL DEFAULT '[]',
    hits INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_episodic_sig ON episodic_runs(signature);

CREATE TABLE IF NOT EXISTS consolidate_cursor (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_run_at TEXT,
    revision INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS pack_meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    revision INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT
);
"""

_store_ctx: ContextVar[Optional["ProfileStore"]] = ContextVar(
    "agent_profile_store", default=None
)


def default_db_path() -> Path:
    override = os.environ.get("AGENT_MEMORY_DB", "").strip()
    if override:
        return Path(override)
    return Path(DEFAULT_DB_PATH)


def bind_store(store: "ProfileStore"):
    """Bind the store used by memory tools for the current run/task."""
    return _store_ctx.set(store)


def reset_store(token) -> None:
    _store_ctx.reset(token)


def get_profile_store() -> "ProfileStore":
    current = _store_ctx.get()
    if current is not None:
        return current
    return ProfileStore(default_db_path())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", (text or "").strip().lower())


def _json_dumps(value: Any) -> str:
    return json.dumps(value if value is not None else [], ensure_ascii=False)


def _check_kind(kind: str) -> str:
    value = (kind or "preference").strip().lower()
    if value not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    return value


def _check_lane(lane: str) -> str:
    value = (lane or "general").strip().lower()
    if value not in LANES:
        raise ValueError(f"lane must be one of {', '.join(LANES)}")
    return value


def _new_id() -> str:
    return "lrn_" + uuid.uuid4().hex[:12]


def _legacy_run_id(signature: str) -> str:
    return "legacy:" + (signature or "none")[:48]


def _units(text: str) -> set[str]:
    compact = re.sub(r"\s+", "", (text or "").lower())
    words = set(re.findall(r"[a-z0-9]{2,}", compact))
    cjk = "".join(re.findall(r"[\u4e00-\u9fff]", compact))
    grams: set[str] = set()
    if len(cjk) == 1:
        grams.add(cjk)
    elif cjk:
        grams.update(cjk[i : i + 2] for i in range(len(cjk) - 1))
    return words | grams


def _jaccard(a: str, b: str) -> float:
    left, right = _units(a), _units(b)
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _topic_overlap(query: str, blob: str) -> float:
    """Reuse the orchestrator scorer when the package is already imported."""
    try:
        from app.agent.orchestrator import _topic_overlap as overlap
    except Exception:  # noqa: BLE001
        return _jaccard(query, blob)
    try:
        return float(overlap(query or "", blob or ""))
    except Exception:  # noqa: BLE001
        return _jaccard(query, blob)


def _goal_signature(goal: str) -> str:
    try:
        from app.agent.orchestrator import _goal_signature as signature
    except Exception:  # noqa: BLE001
        return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", (goal or "").lower())
    return signature(goal)


def _fts_query(text: str) -> Optional[str]:
    tokens: list[str] = []
    for run in re.findall(r"[\u4e00-\u9fff]+", text or ""):
        if len(run) >= 2:
            tokens.append(run)
            tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
    tokens.extend(re.findall(r"[0-9A-Za-z_]{2,}", text or ""))
    uniq = list(dict.fromkeys(tokens))[:12]
    if not uniq:
        return None
    return " OR ".join(f'"{token}"' for token in uniq)


class ProfileStore:
    """Learnings and episodic run rows in ``data/agent/memory.db``."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path is not None else default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._ready = False
        self._fts: Optional[bool] = None

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=3000")
        return conn

    def _ensure(self) -> None:
        if self._ready:
            return
        with self._lock:
            if self._ready:
                return
            conn = self._connect()
            try:
                conn.executescript(_SCHEMA)
                if self._fts is None:
                    try:
                        conn.execute(
                            "CREATE VIRTUAL TABLE IF NOT EXISTS learnings_fts "
                            "USING fts5(learning_id, text, topics)"
                        )
                        self._fts = True
                    except sqlite3.OperationalError:
                        self._fts = False
                conn.commit()
                self._ready = True
            finally:
                conn.close()

    def _write(self, fn):
        self._ensure()
        with self._lock:
            conn = self._connect()
            try:
                result = fn(conn)
                conn.commit()
                return result
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def _read(self, fn):
        self._ensure()
        with self._lock:
            conn = self._connect()
            try:
                return fn(conn)
            finally:
                conn.close()

    def _bump_revision(self, conn: sqlite3.Connection) -> None:
        now = _now()
        conn.execute(
            """
            INSERT INTO pack_meta(id, revision, updated_at) VALUES (1, 1, ?)
            ON CONFLICT(id) DO UPDATE SET
                revision = revision + 1,
                updated_at = excluded.updated_at
            """,
            (now,),
        )

    def _fts_sync(
        self,
        conn: sqlite3.Connection,
        learning_id: str,
        text: str,
        topics: list[str],
        status: str,
    ) -> None:
        if not self._fts:
            return
        try:
            conn.execute(
                "DELETE FROM learnings_fts WHERE learning_id = ?", (learning_id,)
            )
            if status in ("active", "candidate"):
                conn.execute(
                    "INSERT INTO learnings_fts(learning_id, text, topics) VALUES (?, ?, ?)",
                    (learning_id, text, " ".join(topics)),
                )
        except sqlite3.OperationalError:
            self._fts = False

    def revision(self) -> int:
        def _read(conn):
            row = conn.execute("SELECT revision FROM pack_meta WHERE id = 1").fetchone()
            return int(row["revision"]) if row else 0

        return self._read(_read)

    def cursor(self) -> dict[str, Any]:
        def _read(conn):
            row = conn.execute(
                "SELECT last_run_at, revision, note FROM consolidate_cursor WHERE id = 1"
            ).fetchone()
            if row is None:
                return {"last_run_at": None, "revision": 0, "note": ""}
            return {
                "last_run_at": row["last_run_at"],
                "revision": int(row["revision"]),
                "note": row["note"] or "",
            }

        return self._read(_read)

    def bump_cursor(self, note: str = "") -> dict[str, Any]:
        def _op(conn):
            now = _now()
            conn.execute(
                """
                INSERT INTO consolidate_cursor(id, last_run_at, revision, note)
                VALUES (1, ?, 1, ?)
                ON CONFLICT(id) DO UPDATE SET
                    last_run_at = excluded.last_run_at,
                    revision = revision + 1,
                    note = excluded.note
                """,
                (now, note or ""),
            )

        self._write(_op)
        return self.cursor()

    @staticmethod
    def _learning_from_row(row: sqlite3.Row) -> dict[str, Any]:
        topics = json.loads(row["topics_json"] or "[]")
        return {
            "id": row["id"],
            "user_id": row["user_id"],
            "text": row["text"],
            "kind": row["kind"],
            "lane": row["lane"],
            "topics": topics if isinstance(topics, list) else [],
            "confidence": float(row["confidence"] or 0),
            "pinned": bool(row["pinned"]),
            "status": row["status"],
            "source": row["source"],
            "evidence": row["evidence"] or "",
            "superseded_by": row["superseded_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _episodic_from_row(row: sqlite3.Row) -> dict[str, Any]:
        titles = json.loads(row["titles_json"] or "[]")
        topics = json.loads(row["topics_json"] or "[]")
        doc_ids = json.loads(row["knowledge_doc_ids"] or "[]")
        raw_quality = row["quality_json"] or ""
        try:
            quality = json.loads(raw_quality) if raw_quality else None
        except json.JSONDecodeError:
            quality = raw_quality
        return {
            "run_id": row["run_id"],
            "goal": row["goal"],
            "signature": row["signature"],
            "status": row["status"],
            "titles": titles if isinstance(titles, list) else [],
            "topics": topics if isinstance(topics, list) else [],
            "snippet": row["snippet"] or "",
            "result_count": int(row["result_count"] or 0),
            "quality": quality,
            "knowledge_doc_ids": doc_ids if isinstance(doc_ids, list) else [],
            "hits": int(row["hits"] or 1),
            "updated_at": row["updated_at"],
        }

    def get(self, learning_id: str) -> Optional[dict[str, Any]]:
        def _read(conn):
            row = conn.execute(
                "SELECT * FROM learnings WHERE id = ?", (learning_id,)
            ).fetchone()
            return self._learning_from_row(row) if row else None

        return self._read(_read)

    def find_by_norm(self, norm: str, kind: str) -> Optional[dict[str, Any]]:
        def _read(conn):
            row = conn.execute(
                "SELECT * FROM learnings WHERE norm = ? AND kind = ? ORDER BY updated_at DESC LIMIT 1",
                (norm, kind),
            ).fetchone()
            return self._learning_from_row(row) if row else None

        return self._read(_read)

    def list_learnings(
        self,
        *,
        status: Optional[str] = None,
        kind: Optional[str] = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit or 200), 2000))

        def _read(conn):
            clauses = []
            args: list[Any] = []
            if status:
                clauses.append("status = ?")
                args.append(status)
            if kind:
                clauses.append("kind = ?")
                args.append(kind)
            where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
            rows = conn.execute(
                f"SELECT * FROM learnings{where} ORDER BY pinned DESC, updated_at DESC LIMIT ?",
                (*args, limit),
            ).fetchall()
            return [self._learning_from_row(row) for row in rows]

        return self._read(_read)

    def list_for_pack(self) -> list[dict[str, Any]]:
        """Active rows, plus anything the user pinned that is not forgotten."""

        def _read(conn):
            rows = conn.execute(
                """
                SELECT * FROM learnings
                WHERE status != 'forgotten'
                  AND (status = 'active' OR pinned = 1)
                  AND NOT (status = 'superseded' AND pinned = 0)
                ORDER BY pinned DESC, updated_at DESC
                """
            ).fetchall()
            return [self._learning_from_row(row) for row in rows]

        return self._read(_read)

    def count_learnings(self, status: str = "active") -> int:
        def _read(conn):
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM learnings WHERE status = ?", (status,)
            ).fetchone()
            return int(row["n"]) if row else 0

        return self._read(_read)

    def learn(
        self,
        text: str,
        *,
        kind: str = "preference",
        lane: str = "general",
        topics: Optional[list[str]] = None,
        confidence: float = 0.9,
        pinned: bool = False,
        source: str = "user",
        status: str = "active",
        evidence: str = "",
        user_id: str = USER_ID,
    ) -> dict[str, Any]:
        """Insert or refresh a learning. Secrets raise and change nothing."""
        cleaned = require_clean_text(text)
        kind = _check_kind(kind)
        lane = _check_lane(lane)
        status = status if status in STATUSES else "active"
        topic_list = _clean_topics(topics)
        norm = _norm(cleaned.text)
        existing = self.find_by_norm(norm, kind)
        if existing is not None:
            return self._save_learning(
                existing["id"],
                text=cleaned.text,
                norm=norm,
                kind=kind,
                lane=lane,
                topics=topic_list or list(existing.get("topics") or []),
                confidence=max(float(existing.get("confidence") or 0), float(confidence)),
                pinned=bool(pinned) or bool(existing.get("pinned")),
                status="active" if status == "active" else status,
                source=source or existing.get("source") or "user",
                evidence=evidence or existing.get("evidence") or "",
                user_id=user_id or existing.get("user_id") or USER_ID,
                created_at=existing.get("created_at") or _now(),
                respect_forgotten=False,
            )
        return self._save_learning(
            _new_id(),
            text=cleaned.text,
            norm=norm,
            kind=kind,
            lane=lane,
            topics=topic_list,
            confidence=float(confidence),
            pinned=bool(pinned),
            status=status,
            source=source or "user",
            evidence=evidence or "",
            user_id=user_id or USER_ID,
            created_at=_now(),
            respect_forgotten=False,
        )

    def _save_learning(
        self,
        learning_id: str,
        *,
        text: str,
        norm: str,
        kind: str,
        lane: str,
        topics: list[str],
        confidence: float,
        pinned: bool,
        status: str,
        source: str,
        evidence: str,
        user_id: str,
        created_at: str,
        respect_forgotten: bool,
    ) -> dict[str, Any]:
        def _op(conn):
            nonlocal status
            current = conn.execute(
                "SELECT * FROM learnings WHERE id = ?", (learning_id,)
            ).fetchone()
            if current is not None and current["status"] == "forgotten" and respect_forgotten:
                return self._learning_from_row(current)
            # A promoted dream insight must not fall back to candidate on the next pass.
            if (
                current is not None
                and current["status"] == "active"
                and status == "candidate"
            ):
                status = "active"
            now = _now()
            created = current["created_at"] if current is not None else created_at
            conn.execute(
                """
                INSERT INTO learnings (
                    id, user_id, text, norm, kind, lane, topics_json, confidence,
                    pinned, status, source, evidence, superseded_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    user_id = excluded.user_id,
                    text = excluded.text,
                    norm = excluded.norm,
                    kind = excluded.kind,
                    lane = excluded.lane,
                    topics_json = excluded.topics_json,
                    confidence = excluded.confidence,
                    pinned = excluded.pinned,
                    status = excluded.status,
                    source = excluded.source,
                    evidence = excluded.evidence,
                    updated_at = excluded.updated_at
                """,
                (
                    learning_id,
                    user_id or USER_ID,
                    text,
                    norm,
                    kind,
                    lane,
                    _json_dumps(topics),
                    float(confidence),
                    1 if pinned else 0,
                    status,
                    source,
                    evidence or "",
                    created,
                    now,
                ),
            )
            self._fts_sync(conn, learning_id, text, topics, status)
            self._bump_revision(conn)
            row = conn.execute(
                "SELECT * FROM learnings WHERE id = ?", (learning_id,)
            ).fetchone()
            return self._learning_from_row(row)

        return self._write(_op)

    def forget(self, learning_id: str) -> bool:
        """Mark a learning forgotten. Does not delete episodic runs."""

        def _op(conn):
            current = conn.execute(
                "SELECT * FROM learnings WHERE id = ?", (learning_id,)
            ).fetchone()
            if current is None:
                return False
            conn.execute(
                """
                UPDATE learnings
                SET status = 'forgotten', pinned = 0, updated_at = ?
                WHERE id = ?
                """,
                (_now(), learning_id),
            )
            self._fts_sync(conn, learning_id, current["text"], [], "forgotten")
            self._bump_revision(conn)
            return True

        return bool(self._write(_op))

    def pin(self, learning_id: str, pinned: bool = True) -> Optional[dict[str, Any]]:
        def _op(conn):
            current = conn.execute(
                "SELECT * FROM learnings WHERE id = ?", (learning_id,)
            ).fetchone()
            if current is None or current["status"] == "forgotten":
                return None
            conn.execute(
                "UPDATE learnings SET pinned = ?, updated_at = ? WHERE id = ?",
                (1 if pinned else 0, _now(), learning_id),
            )
            self._bump_revision(conn)
            row = conn.execute(
                "SELECT * FROM learnings WHERE id = ?", (learning_id,)
            ).fetchone()
            return self._learning_from_row(row)

        return self._write(_op)

    def edit(
        self,
        learning_id: str,
        *,
        text: Optional[str] = None,
        kind: Optional[str] = None,
        lane: Optional[str] = None,
        topics: Optional[list[str]] = None,
    ) -> Optional[dict[str, Any]]:
        current = self.get(learning_id)
        if current is None:
            return None
        if current["status"] == "forgotten":
            return None
        new_text = current["text"]
        if text is not None and text.strip():
            new_text = require_clean_text(text).text
        new_kind = _check_kind(kind) if kind else current["kind"]
        new_lane = _check_lane(lane) if lane else current["lane"]
        new_topics = _clean_topics(topics) if topics is not None else list(current["topics"])
        return self._save_learning(
            learning_id,
            text=new_text,
            norm=_norm(new_text),
            kind=new_kind,
            lane=new_lane,
            topics=new_topics,
            confidence=float(current["confidence"]),
            pinned=bool(current["pinned"]),
            status=current["status"] if current["status"] != "forgotten" else "active",
            source=current["source"],
            evidence=current["evidence"],
            user_id=current["user_id"] or USER_ID,
            created_at=current["created_at"],
            respect_forgotten=True,
        )

    def recall(
        self,
        query: str = "",
        *,
        limit: int = 8,
        include_candidates: bool = False,
    ) -> dict[str, Any]:
        """Topic-aware recall over learnings, with optional FTS5 hits as a boost."""
        limit = max(1, min(int(limit or 8), 50))
        statuses = ["active"]
        if include_candidates:
            statuses.append("candidate")
        fts_ids = self._fts_ids(query) if query else set()

        def _read(conn):
            marks = ",".join("?" for _ in statuses)
            rows = conn.execute(
                f"SELECT * FROM learnings WHERE status IN ({marks})",
                tuple(statuses),
            ).fetchall()
            return [self._learning_from_row(row) for row in rows]

        ranked: list[tuple[float, dict[str, Any]]] = []
        for row in self._read(_read):
            blob = row["text"] + " " + " ".join(row["topics"])
            if query:
                score = _topic_overlap(query, blob)
                if row["id"] in fts_ids:
                    score = max(score, 0.2) + 0.15
                if score <= 0:
                    continue
            else:
                score = 1.0 if row["pinned"] or row["kind"] == "veto" else 0.5
            ranked.append((score, row))
        ranked.sort(key=lambda item: (item[0], item[1]["pinned"], item[1]["updated_at"]), reverse=True)
        episodic = self.recall_episodic(query, limit=limit)
        return {
            "learnings": [row for _, row in ranked[:limit]],
            "episodic": episodic,
            "user_id": USER_ID,
        }

    def _fts_ids(self, query: str) -> set[str]:
        match = _fts_query(query)
        if not match or not self._fts:
            return set()

        def _read(conn):
            try:
                rows = conn.execute(
                    "SELECT learning_id FROM learnings_fts WHERE learnings_fts MATCH ? LIMIT 40",
                    (match,),
                ).fetchall()
            except sqlite3.OperationalError:
                return []
            return [row["learning_id"] for row in rows]

        try:
            return set(self._read(_read))
        except sqlite3.OperationalError:
            return set()

    def count_episodic(self) -> int:
        def _read(conn):
            row = conn.execute("SELECT COUNT(*) AS n FROM episodic_runs").fetchone()
            return int(row["n"]) if row else 0

        return self._read(_read)

    def get_episodic(self, run_id: str) -> Optional[dict[str, Any]]:
        def _read(conn):
            row = conn.execute(
                "SELECT * FROM episodic_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            return self._episodic_from_row(row) if row else None

        return self._read(_read)

    def upsert_episodic(
        self,
        *,
        run_id: str,
        goal: str,
        signature: str = "",
        status: str = "",
        titles: Optional[list[str]] = None,
        topics: Optional[list[str]] = None,
        snippet: str = "",
        result_count: int = 0,
        quality: Any = None,
        knowledge_doc_ids: Optional[list[str]] = None,
        hits: int = 1,
    ) -> dict[str, Any]:
        signature = signature or _goal_signature(goal)
        run_id = (run_id or "").strip() or f"sig:{signature[:48]}"
        clean_titles = [_clean_asset(item) for item in (titles or [])]
        clean_titles = [item for item in clean_titles if item][:12]
        clean_topics = [_clean_asset(item) for item in (topics or [])]
        clean_topics = [item for item in clean_topics if item][:16]
        clean_snippet = _clean_asset(snippet)[:180]
        try:
            quality_json = (
                json.dumps(quality, ensure_ascii=False, default=str) if quality is not None else ""
            )
        except TypeError:
            quality_json = ""

        def _op(conn):
            current = conn.execute(
                "SELECT * FROM episodic_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            stored_hits = int(hits or 1)
            if current is not None:
                stored_hits = max(stored_hits, int(current["hits"] or 1))
            now = _now()
            conn.execute(
                """
                INSERT INTO episodic_runs (
                    run_id, goal, signature, status, titles_json, topics_json,
                    snippet, result_count, quality_json, knowledge_doc_ids, hits, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    goal = excluded.goal,
                    signature = excluded.signature,
                    status = excluded.status,
                    titles_json = excluded.titles_json,
                    topics_json = excluded.topics_json,
                    snippet = excluded.snippet,
                    result_count = excluded.result_count,
                    quality_json = excluded.quality_json,
                    knowledge_doc_ids = excluded.knowledge_doc_ids,
                    hits = excluded.hits,
                    updated_at = excluded.updated_at
                """,
                (
                    run_id,
                    goal or "",
                    signature,
                    status or "",
                    _json_dumps(clean_titles),
                    _json_dumps(clean_topics),
                    clean_snippet,
                    int(result_count or 0),
                    quality_json,
                    _json_dumps(list(knowledge_doc_ids or [])),
                    stored_hits,
                    now,
                ),
            )
            if signature and not run_id.startswith("legacy:"):
                conn.execute(
                    "DELETE FROM episodic_runs WHERE run_id = ?",
                    (_legacy_run_id(signature),),
                )
            row = conn.execute(
                "SELECT * FROM episodic_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            return self._episodic_from_row(row)

        return self._write(_op)

    def recall_episodic(self, query: str = "", *, limit: int = 8) -> list[dict[str, Any]]:
        """Rank run rows by topic overlap across goal, titles, topics, and snippet."""
        limit = max(1, min(int(limit or 8), 30))

        def _read(conn):
            rows = conn.execute(
                "SELECT * FROM episodic_runs ORDER BY updated_at DESC LIMIT 400"
            ).fetchall()
            return [self._episodic_from_row(row) for row in rows]

        rows = self._read(_read)
        if not query:
            return rows[:limit]
        scored: list[tuple[float, dict[str, Any]]] = []
        for row in rows:
            blob = " ".join(
                [
                    row.get("goal") or "",
                    " ".join(row.get("titles") or []),
                    " ".join(row.get("topics") or []),
                    row.get("snippet") or "",
                ]
            )
            score = _topic_overlap(query, blob)
            if score >= 0.4:
                scored.append((score, row))
        scored.sort(key=lambda item: (item[0], item[1].get("hits") or 1), reverse=True)
        return [row for _, row in scored[:limit]]

    def upsert_scalar(self, key: str, value: str) -> Optional[dict[str, Any]]:
        text = f"{key}: {value}".strip()
        if not value or not text:
            return None
        learning_id = f"lrn_scalar_{key}"
        try:
            cleaned = require_clean_text(text)
        except Exception:
            scrubbed = scrub_memory_text(text, reject_secrets=False)
            if not scrubbed.ok:
                return None
            cleaned_text = scrubbed.text
        else:
            cleaned_text = cleaned.text
        return self._save_learning(
            learning_id,
            text=cleaned_text,
            norm=_norm(cleaned_text),
            kind="preference",
            lane="general",
            topics=[],
            confidence=0.95,
            pinned=False,
            status="active",
            source="import",
            evidence=key,
            user_id=USER_ID,
            created_at=_now(),
            respect_forgotten=False,
        )

    def sync_facts(self, facts: dict[str, Any]) -> int:
        """Collapse per-goal fact copies into a few global fact/veto rows."""
        if not isinstance(facts, dict) or not facts:
            return 0
        groups: dict[tuple, dict[str, Any]] = {}
        for entry in facts.values():
            if not isinstance(entry, dict):
                continue
            tone = str(entry.get("preferred_tone") or "").strip()
            structure = str(entry.get("preferred_structure") or "").strip()
            avoids = tuple(
                sorted(
                    {
                        str(item).strip()
                        for item in (entry.get("avoid_topics") or [])
                        if str(item).strip()
                    }
                )
            )
            domains = []
            for item in entry.get("target_domains") or []:
                text = str(item).strip()
                if text and text not in domains:
                    domains.append(text)
            bucket = groups.setdefault(
                (tone, structure, avoids),
                {"domains": [], "tone": tone, "structure": structure, "avoids": avoids},
            )
            for domain in domains:
                if domain not in bucket["domains"]:
                    bucket["domains"].append(domain)
        written = 0
        for key, bucket in groups.items():
            fact_id = "lrn_fact_" + hashlib.sha1(repr(key).encode("utf-8")).hexdigest()[:12]
            text = _fact_text(bucket)
            if not text:
                continue
            self._save_learning(
                fact_id,
                text=text,
                norm=_norm(text),
                kind="fact",
                lane="general",
                topics=list(bucket["domains"]),
                confidence=0.7,
                pinned=False,
                status="active",
                source="import",
                evidence="user_facts",
                user_id=USER_ID,
                created_at=_now(),
                respect_forgotten=True,
            )
            written += 1
            for avoid in bucket["avoids"]:
                veto_id = "lrn_veto_" + hashlib.sha1(avoid.encode("utf-8")).hexdigest()[:12]
                veto_text = f"Avoid: {avoid}"
                self._save_learning(
                    veto_id,
                    text=veto_text,
                    norm=_norm(veto_text),
                    kind="veto",
                    lane="general",
                    topics=[],
                    confidence=0.9,
                    pinned=False,
                    status="active",
                    source="import",
                    evidence="user_facts.avoid",
                    user_id=USER_ID,
                    created_at=_now(),
                    respect_forgotten=True,
                )
                written += 1
        return written

    def sync_insights(
        self,
        items: list[Any],
        *,
        source_default: str = "import",
    ) -> int:
        """Mirror procedural insights into learnings.

        Dream rows (those carrying ``source_goal``) land as ``candidate`` until
        confidence and hits clear the skill-promotion thresholds. Imported
        history stays ``active`` so existing lessons do not disappear.
        """
        written = 0
        for item in items or []:
            if not isinstance(item, dict):
                continue
            if self._upsert_insight(item, source_default=source_default):
                written += 1
        return written

    def _upsert_insight(self, item: dict[str, Any], *, source_default: str) -> bool:
        from app.core.config import settings

        title = str(item.get("title") or "").strip()
        insight = str(item.get("insight") or "").strip()
        if title and insight and title not in insight:
            text = f"{title}: {insight}"
        else:
            text = insight or title
        if not text or title.upper() == "SKIP":
            return False
        scrubbed = scrub_memory_text(text, reject_secrets=False)
        if not scrubbed.ok:
            return False
        raw_id = str(item.get("id") or "").strip()
        if raw_id.startswith("lrn_"):
            learning_id = raw_id
        elif raw_id:
            learning_id = "lrn_" + raw_id
        else:
            learning_id = "lrn_ins_" + hashlib.sha1(_norm(title or text).encode("utf-8")).hexdigest()[:12]
        applies = [str(topic).strip() for topic in (item.get("applies_to") or []) if str(topic).strip()]
        lane = "situational" if applies else "general"
        lifecycle = str(item.get("lifecycle") or "active").lower()
        source = "dream" if item.get("source_goal") or item.get("source") == "dream" else source_default
        if lifecycle in {"superseded", "merged"}:
            status = "superseded"
        elif source == "dream":
            confidence = float(item.get("confidence") or 0.5)
            hits = int(item.get("hits") or 1)
            promoted = confidence >= float(settings.agent_skill_promote_confidence) and hits >= int(
                settings.agent_skill_promote_hits
            )
            status = "active" if promoted else "candidate"
        else:
            status = "active"
        self._save_learning(
            learning_id,
            text=scrubbed.text,
            norm=_norm(scrubbed.text),
            kind="procedure",
            lane=lane,
            topics=applies,
            confidence=float(item.get("confidence") or 0.55),
            pinned=False,
            status=status,
            source=source,
            evidence=str(item.get("source_goal") or ""),
            user_id=USER_ID,
            created_at=str(item.get("created_at") or _now()),
            respect_forgotten=True,
        )
        return True

    def import_episodic(self, entries: list[Any]) -> int:
        written = 0
        for entry in entries or []:
            if not isinstance(entry, dict):
                continue
            goal = str(entry.get("goal") or "")
            signature = str(entry.get("signature") or "") or _goal_signature(goal)
            if not signature:
                continue
            run_id = str(entry.get("run_id") or "") or _legacy_run_id(signature)
            if self.get_episodic(run_id) is not None:
                continue
            status = ""
            if "steps_ok" in entry:
                status = "ok" if entry.get("steps_ok") else "incomplete"
            self.upsert_episodic(
                run_id=run_id,
                goal=goal,
                signature=signature,
                status=status,
                titles=list(entry.get("titles") or []),
                topics=list(entry.get("topics") or []),
                snippet=str(entry.get("snippet") or ""),
                result_count=int(entry.get("results") or 0),
                quality=entry.get("quality"),
                hits=int(entry.get("hits") or 1),
            )
            written += 1
        return written

    def migrate_profile(self, profile: Optional[dict[str, Any]]) -> dict[str, int]:
        """Import audience/tone, deduped facts, insights, and episodic metadata."""
        data = profile or {}
        stats = {"preferences": 0, "facts": 0, "procedures": 0, "episodic": 0}
        if data.get("audience"):
            if self.upsert_scalar("audience", str(data["audience"])):
                stats["preferences"] += 1
        if data.get("tone"):
            if self.upsert_scalar("tone", str(data["tone"])):
                stats["preferences"] += 1
        stats["facts"] = self.sync_facts(data.get("user_facts") or {})
        stats["procedures"] = self.sync_insights(
            list(data.get("procedural_insights") or []),
            source_default="import",
        )
        episodic = data.get("episodic") or data.get("lessons") or []
        stats["episodic"] = self.import_episodic(list(episodic))
        return stats

    def consolidate_learnings(self) -> dict[str, Any]:
        """Merge near-duplicate active/candidate rows and advance the job cursor.

        No model call. Safe to run at the end of a dream pass.
        """
        rows = self.list_learnings(limit=2000)
        pool = [row for row in rows if row["status"] in {"active", "candidate"}]
        dropped: set[str] = set()
        merged = 0
        for index, left in enumerate(pool):
            if left["id"] in dropped:
                continue
            for right in pool[index + 1 :]:
                if right["id"] in dropped or left["kind"] != right["kind"]:
                    continue
                same = _norm(left["text"]) == _norm(right["text"]) or _jaccard(
                    left["text"], right["text"]
                ) >= 0.92
                if not same:
                    continue
                keep, drop = _prefer_learning(left, right)
                self._mark_superseded(drop["id"], keep["id"])
                dropped.add(drop["id"])
                merged += 1
        cursor = self.bump_cursor(note=f"merged={merged}")
        return {"merged": merged, "revision": cursor["revision"], "cursor": cursor}

    def _mark_superseded(self, learning_id: str, superseded_by: str) -> None:
        def _op(conn):
            conn.execute(
                """
                UPDATE learnings
                SET status = 'superseded', superseded_by = ?, updated_at = ?
                WHERE id = ? AND status != 'forgotten'
                """,
                (superseded_by, _now(), learning_id),
            )
            row = conn.execute(
                "SELECT text FROM learnings WHERE id = ?", (learning_id,)
            ).fetchone()
            if row is not None:
                self._fts_sync(conn, learning_id, row["text"], [], "superseded")
            self._bump_revision(conn)

        self._write(_op)


def _prefer_learning(left: dict[str, Any], right: dict[str, Any]) -> tuple[dict, dict]:
    def key(row: dict[str, Any]):
        return (
            1 if row.get("pinned") else 0,
            1 if row.get("status") == "active" else 0,
            float(row.get("confidence") or 0),
            str(row.get("updated_at") or ""),
        )

    if key(right) > key(left):
        return right, left
    return left, right


def _clean_topics(topics: Optional[list[str]]) -> list[str]:
    out: list[str] = []
    for topic in topics or []:
        text = str(topic).strip()
        if text and text not in out:
            out.append(text[:80])
    return out[:16]


def _clean_asset(value: Any) -> str:
    scrubbed = scrub_memory_text(str(value or ""), reject_secrets=False)
    if not scrubbed.ok:
        return ""
    return scrubbed.text


def _fact_text(bucket: dict[str, Any]) -> str:
    bits = []
    domains = bucket.get("domains") or []
    if domains:
        bits.append("domains=" + ",".join(domains))
    if bucket.get("tone"):
        bits.append("tone=" + bucket["tone"])
    if bucket.get("structure"):
        bits.append("structure=" + bucket["structure"])
    if bucket.get("avoids"):
        bits.append("avoid=" + ",".join(bucket["avoids"]))
    if not bits:
        return ""
    return "User fact: " + "; ".join(bits)
