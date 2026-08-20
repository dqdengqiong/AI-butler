from __future__ import annotations

from datetime import date
from uuid import UUID

from ai_butler.adapters.auth import AuthIdentity
from ai_butler.adapters.conversation_router import (
    ConversationRouter,
)
from ai_butler.adapters.embedding import (
    EmbeddingProvider,
)
from ai_butler.adapters.llm import LLM
from ai_butler.adapters.notification import NotificationProvider
from ai_butler.adapters.search import (
    SearchProvider,
)
from ai_butler.adapters.sms import SmsProvider
from ai_butler.adapters.vector import VectorStore
from ai_butler.api.schemas import (
    AvailabilityRequest,
    CompleteUploadRequest,
    PhoneLoginRequest,
    PhoneVerificationCodeRequest,
    PlanPreviewConfirmationRequestV1,
    PreferencesRequest,
    ProfileRequest,
    SendMessageRequest,
    TaskExecutionRequest,
    UpdateMeRequest,
    UploadIntentRequest,
)
from ai_butler.config import Settings
from ai_butler.domain.errors import ButlerError
from ai_butler.infrastructure.database import AsyncDatabase

from .auth import AuthService
from .bootstrap import BootstrapService
from .context import ButlerContext
from .conversation_queries import ConversationQueryService
from .conversation_repository import ConversationRepository
from .events import EventService
from .files import FileService
from .messages import MessageService
from .plan_previews import PlanPreviewService
from .planning import PlanningService
from .routing import RoutingService
from .runs import RunService
from .scheduler import SchedulerService
from .support import (
    ResponseFactory,
    build_embedding_provider,
    build_llm,
    build_search_provider,
    draft_tasks_for_availability,
    safe_summary,
    validate_availability_overlap,
)
from .users import UserService
from .worker import WorkerService


class ButlerService:
    """组合认证、会话、计划、知识和 Worker 能力的应用门面。"""

    def __init__(
        self,
        database: AsyncDatabase,
        settings: Settings,
        search_provider: SearchProvider | None = None,
        embedding_provider: EmbeddingProvider | None = None,
        vector_store: VectorStore | None = None,
        llm: LLM | None = None,
        sms_provider: SmsProvider | None = None,
        conversation_router: ConversationRouter | None = None,
        notification_provider: NotificationProvider | None = None,
    ) -> None:
        self._context = ButlerContext.build(
            database,
            settings,
            search_provider,
            embedding_provider,
            vector_store,
            llm,
            sms_provider,
            conversation_router,
            notification_provider,
        )
        self.database = self._context.database
        self.settings = self._context.settings
        self.search_provider = self._context.search_provider
        self.embedding_provider = self._context.embedding_provider
        self.vector_store = self._context.vector_store
        self.llm = self._context.llm
        self.availability_interpreter = self._context.availability_interpreter
        self.conversation_router = self._context.conversation_router
        self.evidence_gate = self._context.evidence_gate
        self.phone_cipher = self._context.phone_cipher

        self._events = EventService()
        self._bootstrap = BootstrapService()
        self._responses = ResponseFactory(settings)
        self._repository = ConversationRepository(self._events)
        self._routing = RoutingService(self._context, self._repository)
        self._users = UserService(self._context, self._bootstrap)
        self._auth = AuthService(self._context, self._bootstrap, self._responses)
        self._queries = ConversationQueryService(self._context)
        self._planning = PlanningService(self._context, self._users)
        self._runs = RunService(self._context, self._events, self._repository)
        self._messages = MessageService(
            self._context, self._routing, self._repository, self._events, self._responses
        )
        self._plan_previews = PlanPreviewService(self._context)
        self._worker = WorkerService(self._context, self._events, self._bootstrap)
        self._scheduler = SchedulerService(self._context)
        self._files = FileService(self._context)

    @property
    def sms_provider(self) -> SmsProvider:
        return self._context.sms_provider

    @sms_provider.setter
    def sms_provider(self, provider: SmsProvider) -> None:
        self._context.sms_provider = provider
        self._auth.sms_provider = provider

    def auth_config(self) -> dict[str, object]:
        return self._auth.auth_config()

    async def send_phone_verification_code(
        self, request: PhoneVerificationCodeRequest, idempotency_key: str
    ) -> dict[str, object]:
        return await self._auth.send_phone_verification_code(request, idempotency_key)

    async def phone_login(self, request: PhoneLoginRequest) -> dict[str, object]:
        return await self._auth.phone_login(request)

    async def wechat_login(
        self, identity: AuthIdentity, phone_value: str, device_id: str
    ) -> dict[str, object]:
        return await self._auth.wechat_login(identity, phone_value, device_id)

    async def refresh(self, refresh_token: str, device_id: str) -> dict[str, object]:
        return await self._auth.refresh(refresh_token, device_id)

    async def logout(self, user_id: UUID, refresh_token: str) -> None:
        return await self._auth.logout(user_id, refresh_token)

    async def get_me(self, user_id: UUID) -> dict[str, object]:
        return await self._users.get_me(user_id)

    async def update_me(self, user_id: UUID, request: UpdateMeRequest) -> dict[str, object]:
        return await self._users.update_me(user_id, request)

    async def get_profile(self, user_id: UUID) -> dict[str, object]:
        return await self._users.get_profile(user_id)

    async def put_profile(self, user_id: UUID, request: ProfileRequest) -> dict[str, object]:
        return await self._users.put_profile(user_id, request)

    async def get_availability(self, user_id: UUID) -> dict[str, object]:
        return await self._users.get_availability(user_id)

    async def put_availability(
        self, user_id: UUID, request: AvailabilityRequest
    ) -> dict[str, object]:
        return await self._users.put_availability(user_id, request)

    async def get_preferences(self, user_id: UUID) -> dict[str, object]:
        return await self._users.get_preferences(user_id)

    async def patch_preferences(
        self, user_id: UUID, request: PreferencesRequest
    ) -> dict[str, object]:
        return await self._users.patch_preferences(user_id, request)

    async def delete_account(self, user_id: UUID) -> dict[str, object]:
        return await self._users.delete_account(user_id)

    async def list_agent_definitions(self) -> dict[str, object]:
        return await self._users.list_agent_definitions()

    async def list_conversations(
        self, user_id: UUID, limit: int = 30, cursor: str | None = None
    ) -> dict[str, object]:
        return await self._queries.list_conversations(user_id, limit, cursor)

    async def get_conversation(self, user_id: UUID, conversation_id: UUID) -> dict[str, object]:
        return await self._queries.get_conversation(user_id, conversation_id)

    async def delete_conversation(self, user_id: UUID, conversation_id: UUID) -> None:
        return await self._queries.delete_conversation(user_id, conversation_id)

    async def list_messages(
        self, user_id: UUID, conversation_id: UUID, limit: int = 30, cursor: str | None = None
    ) -> dict[str, object]:
        return await self._queries.list_messages(user_id, conversation_id, limit, cursor)

    async def send_message(self, user_id: UUID, request: SendMessageRequest) -> dict[str, object]:
        return await self._messages.send_message(user_id, request)

    async def confirm_plan_preview(
        self,
        user_id: UUID,
        message_id: UUID,
        request: PlanPreviewConfirmationRequestV1,
        idempotency_key: str,
    ) -> dict[str, object]:
        return await self._plan_previews.confirm(user_id, message_id, request, idempotency_key)

    async def get_run(self, user_id: UUID, run_id: UUID) -> dict[str, object]:
        return await self._runs.get_run(user_id, run_id)

    async def stream_ticket(self, user_id: UUID, run_id: UUID) -> dict[str, object]:
        return await self._runs.stream_ticket(user_id, run_id)

    async def list_events(self, user_id: UUID, run_id: UUID, after: int) -> list[dict[str, object]]:
        return await self._runs.list_events(user_id, run_id, after)

    async def event_owner(self, run_id: UUID) -> UUID:
        return await self._runs.event_owner(run_id)

    async def cancel_run(self, user_id: UUID, run_id: UUID) -> dict[str, object]:
        return await self._runs.cancel_run(user_id, run_id)

    async def retry_run(
        self, user_id: UUID, run_id: UUID, expected_attempt: int, execution_policy: str = "REJECT"
    ) -> dict[str, object]:
        return await self._runs.retry_run(user_id, run_id, expected_attempt, execution_policy)

    async def dashboard(self, user_id: UUID, requested_date: date) -> dict[str, object]:
        return await self._planning.dashboard(user_id, requested_date)

    async def list_plans(self, user_id: UUID) -> dict[str, object]:
        return await self._planning.list_plans(user_id)

    async def list_goals(self, user_id: UUID) -> dict[str, object]:
        return await self._planning.list_goals(user_id)

    async def list_revisions(self, user_id: UUID, plan_id: UUID) -> dict[str, object]:
        return await self._planning.list_revisions(user_id, plan_id)

    async def get_revision(
        self, user_id: UUID, plan_id: UUID, revision_id: UUID
    ) -> dict[str, object]:
        return await self._planning.get_revision(user_id, plan_id, revision_id)

    async def get_plan(self, user_id: UUID, plan_id: UUID) -> dict[str, object]:
        return await self._planning.get_plan(user_id, plan_id)

    async def delete_plan(self, user_id: UUID, plan_id: UUID, idempotency_key: str) -> None:
        return await self._planning.delete_plan(user_id, plan_id, idempotency_key)

    async def list_tasks(
        self, user_id: UUID, date_from: date | None, date_to: date | None
    ) -> dict[str, object]:
        return await self._planning.list_tasks(user_id, date_from, date_to)

    async def get_task(self, user_id: UUID, task_id: UUID) -> dict[str, object]:
        return await self._planning.get_task(user_id, task_id)

    async def execute_task(
        self, user_id: UUID, task_id: UUID, request: TaskExecutionRequest
    ) -> dict[str, object]:
        return await self._planning.execute_task(user_id, task_id, request)

    async def create_upload_intent(
        self, user_id: UUID, request: UploadIntentRequest
    ) -> dict[str, object]:
        return await self._files.create_upload_intent(user_id, request)

    async def store_local_upload(self, file_id: UUID, content: bytes) -> None:
        return await self._files.store_local_upload(file_id, content)

    async def read_local_file(self, file_id: UUID) -> tuple[bytes, str, str]:
        return await self._files.read_local_file(file_id)

    async def complete_upload(
        self, user_id: UUID, file_id: UUID, request: CompleteUploadRequest
    ) -> dict[str, object]:
        return await self._files.complete_upload(user_id, file_id, request)

    async def get_file(self, user_id: UUID, file_id: UUID) -> dict[str, object]:
        return await self._files.get_file(user_id, file_id)

    async def file_download(self, user_id: UUID, file_id: UUID) -> dict[str, object]:
        return await self._files.file_download(user_id, file_id)

    async def delete_file(self, user_id: UUID, file_id: UUID) -> dict[str, object]:
        return await self._files.delete_file(user_id, file_id)

    async def get_document_access(self, user_id: UUID, document_id: UUID) -> dict[str, object]:
        return await self._files.get_document_access(user_id, document_id)

    async def list_files(self, user_id: UUID) -> dict[str, object]:
        return await self._files.list_files(user_id)

    async def get_citation(self, user_id: UUID, citation_id: UUID) -> dict[str, object]:
        return await self._files.get_citation(user_id, citation_id)

    async def worker_poll_once(self, worker_id: UUID) -> bool:
        return await self._worker.worker_poll_once(worker_id)

    async def scheduler_poll_once(self) -> bool:
        return await self._scheduler.scheduler_poll_once()

    async def _fail_run(self, run_id: UUID, error: ButlerError) -> None:
        return await self._worker.fail_run(run_id, error)

    _conversation_response = staticmethod(ConversationQueryService._conversation_response)
    _specialist_response = staticmethod(ConversationQueryService._specialist_response)
    _build_search_provider = staticmethod(build_search_provider)
    _build_embedding_provider = staticmethod(build_embedding_provider)
    _build_llm = staticmethod(build_llm)
    _safe_summary = staticmethod(safe_summary)
    _draft_tasks_for_availability = staticmethod(draft_tasks_for_availability)
    _validate_availability_overlap = staticmethod(validate_availability_overlap)
