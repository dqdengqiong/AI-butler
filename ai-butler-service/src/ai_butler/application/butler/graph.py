"""LangGraph 持久化运行时；业务事实和副作用仍由确定性应用服务负责。"""

from __future__ import annotations

import hashlib
import time
from datetime import UTC, datetime
from typing import Any, TypedDict, cast
from uuid import UUID, uuid4

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from sqlalchemy import text

from ai_butler.agent.contracts import IntentDecisionV1
from ai_butler.agent.runtime import DEFAULT_CAPABILITY_REGISTRY
from ai_butler.agent.versions import (
    CAPABILITY_REGISTRY_VERSION,
    CURRENT_GRAPH_VERSION,
    CURRENT_PROMPT_BUNDLE_VERSION,
    LEGACY_GRAPH_VERSION,
    LEGACY_PROMPT_BUNDLE_VERSION,
)
from ai_butler.domain.errors import ButlerError

from .context import ButlerContext
from .executor import RunExecutor
from .shared import _row

GRAPH_VERSION = CURRENT_GRAPH_VERSION
PROMPT_BUNDLE_VERSION = CURRENT_PROMPT_BUNDLE_VERSION


class ButlerGraphState(TypedDict, total=False):
    """Checkpoint 只保存恢复游标和固定版本，不复制 PostgreSQL 业务事实。"""

    run_id: str
    user_id: str
    action_key: str
    graph_version: str
    prompt_bundle_version: str
    capability_registry_version: str
    registry_fingerprint: str
    last_node: str
    awaiting: str | None
    resume: dict[str, Any] | None
    intent: dict[str, Any]
    resume_target: str | None


class ButlerGraphRuntime:
    """以 segment.thread_id 恢复单图多节点执行，并确认稳定 action key。"""

    def __init__(self, context: ButlerContext, executor: RunExecutor) -> None:
        self.database = context.database
        self._database_url = context.settings.langgraph_database_url
        self._execute_legacy_run = executor._execute_legacy_run
        self._route_v3_run = executor._route_v3_run
        self._respond_v3_run = executor._respond_v3_run
        self._execute_v3_run = executor._execute_v3_run

    async def run(self, run_id: UUID) -> None:
        facts = await self._load_run_facts(run_id)
        self._validate_versions(facts)
        action = str(facts["pending_action"])
        action_key = str(facts["pending_action_key"] or "")
        if not action_key:
            raise ButlerError("RUN_ACTION_KEY_MISSING", "运行恢复标识缺失", 500)
        if facts["last_applied_action_key"] == action_key:
            await self._acknowledge_action(run_id, action_key)
            return

        config: dict[str, Any] = {
            "configurable": {"thread_id": str(facts["thread_id"])},
            "metadata": {
                "run_id": str(run_id),
                "graph_version": str(facts["graph_version"]),
                "prompt_bundle_version": str(facts["prompt_bundle_version"]),
            },
        }
        if facts["graph_version"] == CURRENT_GRAPH_VERSION:
            config["configurable"]["checkpoint_ns"] = CURRENT_GRAPH_VERSION
        async with AsyncPostgresSaver.from_conn_string(self._database_url) as checkpointer:
            graph = self._build_graph(str(facts["graph_version"])).compile(
                checkpointer=checkpointer
            )
            if action == "START":
                graph_input: ButlerGraphState | Command[Any] | None = {
                    "run_id": str(run_id),
                    "user_id": str(facts["user_id"]),
                    "action_key": action_key,
                    "graph_version": str(facts["graph_version"]),
                    "prompt_bundle_version": str(facts["prompt_bundle_version"]),
                    "capability_registry_version": str(facts["capability_registry_version"]),
                    "registry_fingerprint": str(facts["capability_registry_fingerprint"]),
                    "awaiting": None,
                }
            elif action in {"INPUT_RESUME", "APPROVAL_RESUME"}:
                graph_input = Command(resume={"action": action, "action_key": action_key})
            elif action == "RETRY":
                graph_input = None
            else:
                raise ButlerError("RUN_ACTION_INVALID", "运行恢复动作无效", 409)
            await graph.ainvoke(graph_input, config=cast(Any, config))

        # ainvoke 正常返回表示 checkpoint 已确认本次输入（包括 interrupt checkpoint）。
        await self._acknowledge_action(run_id, action_key)

    def _build_graph(
        self, graph_version: str = LEGACY_GRAPH_VERSION
    ) -> StateGraph[ButlerGraphState]:
        """按 run 创建时版本选择图，避免新 Prompt 破坏旧 checkpoint 恢复。"""

        if graph_version == CURRENT_GRAPH_VERSION:
            return self._build_v3_graph()
        return self._build_v2_graph()

    def _build_v2_graph(self) -> StateGraph[ButlerGraphState]:
        builder = StateGraph(ButlerGraphState)
        for node_name in (
            "Initialize",
            "Router",
            "Profile",
            "Research",
            "Planner",
            "Review",
            "Evidence Gate",
        ):
            builder.add_node(node_name, self._pass_node(node_name))
        builder.add_node("Approval", self._approval_node)
        builder.add_node("Executor", self._legacy_executor_node)
        builder.add_node("Feedback/Adjust", self._pass_node("Feedback/Adjust"))
        builder.add_node("Response", self._pass_node("Response"))

        builder.add_edge(START, "Initialize")
        builder.add_edge("Initialize", "Router")
        builder.add_edge("Router", "Profile")
        builder.add_edge("Profile", "Research")
        builder.add_edge("Research", "Planner")
        builder.add_edge("Planner", "Review")
        builder.add_edge("Review", "Evidence Gate")
        builder.add_edge("Evidence Gate", "Approval")
        builder.add_edge("Approval", "Executor")
        builder.add_conditional_edges(
            "Executor",
            self._route_after_executor,
            {"approval": "Approval", "response": "Feedback/Adjust", "end": END},
        )
        builder.add_edge("Feedback/Adjust", "Response")
        builder.add_edge("Response", END)
        return builder

    def _build_v3_graph(self) -> StateGraph[ButlerGraphState]:
        """构建真实意图驱动的 v3 图；业务副作用仍集中在受控应用服务。"""

        builder = StateGraph(ButlerGraphState)
        builder.add_node("Initialize", self._pass_node("Initialize"))
        builder.add_node("Router", self._v3_router_node)
        for node_name in ("Profile", "Research", "Planner", "Review", "Evidence Gate"):
            builder.add_node(node_name, self._pass_node(node_name))
        builder.add_node("Approval", self._approval_node)
        builder.add_node("Executor", self._v3_executor_node)
        builder.add_node("Feedback/Adjust", self._pass_node("Feedback/Adjust"))
        builder.add_node("Response", self._v3_response_node)

        builder.add_edge(START, "Initialize")
        builder.add_edge("Initialize", "Router")
        builder.add_conditional_edges(
            "Router",
            self._route_after_v3_router,
            {
                "response": "Response",
                "plan": "Profile",
                "research": "Research",
                "feedback": "Feedback/Adjust",
                "execute": "Executor",
            },
        )
        builder.add_edge("Profile", "Research")
        builder.add_conditional_edges(
            "Research",
            self._route_after_v3_research,
            {"planner": "Planner", "execute": "Executor"},
        )
        builder.add_edge("Planner", "Review")
        builder.add_edge("Review", "Evidence Gate")
        builder.add_edge("Evidence Gate", "Executor")
        builder.add_edge("Feedback/Adjust", "Executor")
        builder.add_conditional_edges(
            "Executor",
            self._route_after_v3_terminal,
            {"approval": "Approval", "end": END},
        )
        builder.add_conditional_edges(
            "Response",
            self._route_after_v3_terminal,
            {"approval": "Approval", "end": END},
        )
        builder.add_conditional_edges(
            "Approval",
            self._route_after_v3_approval,
            {"router": "Router", "executor": "Executor"},
        )
        return builder

    def _pass_node(self, node_name: str) -> Any:
        async def execute(state: ButlerGraphState) -> ButlerGraphState:
            await self._mark_node(UUID(state["run_id"]), node_name, "SUCCEEDED", 0)
            return {"last_node": node_name}

        return execute

    async def _approval_node(self, state: ButlerGraphState) -> ButlerGraphState:
        await self._mark_node(UUID(state["run_id"]), "Approval", "SUCCEEDED", 0)
        awaiting = state.get("awaiting")
        if awaiting is None:
            return {"last_node": "Approval"}
        resume = interrupt(
            {
                "type": awaiting,
                "run_id": state["run_id"],
            }
        )
        if not isinstance(resume, dict) or not isinstance(resume.get("action_key"), str):
            raise ButlerError("RUN_RESUME_MISMATCH", "运行恢复标识不匹配", 409)
        action_key = str(resume["action_key"])
        await self._validate_resume(UUID(state["run_id"]), state["user_id"], awaiting, action_key)
        return {
            "last_node": "Approval",
            "awaiting": None,
            "resume": resume,
            "action_key": action_key,
        }

    async def _legacy_executor_node(self, state: ButlerGraphState) -> ButlerGraphState:
        run_id = UUID(state["run_id"])
        started = time.monotonic()
        await self._mark_node(run_id, "Executor", "RUNNING", None)
        try:
            await self._execute_legacy_run(run_id)
        except Exception:
            await self._mark_node(
                run_id,
                "Executor",
                "FAILED",
                int((time.monotonic() - started) * 1000),
            )
            raise
        await self._mark_node(
            run_id,
            "Executor",
            "SUCCEEDED",
            int((time.monotonic() - started) * 1000),
        )
        async with self.database.connect() as connection:
            status = (
                await connection.execute(
                    text("SELECT status FROM agent_runs WHERE id=:id"), {"id": run_id}
                )
            ).scalar_one_or_none()
        awaiting = str(status) if status in {"AWAITING_INPUT", "AWAITING_APPROVAL"} else None
        return {"last_node": "Executor", "awaiting": awaiting}

    async def _v3_router_node(self, state: ButlerGraphState) -> ButlerGraphState:
        """调用运行内真实 Router；其结构化结果进入 checkpoint 但不含用户正文。"""

        run_id = UUID(state["run_id"])
        started = time.monotonic()
        await self._mark_node(run_id, "Router", "RUNNING", None)
        decision = await self._route_v3_run(run_id)
        await self._mark_node(
            run_id,
            "Router",
            "SUCCEEDED",
            int((time.monotonic() - started) * 1000),
        )
        return {
            "last_node": "Router",
            "intent": decision.model_dump(mode="json"),
            "awaiting": None,
            "resume_target": None,
        }

    async def _v3_response_node(self, state: ButlerGraphState) -> ButlerGraphState:
        """生成无副作用回答；普通聊天使用真实 Token 流。"""

        run_id = UUID(state["run_id"])
        decision = IntentDecisionV1.model_validate(state["intent"])
        started = time.monotonic()
        await self._mark_node(run_id, "Response", "RUNNING", None)
        await self._respond_v3_run(run_id, decision)
        await self._mark_node(
            run_id,
            "Response",
            "SUCCEEDED",
            int((time.monotonic() - started) * 1000),
        )
        awaiting = await self._awaiting_status(run_id)
        return {
            "last_node": "Response",
            "awaiting": awaiting,
            "resume_target": "Router" if awaiting == "AWAITING_INPUT" else None,
        }

    async def _v3_executor_node(self, state: ButlerGraphState) -> ButlerGraphState:
        """执行受控领域分支；模型不能绕过应用层审批和所有权校验。"""

        run_id = UUID(state["run_id"])
        decision = IntentDecisionV1.model_validate(state["intent"])
        started = time.monotonic()
        await self._mark_node(run_id, "Executor", "RUNNING", None)
        await self._execute_v3_run(run_id, decision)
        await self._mark_node(
            run_id,
            "Executor",
            "SUCCEEDED",
            int((time.monotonic() - started) * 1000),
        )
        awaiting = await self._awaiting_status(run_id)
        return {
            "last_node": "Executor",
            "awaiting": awaiting,
            "resume_target": "Executor" if awaiting is not None else None,
        }

    async def _awaiting_status(self, run_id: UUID) -> str | None:
        async with self.database.connect() as connection:
            status = (
                await connection.execute(
                    text("SELECT status FROM agent_runs WHERE id=:id"), {"id": run_id}
                )
            ).scalar_one_or_none()
        return str(status) if status in {"AWAITING_INPUT", "AWAITING_APPROVAL"} else None

    @staticmethod
    def _route_after_executor(state: ButlerGraphState) -> str:
        if state.get("awaiting") is not None:
            return "approval"
        return "response"

    @staticmethod
    def _route_after_v3_router(state: ButlerGraphState) -> str:
        decision = IntentDecisionV1.model_validate(state["intent"])
        if decision.intent in {"CLARIFY", "UNSUPPORTED"}:
            return "response"
        if decision.intent in {"GENERAL_CHAT", "CIVIL_QA"} and not (
            decision.needs_web or decision.needs_private_knowledge
        ):
            return "response"
        if decision.intent in {"PLAN_CREATE", "PLAN_ADJUST"}:
            return "plan"
        if decision.intent == "TASK_FEEDBACK":
            return "feedback"
        if decision.intent in {"GENERAL_CHAT", "CIVIL_QA"}:
            return "research"
        return "execute"

    @staticmethod
    def _route_after_v3_research(state: ButlerGraphState) -> str:
        decision = IntentDecisionV1.model_validate(state["intent"])
        return "planner" if decision.intent in {"PLAN_CREATE", "PLAN_ADJUST"} else "execute"

    @staticmethod
    def _route_after_v3_terminal(state: ButlerGraphState) -> str:
        return "approval" if state.get("awaiting") is not None else "end"

    @staticmethod
    def _route_after_v3_approval(state: ButlerGraphState) -> str:
        return "router" if state.get("resume_target") == "Router" else "executor"

    async def _load_run_facts(self, run_id: UUID) -> dict[str, Any]:
        async with self.database.connect() as connection:
            row = _row(
                await connection.execute(
                    text(
                        "SELECT r.*,s.thread_id FROM agent_runs r "
                        "JOIN conversation_segments s ON s.id=r.segment_id WHERE r.id=:id"
                    ),
                    {"id": run_id},
                )
            )
        if row is None:
            raise ButlerError("RUN_NOT_FOUND", "运行不存在", 404)
        return row

    @staticmethod
    def _validate_versions(facts: dict[str, Any]) -> None:
        supported = {
            (LEGACY_GRAPH_VERSION, LEGACY_PROMPT_BUNDLE_VERSION),
            (CURRENT_GRAPH_VERSION, CURRENT_PROMPT_BUNDLE_VERSION),
        }
        if (
            (str(facts["graph_version"]), str(facts["prompt_bundle_version"])) not in supported
            or str(facts["capability_registry_version"]) != CAPABILITY_REGISTRY_VERSION
            or str(facts["capability_registry_fingerprint"])
            != DEFAULT_CAPABILITY_REGISTRY.fingerprint
        ):
            raise ButlerError("RUN_VERSION_UNAVAILABLE", "运行创建时版本当前不可恢复", 409)

    async def _validate_resume(
        self, run_id: UUID, user_id: str, awaiting: str, action_key: str
    ) -> None:
        expected_action = "INPUT_RESUME" if awaiting == "AWAITING_INPUT" else "APPROVAL_RESUME"
        facts = await self._load_run_facts(run_id)
        if (
            str(facts["user_id"]) != user_id
            or facts["status"] != "RUNNING"
            or facts["pending_action"] != expected_action
            or facts["pending_action_key"] != action_key
        ):
            raise ButlerError("RUN_RESUME_CONFLICT", "运行恢复事实已变化", 409)

    async def _mark_node(
        self, run_id: UUID, node_name: str, status: str, duration_ms: int | None
    ) -> None:
        async with self.database.transaction() as connection:
            run = _row(
                await connection.execute(
                    text(
                        "SELECT user_id,trace_id,attempt,pending_action_key,"
                        "capability_registry_fingerprint FROM agent_runs WHERE id=:id"
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
            action_key = str(run["pending_action_key"] or "none")
            span_id = hashlib.sha256(f"{action_key}:{node_name}".encode()).hexdigest()[:32]
            await connection.execute(
                text(
                    "INSERT INTO agent_trace_spans(id,agent_run_id,user_id,trace_id,span_id,attempt,"
                    "span_kind,node_name,registry_fingerprint,status,trust_level,duration_ms,ended_at) "
                    "VALUES(:id,:run_id,:user_id,:trace_id,:span_id,:attempt,'NODE',:node_name,"
                    ":fingerprint,:status,'SYSTEM_FACT',:duration_ms,:ended_at) "
                    "ON CONFLICT(trace_id,span_id) DO UPDATE SET "
                    "status=EXCLUDED.status,duration_ms=EXCLUDED.duration_ms,ended_at=EXCLUDED.ended_at"
                ),
                {
                    "id": uuid4(),
                    "run_id": run_id,
                    "user_id": run["user_id"],
                    "trace_id": run["trace_id"],
                    "span_id": span_id,
                    "attempt": run["attempt"],
                    "node_name": node_name,
                    "fingerprint": run["capability_registry_fingerprint"],
                    "status": status,
                    "duration_ms": duration_ms,
                    "ended_at": None if status == "RUNNING" else datetime.now(UTC),
                },
            )

    async def _acknowledge_action(self, run_id: UUID, action_key: str) -> None:
        async with self.database.transaction() as connection:
            await connection.execute(
                text(
                    "UPDATE agent_runs SET last_applied_action_key=:action_key,pending_action='NONE',"
                    "pending_action_key=NULL,updated_at=now() "
                    "WHERE id=:id AND pending_action_key=:action_key"
                ),
                {"id": run_id, "action_key": action_key},
            )
