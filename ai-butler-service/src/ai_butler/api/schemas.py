"""AI 管家公共 HTTP 与 SSE 边界模型。"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, Field, StringConstraints, model_validator

from ai_butler.agent.availability import DailyAvailabilityV1

NonEmpty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class ConsentInput(BaseModel):
    terms_version: NonEmpty
    privacy_version: NonEmpty
    accepted_at: datetime


class WechatLoginRequest(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    login_code: NonEmpty
    phone_code: NonEmpty
    provider: Literal["WECHAT_MINIAPP", "WECHAT_MOCK"] = "WECHAT_MINIAPP"
    device_id: Annotated[str, StringConstraints(max_length=128)]
    consent: ConsentInput


class AuthConfigResponse(BaseModel):
    sms_verification_enabled: bool
    sms_code_length: int = Field(ge=4, le=8)
    sms_code_ttl_seconds: int = Field(ge=60, le=900)
    sms_resend_seconds: int = Field(ge=10, le=300)


class PhoneVerificationCodeRequest(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    phone: Annotated[str, StringConstraints(strip_whitespace=True, min_length=11, max_length=14)]
    device_id: Annotated[str, StringConstraints(min_length=1, max_length=128)]


class PhoneVerificationCodeResponse(BaseModel):
    challenge_id: UUID
    expires_in: int
    resend_after: int


class PhoneLoginRequest(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    phone: Annotated[str, StringConstraints(strip_whitespace=True, min_length=11, max_length=14)]
    verification_challenge_id: UUID | None = None
    verification_code: Annotated[
        str | None, StringConstraints(strip_whitespace=True, max_length=8)
    ] = None
    device_id: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    consent: ConsentInput


class RefreshRequest(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    refresh_token: NonEmpty
    device_id: Annotated[str, StringConstraints(max_length=128)]


class LogoutRequest(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    refresh_token: NonEmpty


class UserResponse(BaseModel):
    id: UUID
    nickname: str | None
    avatar_url: str | None = None
    locale: str
    timezone: str
    status: str
    created_at: datetime
    is_new_user: bool | None = None


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["Bearer"] = "Bearer"
    expires_in: int
    refresh_token: str
    refresh_expires_in: int
    user: UserResponse


class UpdateMeRequest(BaseModel):
    nickname: Annotated[str | None, StringConstraints(max_length=64)] = None
    avatar_file_id: UUID | None = None
    locale: Annotated[str | None, StringConstraints(max_length=16)] = None
    timezone: Annotated[str | None, StringConstraints(max_length=64)] = None


class ProfileRequest(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    expected_version: int = Field(ge=1)
    education_level: str | None = None
    major: str | None = None
    region_code: str | None = None
    current_level: Literal["BEGINNER", "BASIC", "INTERMEDIATE", "ADVANCED"] | None = None
    existing_material_file_ids: list[UUID] = Field(default_factory=list, max_length=20)


class AvailabilityWindow(BaseModel):
    day_of_week: int | None = Field(default=None, ge=1, le=7)
    start_time: time | None = None
    end_time: time | None = None
    available_minutes: int = Field(ge=1, le=1440)
    effective_from: date
    effective_to: date | None = None

    @model_validator(mode="after")
    def validate_window(self) -> AvailabilityWindow:
        if (self.start_time is None) != (self.end_time is None):
            raise ValueError("start_time and end_time must be provided together")
        if (
            self.start_time is not None
            and self.end_time is not None
            and self.end_time <= self.start_time
        ):
            raise ValueError("end_time must be after start_time")
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise ValueError("effective_to must not precede effective_from")
        return self


class AvailabilityRequest(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    expected_version: int = Field(ge=1)
    windows: list[AvailabilityWindow] = Field(max_length=50)


class ReminderSettings(BaseModel):
    enabled: bool = True
    channels: list[Literal["IN_APP", "WECHAT"]] = Field(default=["IN_APP"])
    advance_minutes: int = Field(default=15, ge=0, le=1440)


class PreferencesRequest(BaseModel):
    expected_version: int = Field(ge=1)
    task_reminder: ReminderSettings


class DeleteAccountRequest(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    confirmation: Literal["DELETE_MY_ACCOUNT"]


class AttachmentInput(BaseModel):
    file_id: UUID
    position: int = Field(ge=0, le=8)


class SendMessageRequest(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    client_message_id: Annotated[str, StringConstraints(min_length=8, max_length=128)]
    target_conversation_id: UUID | None = None
    specialist_code: Annotated[str | None, StringConstraints(max_length=64)] = None
    context_policy: Literal["AUTO", "CONTINUE_CURRENT", "ARCHIVE_AND_START"] = "AUTO"
    execution_policy: Literal["REJECT", "CANCEL_OTHER"] = "REJECT"
    content: Annotated[str, StringConstraints(max_length=20_000)] = ""
    attachments: list[AttachmentInput] = Field(default_factory=list, max_length=9)

    @model_validator(mode="after")
    def require_content(self) -> SendMessageRequest:
        if not self.content.strip() and not self.attachments:
            raise ValueError("content or attachments is required")
        return self


class PlanPreviewConfirmationRequestV1(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    expected_preview_hash: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class AgentStarterPromptResponse(BaseModel):
    label: str
    content: str
    behavior: Literal["SEND_MESSAGE", "FILL_COMPOSER"] = "FILL_COMPOSER"


class AgentDefinitionResponse(BaseModel):
    code: str
    name: str
    description: str
    icon: str
    availability: Literal["AVAILABLE", "COMING_SOON"]
    welcome_message: str
    starter_prompts: list[AgentStarterPromptResponse]


class AgentDefinitionListResponse(BaseModel):
    items: list[AgentDefinitionResponse]


class ConversationSpecialistResponse(BaseModel):
    code: str
    name: str
    icon: str


class ActiveRunResponse(BaseModel):
    id: UUID
    status: str


class LastMessagePreviewResponse(BaseModel):
    content: str
    created_at: datetime


class ConversationResponse(BaseModel):
    id: UUID
    title: str
    status: Literal["CURRENT", "ARCHIVED"]
    specialist: ConversationSpecialistResponse | None
    last_message: LastMessagePreviewResponse | None
    last_message_at: datetime | None
    active_run: ActiveRunResponse | None
    created_at: datetime
    updated_at: datetime


class ConversationListResponse(BaseModel):
    items: list[ConversationResponse]
    next_cursor: str | None
    has_more: bool


class PlanPreviewTaskV1(BaseModel):
    task_key: str
    title: str
    scheduled_date: date
    expected_minutes: int = Field(gt=0)
    priority: int = Field(ge=1, le=5)


class PlanPreviewPlanV1(BaseModel):
    title: str
    objective_summary: str
    weekly_minutes: int = Field(gt=0)
    start_date: date
    end_date: date
    stages: list[dict[str, object]] = Field(default_factory=list)
    tasks: list[PlanPreviewTaskV1] = Field(default_factory=list)


class PlanPreviewCardPayloadV1(BaseModel):
    status: Literal["READY", "CONFIRMED", "SUPERSEDED", "DISMISSED", "EXPIRED"]
    operation: Literal["CREATE", "ADJUST"]
    title: str
    plan: PlanPreviewPlanV1
    total_weekly_minutes: int = Field(gt=0)
    available_weekly_minutes: int = Field(gt=0)
    preview_hash: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    generated_at: datetime
    expires_at: datetime
    target_plan_id: UUID | None = None
    expected_current_revision_id: UUID | None = None
    availability: dict[str, object]
    daily_availability: list[DailyAvailabilityV1] = Field(default_factory=list)
    scenario_code: str
    scenario_fields: dict[str, str]
    evidence: list[dict[str, object]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class PlanPreviewCardV1(BaseModel):
    """确认前只存在于聊天消息中的计划预览，不引用未落库业务实体。"""

    schema_version: Literal["1.0"]
    card_id: UUID
    card_type: Literal["PlanPreviewCard"]
    entity_refs: dict[str, object] = Field(default_factory=dict)
    payload: PlanPreviewCardPayloadV1
    actions: list[dict[str, object]]


class CardCollection(BaseModel):
    cards: list[PlanPreviewCardV1 | dict[str, object]] = Field(default_factory=list)


class MessageResponse(BaseModel):
    id: UUID
    role: Literal["USER", "ASSISTANT", "SYSTEM_EVENT"]
    status: str
    content: str
    cards: CardCollection
    agent_run_id: UUID | None
    created_at: datetime


class MessageListResponse(BaseModel):
    items: list[MessageResponse]
    next_cursor: str | None
    has_more: bool


class AcceptedMessageResponse(BaseModel):
    id: UUID
    status: str


class AcceptedRunResponse(BaseModel):
    id: UUID
    status: str
    execution_mode: Literal["START"]
    attempt: int


class RunStreamResponse(BaseModel):
    events_url: str
    ticket: str
    expires_at: datetime
    last_sequence: int


class ConversationTransitionResponse(BaseModel):
    kind: Literal["CONTINUED", "CREATED", "RESUMED"]
    archived_conversation_id: UUID | None = None


class SendMessageResponse(BaseModel):
    schema_version: Literal["1.0"]
    conversation_id: UUID
    transition: ConversationTransitionResponse
    user_message: AcceptedMessageResponse
    assistant_message: AcceptedMessageResponse
    run: AcceptedRunResponse
    stream: RunStreamResponse


class RetryRunRequest(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    expected_attempt: int = Field(ge=0)
    execution_policy: Literal["REJECT", "CANCEL_OTHER"] = "REJECT"


class PlanConfirmationResponseV1(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    preview_message_id: UUID
    plan_id: UUID
    revision_id: UUID
    task_ids: list[UUID]
    status: Literal["CONFIRMED"] = "CONFIRMED"


class TaskExecutionRequest(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    client_execution_id: Annotated[str, StringConstraints(min_length=8, max_length=128)]
    result: Literal["COMPLETED", "PARTIAL", "SKIPPED"]
    duration_minutes: int | None = Field(default=None, ge=0, le=1440)
    feedback: Annotated[str | None, StringConstraints(max_length=4000)] = None
    outcome_data: dict[str, object] = Field(default_factory=dict)
    occurred_at: datetime


class UploadIntentRequest(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    purpose: Literal["AVATAR", "STUDY_MATERIAL", "CHAT_ATTACHMENT"]
    filename: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    declared_mime_type: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    size_bytes: int = Field(gt=0, le=20 * 1024 * 1024)
    sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class CompleteUploadRequest(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class CitationAccessV1(BaseModel):
    """来源访问方式；私有文件 URL 短期有效，客户端不得持久化。"""

    type: Literal["EXTERNAL_URL", "SIGNED_FILE", "UNAVAILABLE"]
    url: str | None
    expires_at: datetime | None


class CitationResponseV1(BaseModel):
    """面向客户端的不可变引用快照，不暴露 Claim 或供应商内部标识。"""

    id: UUID
    source_type: Literal["WEB", "PRIVATE_FILE", "KNOWLEDGE"]
    title: str
    source_organization: str | None
    domain: str | None
    published_at: datetime | None
    retrieved_at: datetime
    evidence_excerpt: str | None
    access: CitationAccessV1
