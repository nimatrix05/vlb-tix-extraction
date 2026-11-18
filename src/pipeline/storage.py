"""Storage abstraction. LocalStorage is the MVP; an S3-compatible backend can implement the same
interface later without touching the collector, batch or export code."""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Protocol

from .security import contained_path


class Storage(Protocol):
    def put(self, tmp_file: Path, isbn13: str, filename: str) -> str: ...
    def exists(self, key: str) -> bool: ...
    def copy_to(self, key: str, dest: Path) -> None: ...
    def local_path(self, key: str) -> Path: ...


class LocalStorage:
    def __init__(self, base: Path):
        self.base = base.resolve()
        self.base.mkdir(parents=True, exist_ok=True)

    def put(self, tmp_file: Path, isbn13: str, filename: str) -> str:
        dest = contained_path(self.base, isbn13, filename)
        dest.parent.mkdir(parents=True, exist_ok=True)
        # The DB is the source of truth for allocated names; a file already at this path can only
        # be an orphan from an interrupted run, so replacing it is safe.
        os.replace(tmp_file, dest)
        try:
            os.chmod(dest, 0o640)  # no effect on Windows ACLs; meaningful on POSIX
        except OSError:
            pass
        return f"{isbn13}/{filename}"

    def _resolve(self, key: str) -> Path:
        isbn, name = key.split("/", 1)
        return contained_path(self.base, isbn, name)

    def exists(self, key: str) -> bool:
        return self._resolve(key).exists()

    def copy_to(self, key: str, dest: Path) -> None:
        shutil.copyfile(self._resolve(key), dest)

    def local_path(self, key: str) -> Path:
        return self._resolve(key)
