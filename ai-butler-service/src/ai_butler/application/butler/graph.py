"""单轮 LangGraph 运行时；每个 run 独立路由并在本轮进入终态。"""

from __future__ import annotations

import hashlib
import time
from typing import Any, TypedDict, cast
from uuid import UUID, uuid4

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph
from sqlalchemy import text

from ai_butler.agent.contracts import IntentDecisionV1
from ai_butler.agent.versions import (
    CURRENT_GRAPH_VERSION,
    CURRENT_PROMPT_BUNDLE_VERSION,
    TOOL_REGISTRY_VERSION,
)
from ai_butler.domain.errors import ButlerError
from ai_butler.tools import DEFAULT_TOOL_REGISTRY

from .context import ButlerContext
from .executor import RunExecutor
from .shared import _row

GRAPH_VERSION = CURRENT_GRAPH_VERSION
PROMPT_BUNDLE_VERSION = CURRENT_PROMPT_BUNDLE_VERSION


class ButlerGraphState(TypedDict, total=False):
    """单轮 checkpoint 只保存路由结果，不包含跨轮待处理状态。"""

    run_id: str
    user_id: str
    graph_version: str
    prompt_bundle_version: str
    tool_registry_version: str
    registry_fingerprint: str
    last_node: str
    intent: dict[str, Any]


class ButlerGraphRuntime:
    """为每个 run 使用独立 checkpoint 线程并执行不可中断的单轮图。"""

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
                "thread_id": str(run_id),
                "checkpoint_ns": CURRENT_GRAPH_VERSION,
            },
            "metadata": {
                "run_id": str(run_id),
                "graph_version": CURRENT_GRAPH_VERSION,
                "prompt_bundle_version": CURRENT_PROMPT_BUNDLE_VERSION,
            },
        }
        graph_input: ButlerGraphState = {
            "run_id": str(run_id),
            "user_id": str(facts["user_id"]),
            "graph_version": CURRENT_GRAPH_VERSION,
            "prompt_bundle_version": CURRENT_PROMPT_BUNDLE_VERSION,
            "tool_registry_version": TOOL_REGISTRY_VERSION,
            "registry_fingerprint": DEFAULT_TOOL_REGISTRY.fingerprint,
        }
        async with AsyncPostgresSaver.from_conn_string(self._database_url) as checkpointer:
            graph = self._build_graph().compile(checkpointer=checkpointer)
            await graph.ainvoke(graph_input, config=cast(Any, config))

    def _build_graph(self) -> StateGraph[ButlerGraphState]:
        builder = StateGraph(ButlerGraphState)
        builder.add_node("Initialize", self._initialize_node)
        builder.add_node("Router", self._router_node)
        builder.add_node("Response", self._response_node)
        builder.add_node("ToolExecutor", self._tool_executor_node)
        builder.add_edge(START, "Initialize")
        builder.add_edge("Initialize", "Router")
        builder.add_conditional_edges(
            "Router",
            self._route_after_router,
            {"response": "Response", "execute": "ToolExecutor"},
        )
        builder.add_edge("Response", END)
        builder.add_edge("ToolExecutor", END)
        return builder

    async def _initialize_node(self, state: ButlerGraphState) -> ButlerGraphState:
        run_id = UUID(state["run_id"])
        started = time.monotonic()
        await self._mark_node(run_id, "Initialize", "RUNNING", None)
        await self._mark_node(
            run_id, "Initialize", "SUCCEEDED", int((time.monotonic() - started) * 1000)
        )
        return {"last_node": "Initialize"}

    async def _router_node(self, state: ButlerGraphState) -> ButlerGraphState:
        run_id = UUID(state["run_id"])
        started = time.monotonic()
        await self._mark_node(run_id, "Router", "RUNNING", None)
        decision = await self._route_run(run_id)
        await self._mark_node(
            run_id, "Router", "SUCCEEDED", int((time.monotonic() - started) * 1000)
        )
        return {"last_node": "Router", "intent": decision.model_dump(mode="json")}

    async def _response_node(self, state: ButlerGraphState) -> ButlerGraphState:
        run_id = UUID(state["run_id"])
        decision = IntentDecisionV1.model_validate(state["intent"])
        started = time.monotonic()
        await self._mark_node(run_id, "Response", "RUNNING", None)
        await self._respond_run(run_id, decision)
        await self._mark_node(
            run_id, "Response", "SUCCEEDED", int((time.monotonic() - started) * 1000)
        )
        return {"last_node": "Response"}

    async def _tool_executor_node(self, state: ButlerGraphState) -> ButlerGraphState:
        run_id = UUID(state["run_id"])
        decision = IntentDecisionV1.model_validate(state["intent"])
        started = time.monotonic()
        await self._mark_node(run_id, "ToolExecutor", "RUNNING", None)
        await self._execute_run(run_id, decision)
        await self._mark_node(
            run_id, "ToolExecutor", "SUCCEEDED", int((time.monotonic() - started) * 1000)
        )
        return {"last_node": "ToolExecutor"}

    @staticmethod
    def _route_after_router(state: ButlerGraphState) -> str:
        decision = IntentDecisionV1.model_validate(state["intent"])
        if decision.intent in {"CLARIFY", "UNSUPPORTED"}:
            return "response"
        if not DEFAULT_TOOL_REGISTRY.resolve(decision.intent, decision.context_needs):
            return "response"
        return "execute"

    async def _load_run_facts(self, run_id: UUID) -> dict[str, Any]:
        async with self.database.connect() as connection:
            row = _row(
                await connection.execute(
                    text("SELECT * FROM agent_runs WHERE id=:id"), {"id": run_id}
                )
            )
        if row is None:
            raise ButlerError("RUN_NOT_FOUND", "运行不存在", 404)
        return row

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
