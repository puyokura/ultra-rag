"""Unified owner/operator CLI for the ultra-RAG monorepo.

Destructive vault operations are local CLI actions. No command accepts a key
value or password as an argv value; values come from a hidden terminal prompt or
an explicit stdin flag used by the owner.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path
from typing import Any

from contracts.config import ConfigError, Layout, api_token, ensure_initialised, load_config, save_config
from contracts.models import ContractError
from contracts.project import ProjectError, resolve_project_id, write_marker
from rag.importer import import_all
from rag.service import RagService
from rag.store import StoreError
from secret_core.audit import AuditLog
from secret_core.crypto import KeystoreError
from secret_core.devices import DeviceRegistry
from secret_core.lease import LeaseManager
from secret_core.dotenv import DotenvError, parse_dotenv
from secret_core.vault import Vault, VaultError
from key_use.service import KeyService
from password_input.service import PasswordBroker, PasswordError


def _layout(args: argparse.Namespace) -> Layout:
    return Layout.at(getattr(args, "home", None))


def _need_config(layout: Layout) -> dict[str, Any]:
    return load_config(layout, create=False)


def _print(value: Any, as_text: bool = False) -> None:
    if as_text and isinstance(value, str):
        sys.stdout.write(value)
        if not value.endswith("\n"):
            sys.stdout.write("\n")
        return
    sys.stdout.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _service(args: argparse.Namespace) -> RagService:
    layout = _layout(args)
    _need_config(layout)
    return RagService(layout)


def cmd_init(args: argparse.Namespace) -> int:
    layout = Layout.at(args.home)
    config = ensure_initialised(layout)
    if args.role:
        config["role"] = args.role
    if args.host:
        config["host"] = args.host
    if args.project_id:
        config["project_id"] = args.project_id
    save_config(layout, config)
    api_token(layout, create=True)
    vault = Vault(layout)
    protector = vault.initialise()
    devices = DeviceRegistry(layout)
    devices.ensure_local(config["device_id"], args.device_name or os.environ.get("COMPUTERNAME") or "local-device")
    lease = LeaseManager(layout, float(config.get("secret", {}).get("lease_seconds", 300)))
    lease.refresh("init")
    revision = None
    if config.get("role") == "server":
        revision = RagService(layout).initialise()
    _print(
        {
            "home": str(layout.home),
            "role": config["role"],
            "host": config["host"],
            "device_id": config["device_id"],
            "vault_protector": protector,
            "rag_revision": revision,
            "next": "run `ultra-rag doctor` or `ultra-rag rag serve`"
            if config.get("role") == "server"
            else "run `ultra-rag rag sync pull --remote URL --token-file FILE`",
        }
    )
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    layout = _layout(args)
    config = _need_config(layout)
    report: dict[str, Any] = {
        "home": str(layout.home),
        "role": config.get("role"),
        "host": config.get("host"),
        "device_id": config.get("device_id"),
        "api_token_present": (layout.control / "api-token").exists(),
        "vault_initialised": (layout.secret / "vault" / "master.key.sealed").exists(),
        "vault_protector": None,
        "rag_revision": None,
        "lease_age_seconds": None,
        "checks": {},
    }
    try:
        vault_meta = json.loads((layout.secret / "vault" / "vault.json").read_text(encoding="utf-8"))
        report["vault_protector"] = vault_meta.get("protector")
    except Exception:
        pass
    try:
        report["rag_revision"] = RagService(layout).revision()
        report["checks"]["rag"] = "ok"
    except Exception as error:
        report["checks"]["rag"] = type(error).__name__
    lease = LeaseManager(layout, float(config.get("secret", {}).get("lease_seconds", 300)))
    report["lease_age_seconds"] = lease.age()
    report["checks"]["lease_fresh"] = lease.fresh()
    try:
        devices = DeviceRegistry(layout)
        report["checks"]["device_authenticated"] = devices.authenticate_local(config["device_id"])
    except Exception as error:
        report["checks"]["device_authenticated"] = type(error).__name__
    _print(report)
    return 0


# ------------------------------------------------------------------------- RAG
def cmd_rag_snapshot(args: argparse.Namespace) -> int:
    result = _service(args).snapshot(
        project_id=args.project_id or None,
        query=args.query or "",
        session_id=args.session_id or None,
        related_limit=args.limit,
    )
    if args.text:
        sys.stdout.write(result["context_text"])
    else:
        _print(result)
    return 0


def cmd_rag_search(args: argparse.Namespace) -> int:
    _print(
        _service(args).search(
            args.query,
            project_id=args.project_id or None,
            scopes=args.scopes,
            include_pending=args.include_pending,
            limit=args.limit,
        )
    )
    return 0


def _project_for(args: argparse.Namespace, scope: str) -> str | None:
    if args.project_id:
        return args.project_id
    if scope == "project":
        return resolve_project_id(os.getcwd())
    return None


def cmd_rag_embed_index(args: argparse.Namespace) -> int:
    result = _service(args).build_embeddings(force=args.rebuild)
    _print(result)
    return 0 if result.get("ok") else 1


def cmd_rag_remember(args: argparse.Namespace) -> int:
    project_id = _project_for(args, args.scope)
    activity = json.loads(args.activity_json) if args.activity_json else None
    result = _service(args).remember(
        statement=args.statement,
        kind=args.kind,
        scope=args.scope,
        project_id=project_id,
        conditions=args.condition,
        exceptions=args.exception,
        activity=activity,
        evidence_kind=args.evidence,
        session_id=args.session_id or None,
    )
    _print(result.to_dict())
    return 0


def cmd_rag_update(args: argparse.Namespace) -> int:
    _print(
        _service(args)
        .update(
            args.memory_id,
            statement=args.statement,
            target_revision=args.target_revision,
            conditions=args.condition,
            exceptions=args.exception,
            evidence_kind=args.evidence,
            session_id=args.session_id or None,
        )
        .to_dict()
    )
    return 0


def cmd_rag_forget(args: argparse.Namespace) -> int:
    _print(_service(args).forget(args.memory_id, session_id=args.session_id or None).to_dict())
    return 0


def cmd_rag_delete(args: argparse.Namespace) -> int:
    if not args.yes:
        raise SystemExit("delete requires --yes (record deletion removes versions and excerpts)")
    _print(_service(args).delete(args.memory_id, confirm=True, session_id=args.session_id or None).to_dict())
    return 0


def cmd_rag_confirm(args: argparse.Namespace) -> int:
    _print(_service(args).confirm(args.candidate_id, decision=args.decision).to_dict())
    return 0


def cmd_rag_candidates(args: argparse.Namespace) -> int:
    _print(_service(args).candidate_page(args.project_id or None, page=args.page))
    return 0
    return 0


def cmd_rag_list(args: argparse.Namespace) -> int:
    service = _service(args)
    _print(
        [
            record.to_dict()
            for record in service.store.list_memories(include_pending=args.include_pending)
            if not args.project_id or record.project_id in (None, args.project_id)
        ]
    )
    return 0


def cmd_rag_get(args: argparse.Namespace) -> int:
    record = _service(args).get_memory(args.memory_id)
    if record is None:
        _print({"error": "memory not found", "memory_id": args.memory_id})
        return 1
    _print(record)
    return 0


def cmd_rag_explain(args: argparse.Namespace) -> int:
    _print(_service(args).explain(args.memory_id))
    return 0


def cmd_rag_reference(args: argparse.Namespace) -> int:
    _print(
        _service(args).reference(
            session_id=args.session_id or None,
            project_id=args.project_id or None,
            query=args.query or "",
            limit=args.limit,
        )
    )
    return 0


def cmd_rag_edit(args: argparse.Namespace) -> int:
    import signal
    import threading

    from rag.direct_edit import DirectEditor

    editor = DirectEditor(_service(args))
    if args.edit_action == "export":
        _print(editor.export())
        return 0
    if args.edit_action == "scan":
        _print(editor.scan(force=args.force).to_dict())
        return 0
    if args.edit_action == "status":
        from contracts.util import read_json, read_jsonl

        state = read_json(editor.state_path, {}) or {}
        events = read_jsonl(editor.events_path)
        _print(
            {
                "workspace": str(editor.workspace),
                "tracked": len(state.get("files", {})),
                "files": state.get("files", {}),
                "recent_events": events[-20:],
            }
        )
        return 0
    if args.edit_action == "watch":
        stop = threading.Event()

        def handle(_signum, _frame) -> None:
            stop.set()

        for name in ("SIGINT", "SIGTERM"):
            if hasattr(signal, name):
                try:
                    signal.signal(getattr(signal, name), handle)
                except (ValueError, OSError):
                    pass
        editor.watch(interval=args.interval, stop_event=stop, export_first=args.export_first)
        return 0
    raise SystemExit("unknown edit action")


def cmd_rag_status(args: argparse.Namespace) -> int:
    _print(_service(args).status(args.update_id))
    return 0


def cmd_rag_verify(args: argparse.Namespace) -> int:
    _print(_service(args).verify())
    return 0


def cmd_rag_rebuild(args: argparse.Namespace) -> int:
    _print(_service(args).rebuild_index())
    return 0


def cmd_rag_import(args: argparse.Namespace) -> int:
    layout = _layout(args)
    service = RagService(layout)
    dsh_home = Path(args.dsh_home).expanduser().resolve() if args.dsh_home else (
        Path(os.environ.get("DSH_HOME", Path.home() / ".dsh"))
    )
    result = import_all(
        service,
        dsh_home,
        limit=args.limit,
        dry_run=args.dry_run,
        project_override=args.project_id or None,
    )
    _print({"dsh_home": str(dsh_home), **result})
    return 0


def cmd_rag_sync(args: argparse.Namespace) -> int:
    from rag.sync import SyncError, sync_once, sync_pull, sync_push, sync_status, token_from_file

    layout = _layout(args)
    if args.action == "status":
        _print(sync_status(layout))
        return 0
    config = load_config(layout, create=False)
    remote = args.remote or config.get("sync_remote")
    if not remote:
        raise SystemExit("--remote is required for pull/push/once (or set sync_remote in config.json)")
    token_file = Path(args.token_file).expanduser() if args.token_file else (layout.control / "sync-token")
    token = token_from_file(token_file)
    try:
        if args.action == "pull":
            result = sync_pull(layout, remote, token)
        elif args.action == "push":
            result = sync_push(layout, remote, token)
        else:
            result = sync_once(layout, remote, token)
    except SyncError as error:
        _print({"ok": False, "error": str(error)})
        return 1
    _print(result)
    return 0 if result.get("ok") else 1


def cmd_rag_project_id(args: argparse.Namespace) -> int:
    path = Path(args.path or os.getcwd())
    if args.write:
        identifier = write_marker(path)
    else:
        identifier = resolve_project_id(path, explicit=args.project_id or None)
    _print({"path": str(path), "project_id": identifier})
    return 0


def cmd_rag_serve(args: argparse.Namespace) -> int:
    from rag.http_server import RagHttpServer

    layout = _layout(args)
    _need_config(layout)
    server = RagHttpServer(layout, host=args.host, port=args.port)
    sys.stderr.write(f"ultra-rag HTTP listening on http://{args.host}:{server.port} (bearer token required)\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.stop()
    return 0


def cmd_rag_overlay(args: argparse.Namespace) -> int:
    from rag.overlay import render_overlay

    layout = _layout(args)
    _need_config(layout)
    text = render_overlay(layout)
    if args.output:
        Path(args.output).expanduser().write_text(text, encoding="utf-8")
        _print({"overlay": str(Path(args.output).expanduser()), "bytes": len(text.encode("utf-8"))})
    else:
        sys.stdout.write(text)
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    from rag.mcp_server import main as mcp_main

    return mcp_main(["--home", str(_layout(args).home)])


# ---------------------------------------------------------------------- secret
def cmd_secret_init(args: argparse.Namespace) -> int:
    layout = _layout(args)
    config = _need_config(layout)
    vault = Vault(layout)
    protector = vault.initialise()
    DeviceRegistry(layout).ensure_local(config["device_id"], os.environ.get("COMPUTERNAME") or "local-device")
    LeaseManager(layout, float(config.get("secret", {}).get("lease_seconds", 300))).refresh("secret-init")
    _print({"vault": str(vault.root), "protector": protector})


def cmd_secret_status(args: argparse.Namespace) -> int:
    layout = _layout(args)
    config = _need_config(layout)
    vault = Vault(layout)
    report = {
        "initialised": vault.initialized,
        "keys": len(vault.list_keys()),
        "devices": DeviceRegistry(layout).list_devices(),
        "lease_age_seconds": LeaseManager(layout, float(config.get("secret", {}).get("lease_seconds", 300))).age(),
        "audit_records": len(AuditLog(layout).read()),
    }
    _print(report)


def _read_secret_value(args: argparse.Namespace) -> str:
    if args.value_stdin:
        value = sys.stdin.readline().rstrip("\r\n")
        if not value:
            raise SystemExit("stdin did not contain a value")
        return value
    if not sys.stdin.isatty():
        raise SystemExit("refusing to read a secret from a non-terminal without --value-stdin")
    return getpass.getpass("Secret value (input hidden): ")


def cmd_secret_recreate(args: argparse.Namespace) -> int:
    if not args.confirm:
        raise SystemExit("refusing to recreate the vault without --confirm")
    layout = _layout(args)
    _need_config(layout)
    result = Vault(layout).recreate()
    _print(result)
    return 0


def _strip_env_assignment(value: str, env_name: str) -> tuple[str, bool]:
    prefix = env_name + "="
    if value.startswith(prefix):
        value = value[len(prefix) :]
        if not value:
            raise SystemExit("the value after the env assignment is empty")
        return value, True
    return value, False


def cmd_secret_add(args: argparse.Namespace) -> int:
    layout = _layout(args)
    _need_config(layout)
    name = args.name or args.env_name.lower().replace("_", "-")
    value, stripped = _strip_env_assignment(_read_secret_value(args), args.env_name)
    meta = Vault(layout).register(name, args.service, args.purpose, value, env_name=args.env_name)
    _print({**meta.to_dict(), "value_stored": True, "env_line_prefix_stripped": stripped})


def cmd_secret_import_env(args: argparse.Namespace) -> int:
    layout = _layout(args)
    _need_config(layout)
    path = Path(args.file)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise VaultError(f"cannot read {path}: {error}") from error
    entries = parse_dotenv(text)
    items = []
    for entry in entries:
        base = entry.key.lower().replace("_", "-")
        items.append(
            {
                "name": f"{args.prefix}{base}",
                "service": args.service,
                "purpose": args.purpose,
                "value": entry.value,
                "env_name": entry.key,
            }
        )
    metas = Vault(layout).register_many(items)
    _print({"file": str(path), "registered": [meta.to_dict() for meta in metas]})


def cmd_secret_register(args: argparse.Namespace) -> int:
    layout = _layout(args)
    _need_config(layout)
    value = _read_secret_value(args)
    stripped = False
    if args.env_name:
        value, stripped = _strip_env_assignment(value, args.env_name)
    meta = Vault(layout).register(args.name, args.service, args.purpose, value, env_name=args.env_name)
    _print({**meta.to_dict(), "value_stored": True, "env_line_prefix_stripped": stripped})


def cmd_secret_rename(args: argparse.Namespace) -> int:
    layout = _layout(args)
    _need_config(layout)
    _print(Vault(layout).rename(args.key_id, args.name, args.service, args.purpose).to_dict())


def cmd_secret_rotate(args: argparse.Namespace) -> int:
    layout = _layout(args)
    _need_config(layout)
    meta = Vault(layout).get_meta(args.key_id)
    value, stripped = _strip_env_assignment(_read_secret_value(args), meta.env_name)
    rotated = Vault(layout).rotate(args.key_id, value)
    _print({**rotated.to_dict(), "env_line_prefix_stripped": stripped})


def cmd_secret_delete(args: argparse.Namespace) -> int:
    if not args.yes:
        raise SystemExit("key deletion requires --yes")
    layout = _layout(args)
    _need_config(layout)
    meta = Vault(layout).delete(args.key_id)
    _print({**meta.to_dict(), "deleted": True})


def cmd_secret_list(args: argparse.Namespace) -> int:
    _print([meta.to_dict() for meta in Vault(_layout(args)).list_keys()])


def cmd_secret_refresh(args: argparse.Namespace) -> int:
    layout = _layout(args)
    config = _need_config(layout)
    _print(LeaseManager(layout, float(config.get("secret", {}).get("lease_seconds", 300))).refresh("owner"))


def cmd_secret_devices(args: argparse.Namespace) -> int:
    layout = _layout(args)
    _need_config(layout)
    registry = DeviceRegistry(layout)
    if args.action == "list":
        _print(registry.list_devices())
    elif args.action == "stop":
        if not args.yes:
            raise SystemExit("device stop requires --yes")
        _print(registry.stop(args.device_id))
    else:
        if not args.yes:
            raise SystemExit("device registration requires --yes")
        record, token = registry.register(args.name)
        _print({**record, "token": token, "note": "store this token on the device now; it is not shown again"})
    return 0


def cmd_secret_audit(args: argparse.Namespace) -> int:
    layout = _layout(args)
    _need_config(layout)
    log = AuditLog(layout)
    if args.action == "list":
        _print(log.read())
    else:
        if not args.yes:
            raise SystemExit("audit deletion requires --yes")
        _print({"deleted_records": log.delete_all()})
    return 0


def cmd_secret_exec(args: argparse.Namespace) -> int:
    service = KeyService(_layout(args))
    result = service.key_exec(
        command=args.command,
        key_ids=args.key,
        host=args.host,
        mode=args.mode,
        timeout_seconds=args.timeout,
        deliverables=args.deliverable,
        deliver_to=args.deliver_to,
        input_paths=args.input,
    )
    _print(result)
    return 0


def cmd_secret_exec_status(args: argparse.Namespace) -> int:
    _print(
        KeyService(_layout(args)).exec_status(
            args.exec_id, allow_other_device=args.allow_other_device
        )
    )
    return 0


def cmd_secret_artifacts(args: argparse.Namespace) -> int:
    _print(
        KeyService(_layout(args)).exec_artifacts(
            args.exec_id, allow_other_device=args.allow_other_device
        )
    )
    return 0


def cmd_secret_stop(args: argparse.Namespace) -> int:
    _print(
        KeyService(_layout(args)).stop(
            args.exec_id,
            reason=args.reason,
            allow_other_device=args.allow_other_device,
        )
    )
    return 0


# -------------------------------------------------------------------- password
def cmd_password_request(args: argparse.Namespace) -> int:
    layout = _layout(args)
    _need_config(layout)
    request = PasswordBroker(layout).create_request(
        host=args.host,
        account=args.account,
        command=args.command,
        adapter=args.adapter,
        session_id=args.session_id or None,
        exec_id=args.exec_id,
        host_key_fingerprint=args.host_key_fingerprint,
        port=args.port,
    )
    _print(request.to_dict())


def cmd_password_list(args: argparse.Namespace) -> int:
    _print(PasswordBroker(_layout(args)).list_requests(include_finished=args.all))


def cmd_password_status(args: argparse.Namespace) -> int:
    broker = PasswordBroker(_layout(args))
    if args.exec_id:
        _print(broker.status_by_exec(args.exec_id))
    elif args.request_id:
        _print(broker.status(args.request_id))
    else:
        raise SystemExit("either a request id or --exec-id is required")
    return 0


def cmd_password_cancel(args: argparse.Namespace) -> int:
    _print(PasswordBroker(_layout(args)).cancel(args.request_id))


def cmd_password_provide(args: argparse.Namespace) -> int:
    layout = _layout(args)
    _need_config(layout)
    broker = PasswordBroker(layout)
    request = broker.status(args.request_id)
    sys.stderr.write(
        f"Target host: {request['host']}\nAccount: {request['account']}\nCommand: {request['command']}\n"
        f"Expires: {request['expires_at']}\n"
    )
    if not sys.stdin.isatty():
        raise SystemExit("refusing to read a password from a non-terminal; this input is terminal-only")
    result = broker.run_with_prompt(args.request_id, lambda label: getpass.getpass(label), timeout=args.timeout)
    _print(result)
    return 0 if result.get("ok") else 1


def cmd_password_sync(args: argparse.Namespace) -> int:
    from rag.sync import RemoteClient, SyncError, token_from_file

    layout = _layout(args)
    _need_config(layout)
    if not args.remote:
        raise SystemExit("--remote is required")
    token_file = Path(args.token_file).expanduser() if args.token_file else (layout.control / "sync-token")
    token = token_from_file(token_file)
    try:
        bundle = RemoteClient(args.remote, token).get("/v1/hostkeys")
    except SyncError as error:
        _print({"ok": False, "error": str(error)})
        return 1
    result = PasswordBroker(layout).import_hostkeys(bundle)
    _print({"ok": True, **result})
    return 0


def cmd_password_hostkey(args: argparse.Namespace) -> int:
    broker = PasswordBroker(_layout(args))
    if args.action == "scan":
        _print(broker.scan_host_key(args.host, args.port))
    elif args.action == "check":
        _print(broker.check_host_key(args.host, args.port))
    elif args.action == "accept":
        _print(broker.accept_host_key(args.host, args.port, args.fingerprint).to_dict())
    else:
        _print(broker.known_host_keys())
    return 0


# ----------------------------------------------------------------------- build
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ultra-rag", description="ultra-RAG personal memory and authentication assistance")
    parser.add_argument("--home", default=None, help="ultra-RAG home (default: ULTRA_RAG_HOME or OS app-data)")
    sub = parser.add_subparsers(dest="group", required=True)

    init = sub.add_parser("init", help="initialise this home")
    init.add_argument("--role", choices=["server", "client"])
    init.add_argument("--host", choices=["pc", "server"])
    init.add_argument("--project-id")
    init.add_argument("--device-name")
    init.set_defaults(func=cmd_init)

    doctor = sub.add_parser("doctor", help="print implementation/runtime state")
    doctor.set_defaults(func=cmd_doctor)

    mcp = sub.add_parser("mcp", help="run the MCP stdio server")
    mcp.set_defaults(func=cmd_mcp)

    rag = sub.add_parser("rag", help="personal memory, session notes, and sync")
    rag_sub = rag.add_subparsers(dest="command", required=True)
    snapshot = rag_sub.add_parser("snapshot")
    snapshot.add_argument("--project-id")
    snapshot.add_argument("--query", default="")
    snapshot.add_argument("--session-id")
    snapshot.add_argument("--limit", type=int, default=8)
    snapshot.add_argument("--text", action="store_true")
    snapshot.set_defaults(func=cmd_rag_snapshot)
    search = rag_sub.add_parser("search")
    search.add_argument("query")
    search.add_argument("--project-id")
    search.add_argument("--scopes", nargs="*")
    search.add_argument("--include-pending", action="store_true")
    search.add_argument("--limit", type=int, default=8)
    search.set_defaults(func=cmd_rag_search)
    embed_index = rag_sub.add_parser("embed-index", help="build the optional local embedding index")
    embed_index.add_argument("--rebuild", action="store_true")
    embed_index.set_defaults(func=cmd_rag_embed_index)
    remember = rag_sub.add_parser("remember")
    remember.add_argument("statement")
    remember.add_argument("--kind", default="preference")
    remember.add_argument("--scope", default="personal", choices=["personal", "shared_lesson", "project", "session"])
    remember.add_argument("--project-id")
    remember.add_argument("--condition", action="append", default=[])
    remember.add_argument("--exception", action="append", default=[])
    remember.add_argument("--evidence", default="explicit_statement")
    remember.add_argument("--session-id")
    remember.add_argument("--activity-json", default=None)
    remember.set_defaults(func=cmd_rag_remember)
    update = rag_sub.add_parser("update")
    update.add_argument("memory_id")
    update.add_argument("--statement", default=None)
    update.add_argument("--condition", action="append", default=[])
    update.add_argument("--exception", action="append", default=[])
    update.add_argument("--target-revision", type=int)
    update.add_argument("--evidence", default="explicit_statement")
    update.add_argument("--session-id")
    update.set_defaults(func=cmd_rag_update)
    forget = rag_sub.add_parser("forget")
    forget.add_argument("memory_id")
    forget.add_argument("--session-id")
    forget.set_defaults(func=cmd_rag_forget)
    delete = rag_sub.add_parser("delete")
    delete.add_argument("memory_id")
    delete.add_argument("--yes", action="store_true")
    delete.add_argument("--session-id")
    delete.set_defaults(func=cmd_rag_delete)
    confirm = rag_sub.add_parser("confirm")
    confirm.add_argument("candidate_id")
    confirm.add_argument("--decision", choices=["adopt", "reject"], default="adopt")
    confirm.set_defaults(func=cmd_rag_confirm)
    candidates = rag_sub.add_parser("candidates")
    candidates.add_argument("--project-id")
    candidates.add_argument("--page", type=int, default=1)
    candidates.set_defaults(func=cmd_rag_candidates)
    list_mem = rag_sub.add_parser("list")
    list_mem.add_argument("--project-id")
    list_mem.add_argument("--include-pending", action="store_true")
    list_mem.set_defaults(func=cmd_rag_list)
    get = rag_sub.add_parser("get")
    get.add_argument("memory_id")
    get.set_defaults(func=cmd_rag_get)
    explain = rag_sub.add_parser("explain")
    explain.add_argument("memory_id")
    explain.set_defaults(func=cmd_rag_explain)
    reference = rag_sub.add_parser("reference")
    reference.add_argument("--session-id")
    reference.add_argument("--project-id")
    reference.add_argument("--query", default="")
    reference.add_argument("--limit", type=int, default=50)
    reference.set_defaults(func=cmd_rag_reference)
    edit = rag_sub.add_parser("edit")
    edit.add_argument("edit_action", choices=["export", "scan", "watch", "status"])
    edit.add_argument("--force", action="store_true")
    edit.add_argument("--interval", type=float, default=2.0)
    edit.add_argument("--export-first", action="store_true")
    edit.set_defaults(func=cmd_rag_edit)
    status = rag_sub.add_parser("status")
    status.add_argument("update_id")
    status.set_defaults(func=cmd_rag_status)
    verify = rag_sub.add_parser("verify")
    verify.set_defaults(func=cmd_rag_verify)
    rebuild = rag_sub.add_parser("rebuild-index")
    rebuild.set_defaults(func=cmd_rag_rebuild)
    imp = rag_sub.add_parser("import")
    imp.add_argument("--dsh-home")
    imp.add_argument("--limit", type=int)
    imp.add_argument("--dry-run", action="store_true")
    imp.add_argument("--project-id")
    imp.set_defaults(func=cmd_rag_import)
    sync = rag_sub.add_parser("sync")
    sync.add_argument("action", choices=["pull", "push", "once", "status"])
    sync.add_argument("--remote")
    sync.add_argument("--token-file")
    sync.set_defaults(func=cmd_rag_sync)
    project = rag_sub.add_parser("project-id")
    project.add_argument("path", nargs="?")
    project.add_argument("--project-id")
    project.add_argument("--write", action="store_true")
    project.set_defaults(func=cmd_rag_project_id)
    serve = rag_sub.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.set_defaults(func=cmd_rag_serve)
    overlay = rag_sub.add_parser("overlay")
    overlay.add_argument("--output")
    overlay.set_defaults(func=cmd_rag_overlay)

    secret = sub.add_parser("secret", help="key vault and keyed execution")
    secret_sub = secret.add_subparsers(dest="command", required=True)
    s_init = secret_sub.add_parser("init")
    s_init.set_defaults(func=cmd_secret_init)
    s_status = secret_sub.add_parser("status")
    s_status.set_defaults(func=cmd_secret_status)
    s_register = secret_sub.add_parser("register")
    s_register.add_argument("--name", required=True)
    s_register.add_argument("--service", required=True)
    s_register.add_argument("--purpose", required=True)
    s_register.add_argument("--env-name")
    s_register.add_argument("--value-stdin", action="store_true")
    s_register.set_defaults(func=cmd_secret_register)
    s_add = secret_sub.add_parser("add")
    s_add.add_argument("env_name")
    s_add.add_argument("--service", required=True)
    s_add.add_argument("--purpose", default="api")
    s_add.add_argument("--name")
    s_add.add_argument("--value-stdin", action="store_true")
    s_add.set_defaults(func=cmd_secret_add)
    s_import_env = secret_sub.add_parser("import-env")
    s_import_env.add_argument("file", nargs="?", default=".env")
    s_import_env.add_argument("--service", default="dotenv")
    s_import_env.add_argument("--purpose", default="env")
    s_import_env.add_argument("--prefix", default="")
    s_import_env.set_defaults(func=cmd_secret_import_env)
    s_rename = secret_sub.add_parser("rename")
    s_rename.add_argument("key_id")
    s_rename.add_argument("--name", required=True)
    s_rename.add_argument("--service")
    s_rename.add_argument("--purpose")
    s_rename.set_defaults(func=cmd_secret_rename)
    s_rotate = secret_sub.add_parser("rotate")
    s_rotate.add_argument("key_id")
    s_rotate.add_argument("--value-stdin", action="store_true")
    s_rotate.set_defaults(func=cmd_secret_rotate)
    s_delete = secret_sub.add_parser("delete")
    s_delete.add_argument("key_id")
    s_delete.add_argument("--yes", action="store_true")
    s_delete.set_defaults(func=cmd_secret_delete)
    s_list = secret_sub.add_parser("list")
    s_list.set_defaults(func=cmd_secret_list)
    s_refresh = secret_sub.add_parser("refresh")
    s_refresh.set_defaults(func=cmd_secret_refresh)
    s_devices = secret_sub.add_parser("devices")
    s_devices.add_argument("action", choices=["list", "register", "stop"])
    s_devices.add_argument("--name", default="owner-device")
    s_devices.add_argument("--device-id")
    s_devices.add_argument("--yes", action="store_true")
    s_devices.set_defaults(func=cmd_secret_devices)
    s_recreate = secret_sub.add_parser("recreate")
    s_recreate.add_argument("--confirm", action="store_true")
    s_recreate.set_defaults(func=cmd_secret_recreate)
    s_audit = secret_sub.add_parser("audit")
    s_audit.add_argument("action", choices=["list", "delete"])
    s_audit.add_argument("--yes", action="store_true")
    s_audit.set_defaults(func=cmd_secret_audit)
    s_exec = secret_sub.add_parser("exec")
    s_exec.add_argument("--command", required=True)
    s_exec.add_argument("--key", action="append", required=True)
    s_exec.add_argument("--host", default="local")
    s_exec.add_argument("--mode", choices=["single", "resident"], default="single")
    s_exec.add_argument("--timeout", type=float)
    s_exec.add_argument("--deliverable", action="append", default=[])
    s_exec.add_argument("--deliver-to")
    s_exec.add_argument("--input", action="append", default=[])
    s_exec.set_defaults(func=cmd_secret_exec)
    s_exec_status = secret_sub.add_parser("exec-status")
    s_exec_status.add_argument("exec_id")
    s_exec_status.add_argument("--allow-other-device", action="store_true")
    s_exec_status.set_defaults(func=cmd_secret_exec_status)
    s_artifacts = secret_sub.add_parser("artifacts")
    s_artifacts.add_argument("exec_id")
    s_artifacts.add_argument("--allow-other-device", action="store_true")
    s_artifacts.set_defaults(func=cmd_secret_artifacts)
    s_stop = secret_sub.add_parser("stop")
    s_stop.add_argument("exec_id")
    s_stop.add_argument("--reason", default="owner_stop")
    s_stop.add_argument("--allow-other-device", action="store_true")
    s_stop.set_defaults(func=cmd_secret_stop)

    password = sub.add_parser("password", help="one-use password input for sudo/ssh")
    password_sub = password.add_subparsers(dest="command", required=True)
    p_req = password_sub.add_parser("request")
    p_req.add_argument("--adapter", choices=["ssh", "sudo"], default="ssh")
    p_req.add_argument("--host", required=True)
    p_req.add_argument("--account", required=True)
    p_req.add_argument("--command", required=True)
    p_req.add_argument("--exec-id", required=True)
    p_req.add_argument("--port", type=int)
    p_req.add_argument("--host-key-fingerprint")
    p_req.add_argument("--session-id")
    p_req.set_defaults(func=cmd_password_request)
    p_list = password_sub.add_parser("list")
    p_list.add_argument("--all", action="store_true")
    p_list.set_defaults(func=cmd_password_list)
    p_status = password_sub.add_parser("status")
    p_status.add_argument("request_id", nargs="?")
    p_status.add_argument("--exec-id")
    p_status.set_defaults(func=cmd_password_status)
    p_cancel = password_sub.add_parser("cancel")
    p_cancel.add_argument("request_id")
    p_cancel.set_defaults(func=cmd_password_cancel)
    p_provide = password_sub.add_parser("provide")
    p_provide.add_argument("request_id")
    p_provide.add_argument("--timeout", type=float, default=120.0)
    p_provide.set_defaults(func=cmd_password_provide)
    p_hostkey = password_sub.add_parser("hostkey")
    p_hostkey.add_argument("action", choices=["scan", "check", "accept", "list"])
    p_hostkey.add_argument("--host", default="")
    p_hostkey.add_argument("--port", type=int, default=22)
    p_hostkey.add_argument("--fingerprint", default="")
    p_hostkey.set_defaults(func=cmd_password_hostkey)
    p_sync = password_sub.add_parser("sync")
    p_sync.add_argument("--remote", required=True)
    p_sync.add_argument("--token-file")
    p_sync.set_defaults(func=cmd_password_sync)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 2
    try:
        return int(args.func(args) or 0)
    except (ContractError, ConfigError, StoreError, ProjectError, VaultError, KeystoreError, PasswordError, DotenvError) as error:
        sys.stderr.write(f"ultra-rag: {error}\n")
        return 2
    except SystemExit:
        raise
    except KeyboardInterrupt:
        sys.stderr.write("ultra-rag: interrupted\n")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
