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
from pathlib import Path

from contracts.models import SearchHit
from contracts.util import ensure_dir, write_lock
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
"""

TERM_RE = re.compile(r"[0-9A-Za-z_]+|[\u3040-\u30ff\u3400-\u9fff]+")


def _fts_quote(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


class SearchIndex:
    def __init__(self, path: Path, store: MemoryStore):
        self.path = path
        self.store = store
        self.lock_file = path.with_suffix(".lock")

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
                count = 0
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
                connection.execute(
                    "INSERT INTO meta(key, value) VALUES ('revision', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(state.revision),),
                )
                connection.commit()
                return {"revision": state.revision, "records": count}
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
            elif memory.status != "adopted" or not memory.autoload:
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
        return hits

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
                f"{project_clause} AND m.autoload = 1 "
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
                    f"{project_clause} AND m.autoload = 1 AND "
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
