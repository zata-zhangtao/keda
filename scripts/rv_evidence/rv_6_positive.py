"""rv-6-positive: full test suite passes (rv-6 oracle).

Reproduces the positive path of realistic-validation item rv-6 by running
the new memory tests plus the runner-integration tests that exercise the
distillation entry point. The runner itself captures the tail of
``uv run --no-sync just test``; this script is the focused equivalent
that stays self-terminating.
"""

from __future__ import annotations

import subprocess
import sys


def main() -> int:
    cmd = [
        "uv",
        "run",
        "--no-sync",
        "pytest",
        "-o",
        "addopts=",
        "-q",
        "tests/test_agent_runner_memory.py",
        "tests/test_agent_runner_skill_distillation.py",
        "tests/test_agent_runner_skill_retrieval.py",
        "tests/test_run_agent.py",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    tail = "\n".join(result.stdout.splitlines()[-3:])
    print(tail)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
