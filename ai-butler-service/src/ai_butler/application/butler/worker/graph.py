"""Segment-scoped LangGraph 运行时；Checkpoint 只承担执行恢复。"""

from __future__ import annotations

import hashlib
import time
from typing import Any, TypedDict, cast
from uuid import UUID, uuid4

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph
from sqlalchemy import text

from ai_butler.agent.contracts import IntentDecisionV1, ShortTermStateV2
from ai_butler.agent.versions import (
    CURRENT_GRAPH_VERSION,
    CURRENT_PROMPT_BUNDLE_VERSION,
    TOOL_REGISTRY_VERSION,
)
from ai_butler.domain.errors import ButlerError
from ai_butler.tools import DEFAULT_TOOL_REGISTRY

from ..context import ButlerContext
from ..shared import _json, _row
from .executor import RunExecutor

GRAPH_VERSION = CURRENT_GRAPH_VERSION
PROMPT_BUNDLE_VERSION = CURRENT_PROMPT_BUNDLE_VERSION


class ButlerGraphState(TypedDict, total=False):
    """ShortTermStateV2 的图内表示；不复制消息、文件和长期记忆正文。"""

    current_run_id: str
    user_id: str
    conversation_id: str
    segment_id: str
    current_goal: str | None
    confirmed_constraints: list[str]
    decisions: list[str]
    open_questions: list[str]
    workflow_session_id: str | None
    latest_summary_id: str | None
    last_processed_message_id: str | None
    context_manifest_id: str | None
    graph_version: str
    prompt_bundle_version: str
    tool_registry_version: str
    policy_version: int
    registry_fingerprint: str
    last_node: str
    intent: dict[str, Any]


class ButlerGraphRuntime:
    """同一 segment 复用 thread；业务 working state 可重建 Checkpoint。"""

    def __init__(self, context: ButlerContext, executor: RunExecutor) -> None:
        self.database = context.database
        self._database_url = context.settings.langgraph_database_url
        self._route_run = executor._route_run
        self._respond_run = executor._respond_run
        self._execute_run = executor._execute_run

    async def run(self, run_id: UUID) -> None:
        facts = await self._load_run_facts(run_id)
        self._validate_versions(facts)
        config: dict[str, Any] = {
            "configurable": {
                "thread_id": str(facts["thread_id"]),
                "checkpoint_ns": CURRENT_GRAPH_VERSION,
            },
            "metadata": {
                "run_id": str(run_id),
                "graph_version": CURRENT_GRAPH_VERSION,
                "prompt_bundle_version": CURRENT_PROMPT_BUNDLE_VERSION,
            },
        }
        short_state = ShortTermStateV2(
            current_run_id=run_id,
            user_id=UUID(str(facts["user_id"])),
            conversation_id=UUID(str(facts["conversation_id"])),
            segment_id=UUID(str(facts["segment_id"])),
            current_goal=facts.get("current_goal") or facts.get("input_summary"),
            confirmed_constraints=tuple(facts.get("confirmed_constraints") or []),
            decisions=tuple(facts.get("decisions") or []),
            open_questions=tuple(facts.get("open_questions") or []),
            workflow_session_id=(
                UUID(str(facts["workflow_session_id"]))
                if facts.get("workflow_session_id")
                else None
            ),
            latest_summary_id=(
                UUID(str(facts["latest_summary_id"])) if facts.get("latest_summary_id") else None
            ),
            last_processed_message_id=(
                UUID(str(facts["last_processed_message_id"]))
                if facts.get("last_processed_message_id")
                else None
            ),
            context_manifest_id=(
                UUID(str(facts["context_manifest_id"]))
                if facts.get("context_manifest_id")
                else None
            ),
            graph_version=CURRENT_GRAPH_VERSION,
            prompt_bundle_version=CURRENT_PROMPT_BUNDLE_VERSION,
            tool_registry_version=TOOL_REGISTRY_VERSION,
            policy_version=int(facts.get("policy_version") or 1),
        )
        graph_input = cast(
            ButlerGraphState,
            {
                **short_state.model_dump(mode="json"),
                "registry_fingerprint": DEFAULT_TOOL_REGISTRY.fingerprint,
            },
        )
        async with AsyncPostgresSaver.from_conn_string(self._database_url) as checkpointer:
            graph = self._build_graph().compile(checkpointer=checkpointer)
            final_state = await graph.ainvoke(graph_input, config=cast(Any, config))
        await self._persist_working_state(facts, final_state)

    def _build_graph(self) -> StateGraph[ButlerGraphState]:
        builder = StateGraph(ButlerGraphState)
        builder.add_node("Initialize", self._initialize_node)
        builder.add_node("Router", self._router_node)
        builder.add_node("GeneralResponse", self._general_response_node)
        builder.add_node("Research", self._research_node)
        builder.add_node("Planning", self._planning_node)
        builder.add_node("TaskCoach", self._task_coach_node)
        builder.add_node("Memory", self._memory_node)
        builder.add_edge(START, "Initialize")
        builder.add_edge("Initialize", "Router")
        builder.add_conditional_edges(
            "Router",
            self._route_after_router,
            {
                "general": "GeneralResponse",
                "research": "Research",
                "planning": "Planning",
                "task_coach": "TaskCoach",
                "memory": "Memory",
            },
        )
        for node in ("GeneralResponse", "Research", "Planning", "TaskCoach", "Memory"):
            builder.add_edge(node, END)
        return builder

    async def _initialize_node(self, state: ButlerGraphState) -> ButlerGraphState:
        run_id = UUID(state["current_run_id"])
        started = time.monotonic()
        await self._mark_node(run_id, "Initialize", "RUNNING", None)
        await self._mark_node(
            run_id, "Initialize", "SUCCEEDED", int((time.monotonic() - started) * 1000)
        )
        return {"last_node": "Initialize"}

    async def _router_node(self, state: ButlerGraphState) -> ButlerGraphState:
        run_id = UUID(state["current_run_id"])
        started = time.monotonic()
        await self._mark_node(run_id, "Router", "RUNNING", None)
        decision = await self._route_run(run_id)
        await self._mark_node(
            run_id, "Router", "SUCCEEDED", int((time.monotonic() - started) * 1000)
        )
        return {"last_node": "Router", "intent": decision.model_dump(mode="json")}

    async def _general_response_node(self, state: ButlerGraphState) -> ButlerGraphState:
        run_id = UUID(state["current_run_id"])
        decision = IntentDecisionV1.model_validate(state["intent"])
        started = time.monotonic()
        await self._mark_node(run_id, "GeneralResponse", "RUNNING", None)
        await self._respond_run(run_id, decision)
        await self._mark_node(
            run_id, "GeneralResponse", "SUCCEEDED", int((time.monotonic() - started) * 1000)
        )
        return {"last_node": "GeneralResponse", "intent": {}}

    async def _research_node(self, state: ButlerGraphState) -> ButlerGraphState:
        return await self._execute_specialist_node(state, "Research")

    async def _planning_node(self, state: ButlerGraphState) -> ButlerGraphState:
        return await self._execute_specialist_node(state, "Planning")

    async def _task_coach_node(self, state: ButlerGraphState) -> ButlerGraphState:
        return await self._execute_specialist_node(state, "TaskCoach")

    async def _memory_node(self, state: ButlerGraphState) -> ButlerGraphState:
        return await self._execute_specialist_node(state, "Memory")

    async def _execute_specialist_node(
        self, state: ButlerGraphState, node_name: str
    ) -> ButlerGraphState:
        run_id = UUID(state["current_run_id"])
        decision = IntentDecisionV1.model_validate(state["intent"])
        started = time.monotonic()
        await self._mark_node(run_id, node_name, "RUNNING", None)
        await self._execute_run(run_id, decision)
        await self._mark_node(
            run_id, node_name, "SUCCEEDED", int((time.monotonic() - started) * 1000)
        )
        return {"last_node": node_name, "intent": {}}

    @staticmethod
    def _route_after_router(state: ButlerGraphState) -> str:
        decision = IntentDecisionV1.model_validate(state["intent"])
        if decision.intent in {"PLAN_CREATE", "PLAN_ADJUST"}:
            return "planning"
        if decision.intent in {"RESEARCH", "CIVIL_QA"}:
            return "research"
        if decision.intent in {"DAILY_PLANNING", "PLAN_REVIEW", "TASK_FEEDBACK"}:
            return "task_coach"
        if decision.intent == "MEMORY":
            return "memory"
        return "general"

    async def _load_run_facts(self, run_id: UUID) -> dict[str, Any]:
        async with self.database.connect() as connection:
            row = _row(
                await connection.execute(
                    text(
                        "SELECT r.*,s.thread_id,w.current_goal,w.confirmed_constraints,w.decisions,"
                        "w.open_questions,w.workflow_session_id,w.latest_summary_id,"
                        "w.last_processed_message_id,w.context_manifest_id,w.policy_version "
                        "FROM agent_runs r JOIN conversation_segments s ON s.id=r.segment_id "
                        "LEFT JOIN conversation_working_states w ON w.conversation_id=r.conversation_id "
                        "AND w.segment_id=r.segment_id WHERE r.id=:id"
                    ),
                    {"id": run_id},
                )
            )
        if row is None:
            raise ButlerError("RUN_NOT_FOUND", "运行不存在", 404)
        return row

    async def _persist_working_state(self, facts: dict[str, Any], state: dict[str, Any]) -> None:
        """成功节点完成后同步可查询事实；segment 已轮换时不污染新线程。"""

        async with self.database.transaction() as connection:
            active_segment = (
                await connection.execute(
                    text("SELECT active_segment_id FROM conversations WHERE id=:id"),
                    {"id": facts["conversation_id"]},
                )
            ).scalar_one_or_none()
            if active_segment is None or str(active_segment) != str(facts["segment_id"]):
                return
            active_workflow = (
                await connection.execute(
                    text(
                        "SELECT id FROM workflow_sessions WHERE conversation_id=:conversation_id "
                        "AND segment_id=:segment_id AND status IN ('ACTIVE','WAITING_INPUT') "
                        "ORDER BY updated_at DESC LIMIT 1"
                    ),
                    {
                        "conversation_id": facts["conversation_id"],
                        "segment_id": facts["segment_id"],
                    },
                )
            ).scalar_one_or_none()
            latest_manifest = (
                await connection.execute(
                    text(
                        "SELECT id FROM context_manifests WHERE agent_run_id=:run_id "
                        "ORDER BY created_at DESC LIMIT 1"
                    ),
                    {"run_id": facts["id"]},
                )
            ).scalar_one_or_none()
            await connection.execute(
                text(
                    "INSERT INTO conversation_working_states(conversation_id,user_id,segment_id,"
                    "current_goal,confirmed_constraints,decisions,open_questions,workflow_session_id,"
                    "latest_summary_id,last_processed_message_id,context_manifest_id,last_completed_node,"
                    "graph_version,prompt_bundle_version,tool_registry_version,policy_version) "
                    "VALUES(:conversation_id,:user_id,:segment_id,:current_goal,"
                    "CAST(:constraints AS jsonb),CAST(:decisions AS jsonb),CAST(:questions AS jsonb),"
                    ":workflow,:summary,:message,:manifest,:node,:graph,:prompt,:tools,:policy) "
                    "ON CONFLICT(conversation_id) DO UPDATE SET segment_id=EXCLUDED.segment_id,"
                    "state_version=conversation_working_states.state_version+1,"
                    "current_goal=EXCLUDED.current_goal,confirmed_constraints=EXCLUDED.confirmed_constraints,"
                    "decisions=EXCLUDED.decisions,open_questions=EXCLUDED.open_questions,"
                    "workflow_session_id=EXCLUDED.workflow_session_id,"
                    "latest_summary_id=EXCLUDED.latest_summary_id,"
                    "last_processed_message_id=EXCLUDED.last_processed_message_id,"
                    "context_manifest_id=EXCLUDED.context_manifest_id,"
                    "last_completed_node=EXCLUDED.last_completed_node,graph_version=EXCLUDED.graph_version,"
                    "prompt_bundle_version=EXCLUDED.prompt_bundle_version,"
                    "tool_registry_version=EXCLUDED.tool_registry_version,"
                    "policy_version=EXCLUDED.policy_version,updated_at=now()"
                ),
                {
                    "conversation_id": facts["conversation_id"],
                    "user_id": facts["user_id"],
                    "segment_id": facts["segment_id"],
                    "current_goal": state.get("current_goal"),
                    "constraints": _json(state.get("confirmed_constraints") or []),
                    "decisions": _json(state.get("decisions") or []),
                    "questions": _json(state.get("open_questions") or []),
                    "workflow": active_workflow,
                    "summary": state.get("latest_summary_id"),
                    "message": facts["pending_message_id"],
                    "manifest": latest_manifest,
                    "node": state.get("last_node"),
                    "graph": CURRENT_GRAPH_VERSION,
                    "prompt": CURRENT_PROMPT_BUNDLE_VERSION,
                    "tools": TOOL_REGISTRY_VERSION,
                    "policy": int(state.get("policy_version") or 1),
                },
            )

    @staticmethod
    def _validate_versions(facts: dict[str, Any]) -> None:
        if (
            str(facts["graph_version"]) != CURRENT_GRAPH_VERSION
            or str(facts["prompt_bundle_version"]) != CURRENT_PROMPT_BUNDLE_VERSION
            or str(facts["tool_registry_version"]) != TOOL_REGISTRY_VERSION
            or str(facts["tool_registry_fingerprint"]) != DEFAULT_TOOL_REGISTRY.fingerprint
        ):
            raise ButlerError("RUN_VERSION_UNAVAILABLE", "运行版本当前不可执行", 409)

    async def _mark_node(
        self, run_id: UUID, node_name: str, status: str, duration_ms: int | None
    ) -> None:
        async with self.database.transaction() as connection:
            run = _row(
                await connection.execute(
                    text(
                        "SELECT user_id,trace_id,attempt,tool_registry_fingerprint "
                        "FROM agent_runs WHERE id=:id"
                    ),
                    {"id": run_id},
                )
            )
            if run is None:
                return
            await connection.execute(
                text(
                    "UPDATE agent_runs SET last_node=:node,heartbeat_at=now(),updated_at=now() "
                    "WHERE id=:id AND status='RUNNING'"
                ),
                {"id": run_id, "node": node_name},
            )
            span_id = hashlib.sha256(f"{run_id}:{node_name}".encode()).hexdigest()[:32]
            await connection.execute(
                text(
                    "INSERT INTO agent_trace_spans(id,agent_run_id,user_id,trace_id,span_id,attempt,"
                    "span_kind,node_name,registry_fingerprint,status,trust_level,duration_ms,ended_at) "
                    "VALUES(:id,:run_id,:user_id,:trace_id,:span_id,:attempt,'NODE',:node_name,"
                    ":fingerprint,CAST(:status AS varchar),'SYSTEM_FACT',:duration_ms,"
                    "CASE WHEN CAST(:status AS varchar)='RUNNING' THEN NULL ELSE now() END) "
                    "ON CONFLICT(trace_id,span_id) DO UPDATE SET status=EXCLUDED.status,"
                    "duration_ms=EXCLUDED.duration_ms,ended_at=EXCLUDED.ended_at"
                ),
                {
                    "id": uuid4(),
                    "run_id": run_id,
                    "user_id": run["user_id"],
                    "trace_id": run["trace_id"],
                    "span_id": span_id,
                    "attempt": run["attempt"],
                    "node_name": node_name,
                    "fingerprint": run["tool_registry_fingerprint"],
                    "status": status,
                    "duration_ms": duration_ms,
                },
            )
