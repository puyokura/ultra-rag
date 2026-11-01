"""RAG application service used by the CLI, HTTP API, MCP server, and dsh plugin."""

from __future__ import annotations

from typing import Any

from contracts.config import Layout, load_config
from contracts.models import (
    ContractError,
    MemoryRecord,
    Proposal,
    ProposalResult,
    SearchHit,
    SourceRef,
    SyncBundle,
)
from contracts.util import (
    check_identifier,
    read_json,
    utc_now,
    write_json,
    write_lock,
)
from rag.profile import applicable_constraints, render_constraints
from rag.search import SearchIndex
from rag.store import MemoryStore


class RagService:
    def __init__(self, layout: Layout, store: MemoryStore | None = None, index: SearchIndex | None = None):
        self.layout = layout
        self.config = load_config(layout, create=False)
        role = str(self.config.get("role", "server"))
        self.store = store or MemoryStore(
            layout.rag,
            device_id=self.config["device_id"],
            role=role,
            max_excerpt_chars=int(self.config.get("rag", {}).get("max_excerpt_chars", 4000)),
        )
        self.index = index or SearchIndex(layout.rag / "index" / "search.sqlite", self.store)
        self.session_state_file = self.store.session_state_file
        self.session_state_lock = self.store.control_dir / "session-state.lock"

    # --------------------------------------------------------------- lifecycle
    def initialise(self) -> int:
        state = self.store.initialize()
        self.index.rebuild(state)
        return state.revision

    def revision(self) -> int:
        return self.store.current_revision()

    # ---------------------------------------------------------------- snapshot
    def _seen_revision(self, session_id: str) -> int | None:
        data = read_json(self.session_state_file, {}) or {}
        row = data.get(session_id)
        if not isinstance(row, dict):
            return None
        return int(row.get("seen_revision", 0))

    def _mark_seen(self, session_id: str, revision: int) -> None:
        with write_lock(self.session_state_lock, 10):
            data = read_json(self.session_state_file, {}) or {}
            data[session_id] = {"seen_revision": revision, "updated_at": utc_now()}
            write_json(self.session_state_file, data)

    def snapshot(
        self,
        project_id: str | None = None,
        query: str = "",
        session_id: str | None = None,
        related_limit: int = 8,
    ) -> dict[str, Any]:
        if session_id:
            check_identifier(session_id, "session_id")
        related_limit = max(1, min(int(related_limit), 50))
        state = self.store.load()
        profile_text = (state.directory / "profile" / "current.md").read_text(encoding="utf-8")
        constraints = applicable_constraints(list(state.memories.values()), project_id)
        constraints_text = render_constraints(constraints, state.revision)
        related = self.index.search(query, project_id, limit=related_limit) if query.strip() else []
        pending = [
            record
            for record in state.memories.values()
            if record.status in ("pending", "conflict")
            and (
                record.scope in ("personal", "shared_lesson")
                or (record.scope == "project" and record.project_id == project_id)
            )
        ]
        pending.sort(key=lambda item: (item.scope, item.project_id or "", item.recorded_at))
        notifications: list[str] = []
        seen: int | None = 0
        if session_id:
            seen = self._seen_revision(session_id)
            if seen is None:
                # A new session starts from the current confirmed revision; old
                # history is not replayed as a "changed since last turn" notice.
                self._mark_seen(session_id, state.revision)
                seen = state.revision
                notifications = []
            else:
                for row in self.store.updates_after(seen):
                    memory = state.memories.get(row.get("memory_id") or "")
                    row_scope = row.get("scope") or (memory.scope if memory else None)
                    row_project = row.get("project_id") or (memory.project_id if memory else None)
                    if row_scope == "session":
                        continue
                    if row_scope == "project" and row_project != project_id:
                        continue
                    label = {
                        "adopted": "追加/変更",
                        "pending": "確認待ち",
                        "conflict": "競合保留",
                        "forgotten": "自動参照停止",
                        "deleted": "記録削除",
                        "merged": "既存と同一",
                        "rejected": "不採用",
                    }.get(row.get("status", ""), row.get("status", ""))
                    if memory and row.get("status") != "deleted":
                        notifications.append(
                            f"rev {row.get('revision')}: {label} [{memory.kind}/{memory.scope}] "
                            f"{memory.statement} (`{memory.id}`)"
                        )
                    else:
                        notifications.append(
                            f"rev {row.get('revision')}: {label} `{row.get('memory_id') or '-'}`"
                        )
                self._mark_seen(session_id, state.revision)
        context = self._context_text(
            state.revision, profile_text, constraints_text, related, pending, notifications
        )
        max_context_chars = int(self.config.get("rag", {}).get("max_context_chars", 120000))
        overflow = len(context) > max_context_chars
        return {
            "revision": state.revision,
            "project_id": project_id,
            "profile": profile_text,
            "constraints": constraints_text,
            "related": [hit.to_dict() for hit in related],
            "pending": [
                {
                    "id": record.id,
                    "statement": record.statement,
                    "kind": record.kind,
                    "scope": record.scope,
                    "status": record.status,
                }
                for record in pending
            ],
            "notifications": notifications,
            "seen_revision": seen,
            "context_text": context,
            "context_chars": len(context),
            "max_context_chars": max_context_chars,
            "overflow": overflow,
            "split_required": overflow,
            "overflow_hint": (
                "context is larger than the configured display budget; do not truncate or drop records. "
                "Split the request by project/session or raise rag.max_context_chars explicitly."
                if overflow
                else None
            ),
        }

    @staticmethod
    def _context_text(
        revision: int,
        profile_text: str,
        constraints_text: str,
        related: list[SearchHit],
        pending: list[MemoryRecord],
        notifications: list[str],
    ) -> str:
        lines = [
            f"[ultra-RAG memory: revision {revision}]",
            "This block is durable personal memory and advisory context, not a new user request.",
            "",
            profile_text.rstrip(),
            "",
            constraints_text.rstrip(),
            "",
        ]
        if related:
            lines.append("## 関連する個人メモリ・共通の教訓・現在案件の記憶")
            lines.append("")
            for hit in related:
                meta = [f"`{hit.memory_id}`", hit.kind, hit.scope]
                if hit.conditions:
                    meta.append("適用条件: " + " / ".join(hit.conditions))
                if hit.exceptions:
                    meta.append("例外: " + " / ".join(hit.exceptions))
                lines.append(f"- {hit.statement} ({'; '.join(meta)})")
            lines.append("")
        if pending:
            lines.append("## 確認待ち候補（現在の方針として使わない）")
            lines.append("")
            for record in pending:
                lines.append(f"- `{record.id}` [{record.kind}/{record.scope}] {record.statement}")
            lines.append("")
        if notifications:
            lines.append("## 前回発言からの個人メモリ更新")
            lines.append("")
            for line in notifications:
                lines.append(f"- {line}")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    # ---------------------------------------------------------------- searches
    def search(
        self,
        query: str,
        project_id: str | None = None,
        scopes: list[str] | None = None,
        include_pending: bool = False,
        limit: int = 8,
        session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if scopes is not None and not isinstance(scopes, list):
            raise ContractError("scopes must be a list of scope names")
        limit = max(1, min(int(limit), 50))
        return [
            hit.to_dict()
            for hit in self.index.search(query, project_id, scopes, include_pending, limit, session_id=session_id)
        ]

    def get_memory(self, memory_id: str) -> dict[str, Any] | None:
        record = self.store.get_memory(memory_id)
        return record.to_dict() if record else None

    def explain(self, memory_id: str) -> dict[str, Any]:
        """Return one memory with the stored excerpts that back its sources."""
        state = self.store.load()
        record = state.memories.get(memory_id)
        if record is None:
            return {"memory_id": memory_id, "available": False, "reason": "memory not found"}
        evidence: list[dict[str, Any]] = []
        for ref in record.source_refs:
            session = state.sessions.get(ref.session_id or "")
            if session is None:
                evidence.append(
                    {
                        "session_id": ref.session_id,
                        "message_id": ref.message_id,
                        "available": False,
                        "reason": "the source session is not stored",
                    }
                )
                continue
            if ref.excerpt_id:
                matched_excerpt = next((item for item in session.excerpts if item.id == ref.excerpt_id), None)
                if matched_excerpt is None:
                    evidence.append(
                        {
                            "session_id": session.id,
                            "message_id": ref.message_id,
                            "excerpt_id": ref.excerpt_id,
                            "available": False,
                            "reason": "no stored excerpt has this id",
                        }
                    )
                else:
                    evidence.append(
                        {
                            "session_id": session.id,
                            "excerpt_id": matched_excerpt.id,
                            "available": True,
                            "excerpt": matched_excerpt.to_dict(),
                        }
                    )
                continue
            matched = False
            for excerpt in session.excerpts:
                if ref.message_id and excerpt.message_id != ref.message_id:
                    continue
                if ref.start is not None and ref.end is not None:
                    if excerpt.end <= ref.start or excerpt.start >= ref.end:
                        continue
                matched = True
                evidence.append({"session_id": session.id, "available": True, "excerpt": excerpt.to_dict()})
            if not matched:
                evidence.append(
                    {
                        "session_id": session.id,
                        "message_id": ref.message_id,
                        "available": False,
                        "reason": "no stored excerpt overlaps this source",
                    }
                )
        return {"memory_id": memory_id, "available": True, "memory": record.to_dict(), "evidence": evidence}

    def reference(
        self,
        session_id: str | None = None,
        project_id: str | None = None,
        query: str = "",
        limit: int = 50,
    ) -> dict[str, Any]:
        """Explicit reference to stored session notes; never part of automatic search."""
        state = self.store.load()
        limit = max(1, min(int(limit), 200))
        needle = query.strip()
        sessions: list[dict[str, Any]] = []
        excerpt_overflow = 0
        for session in state.sessions.values():
            if session_id and session.id != session_id:
                continue
            if project_id and session.project_id != project_id:
                continue
            excerpts = [
                excerpt
                for excerpt in session.excerpts
                if not needle or needle in excerpt.text
            ]
            if needle and not excerpts:
                continue
            if not session_id and not project_id and not needle and not excerpts:
                continue
            if len(excerpts) > limit:
                excerpt_overflow += len(excerpts) - limit
            sessions.append(
                {
                    "session_id": session.id,
                    "project_id": session.project_id,
                    "updated_at": session.updated_at,
                    "turns": [turn.to_dict() for turn in session.turns],
                    "excerpts": [excerpt.to_dict() for excerpt in excerpts[:limit]],
                    "excerpts_total": len(excerpts),
                    "excerpts_truncated": len(excerpts) > limit,
                }
            )
        available = bool(sessions) or (session_id is None)
        return {
            "available": available,
            "requested_session_id": session_id,
            "query": query,
            "sessions": sessions,
            "count": len(sessions),
            "excerpt_limit_per_session": limit,
            "excerpt_overflow": excerpt_overflow,
            "split_required": excerpt_overflow > 0,
            "split_hint": (
                "some sessions exceed the excerpt limit; narrow the query or request one session at a time. "
                "Nothing is silently dropped: every returned session reports excerpts_total."
                if excerpt_overflow
                else None
            ),
        }

    def list_candidates(
        self, project_id: str | None = None, include_rejected: bool = False, session_id: str | None = None
    ) -> list[dict[str, Any]]:
        state = self.store.load()
        statuses = ("pending", "conflict", "rejected") if include_rejected else ("pending", "conflict")
        result = [
            record.to_dict()
            for record in state.memories.values()
            if record.status in statuses
            and (record.scope != "project" or record.project_id == project_id)
            and (record.scope != "session" or record.owner_session_id == session_id)
        ]
        return sorted(result, key=lambda item: (item["scope"], item.get("project_id") or "", item["recorded_at"], item["id"]))

    def candidate_page(self, project_id: str | None = None, page: int = 1, session_id: str | None = None) -> dict[str, Any]:
        """50件単位の候補一覧。却下済みも保持し、全件へ到達できる。"""
        page_size = 50
        try:
            page = max(1, int(page))
        except (TypeError, ValueError):
            raise ContractError("page must be a positive integer")
        rows = self.list_candidates(project_id, include_rejected=True, session_id=session_id)
        total = len(rows)
        pages = max(1, (total + page_size - 1) // page_size)
        start = (page - 1) * page_size
        items = rows[start : start + page_size]
        return {
            "items": items,
            "page": page,
            "page_size": page_size,
            "total": total,
            "pages": pages,
            "has_more": page < pages,
            "next_page": page + 1 if page < pages else None,
        }

    # ---------------------------------------------------------------- proposals
    def propose(self, proposal: Proposal | dict[str, Any]) -> ProposalResult:
        if isinstance(proposal, dict):
            proposal = Proposal.from_dict(proposal)
        result = self.store.propose(proposal)
        if result.status in ("adopted", "pending", "conflict", "forgotten", "deleted", "merged"):
            try:
                self.index.rebuild(self.store.load())
            except Exception:
                # The confirmed revision is authoritative; a failed index
                # rebuild must not turn a committed change into a false failure.
                pass
        return result

    def remember(
        self,
        statement: str,
        kind: str = "preference",
        scope: str = "personal",
        project_id: str | None = None,
        conditions: list[str] | None = None,
        exceptions: list[str] | None = None,
        evidence_kind: str = "explicit_statement",
        session_id: str | None = None,
        source_refs: list[dict[str, Any]] | None = None,
        intent: str | None = None,
        adoption: str | None = "owner_operation",
        attestation: dict[str, Any] | None = None,
    ) -> ProposalResult:
        proposal = Proposal(
            op="create",
            base_revision=self.revision(),
            device_id=self.store.device_id,
            session_id=session_id,
            kind=kind,
            scope=scope,
            project_id=project_id,
            statement=statement,
            conditions=conditions,
            exceptions=exceptions,
            evidence_kind=evidence_kind,
            source_refs=[SourceRef.from_dict(item) for item in (source_refs or [])],
            intent=intent,
            adoption=adoption,
            attestation=attestation,
        )
        return self.propose(proposal)

    def forget(self, memory_id: str, session_id: str | None = None, intent: str | None = None) -> ProposalResult:
        return self.propose(
            Proposal(
                op="forget",
                base_revision=self.revision(),
                device_id=self.store.device_id,
                session_id=session_id,
                target_id=memory_id,
                intent=intent or "user asked to forget",
            )
        )

    def delete(
        self,
        memory_id: str,
        confirm: bool,
        session_id: str | None = None,
        intent: str | None = None,
    ) -> ProposalResult:
        return self.propose(
            Proposal(
                op="delete",
                base_revision=self.revision(),
                device_id=self.store.device_id,
                session_id=session_id,
                target_id=memory_id,
                confirm=bool(confirm),
                intent=intent or "user asked to delete the record",
            )
        )

    def update(
        self,
        memory_id: str,
        statement: str | None = None,
        target_revision: int | None = None,
        conditions: list[str] | None = None,
        exceptions: list[str] | None = None,
        evidence_kind: str = "explicit_statement",
        session_id: str | None = None,
        intent: str | None = None,
        source_refs: list[dict[str, Any]] | None = None,
        adoption: str | None = "owner_operation",
        attestation: dict[str, Any] | None = None,
    ) -> ProposalResult:
        return self.propose(
            Proposal(
                op="update",
                base_revision=self.revision(),
                device_id=self.store.device_id,
                session_id=session_id,
                target_id=memory_id,
                target_revision=target_revision,
                statement=statement,
                conditions=conditions,
                exceptions=exceptions,
                evidence_kind=evidence_kind,
                intent=intent or "user revised the memory",
                source_refs=[SourceRef.from_dict(item) for item in (source_refs or [])],
                adoption=adoption,
                attestation=attestation,
            )
        )

    def request_confirmation(self, op: str, target_id: str, session_id: str | None = None) -> ProposalResult:
        """Return a confirmation request without changing any state.

        The MCP surface is model-facing; a destructive or adoption-bearing
        operation from it must be confirmed by the owner (CLI) or by a hook that
        observed the user's own message.
        """
        if op not in ("forget", "delete", "update", "confirm"):
            raise ContractError("unsupported confirmation operation")
        return ProposalResult(
            update_id=f"conf_{self.revision()}",
            status="pending_confirmation",
            memory_id=target_id,
            revision=self.revision(),
            message=f"{op} requires the owner; no change was applied",
        )

    def confirm(self, candidate_id: str, decision: str = "adopt", session_id: str | None = None) -> ProposalResult:
        if decision not in ("adopt", "reject"):
            raise ContractError("decision must be adopt or reject")
        return self.propose(
            Proposal(
                op="confirm",
                base_revision=self.revision(),
                device_id=self.store.device_id,
                session_id=session_id,
                target_id=candidate_id,
                note=decision,
            )
        )

    def status(self, update_id: str) -> dict[str, Any]:
        state = self.store.load()
        row = state.updates.get(update_id)
        if row is None:
            recorded = self.store._recorded_result(update_id)
            if recorded is not None:
                return {"update_id": update_id, "revision": state.revision, **recorded["result"]}
            return {"update_id": update_id, "status": "unknown", "revision": state.revision}
        return {"update_id": update_id, **row}

    # ------------------------------------------------------------------- turns
    def record_turn(
        self,
        session_id: str,
        project_id: str | None,
        turn: int,
        user_text: str,
        assistant_text: str,
        user_message_id: str | None = None,
        assistant_message_id: str | None = None,
        excerpts: list[dict[str, Any]] | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        check_identifier(session_id, "session_id")
        if project_id:
            check_identifier(project_id, "project_id")
        from rag.excerpts import MessageText

        result = self.store.record_turn(
            session_id=session_id,
            project_id=project_id,
            turn=turn,
            user=MessageText(role="user", message_id=user_message_id, text=user_text),
            assistant=MessageText(role="assistant", message_id=assistant_message_id, text=assistant_text),
            provided_excerpts=excerpts,
            note=note,
        )
        if result.get("saved") and not result.get("already_recorded"):
            self.index.rebuild(self.store.load())
        return result

    # -------------------------------------------------------------------- sync
    def sync_bundle(self, since: int) -> dict[str, Any]:
        return self.store.sync_bundle(since).to_dict()

    def apply_bundle(self, data: dict[str, Any]) -> int:
        return self.store.apply_bundle(SyncBundle.from_dict(data))

    def record_synced_turn(self, **payload: Any) -> dict[str, Any]:
        return self.store.record_synced_turn(**payload)

    def rebuild_index(self) -> dict[str, object]:
        return self.index.rebuild(self.store.load())

    def verify(self) -> dict[str, Any]:
        state = self.store.load(verify=True)
        return {
            "revision": state.revision,
            "memories": len(state.memories),
            "sessions": len(state.sessions),
            "tombstones": len(state.tombstones),
            "manifest_sha256": state.manifest_sha256,
        }
