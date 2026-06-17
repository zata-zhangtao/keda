"""Create a clean release zip for distribution.

This script packages only git-tracked files into a zip under `dist/`, while
excluding local environment artifacts (e.g. `.venv/`, `.uv/`) and uv-related
files that are not required for publishing (e.g. `uv.lock`).

It does NOT delete or modify files in the working tree.
"""

from __future__ import annotations

import fnmatch
import subprocess
import sys
import time
import zipfile
from pathlib import Path


def _run_git_ls_files(repo_root: Path) -> list[str]:
    """Return git-tracked files (relative paths) using `git ls-files`.

    Args:
        repo_root (Path): Repository root directory.

    Returns:
        list[str]: Tracked file paths relative to repo root.

    Raises:
        RuntimeError: If git is not available or command fails.
    """
    try:
        completed = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=str(repo_root),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("git not found in PATH") from exc
    except subprocess.CalledProcessError as exc:
        msg = exc.stderr.decode(errors="ignore").strip()
        raise RuntimeError(f"git ls-files failed: {msg}") from exc

    raw = completed.stdout.split(b"\x00")
    return [p.decode("utf-8", errors="ignore") for p in raw if p]


def _read_project_version(repo_root: Path) -> str | None:
    """Read version from `pyproject.toml` if possible.

    Args:
        repo_root (Path): Repository root directory.

    Returns:
        str | None: Version string or None if unavailable.
    """
    pyproject = repo_root / "pyproject.toml"
    if not pyproject.exists():
        return None

    try:
        import tomllib  # py3.11+

        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        version = data.get("project", {}).get("version")
        return str(version) if version else None
    except Exception:
        return None


def _should_exclude(rel_path: str) -> bool:
    """Whether a tracked file should be excluded from the release zip.

    Args:
        rel_path (str): Git-tracked path relative to repo root.

    Returns:
        bool: True if excluded.
    """
    normalized = rel_path.replace("\\", "/")
    normalized_path = Path(normalized)

    # Exclude obvious local/CI artifacts even if accidentally tracked.
    exclude_globs = [
        ".venv/**",
        ".uv/**",
        ".ruff_cache/**",
        "__pycache__/**",
        "**/__pycache__/**",
        "dist/**",
        "logs/**",
        "*.pyc",
        "*.pyo",
        "*.log",
        # uv lock is useful for dev reproducibility but not required in release zip.
        "uv.lock",
        ".claude/**",
        "findings.md",
        "progress.md",
        "task_plan.md",
    ]

    if any(fnmatch.fnmatch(normalized, pat) for pat in exclude_globs):
        return True

    # Never ship real environment files, while keeping example files available.
    if normalized_path.name == ".env":
        return True

    return False


def build_release_zip(repo_root: Path) -> Path:
    """Build release zip under `dist/` from git-tracked files.

    Args:
        repo_root (Path): Repository root directory.

    Returns:
        Path: The created zip path.
    """
    version = _read_project_version(repo_root)
    ts = time.strftime("%Y%m%d-%H%M%S")
    project_name = repo_root.name.replace(" ", "-")
    base_name = project_name + (f"-v{version}" if version else "") + f"-{ts}.zip"

    dist_dir = repo_root / "dist"
    dist_dir.mkdir(parents=True, exist_ok=True)
    zip_path = dist_dir / base_name

    tracked = _run_git_ls_files(repo_root)
    included = [p for p in tracked if not _should_exclude(p)]

    # Use deterministic zip options where possible.
    compression = zipfile.ZIP_DEFLATED
    with zipfile.ZipFile(zip_path, "w", compression=compression) as zf:
        for rel in included:
            abs_path = repo_root / rel
            if not abs_path.is_file():
                # Skip if file vanished or is not a regular file.
                continue
            # Ensure zip uses forward slashes.
            arcname = rel.replace("\\", "/")
            zf.write(abs_path, arcname=arcname)

    return zip_path


def main() -> int:
    """CLI entrypoint."""
    repo_root = Path(__file__).resolve().parents[2]

    try:
        zip_path = build_release_zip(repo_root)
    except RuntimeError as exc:
        print(f"[release] ERROR: {exc}", file=sys.stderr)
        return 1

    size_mb = zip_path.stat().st_size / (1024 * 1024)
    print(f"[release] OK: {zip_path} ({size_mb:.2f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
