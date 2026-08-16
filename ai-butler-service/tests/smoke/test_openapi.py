from ai_butler.api.app import create_app


def test_openapi_contains_health_routes() -> None:
    paths = create_app().openapi()["paths"]
    assert "/health/live" in paths
    assert "/health/ready" in paths


def test_openapi_exposes_multi_conversation_contract_only() -> None:
    paths = create_app().openapi()["paths"]
    assert "/v1/auth/config" in paths
    assert "/v1/auth/phone/verification-codes" in paths
    assert "/v1/auth/phone/login" in paths
    assert "/v1/auth/wechat/login" in paths
    assert "/v1/agent-definitions" in paths
    assert "/v1/conversations" in paths
    assert "/v1/conversations/{conversation_id}/messages" in paths
    assert paths["/v1/conversations"].get("post") is None
    assert paths["/v1/conversations/{conversation_id}/messages"].get("post") is None
    assert "/v1/messages" in paths
    assert "/v1/chat" not in paths
    assert "/v1/chat/messages" not in paths
