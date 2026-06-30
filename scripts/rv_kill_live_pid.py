#!/usr/bin/env python3
"""Kill the subprocess whose pid is recorded in ``--pid-file``.

Used by Realistic Validation commands to clean up the long-lived sleep
process spawned by ``rv_spawn_live_pid.py``. Idempotent: missing or
already-dead pid is a no-op exit 0.

Usage:
    python3 scripts/rv_kill_live_pid.py --pid-file <fixture>/live.pid
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid-file", required=True, type=Path)
    args = parser.parse_args(argv)

    if not args.pid_file.exists():
        return 0
    raw = args.pid_file.read_text(encoding="utf-8").strip()
    try:
        pid = int(raw.splitlines()[0])
    except (ValueError, IndexError):
        return 0
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return 0
    except PermissionError:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
