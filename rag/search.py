"""Rebuildable SQLite FTS5 search index.

The index is a derivative of the current readable revision. Every search
verifies the index revision and re-checks each hit against the confirmed
revision, so a stale index can narrow results but cannot resurrect a forgotten
or deleted memory. Trigram tokenization supports Japanese substring matching;
short queries fall back to LIKE.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable

from contracts.models import SearchHit
from contracts.util import canonical_json, ensure_dir, sha256_hex, utc_now, write_lock
from rag.embeddings import EmbeddingUnavailable, Embedder, text_digest
from rag.store import MemoryStore, RevisionState

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS memories (
  id TEXT PRIMARY KEY,
  statement TEXT NOT NULL,
  conditions TEXT NOT NULL DEFAULT '[]',
  exceptions TEXT NOT NULL DEFAULT '[]',
  kind TEXT NOT NULL,
  scope TEXT NOT NULL,
  project_id TEXT,
  status TEXT NOT NULL,
  revision INTEGER NOT NULL,
  autoload INTEGER NOT NULL,
  source_refs TEXT NOT NULL DEFAULT '[]'
);
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
  id UNINDEXED,
  text,
  tokenize='trigram'
);
CREATE TABLE IF NOT EXISTS excerpts (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  project_id TEXT,
  message_id TEXT,
  role TEXT NOT NULL,
  start INTEGER NOT NULL,
  end INTEGER NOT NULL,
  turn INTEGER,
  text TEXT NOT NULL,
  recorded_at TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS excerpts_fts USING fts5(
  id UNINDEXED,
  text,
  tokenize='trigram'
);
CREATE TABLE IF NOT EXISTS embeddings (
  source_kind TEXT NOT NULL,
  source_id TEXT NOT NULL,
  model_id TEXT NOT NULL,
  prompt_id TEXT NOT NULL,
  dim INTEGER NOT NULL,
  text_hash TEXT NOT NULL,
  vec BLOB NOT NULL,
  status TEXT NOT NULL,
  scope TEXT NOT NULL,
  project_id TEXT,
  session_id TEXT,
  message_id TEXT,
  part_start INTEGER,
  part_end INTEGER,
  turn INTEGER,
  revision INTEGER NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(source_kind, source_id)
);
CREATE INDEX IF NOT EXISTS embeddings_scope ON embeddings(scope, status);
CREATE TABLE IF NOT EXISTS embedding_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

TERM_RE = re.compile(r"[0-9A-Za-z_]+|[\u3040-\u30ff\u3400-\u9fff]+")


def _fts_quote(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


class SearchIndex:
    def __init__(
        self,
        path: Path,
        store: MemoryStore,
        embedding_enabled: bool = False,
        embedder_factory: Callable[[], Embedder] | None = None,
    ):
        self.path = path
        self.store = store
        self.lock_file = path.with_suffix(".lock")
        self.embedding_enabled = bool(embedding_enabled)
        self._embedder_factory = embedder_factory
        self._embedder: Embedder | None = None
        self._embedding_error: str | None = None
        self._embedding_lock = threading.Lock()

    def _connect(self) -> sqlite3.Connection:
        ensure_dir(self.path.parent)
        connection = sqlite3.connect(str(self.path), timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.executescript(SCHEMA)
        return connection

    def rebuild(self, state: RevisionState | None = None) -> dict[str, object]:
        if state is None:
            state = self.store.load()
        with write_lock(self.lock_file, 30):
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("DELETE FROM memories")
                connection.execute("DELETE FROM memories_fts")
                connection.execute("DELETE FROM excerpts")
                connection.execute("DELETE FROM excerpts_fts")
                count = 0
                excerpt_count = 0
                for record in state.memories.values():
                    if record.status in ("deleted",):
                        continue
                    text = "\n".join([record.statement, *record.conditions, *record.exceptions])
                    connection.execute(
                        "INSERT INTO memories(id, statement, conditions, exceptions, kind, scope, project_id, status, revision, autoload, source_refs) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            record.id,
                            record.statement,
                            json.dumps(record.conditions, ensure_ascii=False),
                            json.dumps(record.exceptions, ensure_ascii=False),
                            record.kind,
                            record.scope,
                            record.project_id,
                            record.status,
                            record.revision,
                            1 if record.autoload else 0,
                            json.dumps([ref.to_dict() for ref in record.source_refs], ensure_ascii=False),
                        ),
                    )
                    connection.execute(
                        "INSERT INTO memories_fts(id, text) VALUES (?, ?)",
                        (record.id, text),
                    )
                    count += 1
                for session in state.sessions.values():
                    for excerpt in session.excerpts:
                        connection.execute(
                            "INSERT INTO excerpts(id, session_id, project_id, message_id, role, start, end, turn, text, recorded_at) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (
                                excerpt.id,
                                excerpt.session_id,
                                session.project_id,
                                excerpt.message_id,
                                excerpt.role,
                                excerpt.start,
                                excerpt.end,
                                excerpt.turn,
                                excerpt.text,
                                excerpt.recorded_at,
                            ),
                        )
                        connection.execute(
                            "INSERT INTO excerpts_fts(id, text) VALUES (?, ?)",
                            (excerpt.id, excerpt.text),
                        )
                        excerpt_count += 1
                connection.execute(
                    "INSERT INTO meta(key, value) VALUES ('revision', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(state.revision),),
                )
                connection.commit()
                return {"revision": state.revision, "records": count, "excerpts": excerpt_count}
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def _indexed_revision(self, connection: sqlite3.Connection) -> int | None:
        row = connection.execute("SELECT value FROM meta WHERE key='revision'").fetchone()
        return int(row["value"]) if row else None

    def ensure_current(self, state: RevisionState) -> None:
        if not self.path.exists():
            self.rebuild(state)
            return
        connection = self._connect()
        try:
            revision = self._indexed_revision(connection)
        finally:
            connection.close()
        if revision != state.revision:
            self.rebuild(state)

    # ---------------------------------------------------------- embeddings
    def _get_embedder(self) -> Embedder:
        if self._embedder is not None:
            return self._embedder
        if self._embedder_factory is None:
            raise EmbeddingUnavailable("no embedding backend configured")
        self._embedder = self._embedder_factory()
        return self._embedder

    def _embedding_signature(self, embedder: Embedder) -> str:
        return canonical_json(
            {
                "model_id": embedder.model_id,
                "prompt_id": embedder.prompt_id,
                "dim": embedder.dim,
            }
        )

    def _forbidden_excerpt_ids(self, state: RevisionState) -> set[str]:
        forbidden: set[str] = set()
        for tombstone in state.tombstones:
            memory_id = (
                tombstone.get("id")
                or tombstone.get("memory_id")
                or tombstone.get("target_id")
            )
            record = state.memories.get(memory_id) if memory_id else None
            if record is None:
                continue
            for ref in record.source_refs:
                if ref.excerpt_id:
                    forbidden.add(ref.excerpt_id)
        return forbidden

    def _embedding_sources(
        self, connection: sqlite3.Connection, state: RevisionState
    ) -> list[dict[str, Any]]:
        sources: list[dict[str, Any]] = []
        for row in connection.execute(
            "SELECT id, statement, conditions, exceptions, status, scope, project_id, revision "
            "FROM memories WHERE status IN ('adopted', 'pending', 'conflict')"
        ):
            conditions = json.loads(row["conditions"] or "[]")
            exceptions = json.loads(row["exceptions"] or "[]")
            text = "\n".join([row["statement"], *conditions, *exceptions])
            sources.append(
                {
                    "source_kind": "memory",
                    "source_id": row["id"],
                    "text": text,
                    "status": row["status"],
                    "scope": row["scope"],
                    "project_id": row["project_id"],
                    "session_id": None,
                    "message_id": None,
                    "part_start": None,
                    "part_end": None,
                    "turn": None,
                    "revision": row["revision"],
                }
            )
        forbidden = self._forbidden_excerpt_ids(state)
        for row in connection.execute(
            "SELECT id, session_id, project_id, message_id, start, end, turn, text "
            "FROM excerpts"
        ):
            if row["id"] in forbidden:
                continue
            sources.append(
                {
                    "source_kind": "excerpt",
                    "source_id": row["id"],
                    "text": row["text"],
                    "status": "adopted",
                    "scope": "session",
                    "project_id": row["project_id"],
                    "session_id": row["session_id"],
                    "message_id": row["message_id"],
                    "part_start": row["start"],
                    "part_end": row["end"],
                    "turn": row["turn"],
                    "revision": state.revision,
                }
            )
        return sources

    def build_embeddings(
        self, state: RevisionState | None = None, force: bool = False
    ) -> dict[str, Any]:
        """Rebuild or incrementally refresh the derivative embedding table."""
        if not self.embedding_enabled:
            return {"ok": False, "enabled": False, "reason": "embedding search is disabled"}
        with self._embedding_lock:
            try:
                embedder = self._get_embedder()
            except EmbeddingUnavailable as error:
                self._embedding_error = str(error)
                return {"ok": False, "enabled": True, "error": str(error)}
            state = state or self.store.load()
            started = time.perf_counter()
            with write_lock(self.lock_file, 30):
                connection = self._connect()
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    signature = self._embedding_signature(embedder)
                    row = connection.execute(
                        "SELECT value FROM embedding_meta WHERE key='signature'"
                    ).fetchone()
                    previous = row["value"] if row else None
                    if force or previous != signature:
                        connection.execute("DELETE FROM embeddings")
                        connection.execute(
                            "INSERT INTO embedding_meta(key, value) VALUES ('signature', ?) "
                            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (signature,),
                        )
                    sources = self._embedding_sources(connection, state)
                    existing = {
                        (item["source_kind"], item["source_id"]): dict(item)
                        for item in connection.execute(
                            "SELECT source_kind, source_id, text_hash, vec, status, scope, "
                            "project_id, session_id, message_id, part_start, part_end, turn, revision "
                            "FROM embeddings"
                        )
                    }
                    changed: list[dict[str, Any]] = []
                    upserts: list[tuple[dict[str, Any], bytes | None]] = []
                    for source in sources:
                        source["text_hash"] = text_digest(source["text"])
                        previous = existing.get((source["source_kind"], source["source_id"]))
                        metadata = (
                            source["status"],
                            source["scope"],
                            source["project_id"],
                            source["session_id"],
                            source["message_id"],
                            source["part_start"],
                            source["part_end"],
                            source["turn"],
                            source["revision"],
                        )
                        if previous is None:
                            changed.append(source)
                            upserts.append((source, None))
                            continue
                        previous_metadata = (
                            previous["status"],
                            previous["scope"],
                            previous["project_id"],
                            previous["session_id"],
                            previous["message_id"],
                            previous["part_start"],
                            previous["part_end"],
                            previous["turn"],
                            previous["revision"],
                        )
                        if previous["text_hash"] != source["text_hash"]:
                            changed.append(source)
                            upserts.append((source, None))
                        elif previous_metadata != metadata:
                            upserts.append((source, previous["vec"]))
                    vectors = (
                        embedder.encode_documents([item["text"] for item in changed])
                        if changed
                        else []
                    )
                    if len(vectors) != len(changed):
                        raise EmbeddingUnavailable("embedding backend returned a wrong row count")
                    import numpy as np

                    now = utc_now()
                    for source, vector in zip(changed, vectors, strict=True):
                        source["_vector"] = np.asarray(vector, dtype=np.float32).reshape(-1)
                    for source, previous_vec in upserts:
                        array = source.get("_vector")
                        if array is None:
                            array = np.frombuffer(previous_vec, dtype=np.float32)
                        if int(array.shape[0]) != embedder.dim:
                            raise EmbeddingUnavailable("embedding backend returned a wrong dimension")
                        connection.execute(
                            "INSERT INTO embeddings("
                            "source_kind, source_id, model_id, prompt_id, dim, text_hash, vec, "
                            "status, scope, project_id, session_id, message_id, part_start, part_end, "
                            "turn, revision, updated_at"
                            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                            "ON CONFLICT(source_kind, source_id) DO UPDATE SET "
                            "model_id=excluded.model_id, prompt_id=excluded.prompt_id, dim=excluded.dim, "
                            "text_hash=excluded.text_hash, vec=excluded.vec, status=excluded.status, "
                            "scope=excluded.scope, project_id=excluded.project_id, "
                            "session_id=excluded.session_id, message_id=excluded.message_id, "
                            "part_start=excluded.part_start, part_end=excluded.part_end, "
                            "turn=excluded.turn, revision=excluded.revision, updated_at=excluded.updated_at",
                            (
                                source["source_kind"],
                                source["source_id"],
                                embedder.model_id,
                                embedder.prompt_id,
                                embedder.dim,
                                source["text_hash"],
                                array.tobytes(),
                                source["status"],
                                source["scope"],
                                source["project_id"],
                                source["session_id"],
                                source["message_id"],
                                source["part_start"],
                                source["part_end"],
                                source["turn"],
                                source["revision"],
                                now,
                            ),
                        )
                    current = {(item["source_kind"], item["source_id"]) for item in sources}
                    for key in existing:
                        if key not in current:
                            connection.execute(
                                "DELETE FROM embeddings WHERE source_kind=? AND source_id=?",
                                key,
                            )
                    connection.execute(
                        "INSERT INTO embedding_meta(key, value) VALUES ('revision', ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (str(state.revision),),
                    )
                    connection.commit()
                except Exception as error:
                    connection.rollback()
                    self._embedding_error = str(error)
                    return {"ok": False, "enabled": True, "error": str(error)}
                finally:
                    connection.close()
            self._embedding_error = None
            return {
                "ok": True,
                "enabled": True,
                "model_id": embedder.model_id,
                "dim": embedder.dim,
                "revision": state.revision,
                "total": len(sources),
                "embedded": len(changed),
                "reused": len(sources) - len(changed),
                "seconds": round(time.perf_counter() - started, 3),
            }

    def ensure_embeddings(self, state: RevisionState | None = None, force: bool = False) -> None:
        if not self.embedding_enabled:
            return
        state = state or self.store.load()
        embedder = self._get_embedder()
        connection = self._connect()
        try:
            signature = connection.execute(
                "SELECT value FROM embedding_meta WHERE key='signature'"
            ).fetchone()
            revision = connection.execute(
                "SELECT value FROM embedding_meta WHERE key='revision'"
            ).fetchone()
        finally:
            connection.close()
        expected = self._embedding_signature(embedder)
        if (
            force
            or signature is None
            or signature["value"] != expected
            or revision is None
            or revision["value"] != str(state.revision)
        ):
            result = self.build_embeddings(state, force=force)
            if not result.get("ok"):
                raise EmbeddingUnavailable(str(result.get("error") or result.get("reason")))

    def embedding_status(self) -> dict[str, Any]:
        if not self.embedding_enabled:
            return {"enabled": False}
        connection = self._connect()
        try:
            signature = connection.execute(
                "SELECT value FROM embedding_meta WHERE key='signature'"
            ).fetchone()
            revision = connection.execute(
                "SELECT value FROM embedding_meta WHERE key='revision'"
            ).fetchone()
            count = connection.execute("SELECT COUNT(*) AS value FROM embeddings").fetchone()
        finally:
            connection.close()
        return {
            "enabled": True,
            "signature": signature["value"] if signature else None,
            "revision": int(revision["value"]) if revision else None,
            "vectors": int(count["value"]) if count else 0,
            "error": self._embedding_error,
        }

    def _semantic_hits(
        self,
        query: str,
        state: RevisionState,
        project_id: str | None,
        allowed_scopes: set[str],
        include_pending: bool,
        limit: int,
        session_id: str | None,
    ) -> list[SearchHit]:
        import numpy as np

        embedder = self._get_embedder()
        query_vector = np.asarray(embedder.encode_query(query), dtype=np.float32).reshape(-1)
        if int(query_vector.shape[0]) != embedder.dim:
            raise EmbeddingUnavailable("query embedding has a wrong dimension")
        statuses = {"adopted", "pending", "conflict"} if include_pending else {"adopted"}
        forbidden = self._forbidden_excerpt_ids(state)
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM embeddings WHERE model_id=? AND prompt_id=? AND dim=?",
                (embedder.model_id, embedder.prompt_id, embedder.dim),
            ).fetchall()
            excerpt_ids = [
                row["source_id"]
                for row in rows
                if row["source_kind"] == "excerpt" and row["session_id"] == session_id
            ]
            excerpt_meta: dict[str, sqlite3.Row] = {}
            if excerpt_ids:
                placeholders = ",".join("?" for _ in excerpt_ids)
                for item in connection.execute(
                    f"SELECT id, session_id, project_id, message_id, start, end, text "
                    f"FROM excerpts WHERE id IN ({placeholders})",
                    excerpt_ids,
                ):
                    excerpt_meta[item["id"]] = item
        finally:
            connection.close()

        ranked: list[tuple[float, SearchHit]] = []
        for row in rows:
            if row["source_kind"] == "memory":
                record = state.memories.get(row["source_id"])
                if record is None or record.status not in statuses:
                    continue
                if record.scope not in allowed_scopes:
                    continue
                if record.scope == "session" and (
                    not session_id or record.owner_session_id != session_id
                ):
                    continue
                if record.scope == "project":
                    if not project_id or record.project_id != project_id:
                        continue
                vector = np.frombuffer(row["vec"], dtype=np.float32)
                score = float(np.dot(vector, query_vector))
                ranked.append(
                    (
                        score,
                        SearchHit(
                            memory_id=record.id,
                            statement=record.statement,
                            kind=record.kind,
                            scope=record.scope,
                            project_id=record.project_id,
                            status=record.status,
                            revision=record.revision,
                            score=score,
                            conditions=list(record.conditions),
                            exceptions=list(record.exceptions),
                            source_refs=list(record.source_refs),
                            hit_type="semantic",
                        ),
                    )
                )
                continue
            if not session_id or "session" not in allowed_scopes:
                continue
            if row["session_id"] != session_id or row["source_id"] in forbidden:
                continue
            if project_id and row["project_id"] not in (None, project_id):
                continue
            meta = excerpt_meta.get(row["source_id"])
            if meta is None:
                continue
            vector = np.frombuffer(row["vec"], dtype=np.float32)
            score = float(np.dot(vector, query_vector))
            ranked.append(
                (
                    score,
                    SearchHit(
                        memory_id="",
                        statement=meta["text"],
                        kind="excerpt",
                        scope="session",
                        project_id=meta["project_id"],
                        status="adopted",
                        revision=state.revision,
                        score=score,
                        hit_type="semantic",
                        excerpt_id=meta["id"],
                        session_id=meta["session_id"],
                        message_id=meta["message_id"],
                        start=meta["start"],
                        end=meta["end"],
                    ),
                )
            )
        ranked.sort(key=lambda item: item[0], reverse=True)
        return [hit for _, hit in ranked[: max(limit * 4, 16)]]

    def _fuse(
        self, literal: list[SearchHit], semantic: list[SearchHit], limit: int
    ) -> list[SearchHit]:
        fused: dict[str, list[Any]] = {}
        for hits in (literal, semantic):
            for rank, hit in enumerate(hits, start=1):
                key = hit.excerpt_id or hit.memory_id
                entry = fused.setdefault(key, [0.0, hit])
                entry[0] += 1.0 / (60 + rank)
        ordered = sorted(
            fused.values(),
            key=lambda entry: (-entry[0], entry[1].excerpt_id or entry[1].memory_id),
        )
        return [entry[1] for entry in ordered[:limit]]

    def search(
        self,
        query: str,
        project_id: str | None,
        scopes: list[str] | None = None,
        include_pending: bool = False,
        limit: int = 8,
        session_id: str | None = None,
    ) -> list[SearchHit]:
        state = self.store.load()
        self.ensure_current(state)
        text = (query or "").strip()
        if not text:
            return []
        allowed_scopes = set(scopes or ["personal", "shared_lesson", "project"])
        rows = self._query_rows(text, project_id, allowed_scopes, include_pending, max(limit * 4, 16))
        hits: list[SearchHit] = []
        seen: set[str] = set()
        include_excerpts = session_id is not None and (scopes is None or "session" in scopes)
        for row in rows:
            memory = state.memories.get(row["id"])
            if memory is None:
                continue
            if memory.id in seen:
                continue
            if memory.status == "deleted":
                continue
            if include_pending:
                if memory.status not in ("adopted", "pending", "conflict"):
                    continue
            elif memory.status != "adopted":
                continue
            if memory.scope == "project" and memory.project_id != project_id:
                continue
            if memory.scope not in allowed_scopes:
                continue
            if memory.scope == "session" and (not session_id or memory.owner_session_id != session_id):
                continue
            seen.add(memory.id)
            score = float(row["score"]) if row["score"] is not None else 0.0
            hits.append(
                SearchHit(
                    memory_id=memory.id,
                    statement=memory.statement,
                    kind=memory.kind,
                    scope=memory.scope,
                    project_id=memory.project_id,
                    status=memory.status,
                    revision=memory.revision,
                    score=score,
                    conditions=list(memory.conditions),
                    exceptions=list(memory.exceptions),
                    source_refs=list(memory.source_refs),
                )
            )
            if len(hits) >= limit:
                break
        if include_excerpts and len(hits) < limit:
            blocked = set()
            for tombstone in state.tombstones:
                for fingerprint in tombstone.get("source_fingerprints", []):
                    blocked.add(fingerprint)
            for row in self._excerpt_rows(text, session_id, project_id, max(limit * 4, 16)):
                excerpt_id = row["id"]
                fingerprint = sha256_hex(
                    canonical_json(
                        {
                            "session_id": row["session_id"],
                            "message_id": row["message_id"],
                            "start": row["start"],
                            "end": row["end"],
                        }
                    )
                )
                if fingerprint in blocked:
                    continue
                hits.append(
                    SearchHit(
                        memory_id="",
                        statement=row["text"],
                        kind="excerpt",
                        scope="session",
                        project_id=row["project_id"],
                        status="adopted",
                        revision=state.revision,
                        score=float(row["score"]) if row["score"] is not None else 0.0,
                        hit_type="excerpt",
                        excerpt_id=excerpt_id,
                        session_id=row["session_id"],
                        message_id=row["message_id"],
                        start=row["start"],
                        end=row["end"],
                    )
                )
                if len(hits) >= limit:
                    break
        if self.embedding_enabled:
            semantic: list[SearchHit] = []
            try:
                self.ensure_embeddings(state)
                semantic = self._semantic_hits(
                    text,
                    state,
                    project_id,
                    allowed_scopes,
                    include_pending,
                    limit,
                    session_id,
                )
            except Exception as error:
                # Literal search is the guaranteed path; a missing model, a
                # download failure, or a corrupt vector must never break search.
                self._embedding_error = str(error)
            if semantic:
                hits = self._fuse(hits, semantic, limit)
        return hits

    def _excerpt_rows(
        self, text: str, session_id: str, project_id: str | None, limit: int
    ) -> list[sqlite3.Row]:
        connection = self._connect()
        try:
            project_clause = ""
            params: list[object] = [session_id]
            if project_id:
                project_clause = " AND (e.project_id = ? OR e.project_id IS NULL)"
                params.append(project_id)
            sql = (
                "SELECT e.id, e.session_id, e.project_id, e.message_id, e.start, e.end, e.text, bm25(excerpts_fts) AS score "
                "FROM excerpts_fts JOIN excerpts e ON e.id = excerpts_fts.id "
                f"WHERE e.session_id = ?{project_clause} AND excerpts_fts MATCH ? ORDER BY score LIMIT ?"
            )
            rows: list[sqlite3.Row] = []
            try:
                rows = list(connection.execute(sql, (*params, _fts_quote(text), limit)).fetchall())
            except sqlite3.OperationalError:
                rows = []
            if not rows:
                like_sql = (
                    "SELECT e.id, e.session_id, e.project_id, e.message_id, e.start, e.end, e.text, 0.0 AS score "
                    f"FROM excerpts e WHERE e.session_id = ?{project_clause} AND e.text LIKE ? LIMIT ?"
                )
                pattern = f"%{text}%"
                rows = list(connection.execute(like_sql, (*params, pattern, limit)).fetchall())
            return rows
        finally:
            connection.close()

    def _query_rows(
        self,
        text: str,
        project_id: str | None,
        allowed_scopes: set[str],
        include_pending: bool,
        limit: int,
    ) -> list[sqlite3.Row]:
        connection = self._connect()
        try:
            statuses = ("adopted", "pending", "conflict") if include_pending else ("adopted",)
            placeholders = ",".join("?" for _ in statuses)
            scope_placeholders = ",".join("?" for _ in allowed_scopes)
            params: list[object] = [*statuses, *sorted(allowed_scopes)]
            project_clause = ""
            if project_id:
                project_clause = " AND (m.scope != 'project' OR m.project_id = ?)"
                params.append(project_id)
            else:
                project_clause = " AND m.scope != 'project'"
            sql = (
                "SELECT m.id, bm25(memories_fts) AS score FROM memories_fts "
                "JOIN memories m ON m.id = memories_fts.id "
                f"WHERE m.status IN ({placeholders}) AND m.scope IN ({scope_placeholders}) "
                f"{project_clause} "
                "AND memories_fts MATCH ? ORDER BY score LIMIT ?"
            )
            rows: list[sqlite3.Row] = []
            try:
                rows = list(connection.execute(sql, (*params, _fts_quote(text), limit)).fetchall())
            except sqlite3.OperationalError:
                rows = []
            if not rows:
                terms = TERM_RE.findall(text)
                if len(terms) > 1:
                    match = " AND ".join(_fts_quote(term) for term in terms if len(term) >= 2)
                    if match:
                        try:
                            rows = list(
                                connection.execute(sql, (*params, match, limit)).fetchall()
                            )
                        except sqlite3.OperationalError:
                            rows = []
            if not rows:
                like_sql = (
                    "SELECT m.id, 0.0 AS score FROM memories m "
                    f"WHERE m.status IN ({placeholders}) AND m.scope IN ({scope_placeholders}) "
                    f"{project_clause} AND "
                    "(m.statement LIKE ? OR m.conditions LIKE ? OR m.exceptions LIKE ?) "
                    "LIMIT ?"
                )
                pattern = f"%{text}%"
                rows = list(connection.execute(like_sql, (*params, pattern, pattern, pattern, limit)).fetchall())
            return rows
        finally:
            connection.close()

    def rebuild_from_text(self) -> dict[str, object]:
        """Rebuild after the readable files changed outside a normal proposal."""
        return self.rebuild(self.store.load())
