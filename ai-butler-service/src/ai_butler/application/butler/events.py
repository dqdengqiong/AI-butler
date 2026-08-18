from __future__ import annotations

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from .shared import (
    _json,
)


class EventService:
    def __init__(self) -> None:
        pass

    async def _append_event(
        self,
        connection: AsyncConnection,
        run_id: UUID,
        user_id: UUID,
        event_type: str,
        payload: dict[str, object],
        attempt: int,
    ) -> int:
        """在同一事务原子分配 sequence 并插入持久化事件。"""

        sequence = int(
            (
                await connection.execute(
                    text(
                        "UPDATE agent_runs SET last_event_sequence=last_event_sequence+1 "
                        "WHERE id=:id RETURNING last_event_sequence"
                    ),
                    {"id": run_id},
                )
            ).scalar_one()
        )
        await connection.execute(
            text(
                "INSERT INTO agent_run_events(agent_run_id,user_id,sequence,event_type,attempt,payload) "
                "VALUES(:run_id,:user_id,:sequence,:event_type,:attempt,CAST(:payload AS jsonb))"
            ),
            {
                "run_id": run_id,
                "user_id": user_id,
                "sequence": sequence,
                "event_type": event_type,
                "attempt": attempt,
                "payload": _json(payload),
            },
        )
        return sequence
