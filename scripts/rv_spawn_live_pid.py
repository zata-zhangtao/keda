#!/usr/bin/env python3
"""Spawn a long-lived subprocess and write its pid to ``--pid-file``.

Used by Realistic Validation commands that need a real running pid (e.g.
``iar daemon status`` whose liveness probe ``os.kill(pid, 0)`` reports a
managed record as alive only when the pid exists).

Usage:
    python3 scripts/rv_spawn_live_pid.py \\
        --pid-file <fixture>/live.pid \\
        --ready-file <fixture>/live.ready \\
        --cmd 'python3 -c "import time; time.sleep(3600)"'
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid-file", required=True, type=Path)
    parser.add_argument("--ready-file", required=True, type=Path)
    parser.add_argument(
        "--cmd",
        required=True,
        help="Shell command to spawn (via /bin/sh -c). Will run until killed.",
    )
    args = parser.parse_args(argv)

    args.pid_file.parent.mkdir(parents=True, exist_ok=True)
    args.ready_file.parent.mkdir(parents=True, exist_ok=True)
    if args.ready_file.exists():
        args.ready_file.unlink()

    proc = subprocess.Popen(
        ["/bin/sh", "-c", args.cmd],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    args.pid_file.write_text(f"{proc.pid}\n", encoding="utf-8")
    # Wait briefly for the child to be schedulable, then signal readiness.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            os.kill(proc.pid, 0)
            break
        except ProcessLookupError:
            time.sleep(0.05)
    args.ready_file.write_text("ready\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
