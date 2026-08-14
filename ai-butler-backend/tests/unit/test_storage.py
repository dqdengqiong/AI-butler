from pathlib import Path

import pytest

from ai_butler.adapters.storage import LocalObjectStorage


async def test_local_storage_round_trip(tmp_path: Path) -> None:
    storage = LocalObjectStorage(tmp_path)
    await storage.put("users/a/file.txt", b"content")
    assert await storage.get("users/a/file.txt") == b"content"


async def test_local_storage_rejects_path_escape(tmp_path: Path) -> None:
    storage = LocalObjectStorage(tmp_path)
    with pytest.raises(ValueError, match="escapes storage root"):
        await storage.put("../secret", b"content")
