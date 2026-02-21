from __future__ import annotations

import os
import shutil
import stat
import tempfile
from pathlib import Path


def choose_backup_path(config_path: Path) -> Path:
    first = config_path.with_name(f"{config_path.name}.old")
    if not first.exists():
        return first

    idx = 1
    while True:
        candidate = config_path.with_name(f"{config_path.name}.old.{idx}")
        if not candidate.exists():
            return candidate
        idx += 1


def write_backup(config_path: Path) -> Path:
    backup_path = choose_backup_path(config_path)
    shutil.copy2(config_path, backup_path)
    return backup_path


def atomic_write(path: Path, content: str, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    tmp_path = Path(tmp_name)

    try:
        with os.fdopen(fd, "w", encoding="utf8") as tmp_file:
            tmp_file.write(content)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())

        if mode is not None:
            os.chmod(tmp_path, stat.S_IMODE(mode))

        os.replace(tmp_path, path)
        _fsync_parent_dir(path)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise


def _fsync_parent_dir(path: Path) -> None:
    # Best-effort directory sync for durable rename on POSIX filesystems.
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        dir_fd = os.open(str(path.parent), flags)
    except OSError:
        return
    try:
        try:
            os.fsync(dir_fd)
        except OSError:
            # The file is already atomically replaced; treat directory fsync
            # failure as non-fatal best-effort durability.
            return
    finally:
        os.close(dir_fd)
