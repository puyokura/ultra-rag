"""PC/server synchronization for the RAG replica.

The home server is authoritative. A client home pulls confirmed revision
deltas, queues proposals/turns while offline, and pushes them with their
original ``base_revision`` so the server can keep conflicting changes. Deletion
control always arrives from the server before queued changes are pushed.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from contracts.config import Layout
from contracts.util import atomic_write_text, read_json, read_jsonl, utc_now, write_json
from rag.service import RagService

TRANSIENT_STATUSES = ("conflict", "unsynced")
DROP_STATUSES = ("adopted", "pending", "forgotten", "deleted", "merged", "duplicate", "rejected")


class SyncError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class RemoteClient:
    def __init__(self, base_url: str, token: str, timeout: float = 15.0):
        self.base_url = base_url.rstrip("/")
        if not (self.base_url.startswith("http://") or self.base_url.startswith("https://")):
            raise SyncError("remote must be an http:// or https:// URL")
        self.token = token
        self.timeout = timeout

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        data = None
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = response.read().decode("utf-8")
                return json.loads(payload) if payload else {}
        except urllib.error.HTTPError as error:
            try:
                payload = json.loads(error.read().decode("utf-8"))
                message = payload.get("error", f"HTTP {error.code}")
            except Exception:
                message = f"HTTP {error.code}"
            raise SyncError(message, error.code)
        except urllib.error.URLError as error:
            raise SyncError(f"cannot reach the home server: {error.reason}")
        except TimeoutError:
            raise SyncError("the home server request timed out")

    def get(self, path: str) -> dict[str, Any]:
        return self._request("GET", path)

    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", path, body)


def token_from_file(path: Path) -> str:
    if not path.exists():
        raise SyncError(f"sync token file does not exist: {path}")
    return path.read_text(encoding="utf-8").strip()


def sync_pull(layout: Layout, remote: str, token: str) -> dict[str, Any]:
    service = RagService(layout)
    client = RemoteClient(remote, token)
    since = service.revision() if service.store.exists() else 0
    try:
        bundle = client.get(f"/v1/sync/changes?since={since}")
        applied = service.apply_bundle(bundle)
    except SyncError as error:
        _record_state(layout, {"offline": True, "remote": remote, "error": str(error), "at": utc_now()})
        return {"ok": False, "offline": True, "error": str(error), "local_revision": since}
    _record_state(
        layout,
        {
            "offline": False,
            "remote": remote,
            "last_revision": applied,
            "last_sync_at": utc_now(),
            "files": len(bundle.get("files", [])),
            "deleted": len(bundle.get("deleted_paths", [])),
        },
    )
    return {
        "ok": True,
        "revision": applied,
        "files": len(bundle.get("files", [])),
        "deleted": len(bundle.get("deleted_paths", [])),
    }


def _record_state(layout: Layout, value: dict[str, Any]) -> None:
    path = layout.rag / "control" / "sync-state.json"
    current = read_json(path, {}) or {}
    current.update(value)
    write_json(path, current)


def sync_status(layout: Layout) -> dict[str, Any]:
    service = RagService(layout)
    queue = read_jsonl(service.store.queue_file)
    return {
        "role": service.store.role,
        "revision": service.revision() if service.store.exists() else 0,
        "unsynced": len(queue),
        "state": read_json(layout.rag / "control" / "sync-state.json", {}) or {},
    }


def _rewrite_queue(path: Path, entries: list[dict[str, Any]]) -> None:
    atomic_write_text(path, "".join(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n" for entry in entries))


def sync_push(layout: Layout, remote: str, token: str) -> dict[str, Any]:
    service = RagService(layout)
    if service.store.role == "server":
        raise SyncError("this home is the authoritative server; push is only valid for a client replica")
    client = RemoteClient(remote, token)
    queue = read_jsonl(service.store.queue_file)
    if not queue:
        return {"ok": True, "pushed": 0, "remaining": 0}
    remaining: list[dict[str, Any]] = []
    pushed = 0
    conflicts = 0
    rejected = 0
    for entry in queue:
        kind = entry.get("kind")
        payload = entry.get("payload", {})
        try:
            if kind == "proposal":
                result = client.post("/v1/propose", payload)
            elif kind == "turn":
                body = dict(payload)
                user = body.pop("user", None)
                assistant = body.pop("assistant", None)
                if isinstance(user, dict):
                    body.setdefault("user_text", user.get("text", ""))
                    body.setdefault("user_message_id", user.get("message_id"))
                if isinstance(assistant, dict):
                    body.setdefault("assistant_text", assistant.get("text", ""))
                    body.setdefault("assistant_message_id", assistant.get("message_id"))
                response = client.post("/v1/turn", body)
                result = {"status": "adopted" if response.get("saved", True) else "rejected"}
            else:
                rejected += 1
                continue
            status = result.get("status", "unknown")
            if status in TRANSIENT_STATUSES:
                entry = {**entry, "status": status, "last_result": result, "last_attempt": utc_now()}
                remaining.append(entry)
                conflicts += 1
            elif status == "rejected":
                rejected += 1
            elif status in DROP_STATUSES:
                pushed += 1
            else:
                entry = {**entry, "status": status, "last_result": result, "last_attempt": utc_now()}
                remaining.append(entry)
        except SyncError as error:
            entry = {**entry, "status": "retry", "error": str(error), "last_attempt": utc_now()}
            remaining.append(entry)
    _rewrite_queue(service.store.queue_file, remaining)
    log_path = layout.rag / "control" / "sync-log.jsonl"
    with open(log_path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(
            json.dumps(
                {
                    "at": utc_now(),
                    "pushed": pushed,
                    "remaining": len(remaining),
                    "conflicts": conflicts,
                    "rejected": rejected,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n"
        )
    return {
        "ok": len(remaining) == 0,
        "pushed": pushed,
        "remaining": len(remaining),
        "conflicts": conflicts,
        "rejected": rejected,
    }


def sync_once(layout: Layout, remote: str, token: str) -> dict[str, Any]:
    pull = sync_pull(layout, remote, token)
    if not pull.get("ok"):
        return {"ok": False, "pull": pull, "push": {"skipped": "offline"}}
    push = sync_push(layout, remote, token)
    final_pull = sync_pull(layout, remote, token)
    return {"ok": bool(push.get("ok")) and bool(final_pull.get("ok")), "pull": final_pull, "push": push}
