from __future__ import annotations

from typing import Any
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ai_butler.agent.availability import (
    AvailabilityInterpretationV1,
    quick_availability_options,
)

from .context import ButlerContext
from .events import EventService
from .shared import (
    _json,
    _row,
)


class InterruptionService:
    def __init__(self, context: ButlerContext, events: EventService) -> None:
        self.database = context.database
        self._append_event = events._append_event

    async def _emit_progress(self, run: dict[str, Any], code: str) -> None:
        """在独立短事务中持久化网络调用前后的预定义进度，不泄露查询正文。"""

        async with self.database.transaction() as connection:
            current = _row(
                await connection.execute(
                    text("SELECT status FROM agent_runs WHERE id=:id FOR UPDATE"),
                    {"id": run["id"]},
                )
            )
            if current is None or current["status"] != "RUNNING":
                return
            await self._append_event(
                connection, run["id"], run["user_id"], "progress", {"code": code}, run["attempt"]
            )

    async def _interrupt_for_input(self, connection: AsyncConnection, run: dict[str, Any]) -> None:
        content = "请直接描述你的学习时间，例如：每天 1 小时，周末不学习。"
        card: dict[str, object] = {
            "schema_version": "1.0",
            "cards": [
                {
                    "schema_version": "1.0",
                    "card_id": str(uuid4()),
                    "card_type": "SelectionCard",
                    "entity_refs": {},
                    "payload": {
                        "question": content,
                        "description": "可以在下方输入自然语言，也可以选择一个常用安排。",
                        "input_mode": "NATURAL_LANGUAGE",
                        "phase": "COLLECT_AVAILABILITY",
                        "input_placeholder": "例如：每天 1 小时，周末不学习",
                        "options": list(quick_availability_options()),
                    },
                    "actions": [
                        {
                            "action_id": "submit-selection",
                            "action_type": "SUBMIT_SELECTION",
                            "label": "确认选择",
                        }
                    ],
                }
            ],
        }
        await connection.execute(
            text(
                "UPDATE messages SET status='COMPLETED',content=:content,structured_content=CAST(:cards AS jsonb),"
                "updated_at=now() WHERE id=:id"
            ),
            {"content": content, "cards": _json(card), "id": run["response_message_id"]},
        )
        await connection.execute(
            text("UPDATE agent_runs SET status='AWAITING_INPUT',updated_at=now() WHERE id=:id"),
            {"id": run["id"]},
        )
        await self._append_event(
            connection,
            run["id"],
            run["user_id"],
            "interrupt",
            {"type": "INPUT", "message": content, "cards": card["cards"]},
            run["attempt"],
        )

    async def _interrupt_for_availability_confirmation(
        self,
        connection: AsyncConnection,
        run: dict[str, Any],
        interpretation: AvailabilityInterpretationV1,
    ) -> None:
        """持久化服务端解析候选，等待用户显式确认后才允许 Planner 使用。"""

        if interpretation.status != "COMPLETE" or interpretation.weekly_minutes is None:
            await self._interrupt_for_availability_clarification(
                connection,
                run,
                interpretation.question or "请重新描述你的学习时间。",
            )
            return
        content = f"我理解的是：{interpretation.summary}。请确认是否正确。"
        card: dict[str, object] = {
            "schema_version": "1.0",
            "cards": [
                {
                    "schema_version": "1.0",
                    "card_id": str(uuid4()),
                    "card_type": "SelectionCard",
                    "entity_refs": {},
                    "payload": {
                        "question": "确认学习时间",
                        "description": interpretation.summary,
                        "input_mode": "NATURAL_LANGUAGE",
                        "phase": "CONFIRM_AVAILABILITY",
                        "input_placeholder": "如需修改，也可以直接输入新的时间安排",
                        "interpretation": interpretation.model_dump(mode="json"),
                        "options": [
                            {"id": "confirm-availability", "label": "确认并生成计划"},
                            {"id": "revise-availability", "label": "重新描述"},
                        ],
                    },
                    "actions": [
                        {
                            "action_id": "submit-selection",
                            "action_type": "SUBMIT_SELECTION",
                            "label": "提交",
                        }
                    ],
                }
            ],
        }
        await self._write_input_interrupt(connection, run, content, card)

    async def _interrupt_for_availability_clarification(
        self, connection: AsyncConnection, run: dict[str, Any], question: str
    ) -> None:
        """在解析含糊、冲突或用户要求重写时恢复自然语言输入状态。"""

        content = question
        card: dict[str, object] = {
            "schema_version": "1.0",
            "cards": [
                {
                    "schema_version": "1.0",
                    "card_id": str(uuid4()),
                    "card_type": "SelectionCard",
                    "entity_refs": {},
                    "payload": {
                        "question": question,
                        "description": "请在下方输入具体的每天或每周学习时间。",
                        "input_mode": "NATURAL_LANGUAGE",
                        "phase": "COLLECT_AVAILABILITY",
                        "input_placeholder": "例如：工作日每天 1 小时，周末休息",
                        "options": list(quick_availability_options()),
                    },
                    "actions": [
                        {
                            "action_id": "submit-selection",
                            "action_type": "SUBMIT_SELECTION",
                            "label": "确认选择",
                        }
                    ],
                }
            ],
        }
        await self._write_input_interrupt(connection, run, content, card)

    async def _write_input_interrupt(
        self,
        connection: AsyncConnection,
        run: dict[str, Any],
        content: str,
        card: dict[str, object],
    ) -> None:
        """原子完成当前回复并把同一 run 放回等待输入状态。"""

        await connection.execute(
            text(
                "UPDATE messages SET status='COMPLETED',content=:content,"
                "structured_content=CAST(:cards AS jsonb),updated_at=now() WHERE id=:id"
            ),
            {"content": content, "cards": _json(card), "id": run["response_message_id"]},
        )
        await connection.execute(
            text("UPDATE agent_runs SET status='AWAITING_INPUT',updated_at=now() WHERE id=:id"),
            {"id": run["id"]},
        )
        await self._append_event(
            connection,
            run["id"],
            run["user_id"],
            "interrupt",
            {"type": "INPUT", "message": content, "cards": card["cards"]},
            run["attempt"],
        )
