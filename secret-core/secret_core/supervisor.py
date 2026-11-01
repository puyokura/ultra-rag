"""Detached supervisor for a resident key execution.

The CLI exits as soon as the execution is running. This process owns the child
process, its stdout/stderr pipes, the independent monitor, and the final state
and audit record, so a long-running command is not tied to the terminal that
started it.
"""

from __future__ import annotations

import argparse
import sys

from contracts.config import ConfigError, Layout, load_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ultra-rag-supervisor", description="resident execution supervisor")
    parser.add_argument("--home", default=None)
    parser.add_argument("--exec-id", required=True)
    parser.add_argument("--started-at", type=float, required=True)
    args = parser.parse_args(argv)
    layout = Layout.at(args.home)
    try:
        load_config(layout, create=False)
        from secret_core.runner import ExecManager

        manager = ExecManager(layout)
    except (ConfigError, Exception) as error:  # no state can be written without a layout
        print(f"ultra-rag-supervisor: {type(error).__name__}", file=sys.stderr)
        return 2
    try:
        manager.supervise_resident(args.exec_id, args.started_at)
    except Exception as error:  # keep the execution visible as failed, not silent
        from contracts.models import ExecState
        from contracts.util import utc_now

        state = manager._state(args.exec_id) or ExecState(
            exec_id=args.exec_id, status="running", generation=manager.epoch
        )
        state.status = "failed"
        state.ended_at = utc_now()
        state.detail = f"supervisor_failed: {type(error).__name__}"
        manager._write_state(args.exec_id, state)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
