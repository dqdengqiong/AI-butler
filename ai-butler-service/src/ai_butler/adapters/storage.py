from __future__ import annotations

from pathlib import Path
from typing import Protocol


class ObjectStorage(Protocol):
    async def put(self, key: str, content: bytes) -> None: ...

    async def get(self, key: str) -> bytes: ...


class LocalObjectStorage:
    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def _path_for(self, key: str) -> Path:
        target = (self._root / key).resolve()
        if not target.is_relative_to(self._root):
            raise ValueError("object key escapes storage root")
        return target

    async def put(self, key: str, content: bytes) -> None:
        target = self._path_for(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    async def get(self, key: str) -> bytes:
        return self._path_for(key).read_bytes()
