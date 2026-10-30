"""Permission-lease refresher.

The five-minute lease exists so that an execution stops when the management
channel that can confirm key permissions disappears. This daemon is that
management channel: systemd (or the owner) runs it while key supply should be
allowed. It writes only a timestamp and a source label; it never reads or writes
a key value. If it stops, the lease expires and new supply is refused while
existing executions are stopped by their monitors.
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading

from contracts.config import ConfigError, Layout, load_config
from secret_core.lease import LeaseManager


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ultra-rag-lease", description="refresh the permission lease")
    parser.add_argument("--home", default=None)
    parser.add_argument("--interval", type=float, default=60.0)
    args = parser.parse_args(argv)
    layout = Layout.at(args.home)
    try:
        config = load_config(layout, create=False)
    except ConfigError as error:
        print(f"ultra-rag-lease: {error}", file=sys.stderr)
        return 2
    lease = LeaseManager(layout, float(config.get("secret", {}).get("lease_seconds", 300)))
    interval = float(args.interval)
    if interval <= 0 or interval >= lease.seconds:
        print("ultra-rag-lease: interval must be positive and shorter than the lease", file=sys.stderr)
        return 2
    stop = threading.Event()

    def handle(signum, _frame) -> None:
        stop.set()

    for name in ("SIGINT", "SIGTERM"):
        if hasattr(signal, name):
            try:
                signal.signal(getattr(signal, name), handle)
            except (ValueError, OSError):
                pass
    lease.refresh("lease-daemon")
    while not stop.wait(interval):
        try:
            lease.refresh("lease-daemon")
        except OSError as error:
            print(f"ultra-rag-lease: refresh failed: {error}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
