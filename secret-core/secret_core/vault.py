"""Encrypted key vault with metadata-only listing.

The vault file contains plaintext metadata (id/name/service/purpose/env name)
and an AES-GCM ciphertext of the value. Values are decrypted only inside the
execution manager for one selected id list; there is no API that returns a value
to a model.
"""

from __future__ import annotations

import hashlib
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from contracts.config import Layout, load_config, save_config
from contracts.models import KeyMeta
from contracts.util import append_jsonl, ensure_dir, new_id, read_json, utc_now, write_json, write_lock
from secret_core.crypto import decrypt_value, encrypt_value, protect_master_key, unseal_master_key
from secret_core.revocation import Revocations

ENV_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")


class VaultError(RuntimeError):
    pass


class Vault:
    def __init__(self, layout: Layout, allow_create: bool = False):
        self.layout = layout
        self.root = layout.secret / "vault"
        self.entries = self.root / "entries"
        self.sealed_key = self.root / "master.key.sealed"
        self.lock_file = layout.secret / "control" / "vault.lock"
        self.revocations = Revocations(layout)
        if allow_create:
            self.initialise()

    @property
    def initialized(self) -> bool:
        return self.sealed_key.exists()

    def initialise(self) -> str:
        with write_lock(self.lock_file, 20):
            if self.sealed_key.exists():
                return "exists"
            ensure_dir(self.root)
            ensure_dir(self.entries)
            protector = protect_master_key(self.sealed_key)
            write_json(
                self.root / "vault.json",
                {"version": 1, "protector": protector, "created_at": utc_now()},
            )
            return protector

    def _key(self) -> bytes:
        if not self.sealed_key.exists():
            raise VaultError("the vault is not initialised")
        return unseal_master_key(self.sealed_key)

    def _entry_path(self, key_id: str) -> Path:
        if not re.match(r"^key_[0-9a-f]{32}$", key_id):
            raise VaultError("invalid key id")
        return self.entries / f"{key_id}.json"

    def _read_entry(self, key_id: str) -> dict:
        path = self._entry_path(key_id)
        if not path.exists():
            raise VaultError("key is not registered")
        return read_json(path, None)

    def _active_entries(self) -> list[dict]:
        if not self.entries.is_dir():
            return []
        rows = []
        for path in sorted(self.entries.glob("*.json")):
            try:
                row = read_json(path, None)
            except Exception:
                continue
            if isinstance(row, dict) and isinstance(row.get("meta"), dict):
                rows.append(row)
        return rows

    def list_keys(self) -> list[KeyMeta]:
        metas = []
        for row in self._active_entries():
            meta = dict(row["meta"])
            if meta.get("status", "active") != "active":
                continue
            metas.append(KeyMeta(**{key: meta.get(key) for key in KeyMeta.__dataclass_fields__}))
        return sorted(metas, key=lambda item: (item.service, item.name, item.id))

    def get_meta(self, key_id: str) -> KeyMeta:
        row = self._read_entry(key_id)
        meta = dict(row["meta"])
        return KeyMeta(**{key: meta.get(key) for key in KeyMeta.__dataclass_fields__})

    def register(
        self,
        name: str,
        service: str,
        purpose: str,
        value: str,
        env_name: str | None = None,
    ) -> KeyMeta:
        return self.register_many(
            [{"name": name, "service": service, "purpose": purpose, "value": value, "env_name": env_name}]
        )[0]

    def register_many(self, items: list[dict]) -> list[KeyMeta]:
        """Validate every item before writing any entry, so an import is all-or-nothing."""
        if not self.initialized:
            self.initialise()
        if not items:
            raise VaultError("at least one key is required")
        with write_lock(self.lock_file, 20):
            used = {row["meta"].get("env_name") for row in self._active_entries()}
            master = self._key()
            pending: list[tuple[KeyMeta, dict]] = []
            for item in items:
                name = str(item.get("name", "")).strip()
                service = str(item.get("service", "")).strip()
                purpose = str(item.get("purpose", "")).strip()
                value = item.get("value", "")
                if not name or not service:
                    raise VaultError("name and service are required")
                if not value:
                    raise VaultError("the value must not be empty")
                env_name = item.get("env_name") or ("ULTRA_RAG_KEY_" + new_id("key").split("_")[1][:12].upper())
                if not ENV_RE.match(env_name):
                    raise VaultError("env_name must match [A-Z_][A-Z0-9_]*")
                if env_name in used:
                    raise VaultError(f"env_name is already used by another key: {env_name}")
                used.add(env_name)
                key_id = new_id("key")
                meta = KeyMeta(
                    id=key_id,
                    name=name,
                    service=service,
                    purpose=purpose,
                    env_name=env_name,
                    status="active",
                    created_at=utc_now(),
                    updated_at=utc_now(),
                )
                pending.append((meta, {"meta": meta.to_dict(), "sealed": encrypt_value(master, key_id, value)}))
            for meta, row in pending:
                write_json(self._entry_path(meta.id), row)
            return [meta for meta, _ in pending]

    def rotate(self, key_id: str, value: str) -> KeyMeta:
        if not value:
            raise VaultError("the value must not be empty")
        with write_lock(self.lock_file, 20):
            row = self._read_entry(key_id)
            meta = dict(row["meta"])
            meta["updated_at"] = utc_now()
            sealed = encrypt_value(self._key(), key_id, value)
            write_json(self._entry_path(key_id), {"meta": meta, "sealed": sealed})
            self.revocations.rotate_key(key_id)
            return KeyMeta(**{key: meta.get(key) for key in KeyMeta.__dataclass_fields__})

    def rename(
        self,
        key_id: str,
        name: str,
        service: str | None = None,
        purpose: str | None = None,
    ) -> KeyMeta:
        """Change metadata only; the sealed value is preserved byte-for-byte."""
        if not name.strip():
            raise VaultError("name is required")
        if service is not None and not service.strip():
            raise VaultError("service must not be empty")
        with write_lock(self.lock_file, 20):
            row = self._read_entry(key_id)
            meta = dict(row["meta"])
            meta["name"] = name.strip()
            if service is not None:
                meta["service"] = service.strip()
            if purpose is not None:
                meta["purpose"] = purpose.strip()
            meta["updated_at"] = utc_now()
            write_json(self._entry_path(key_id), {"meta": meta, "sealed": row["sealed"]})
            return KeyMeta(**{key: meta.get(key) for key in KeyMeta.__dataclass_fields__})

    def delete(self, key_id: str) -> KeyMeta:
        with write_lock(self.lock_file, 20):
            row = self._read_entry(key_id)
            meta = dict(row["meta"])
            meta["status"] = "deleted"
            meta["updated_at"] = utc_now()
            self.revocations.revoke_key(key_id, "deleted")
            path = self._entry_path(key_id)
            try:
                path.unlink()
            except OSError as error:
                raise VaultError(f"cannot delete the vault entry: {error}") from error
            return KeyMeta(**{key: meta.get(key) for key in KeyMeta.__dataclass_fields__})

    def resolve(self, key_ids: list[str]) -> dict[str, str]:
        """Decrypt selected active values for one execution. Internal only."""
        values: dict[str, str] = {}
        for key_id in key_ids:
            if self.revocations.key_revoked(key_id):
                raise VaultError(f"key {key_id} is revoked")
            row = self._read_entry(key_id)
            meta = row["meta"]
            if meta.get("status", "active") != "active":
                raise VaultError(f"key {key_id} is not active")
            env_name = meta["env_name"]
            if env_name in values:
                raise VaultError(f"duplicate env name in the selected keys: {env_name}")
            values[env_name] = decrypt_value(self._key(), key_id, row["sealed"])
        return values

    def selected_meta(self, key_ids: list[str]) -> list[KeyMeta]:
        return [self.get_meta(key_id) for key_id in key_ids]

    def recreate(self) -> dict:
        """Archive the current vault and identities, then create an empty vault.

        Old execution ids, device tokens, and key ids are invalidated. Nothing is
        re-executed automatically. The archive is retained without deletion.
        """
        with write_lock(self.lock_file, 60):
            if not self.sealed_key.exists():
                raise VaultError("the vault is not initialised")
            config = load_config(self.layout, create=False)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            archive = self.layout.secret / "archive" / f"vault-recreated-{stamp}"
            suffix = 0
            while archive.exists():
                suffix += 1
                archive = self.layout.secret / "archive" / f"vault-recreated-{stamp}-{suffix}"
            ensure_dir(archive)

            run_dir = self.layout.run
            exec_ids: list[str] = []
            if run_dir.is_dir():
                exec_ids = sorted(path.name for path in run_dir.iterdir() if path.is_dir() and path.name.startswith("exec_"))
            self._stop_active(exec_ids)

            device_data = read_json(self.layout.secret / "devices.json", {}) or {}
            device_ids = sorted((device_data.get("devices") or {}).keys())

            # Copy the sealed vault first; only then move the original aside.
            if (self.layout.secret / "vault").exists():
                shutil.copytree(self.layout.secret / "vault", archive / "vault-copy")
            if (self.layout.secret / "audit").exists():
                shutil.copytree(self.layout.secret / "audit", archive / "audit-old")
            for name in ("devices.json", "invalidated.json"):
                source = self.layout.secret / name
                if source.exists():
                    shutil.copy2(source, archive / name)
            if self.layout.config_file.exists():
                shutil.copy2(self.layout.config_file, archive / "config.json")
            if self.layout.password.exists():
                shutil.copytree(self.layout.password, archive / "password-old")

            if (self.layout.secret / "vault").exists():
                shutil.move(str(self.layout.secret / "vault"), str(archive / "vault-old"))
            if (self.layout.secret / "audit").exists():
                shutil.move(str(self.layout.secret / "audit"), str(archive / "audit"))
            if (self.layout.secret / "devices").exists():
                shutil.move(str(self.layout.secret / "devices"), str(archive / "devices"))
            if (self.layout.secret / "devices.json").exists():
                shutil.move(str(self.layout.secret / "devices.json"), str(archive / "devices.json.old"))
            if self.layout.password.exists():
                shutil.move(str(self.layout.password), str(archive / "password"))
            if run_dir.exists():
                shutil.move(str(run_dir), str(archive / "run"))
            ensure_dir(run_dir)

            previous_invalid = read_json(self.layout.secret / "invalidated.json", {}) or {}
            invalidated = {
                "version": 1,
                "reason": "vault_recreated",
                "created_at": utc_now(),
                "archive": str(archive),
                "exec_ids": sorted(set((previous_invalid.get("exec_ids") or []) + exec_ids)),
                "device_ids": sorted(set((previous_invalid.get("device_ids") or []) + device_ids)),
            }
            write_json(self.layout.secret / "invalidated.json", invalidated)

            new_device_id = new_id("dev")
            config["device_id"] = new_device_id
            save_config(self.layout, config)

            ensure_dir(self.root)
            ensure_dir(self.entries)
            protector = protect_master_key(self.sealed_key)
            write_json(
                self.root / "vault.json",
                {"version": 1, "protector": protector, "created_at": utc_now(), "recreated": True},
            )
            audit_dir = self.layout.secret / "audit"
            ensure_dir(audit_dir)
            append_jsonl(
                audit_dir / "audit.jsonl",
                {
                    "phase": "vault_recreate",
                    "at": utc_now(),
                    "archive": str(archive),
                    "old_exec_ids": len(exec_ids),
                    "old_device_ids": len(device_ids),
                    "new_device_id": new_device_id,
                },
            )
            manifest = {
                "archive": str(archive),
                "created_at": utc_now(),
                "old_exec_ids": exec_ids,
                "old_device_ids": device_ids,
                "files": {},
            }
            for path in sorted(archive.rglob("*")):
                if path.is_file():
                    digest = hashlib.sha256(path.read_bytes()).hexdigest()
                    manifest["files"][path.relative_to(archive).as_posix()] = digest
            write_json(archive / "manifest.json", manifest)
            return {
                "archive": str(archive),
                "invalidated_exec_ids": exec_ids,
                "invalidated_device_ids": device_ids,
                "new_device_id": new_device_id,
                "keys": 0,
            }

    def _stop_active(self, exec_ids: list[str]) -> None:
        if not exec_ids:
            return
        from secret_core.runner import ExecManager

        manager = ExecManager(self.layout)
        for exec_id in exec_ids:
            try:
                state = manager.status(exec_id)
            except Exception:
                continue
            if state.status in ("running", "starting"):
                manager.stop(exec_id, reason="vault_recreate")
        deadline = datetime.now(timezone.utc).timestamp() + 10
        while datetime.now(timezone.utc).timestamp() < deadline:
            remaining = []
            for exec_id in exec_ids:
                try:
                    if manager.status(exec_id).status in ("running", "starting"):
                        remaining.append(exec_id)
                except Exception:
                    continue
            if not remaining:
                return
            import time

            time.sleep(0.2)
        raise VaultError("active executions did not stop; vault recreation was not performed")

    def close(self) -> None:
        return None


def backup_vault(layout: Layout, destination: Path) -> Path:
    """Owner-only backup of the sealed vault (still host-bound, no recovery key)."""
    source = layout.secret / "vault"
    if not source.exists():
        raise VaultError("the vault does not exist")
    ensure_dir(destination.parent)
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination)
    return destination
