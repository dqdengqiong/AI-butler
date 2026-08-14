from uuid import UUID

import pytest

from ai_butler.security import (
    InvalidTokenError,
    ResourceNotFoundError,
    issue_access_token,
    issue_refresh_token,
    issue_signed_ticket,
    qdrant_user_filter,
    refresh_token_session_id,
    require_owner,
    token_hmac,
    verify_access_token,
    verify_signed_ticket,
)

USER_A = UUID("00000000-0000-4000-8000-000000000001")
USER_B = UUID("00000000-0000-4000-8000-000000000002")


def test_owner_can_access_resource() -> None:
    require_owner(USER_A, USER_A)


def test_cross_user_access_is_rejected() -> None:
    with pytest.raises(ResourceNotFoundError, match="resource not found"):
        require_owner(USER_A, USER_B)


def test_qdrant_filter_is_derived_from_authenticated_user() -> None:
    assert qdrant_user_filter(USER_A) == {
        "must": [{"key": "tenant_id", "match": {"value": str(USER_A)}}]
    }


def test_access_token_round_trip_and_expiry() -> None:
    session_id = UUID("00000000-0000-4000-8000-000000000010")
    token = issue_access_token(USER_A, session_id, "synthetic-secret", 60, now=100)
    claims = verify_access_token(token, "synthetic-secret", now=120)
    assert claims.user_id == USER_A
    assert claims.session_id == session_id
    with pytest.raises(InvalidTokenError):
        verify_access_token(token, "synthetic-secret", now=160)


def test_refresh_token_is_random_and_only_hmac_is_persistable() -> None:
    session_id = UUID("00000000-0000-4000-8000-000000000010")
    first = issue_refresh_token(session_id)
    second = issue_refresh_token(session_id)
    assert first != second
    assert refresh_token_session_id(first) == session_id
    assert token_hmac(first, "synthetic-secret") != token_hmac(second, "synthetic-secret")


def test_signed_ticket_is_bound_to_resource() -> None:
    ticket = issue_signed_ticket(USER_A, "synthetic-secret", 60)
    verify_signed_ticket(ticket, USER_A, "synthetic-secret")
    with pytest.raises(InvalidTokenError):
        verify_signed_ticket(ticket, USER_B, "synthetic-secret")
