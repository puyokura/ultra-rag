"""Model-facing key operations: metadata listing, keyed execution, status.

There is no function that returns a key value. ``key_exec`` accepts a reference
id list and command; the execution manager supplies values to the child only.
"""

from __future__ import annotations

from typing import Any

from contracts.config import Layout, load_config
from contracts.models import ExecRequest
from secret_core.runner import ExecManager
from secret_core.vault import Vault


class KeyService:
    def __init__(self, layout: Layout):
        self.layout = layout
        self.config = load_config(layout, create=False)
        self.vault = Vault(layout)
        self.manager = ExecManager(layout, self.config)

    def key_list(self) -> list[dict[str, Any]]:
        """Metadata only: id, name, service, purpose, env name, status, timestamps."""
        return [meta.to_dict() for meta in self.vault.list_keys()]

    def key_exec(
        self,
        command: str,
        key_ids: list[str],
        host: str = "local",
        mode: str = "single",
        timeout_seconds: float | None = None,
        session_id: str | None = None,
        deliverables: list[str] | None = None,
        deliver_to: str | None = None,
        input_paths: list[str] | None = None,
    ) -> dict[str, Any]:
        if timeout_seconds is None:
            timeout_seconds = float(self.config.get("secret", {}).get("default_exec_seconds", 120))
        request = ExecRequest(
            command=command,
            key_ids=list(key_ids),
            device_id=self.config["device_id"],
            host=host,
            mode="resident" if mode == "resident" else "single",
            timeout_seconds=float(timeout_seconds),
            session_id=session_id,
            deliverables=list(deliverables or []),
            deliver_to=deliver_to,
            input_paths=list(input_paths or []),
        )
        return self.manager.execute(request).to_dict()

    def exec_status(self, exec_id: str, allow_other_device: bool = False) -> dict[str, Any]:
        return self.manager.status(
            exec_id,
            caller_device=self.config["device_id"],
            allow_other=allow_other_device,
        ).to_dict()

    def exec_artifacts(self, exec_id: str, allow_other_device: bool = False) -> dict[str, Any]:
        result = self.manager.status(exec_id, caller_device=self.config["device_id"], allow_other=allow_other_device)
        if result.status == "rejected":
            return result.to_dict()
        return self.manager.import_artifacts(exec_id)

    def stop(self, exec_id: str, reason: str = "owner_stop", allow_other_device: bool = False) -> dict[str, Any]:
        return self.manager.stop(
            exec_id,
            reason,
            caller_device=self.config["device_id"],
            allow_other=allow_other_device,
        ).to_dict()
