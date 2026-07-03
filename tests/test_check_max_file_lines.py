"""Regression tests for the check_max_file_lines hook."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CHECK_MAX_FILE_LINES_SCRIPT_PATH = REPO_ROOT / "hooks" / "shared" / "check_max_file_lines.py"


def load_check_max_file_lines_module() -> ModuleType:
    """Load the check_max_file_lines hook module directly from disk."""
    module_spec = importlib.util.spec_from_file_location(
        "check_max_file_lines_hook", CHECK_MAX_FILE_LINES_SCRIPT_PATH
    )
    assert module_spec is not None
    assert module_spec.loader is not None
    check_max_file_lines_module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(check_max_file_lines_module)
    return check_max_file_lines_module


def run_hook(argv: list[str], cwd_path: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run the hook as a subprocess with the given argv."""
    command_parts = [
        "uv",
        "run",
        "python",
        str(CHECK_MAX_FILE_LINES_SCRIPT_PATH),
        *argv,
    ]
    return subprocess.run(
        command_parts,
        cwd=cwd_path or REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def write_file_with_lines(file_path: Path, line_count: int) -> None:
    """Write a file with exactly ``line_count`` non-empty lines."""
    file_path.write_text(
        "\n".join(f"line {index}" for index in range(line_count)) + "\n",
        encoding="utf-8",
    )


def test_single_file_under_limit_returns_zero(tmp_path: Path) -> None:
    """A file below the max-lines threshold should pass."""
    target_file = tmp_path / "short.py"
    write_file_with_lines(target_file, 3)

    result = run_hook(["--max-lines", "5", str(target_file)])

    assert result.returncode == 0
    assert "ERROR" not in result.stdout
    assert "WARNING" not in result.stdout


def test_single_file_over_limit_returns_one(tmp_path: Path) -> None:
    """A file above the max-lines threshold should fail."""
    target_file = tmp_path / "long.py"
    write_file_with_lines(target_file, 7)

    result = run_hook(["--max-lines", "5", str(target_file)])

    assert result.returncode == 1
    assert "long.py" in result.stdout
    assert "7 非空行" in result.stdout


def test_warn_only_returns_zero_but_prints_warning(tmp_path: Path) -> None:
    """The --warn-only flag should suppress the non-zero exit code."""
    target_file = tmp_path / "long.py"
    write_file_with_lines(target_file, 7)

    result = run_hook(["--max-lines", "5", "--warn-only", str(target_file)])

    assert result.returncode == 0
    assert "[WARNING]" in result.stdout
    assert "long.py" in result.stdout


def test_directory_is_recursively_expanded(tmp_path: Path) -> None:
    """Passing a directory should check all regular files inside it."""
    nested_dir = tmp_path / "nested"
    nested_dir.mkdir()
    short_file = tmp_path / "short.py"
    long_file = nested_dir / "long.py"
    write_file_with_lines(short_file, 3)
    write_file_with_lines(long_file, 12)

    result = run_hook(["--max-lines", "5", str(tmp_path)])

    assert result.returncode == 1
    assert "long.py" in result.stdout
    assert "short.py" not in result.stdout


def test_glob_filters_directory_contents(tmp_path: Path) -> None:
    """--glob should limit directory recursion to matching files."""
    py_file = tmp_path / "long.py"
    txt_file = tmp_path / "long.txt"
    write_file_with_lines(py_file, 12)
    write_file_with_lines(txt_file, 12)

    result = run_hook(["--max-lines", "5", "--glob", "*.py", str(tmp_path)])

    assert result.returncode == 1
    assert "long.py" in result.stdout
    assert "long.txt" not in result.stdout


def test_missing_path_returns_error(tmp_path: Path) -> None:
    """A non-existent path should produce a clear error and non-zero exit."""
    missing_path = tmp_path / "does-not-exist.py"

    result = run_hook(["--max-lines", "5", str(missing_path)])

    assert result.returncode == 1
    assert "does-not-exist.py" in result.stdout
    assert "Path does not exist" in result.stdout


def test_mixed_files_and_directories(tmp_path: Path) -> None:
    """A mix of files and directories should be handled together."""
    dir_path = tmp_path / "dir"
    dir_path.mkdir()
    direct_file = tmp_path / "direct_long.py"
    nested_file = dir_path / "nested_long.py"
    write_file_with_lines(direct_file, 12)
    write_file_with_lines(nested_file, 12)

    result = run_hook(["--max-lines", "5", str(direct_file), str(dir_path)])

    assert result.returncode == 1
    assert "direct_long.py" in result.stdout
    assert "nested_long.py" in result.stdout


def test_duplicate_paths_are_deduplicated(tmp_path: Path) -> None:
    """The same file passed twice (or via directory and file) is checked once."""
    target_file = tmp_path / "long.py"
    write_file_with_lines(target_file, 12)

    result = run_hook(["--max-lines", "5", str(target_file), str(tmp_path), str(target_file)])

    assert result.returncode == 1
    assert result.stdout.count("long.py") == 1


def test_expand_paths_function_directly(tmp_path: Path) -> None:
    """_expand_paths should return files and errors correctly."""
    module = load_check_max_file_lines_module()
    py_file = tmp_path / "a.py"
    txt_file = tmp_path / "a.txt"
    sub_dir = tmp_path / "sub"
    sub_dir.mkdir()
    nested_py = sub_dir / "b.py"
    write_file_with_lines(py_file, 1)
    write_file_with_lines(txt_file, 1)
    write_file_with_lines(nested_py, 1)

    files, errors = module._expand_paths(  # noqa: SLF001
        [str(py_file), str(txt_file), str(sub_dir), str(tmp_path / "missing")],
        glob_pattern="*.py",
    )

    # Explicitly passed files are always included; glob only filters directory recursion.
    assert set(files) == {py_file, txt_file, nested_py}
    assert len(errors) == 1
    assert "missing" in errors[0]


@pytest.mark.parametrize(
    ("command_parts", "expected_returncode"),
    [
        (
            [
                "uv",
                "run",
                "python",
                "hooks/shared/check_max_file_lines.py",
                "--max-lines",
                "1000",
                "--glob",
                "*.py",
                "src/backend",
            ],
            None,  # May be 0 or 1 depending on repository state; must not crash.
        ),
    ],
)
def test_argv_list_execution_does_not_crash(
    command_parts: list[str], expected_returncode: int | None
) -> None:
    """Running the new command as an argv list should not raise argparse errors."""
    result = subprocess.run(
        command_parts,
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert "unrecognized arguments" not in result.stderr
    assert "unrecognized arguments" not in result.stdout
    if expected_returncode is not None:
        assert result.returncode == expected_returncode


def test_old_find_command_tokenized_fails_with_argparse_error() -> None:
    """Tokenizing the old shell command should still fail as before."""
    import shlex

    old_command = (
        "uv run python hooks/shared/check_max_file_lines.py --max-lines 1000 "
        "$(find src/backend -name '*.py')"
    )
    command_parts = shlex.split(old_command)

    result = subprocess.run(
        command_parts,
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert result.returncode != 0
    assert "unrecognized arguments" in result.stderr
