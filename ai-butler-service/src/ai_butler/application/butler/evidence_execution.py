from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ai_butler.agent.evidence import AnswerSegmentV1, NumberedEvidence, RagAnswerV1

from .bootstrap import BootstrapService
from .completion import CompletionService
from .context import ButlerContext
from .events import EventService
from .shared import (
    PUBLIC_CHUNK_ID,
)


class EvidenceExecutionService:
    def __init__(
        self,
        context: ButlerContext,
        events: EventService,
        completion: CompletionService,
        bootstrap: BootstrapService,
    ) -> None:
        self.evidence_gate = context.evidence_gate
        self._append_event = events._append_event
        self._complete_run = completion._complete_run
        self._ensure_synthetic_source = bootstrap._ensure_synthetic_source

    async def _complete_rag_run(
        self,
        connection: AsyncConnection,
        run: dict[str, Any],
        evidence: tuple[NumberedEvidence, ...],
    ) -> None:
        """生成确定性 RAG 回答，并在同一事务保存事实、引用和 SourceCard。"""

        if not evidence:
            await self._complete_run(
                connection,
                run,
                "暂时没有找到可引用的来源，因此我不会基于未核实信息给出具体结论。你可以稍后重试或补充资料。",
            )
            return
        await self._append_event(
            connection,
            run["id"],
            run["user_id"],
            "progress",
            {"code": "GENERATING_ANSWER"},
            run["attempt"],
        )
        answer = RagAnswerV1(
            segments=(
                AnswerSegmentV1(
                    text=evidence[0].result.content[:800],
                    evidence_refs=(evidence[0].result.evidence_ref,),
                ),
            ),
            warnings=("合成离线来源仅用于验收，不代表真实考试公告。",)
            if evidence[0].result.source_type == "KNOWLEDGE"
            else (),
        )
        response = self.evidence_gate.render(answer, evidence)
        source_card = await self._persist_evidence(
            connection, run, evidence, claim_text=answer.segments[0].text
        )
        await self._complete_run(
            connection,
            run,
            response,
            cards=[source_card] if source_card else [],
        )

    async def _persist_evidence(
        self,
        connection: AsyncConnection,
        run: dict[str, Any],
        evidence: tuple[NumberedEvidence, ...],
        *,
        claim_text: str,
        plan_revision_id: UUID | None = None,
    ) -> dict[str, object] | None:
        """原子保存一条回答 Claim 与有序 Citation，并返回只读 SourceCard。

        SourceCard 使用数据库生成的 citation ID，客户端不能根据标题或 URL
        反推实体。外部 URL 已在 EvidenceGate 中规范化；私有来源仍需在详情
        查询时重新校验用户所有权。
        """

        if not evidence:
            return None
        if any(item.result.source_type == "KNOWLEDGE" for item in evidence):
            await self._ensure_synthetic_source(connection)
        claim_id = uuid4()
        await connection.execute(
            text(
                "INSERT INTO claims(id,agent_run_id,plan_revision_id,claim_key,claim_text,claim_type) "
                "VALUES(:id,:run,:revision,:key,:claim,'FACT')"
            ),
            {
                "id": claim_id,
                "run": run["id"],
                "revision": plan_revision_id,
                "key": f"rag-answer-{claim_id}",
                "claim": claim_text[:4000],
            },
        )
        citation_ids: list[str] = []
        sources: list[dict[str, object]] = []
        for item in evidence:
            citation_id = uuid4()
            source_type = item.result.source_type
            knowledge_chunk_id = item.result.knowledge_chunk_id
            if source_type == "KNOWLEDGE" and knowledge_chunk_id is None:
                knowledge_chunk_id = PUBLIC_CHUNK_ID
            await connection.execute(
                text(
                    "INSERT INTO citations(id,claim_id,knowledge_chunk_id,source_url_snapshot,"
                    "evidence_excerpt,relation,relevance_score,source_type,source_title_snapshot,"
                    "source_organization_snapshot,source_domain_snapshot,published_at_snapshot,"
                    "retrieved_at_snapshot,source_rank) VALUES(:id,:claim,:chunk,:url,:excerpt,'SUPPORTS',"
                    ":score,:source_type,:title,:organization,:domain,:published,now(),:rank)"
                ),
                {
                    "id": citation_id,
                    "claim": claim_id,
                    "chunk": knowledge_chunk_id,
                    "url": item.canonical_url,
                    "excerpt": item.result.content[:1000],
                    "score": item.result.score,
                    "source_type": source_type,
                    "title": item.result.title[:300],
                    "organization": item.result.source_organization,
                    "domain": item.domain or item.result.source_organization,
                    "published": item.result.published_at,
                    "rank": item.index,
                },
            )
            citation_ids.append(str(citation_id))
            sources.append(
                {
                    "citation_id": str(citation_id),
                    "index": item.index,
                    "title": item.result.title,
                    "domain": item.domain or item.result.source_organization,
                    "source_type": source_type,
                    "source_level": item.source_level,
                    "published_at": item.result.published_at.isoformat()
                    if item.result.published_at
                    else None,
                }
            )
        return {
            "schema_version": "1.0",
            "card_id": str(uuid4()),
            "card_type": "SourceCard",
            "entity_refs": {"citation_ids": citation_ids},
            "payload": {"title": "参考来源", "sources": sources},
            "actions": [
                {
                    "action_id": f"open-source-{source['index']}",
                    "action_type": "OPEN_SOURCE",
                    "label": f"查看来源 {source['index']}",
                    "citation_id": source["citation_id"],
                }
                for source in sources
            ],
        }
