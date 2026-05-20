"""Subprocess runner implementation."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


@dataclass(frozen=True)
class CommandResult:
    """Captured subprocess result."""

    command: tuple[str, ...]
    return_code: int
    stdout: str
    stderr: str


class SubprocessRunner:
    """Run commands using the subprocess module.

    Implements the ``IProcessRunner`` interface from
    ``backend.core.shared.interfaces.agent_runner`` via duck typing.
    """

    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        check: bool = True,
        timeout: int | None = None,
    ) -> CommandResult:
        """Run a subprocess and capture output."""
        completed = subprocess.run(
            list(command),
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
        )
        result = CommandResult(
            command=tuple(command),
            return_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
        if check and completed.returncode != 0:
            raise subprocess.CalledProcessError(
                completed.returncode,
                list(command),
                output=completed.stdout,
                stderr=completed.stderr,
            )
        return result
