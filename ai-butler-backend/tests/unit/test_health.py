from fastapi.testclient import TestClient
from tests.conftest import StubDatabase

from ai_butler.api.app import create_app


def test_live(client: TestClient) -> None:
    response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_when_postgres_is_up(client: TestClient) -> None:
    response = client.get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "checks": {"postgres": "up"}}


def test_ready_when_postgres_is_down() -> None:
    with TestClient(create_app(database=StubDatabase("down"))) as client:
        response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "unready", "checks": {"postgres": "down"}}
