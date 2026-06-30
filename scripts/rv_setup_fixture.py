#!/usr/bin/env python3
"""Build a temp IAR fixture (config + processes.json + log file) for RV checks.

Usage:
    python3 scripts/rv_setup_fixture.py \
        --fixture-dir /tmp/iar-rv-fixture \
        --repo-id fixture-repo \
        --process-id abc123 \
        --kind daemon \
        --log-lines 50
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import textwrap
from pathlib import Path


def build_fixture(
    *,
    fixture_dir: Path,
    repo_id: str,
    process_id: str,
    kind: str,
    log_lines: int,
    running: bool,
    live_pid: int | None = None,
) -> dict[str, str]:
    """Materialize a temp IAR config + processes.json + log file.

    Returns a dict of paths the caller can pass as env vars / CLI args.
    If ``live_pid`` is given, that pid is written into the registry so the
    supervisor's liveness probe (``os.kill(pid, 0)``) reports the record as
    alive — required for the ``iar daemon status`` RV to render a managed
    running row.
    """
    fixture_dir.mkdir(parents=True, exist_ok=True)
    config_path = fixture_dir / "config.toml"
    registry_path = fixture_dir / "processes.json"
    log_path = fixture_dir / "logs" / f"{kind}-{process_id}.log"

    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as log_file:
        # Sentinel line first so the initial tail prints a known marker that
        # the follow driver can grep for.
        log_file.write("INITIAL-SENTINEL-FIRST-LINE\n")
        for index in range(log_lines):
            log_file.write(f"line {index:04d}: daemon step {index}\n")

    status = "running" if running else "exited"
    pid_value = live_pid if live_pid is not None else 4321
    processes_payload = {
        process_id: {
            "process_id": process_id,
            "repo_id": repo_id,
            "kind": kind,
            "pid": pid_value,
            "status": status,
            "exit_code": None,
            "log_path": str(log_path),
            "command": ["uv", "run", "iar", "daemon", "--repo-id", repo_id],
            "started_at": "2026-06-23T00:00:00+00:00",
            "stopped_at": None,
        }
    }
    registry_path.write_text(
        json.dumps(processes_payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    # Minimal config.toml: only the [agent_runner.console] section is required
    # for create_process_supervisor() to resolve its registry_path / log_dir;
    # the repository entry is required so resolve_repository_targets() can
    # resolve the repo_id CLI argument to a context.
    repo_path = fixture_dir / "repo"
    repo_path.mkdir(parents=True, exist_ok=True)
    # Real Git repo so resolve_repository_targets accepts the path.
    subprocess.run(
        ["git", "-C", str(repo_path), "init", "--quiet"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo_path), "config", "user.email", "fixture@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo_path), "config", "user.name", "Fixture Bot"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo_path), "commit", "--allow-empty", "-m", "init"],
        check=True,
    )

    config_path.write_text(
        textwrap.dedent(
            f"""
            [agent_runner.console]
            process_registry_path = "{registry_path}"
            process_log_dir = "{fixture_dir / 'logs'}"

            [agent_runner.repositories.{repo_id}]
            path = "{repo_path}"
            enabled = true
            display_name = "{repo_id} (fixture)"
            """
        ).lstrip(),
        encoding="utf-8",
    )

    return {
        "config": str(config_path),
        "registry": str(registry_path),
        "log": str(log_path),
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture-dir", required=True, type=Path)
    parser.add_argument("--repo-id", required=True)
    parser.add_argument("--process-id", required=True)
    parser.add_argument("--kind", choices=("daemon", "review_daemon"), default="daemon")
    parser.add_argument("--log-lines", type=int, default=50)
    parser.add_argument(
        "--not-running",
        action="store_true",
        help="Mark the record as exited instead of running.",
    )
    parser.add_argument(
        "--live-pid",
        type=int,
        default=None,
        help=(
            "Write this real pid into the registry so the supervisor liveness "
            "probe reports the record as alive (use for daemon-status RV)."
        ),
    )
    args = parser.parse_args(argv)

    paths = build_fixture(
        fixture_dir=args.fixture_dir,
        repo_id=args.repo_id,
        process_id=args.process_id,
        kind=args.kind,
        log_lines=args.log_lines,
        running=not args.not_running,
        live_pid=args.live_pid,
    )
    json.dump(paths, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
