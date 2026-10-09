"""File storage for uploads and exports.

Callers work with storage keys (e.g. `uploads/<uuid>.csv`), never raw paths, so `LocalStorage`
(a shared volume) can later be swapped for an object store without touching callers.
Methods are blocking; call them from a thread (`asyncio.to_thread`) in async code.
"""

import os
import uuid
from pathlib import Path
from typing import BinaryIO, Protocol


class Storage(Protocol):
    def new_key(self, prefix: str, suffix: str) -> str: ...
    def open_write(self, key: str) -> BinaryIO: ...
    def open_read(self, key: str) -> BinaryIO: ...
    def size(self, key: str) -> int: ...
    def exists(self, key: str) -> bool: ...
    def delete(self, key: str) -> None: ...
    def move(self, src: str, dst: str) -> None: ...


class LocalStorage:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise ValueError(f"invalid storage key: {key!r}")
        return path

    def new_key(self, prefix: str, suffix: str) -> str:
        return f"{prefix}/{uuid.uuid4().hex}{suffix}"

    def open_write(self, key: str) -> BinaryIO:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path.open("wb")

    def open_read(self, key: str) -> BinaryIO:
        return self._path(key).open("rb")

    def size(self, key: str) -> int:
        return self._path(key).stat().st_size

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def delete(self, key: str) -> None:
        try:
            os.remove(self._path(key))
        except FileNotFoundError:
            pass

    def move(self, src: str, dst: str) -> None:
        """Atomically replace `dst` with `src` (used to publish finished files)."""
        os.replace(self._path(src), self._path(dst))
