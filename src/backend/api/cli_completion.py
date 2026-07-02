"""Shell completion helpers and command registration for the Typer CLI.

Keeping completion logic in a dedicated module prevents ``cli_typer.py`` from
 growing with installation utilities that are only exercised once per shell.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path

import typer
from typer.completion import get_completion_script


class CompletionShellChoice(str, Enum):
    """Shells supported by the explicit completion installer."""

    bash = "bash"
    zsh = "zsh"
    fish = "fish"


def _completion_script(shell: CompletionShellChoice) -> str:
    """Return the shell completion script for the iAR executable."""
    return get_completion_script(
        prog_name="iar",
        complete_var="_IAR_COMPLETE",
        shell=shell.value,
    )


def _append_unique_line(file_path: Path, line: str) -> bool:
    """Append a shell profile line when it is not already present."""
    existing_text = ""
    if file_path.exists():
        existing_text = file_path.read_text(encoding="utf-8")
        if line in existing_text.splitlines():
            return False
    file_path.parent.mkdir(parents=True, exist_ok=True)
    with file_path.open("a", encoding="utf-8") as profile_file:
        if existing_text and not existing_text.endswith("\n"):
            profile_file.write("\n")
        profile_file.write(f"{line}\n")
    return True


def _install_completion_script(
    shell: CompletionShellChoice,
) -> tuple[Path, Path | None]:
    """Install iAR shell completion and return the script/profile paths."""
    script_content = _completion_script(shell)
    home_path = Path.home()
    if shell is CompletionShellChoice.zsh:
        completion_dir = home_path / ".zsh" / "completions"
        completion_path = completion_dir / "_iar"
        completion_path.parent.mkdir(parents=True, exist_ok=True)
        completion_path.write_text(f"{script_content}\n", encoding="utf-8")
        zshrc_path = home_path / ".zshrc"
        _append_unique_line(zshrc_path, "autoload -Uz compinit && compinit")
        source_line = f'[ -f "{completion_path}" ] && source "{completion_path}"'
        _append_unique_line(zshrc_path, source_line)
        return completion_path, zshrc_path
    if shell is CompletionShellChoice.bash:
        completion_path = home_path / ".config" / "iar" / "iar_completion.bash"
        completion_path.parent.mkdir(parents=True, exist_ok=True)
        completion_path.write_text(f"{script_content}\n", encoding="utf-8")
        bashrc_path = home_path / ".bashrc"
        source_line = f'[ -f "{completion_path}" ] && source "{completion_path}"'
        _append_unique_line(bashrc_path, source_line)
        return completion_path, bashrc_path
    completion_path = home_path / ".config" / "fish" / "completions" / "iar.fish"
    completion_path.parent.mkdir(parents=True, exist_ok=True)
    completion_path.write_text(f"{script_content}\n", encoding="utf-8")
    return completion_path, None


def register_completion_commands(completion_app: typer.Typer) -> None:
    """Register ``completion show`` and ``completion install`` commands."""

    @completion_app.command("show")
    def completion_show_command(
        shell: CompletionShellChoice = CompletionShellChoice.zsh,
    ) -> int:
        """Print a shell completion script."""
        typer.echo(_completion_script(shell))
        return 0

    @completion_app.command("install")
    def completion_install_command(
        shell: CompletionShellChoice = CompletionShellChoice.zsh,
    ) -> int:
        """Install shell completion for the current user."""
        completion_path, profile_path = _install_completion_script(shell)
        typer.echo(f"Installed {shell.value} completion: {completion_path}")
        if profile_path is not None:
            typer.echo(f"Reload your shell with: source {profile_path}")
        else:
            typer.echo("Open a new terminal session to activate completion.")
        return 0
