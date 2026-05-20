"""File archiving utilities with incremental backup support via GNU tar."""

import subprocess
from pathlib import Path


def archive_directory(
    source_dir: Path,
    output_path: Path,
    snapshot_file: Path,
    is_full: bool,
) -> None:
    """Create a tar.gz archive of a directory, supporting incremental backups.

    Uses GNU tar's ``--listed-incremental`` mechanism:
    - Full backup resets the snapshot file and archives everything.
    - Incremental backup archives only files changed since the last snapshot.

    Args:
        source_dir: Directory to archive.
        output_path: Path to write the resulting ``.tar.gz`` file.
        snapshot_file: Path to the tar snapshot file that tracks incremental state.
        is_full: If ``True``, reset the snapshot and perform a full backup.
    """
    if is_full and snapshot_file.exists():
        snapshot_file.unlink()

    cmd: list[str] = [
        "tar",
        "-czf",
        str(output_path),
        "--listed-incremental",
        str(snapshot_file),
    ]
    cmd.append(str(source_dir))
    subprocess.run(cmd, check=True)
