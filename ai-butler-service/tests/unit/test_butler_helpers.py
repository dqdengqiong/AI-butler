from __future__ import annotations

from datetime import UTC, date, datetime, time
from uuid import uuid4

import pytest
from pydantic import ValidationError

from ai_butler.agent.availability import AvailabilityInterpretationV1, AvailabilityWindowV1
from ai_butler.api.schemas import (
    ApprovalDecisionRequest,
    AttachmentInput,
    AvailabilityRequest,
    AvailabilityWindow,
    SelectionInput,
    SendMessageRequest,
)
from ai_butler.application.butler import (
    ButlerService,
    _decode_cursor,
    _encode_cursor,
    _message_request_hash,
)
from ai_butler.config import Settings
from ai_butler.domain.errors import ButlerError


@pytest.mark.parametrize(
    "override",
    [
        {"phone_lookup_secret": "short"},  # pragma: allowlist secret
        {"sms_provider": "real"},
        {"sms_code_length": 3, "sms_mock_code": "123"},
        {"sms_mock_code": "abcdef"},
        {"sms_max_attempts": 0},
        {"sms_code_ttl_seconds": 59},
        {"sms_resend_seconds": 9},
        {"sms_phone_hourly_limit": 0},
        {"conversation_topic_idle_seconds": 3599},
        {"conversation_topic_confidence": 0.49},
    ],
)
def test_security_settings_reject_unsafe_values(override: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Settings(**override)  # type: ignore[arg-type]


def test_request_schema_rejects_incomplete_ranges_and_edit() -> None:
    today = date(2026, 8, 16)
    with pytest.raises(ValidationError, match="provided together"):
        AvailabilityWindow(
            day_of_week=1,
            start_time=time(8),
            available_minutes=60,
            effective_from=today,
        )
    with pytest.raises(ValidationError, match="after start_time"):
        AvailabilityWindow(
            day_of_week=1,
            start_time=time(9),
            end_time=time(8),
            available_minutes=60,
            effective_from=today,
        )
    with pytest.raises(ValidationError, match="must not precede"):
        AvailabilityWindow(
            day_of_week=1,
            available_minutes=60,
            effective_from=today,
            effective_to=date(2026, 8, 15),
        )
    with pytest.raises(ValidationError, match="feedback is required"):
        ApprovalDecisionRequest(
            approval_id=uuid4(),
            expected_approval_version=1,
            action="EDIT",
            feedback=" ",
        )


def test_cursor_round_trip_and_rejects_malformed_shapes() -> None:
    values = ["1", datetime.now(UTC).isoformat(), str(uuid4())]
    assert _decode_cursor(_encode_cursor(*values), 3) == values

    for cursor, parts in (
        ("not-base64", 3),
        (_encode_cursor("only-one"), 3),
        ("WzEsMl0", 2),
    ):
        with pytest.raises(ButlerError) as error:
            _decode_cursor(cursor, parts)
        assert error.value.code == "INVALID_CURSOR"


def test_message_hash_is_canonical_for_content_and_attachment_order() -> None:
    first_file = uuid4()
    second_file = uuid4()
    card_id = uuid4()
    first = SendMessageRequest(
        client_message_id="message-one",
        content=" 继续计划 ",
        attachments=[
            AttachmentInput(file_id=second_file, position=2),
            AttachmentInput(file_id=first_file, position=1),
        ],
        selection=SelectionInput(
            card_id=card_id,
            action_id="confirm",
            selected_option_ids=["morning"],
        ),
    )
    reordered = first.model_copy(
        update={
            "content": "继续计划",
            "attachments": list(reversed(first.attachments)),
        }
    )
    changed = first.model_copy(update={"content": "修改计划"})

    assert _message_request_hash(first) == _message_request_hash(reordered)
    assert _message_request_hash(first) != _message_request_hash(changed)


def test_conversation_projection_includes_specialist_run_and_preview() -> None:
    now = datetime.now(UTC)
    conversation_id = uuid4()
    run_id = uuid4()
    service = object.__new__(ButlerService)
    projected = service._conversation_response(
        {
            "id": conversation_id,
            "title": "备考计划",
            "status": "CURRENT",
            "specialist_code": "CIVIL_SERVICE_EXAM",
            "specialist_name": "考公助理",
            "specialist_metadata": {"icon": "exam"},
            "active_run_id": run_id,
            "active_run_status": "AWAITING_INPUT",
            "last_message_content": "这是一段会被限制长度的消息" * 20,
            "last_message_created_at": now,
            "last_message_at": now,
            "created_at": now,
            "updated_at": now,
        }
    )

    assert projected["specialist"] == {
        "code": "CIVIL_SERVICE_EXAM",
        "name": "考公助理",
        "icon": "exam",
    }
    assert projected["active_run"] == {"id": run_id, "status": "AWAITING_INPUT"}
    assert len(projected["last_message"]["content"]) == 120  # type: ignore[index]
    assert ButlerService._specialist_response(None) is None
    assert ButlerService._specialist_response(
        {"code": "CIVIL_SERVICE_EXAM", "name": "考公助理", "icon": "exam"}
    ) == {"code": "CIVIL_SERVICE_EXAM", "name": "考公助理", "icon": "exam"}


def test_provider_factories_summaries_and_draft_scheduling() -> None:
    placeholder_credential = "credential-placeholder"
    assert ButlerService._build_search_provider(Settings()).__class__.__name__ == (
        "FakeSearchProvider"
    )
    assert ButlerService._build_embedding_provider(Settings()).__class__.__name__ == (
        "FakeEmbeddingProvider"
    )
    assert ButlerService._build_llm(Settings()).__class__.__name__ == "FakeLLM"
    assert (
        ButlerService._build_search_provider(
            Settings(search_provider="tavily", tavily_api_key=placeholder_credential)
        ).__class__.__name__
        == "TavilySearchProvider"
    )
    assert (
        ButlerService._build_embedding_provider(
            Settings(embedding_model="text-embedding", llm_api_key=placeholder_credential)
        ).__class__.__name__
        == "OpenAICompatibleEmbeddingProvider"
    )
    assert (
        ButlerService._build_llm(
            Settings(llm_provider="openai-compatible", llm_api_key=placeholder_credential)
        ).__class__.__name__
        == "OpenAICompatibleLLM"
    )
    with pytest.raises(ValueError, match="unsupported search provider"):
        ButlerService._build_search_provider(Settings(search_provider="unknown"))
    with pytest.raises(ValueError, match="unsupported llm provider"):
        ButlerService._build_llm(Settings(llm_provider="unknown"))
    assert ButlerService._safe_summary("  两行\n 请求  ") == "用户提交了 5 个字符的请求"

    interpretation = AvailabilityInterpretationV1(
        status="COMPLETE",
        weekly_minutes=20,
        windows=(AvailabilityWindowV1(day_of_week=1, available_minutes=20),),
    )
    tasks = ButlerService._draft_tasks_for_availability(date(2026, 8, 16), interpretation)
    assert [item["day_offset"] for item in tasks] == [1, 8, 15]
    assert all(item["minutes"] == 20 for item in tasks)

    no_days = AvailabilityInterpretationV1(
        status="COMPLETE",
        weekly_minutes=60,
        excluded_days=(1, 2, 3, 4, 5, 6, 7),
    )
    assert ButlerService._draft_tasks_for_availability(date(2026, 8, 16), no_days) == []


def test_availability_overlap_validation() -> None:
    base = date(2026, 8, 16)
    valid = AvailabilityRequest(
        expected_version=1,
        windows=[
            AvailabilityWindow(
                day_of_week=1,
                start_time=time(8),
                end_time=time(9),
                available_minutes=60,
                effective_from=base,
            ),
            AvailabilityWindow(
                day_of_week=2,
                start_time=time(8, 30),
                end_time=time(9, 30),
                available_minutes=60,
                effective_from=base,
            ),
        ],
    )
    ButlerService._validate_availability_overlap(valid)

    default_overlap = AvailabilityRequest(
        expected_version=1,
        windows=[
            AvailabilityWindow(day_of_week=1, available_minutes=30, effective_from=base),
            AvailabilityWindow(day_of_week=1, available_minutes=60, effective_from=base),
        ],
    )
    with pytest.raises(ButlerError, match="重复默认项"):
        ButlerService._validate_availability_overlap(default_overlap)

    timed_overlap = AvailabilityRequest(
        expected_version=1,
        windows=[
            AvailabilityWindow(
                day_of_week=1,
                start_time=time(8),
                end_time=time(10),
                available_minutes=120,
                effective_from=base,
            ),
            AvailabilityWindow(
                day_of_week=1,
                start_time=time(9),
                end_time=time(11),
                available_minutes=120,
                effective_from=base,
            ),
        ],
    )
    with pytest.raises(ButlerError, match="不能重叠"):
        ButlerService._validate_availability_overlap(timed_overlap)
