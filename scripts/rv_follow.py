#!/usr/bin/env python3
"""Realistic validation driver: spawn ``iar logs -f`` and prove follow behaviour.

Steps:
1. Launch ``uv run iar logs -f`` as a subprocess with the temp config.
2. Wait for the initial tail (containing a sentinel line) to flush to stdout.
3. Append a sentinel line to the log file.
4. Wait for the appended sentinel to appear in the subprocess stdout.
5. Send SIGINT and verify exit code is 0.
6. Capture ``before.txt`` (initial tail) and ``after.txt`` (initial + appended).

Usage:
    python3 scripts/rv_follow.py \\
        --fixture-dir /tmp/iar-rv-115-follow \\
        --repo-id fixture-repo \\
        --process-id abc123 \\
        --log-file /tmp/iar-rv-115-follow/logs/daemon-abc123.log \\
        --before-output .iar/evidence/rv-2-logs-follow-before.txt \\
        --after-output .iar/evidence/rv-2-logs-follow-after.txt
"""

from __future__ import annotations

import argparse
import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path


INITIAL_SENTINEL = "INITIAL-SENTINEL-FIRST-LINE"
APPEND_SENTINEL = "APPEND-SENTINEL-FOLLOW-LINE"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture-dir", required=True, type=Path)
    parser.add_argument("--repo-id", required=True)
    parser.add_argument("--before-output", required=True, type=Path)
    parser.add_argument("--after-output", required=True, type=Path)
    parser.add_argument("--log-file", required=True, type=Path)
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args(argv)

    env = os.environ.copy()
    env["IAR_CONFIG"] = str(args.fixture_dir / "config.toml")
    env["PYTHONUNBUFFERED"] = "1"

    cmd = ["uv", "run", "iar", "logs", "--repo-id", args.repo_id, "--follow"]

    print(f"[rv-follow] launching: {' '.join(cmd)}", file=sys.stderr, flush=True)
    proc = subprocess.Popen(
        cmd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )

    start = time.monotonic()
    stdout_buffer = b""
    stderr_buffer = b""
    saw_initial = False
    appended = False
    sent_sigint = False
    exit_code: int | None = None

    try:
        while True:
            if time.monotonic() - start > args.timeout:
                print("[rv-follow] timeout exceeded, killing", file=sys.stderr)
                proc.kill()
                break

            readable, _, _ = select.select([proc.stdout, proc.stderr], [], [], 0.5)
            for stream in readable:
                chunk = stream.read(4096)
                if not chunk:
                    continue
                if stream is proc.stdout:
                    stdout_buffer += chunk
                    print(
                        f"[rv-follow] stdout chunk len={len(chunk)} " f"total={len(stdout_buffer)}",
                        file=sys.stderr,
                        flush=True,
                    )
                    if not saw_initial and INITIAL_SENTINEL.encode() in stdout_buffer:
                        saw_initial = True
                        args.before_output.write_bytes(stdout_buffer)
                        print(
                            "[rv-follow] initial tail captured, appending marker",
                            file=sys.stderr,
                            flush=True,
                        )
                        with args.log_file.open("a", encoding="utf-8") as log_file:
                            log_file.write(APPEND_SENTINEL + "\n")
                        appended = True
                    if appended and APPEND_SENTINEL.encode() in stdout_buffer and not sent_sigint:
                        args.after_output.write_bytes(stdout_buffer)
                        print(
                            "[rv-follow] appended line captured, sending SIGINT",
                            file=sys.stderr,
                            flush=True,
                        )
                        proc.send_signal(signal.SIGINT)
                        sent_sigint = True
                else:
                    stderr_buffer += chunk
            if sent_sigint and proc.poll() is not None:
                exit_code = proc.returncode
                break
    finally:
        if proc.poll() is None:
            try:
                exit_code = proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                exit_code = proc.wait(timeout=5)

    if not args.before_output.exists():
        args.before_output.write_bytes(stdout_buffer)
    if not args.after_output.exists():
        args.after_output.write_bytes(stdout_buffer)

    stderr_text = stderr_buffer.decode("utf-8", errors="replace")
    print(f"[rv-follow] stderr:\n{stderr_text}", file=sys.stderr)
    print(f"[rv-follow] exit code: {exit_code}", file=sys.stderr)
    print(f"[rv-follow] saw_initial={saw_initial} appended={appended}", file=sys.stderr)

    if exit_code == 0 and saw_initial and appended:
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
