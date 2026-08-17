"""应用服务共享且显式注入的基础设施依赖。"""

from __future__ import annotations

from dataclasses import dataclass

from ai_butler.adapters.conversation_router import (
    ConversationRouter,
    FakeConversationRouter,
    LLMConversationRouter,
)
from ai_butler.adapters.embedding import EmbeddingProvider
from ai_butler.adapters.llm import LLM
from ai_butler.adapters.search import SearchProvider
from ai_butler.adapters.sms import MockSmsProvider, SmsProvider
from ai_butler.adapters.vector import QdrantVectorStore, VectorStore
from ai_butler.agent.availability import AvailabilityInterpreter
from ai_butler.agent.evidence import EvidenceGate
from ai_butler.config import Settings
from ai_butler.infrastructure.database import AsyncDatabase
from ai_butler.phone import PhoneCipher

from .support import build_embedding_provider, build_llm, build_search_provider


@dataclass(slots=True)
class ButlerContext:
    """能力模块共享的依赖集合；不包含能力模块注册表或隐式查找。"""

    database: AsyncDatabase
    settings: Settings
    search_provider: SearchProvider
    embedding_provider: EmbeddingProvider
    vector_store: VectorStore
    availability_interpreter: AvailabilityInterpreter
    conversation_router: ConversationRouter
    evidence_gate: EvidenceGate
    sms_provider: SmsProvider
    phone_cipher: PhoneCipher

    @classmethod
    def build(
        cls,
        database: AsyncDatabase,
        settings: Settings,
        search_provider: SearchProvider | None = None,
        embedding_provider: EmbeddingProvider | None = None,
        vector_store: VectorStore | None = None,
        llm: LLM | None = None,
        sms_provider: SmsProvider | None = None,
        conversation_router: ConversationRouter | None = None,
    ) -> ButlerContext:
        resolved_search = search_provider or build_search_provider(settings)
        resolved_embedding = embedding_provider or build_embedding_provider(settings)
        resolved_vector = vector_store or QdrantVectorStore(
            settings.qdrant_url,
            settings.qdrant_collection,
            settings.embedding_dimensions,
        )
        resolved_llm = llm or build_llm(settings)
        resolved_router = conversation_router or (
            FakeConversationRouter()
            if settings.llm_provider == "fake"
            else LLMConversationRouter(resolved_llm)
        )
        if sms_provider is None and settings.sms_provider != "mock":
            raise ValueError("unsupported sms provider")
        return cls(
            database=database,
            settings=settings,
            search_provider=resolved_search,
            embedding_provider=resolved_embedding,
            vector_store=resolved_vector,
            availability_interpreter=AvailabilityInterpreter(resolved_llm),
            conversation_router=resolved_router,
            evidence_gate=EvidenceGate(tuple(settings.official_source_domains)),
            sms_provider=sms_provider or MockSmsProvider(),
            phone_cipher=PhoneCipher(settings.phone_encryption_secret),
        )
