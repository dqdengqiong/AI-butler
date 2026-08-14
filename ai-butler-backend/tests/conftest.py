from __future__ import annotations

from typing import Literal

import pytest
from fastapi.testclient import TestClient

from ai_butler.api.app import create_app


class StubDatabase:
    def __init__(self, status: Literal["up", "down"] = "up") -> None:
        self.status = status
        self.closed = False

    async def ping(self) -> bool:
        return self.status == "up"

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def database() -> StubDatabase:
    return StubDatabase()


@pytest.fixture
def client(database: StubDatabase) -> TestClient:
    with TestClient(create_app(database=database)) as test_client:
        yield test_client
