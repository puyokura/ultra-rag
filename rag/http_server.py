"""Loopback HTTP API for the RAG memory store.

Authentication is a bearer token stored in ``<home>/control/api-token`` with
owner-only permissions. The server is bound to loopback by default; deployment
beyond loopback requires a transport decision that this project does not make
silently. Request bodies are never logged.
"""

from __future__ import annotations

import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from contracts.config import Layout, api_token, load_config
from contracts.models import ContractError, Proposal
from contracts.util import sha256_hex
from rag.service import RagService
from rag.store import StoreError

MAX_BODY = 8 * 1024 * 1024


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class RagHttpServer:
    def __init__(self, layout: Layout, host: str = "127.0.0.1", port: int = 8765):
        self.layout = layout
        self.token = api_token(layout, create=False)
        self.service = RagService(layout)
        self.config = load_config(layout, create=False)
        self.httpd = ThreadingHTTPServer((host, port), self._handler())
        self.httpd.daemon_threads = True
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return int(self.httpd.server_address[1])

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        server = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "ultra-rag/0.1"
            sys_version = ""

            def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
                # No request body, query, or authorization value is logged.
                return

            def _authorized(self) -> bool:
                header = self.headers.get("Authorization", "")
                if not header.startswith("Bearer "):
                    return False
                return hmac.compare_digest(header[7:].strip(), server.token)

            def _send(self, status: int, payload: dict[str, Any]) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)

            def _read_json(self) -> dict[str, Any]:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    raise ApiError(400, "invalid Content-Length")
                if length <= 0:
                    return {}
                if length > MAX_BODY:
                    raise ApiError(413, "request body is too large")
                raw = self.rfile.read(length)
                try:
                    value = json.loads(raw.decode("utf-8"))
                except Exception:
                    raise ApiError(400, "request body must be JSON")
                if not isinstance(value, dict):
                    raise ApiError(400, "request body must be a JSON object")
                return value

            def _dispatch(self, method: str) -> None:
                parsed = urlparse(self.path)
                try:
                    if parsed.path == "/healthz" and method == "GET":
                        self._send(200, {"ok": True, "revision": server.service.revision()})
                        return
                    if not self._authorized():
                        raise ApiError(401, "missing or invalid bearer token")
                    routes = {
                        ("GET", "/v1/revision"): lambda: {"revision": server.service.revision()},
                        ("POST", "/v1/snapshot"): lambda: server.service.snapshot(**server._snapshot_args(self._read_json())),
                        ("POST", "/v1/search"): lambda: server.service.search(**server._search_args(self._read_json())),
                        ("POST", "/v1/propose"): lambda: server.service.propose(Proposal.from_dict(self._read_json())).to_dict(),
                        ("POST", "/v1/remember"): lambda: server.service.remember(**server._remember_args(self._read_json())).to_dict(),
                        ("POST", "/v1/update"): lambda: server.service.update(**self._read_json()).to_dict(),
                        ("POST", "/v1/forget"): lambda: server.service.forget(**self._read_json()).to_dict(),
                        ("POST", "/v1/delete"): lambda: server.service.delete(**self._read_json()).to_dict(),
                        ("POST", "/v1/confirm"): lambda: server.service.confirm(**self._read_json()).to_dict(),
                        ("POST", "/v1/turn"): lambda: _turn(server, self._read_json()),
                        ("POST", "/v1/explain"): lambda: server.service.explain(**server._explain_args(self._read_json())),
                        ("POST", "/v1/reference"): lambda: server.service.reference(**server._reference_args(self._read_json())),
                        ("GET", "/v1/candidates"): lambda: server.service.list_candidates(_query_one(parsed.query, "project_id")),
                        ("GET", "/v1/sync/changes"): lambda: server.service.sync_bundle(int(_query_one(parsed.query, "since") or "0")),
                        ("POST", "/v1/sync/apply"): lambda: {"revision": server.service.apply_bundle(self._read_json())},
                        ("POST", "/v1/index/rebuild"): lambda: server.service.rebuild_index(),
                        ("GET", "/v1/verify"): lambda: server.service.verify(),
                        ("GET", "/v1/status"): lambda: server.service.status(_query_one(parsed.query, "update_id") or ""),
                        ("GET", "/v1/hostkeys"): lambda: _hostkeys(server.layout),
                    }
                    key = (method, parsed.path)
                    if key in routes:
                        self._send(200, routes[key]())
                        return
                    if method == "GET" and parsed.path.startswith("/v1/memory/"):
                        memory_id = parsed.path[len("/v1/memory/") :]
                        record = server.service.get_memory(memory_id)
                        if record is None:
                            raise ApiError(404, "memory not found")
                        self._send(200, record)
                        return
                    raise ApiError(404, "unknown endpoint")
                except ApiError as error:
                    self._send(error.status, {"error": error.message})
                except ContractError as error:
                    self._send(400, {"error": str(error)})
                except StoreError as error:
                    self._send(409, {"error": str(error)})
                except TypeError as error:
                    self._send(400, {"error": f"invalid arguments: {error}"})
                except (KeyError, ValueError) as error:
                    self._send(400, {"error": f"invalid payload: {type(error).__name__}"})
                except Exception as error:  # never echo raw exception content-bearing data
                    self._send(500, {"error": f"internal error ({sha256_hex(str(error))[:12]})"})

            def do_GET(self) -> None:  # noqa: N802
                self._dispatch("GET")

            def do_POST(self) -> None:  # noqa: N802
                self._dispatch("POST")

        return Handler

    # Request argument filtering keeps unknown keys from becoming TypeError
    # messages that echo user-controlled values.
    @staticmethod
    def _snapshot_args(body: dict[str, Any]) -> dict[str, Any]:
        return _pick(body, {"project_id", "query", "session_id", "related_limit"})

    @staticmethod
    def _search_args(body: dict[str, Any]) -> dict[str, Any]:
        return _pick(body, {"query", "project_id", "scopes", "include_pending", "limit"})

    @staticmethod
    def _explain_args(body: dict[str, Any]) -> dict[str, Any]:
        return _pick(body, {"memory_id"})

    @staticmethod
    def _reference_args(body: dict[str, Any]) -> dict[str, Any]:
        return _pick(body, {"session_id", "project_id", "query", "limit"})

    @staticmethod
    def _remember_args(body: dict[str, Any]) -> dict[str, Any]:
        return _pick(
            body,
            {
                "statement",
                "kind",
                "scope",
                "project_id",
                "conditions",
                "exceptions",
                "evidence_kind",
                "session_id",
                "source_refs",
                "intent",
            },
        )

    def serve_forever(self) -> None:
        self.httpd.serve_forever()

    def start_background(self) -> "RagHttpServer":
        thread = threading.Thread(target=self.httpd.serve_forever, name="ultra-rag-http", daemon=True)
        thread.start()
        self._thread = thread
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def _query_one(query: str, name: str) -> str | None:
    values = parse_qs(query, keep_blank_values=True).get(name)
    return values[0] if values else None


def _hostkeys(layout: Layout) -> dict[str, Any]:
    from password_input.service import PasswordBroker

    return PasswordBroker(layout).export_hostkeys()


def _pick(body: dict[str, Any], allowed: set[str]) -> dict[str, Any]:
    return {key: value for key, value in body.items() if key in allowed}


def _turn(server, payload: dict[str, Any]) -> dict[str, Any]:
    """Apply a queued turn from its validated excerpts, or as raw text."""
    if "user_text" in payload:
        return server.service.record_turn(**payload)
    return server.service.record_synced_turn(**payload)
