"""Minimal OpenAI-compatible endpoint used only by the dsh integration harness.

This is a test double for the model provider, not a model. It emits one
`memory_remember` tool call and then a final answer, so the harness can exercise
dsh boot, plugin context injection, MCP tool execution, and turn-end persistence.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class FakeModelServer:
    def __init__(self, host: str = "127.0.0.1", port: int = 0, tool_name: str = "mcp__ultra-rag__memory_remember"):
        self.tool_name = tool_name
        self.requests: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: Any) -> None:
                return

            def do_GET(self) -> None:  # noqa: N802
                if self.path.rstrip("/").endswith("/models"):
                    body = json.dumps({"object": "list", "data": [{"id": "fake-model", "object": "model"}]}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_error(404)

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                with server.lock:
                    server.requests.append(payload)
                messages = payload.get("messages", [])
                has_tool_result = any(message.get("role") == "tool" for message in messages)
                tools = payload.get("tools") or []
                if has_tool_result or not tools:
                    self._respond_text(payload, "MEMORY-REGISTERED")
                else:
                    arguments = json.dumps(
                        {
                            "statement": "以後、回答は日本語で簡潔に書くこと",
                            "kind": "preference",
                            "scope": "personal",
                        },
                        ensure_ascii=False,
                    )
                    self._respond_tool_call(payload, arguments)

            def _respond_text(self, payload: dict[str, Any], text: str) -> None:
                if not payload.get("stream"):
                    body = json.dumps(
                        {
                            "id": "chatcmpl-fake",
                            "object": "chat.completion",
                            "created": int(time.time()),
                            "model": "fake-model",
                            "choices": [
                                {
                                    "index": 0,
                                    "message": {"role": "assistant", "content": text},
                                    "finish_reason": "stop",
                                }
                            ],
                            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                        }
                    ).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                chunks = [
                    {"choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]},
                    {"choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]},
                    {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
                ]
                self._sse(chunks)

            def _respond_tool_call(self, payload: dict[str, Any], arguments: str) -> None:
                tool_call = {
                    "id": "call_fake_1",
                    "type": "function",
                    "function": {"name": server.tool_name, "arguments": arguments},
                }
                if not payload.get("stream"):
                    body = json.dumps(
                        {
                            "id": "chatcmpl-fake",
                            "object": "chat.completion",
                            "created": int(time.time()),
                            "model": "fake-model",
                            "choices": [
                                {
                                    "index": 0,
                                    "message": {"role": "assistant", "content": None, "tool_calls": [tool_call]},
                                    "finish_reason": "tool_calls",
                                }
                            ],
                            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                        }
                    ).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                chunks = [
                    {
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "role": "assistant",
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "id": tool_call["id"],
                                            "type": "function",
                                            "function": {"name": server.tool_name, "arguments": arguments},
                                        }
                                    ],
                                },
                                "finish_reason": None,
                            }
                        ]
                    },
                    {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
                ]
                self._sse(chunks)

            def _sse(self, chunks: list[dict[str, Any]]) -> None:
                body = bytearray()
                for chunk in chunks:
                    record = {
                        "id": "chatcmpl-fake",
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": "fake-model",
                        **chunk,
                    }
                    body.extend(f"data: {json.dumps(record, ensure_ascii=False)}\n\n".encode("utf-8"))
                body.extend(b"data: [DONE]\n\n")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(bytes(body))
                self.wfile.flush()

        self.httpd = ThreadingHTTPServer((host, port), Handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return int(self.httpd.server_address[1])

    def start(self) -> "FakeModelServer":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
