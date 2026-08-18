"""只记录模型调用元数据的数据库审计适配器。"""

from __future__ import annotations

import logging
from uuid import uuid4

from sqlalchemy import text

from ai_butler.adapters.llm import ModelInvocation
from ai_butler.infrastructure.database import AsyncDatabase

logger = logging.getLogger(__name__)


class DatabaseModelInvocationRecorder:
    def __init__(self, database: AsyncDatabase) -> None:
        self._database = database

    async def record(self, invocation: ModelInvocation) -> None:
        try:
            async with self._database.transaction() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO model_invocations("
                        "id,request_id,run_id,task,provider,model,prompt_version,schema_version,"
                        "attempt,route_role,status,input_tokens,cached_input_tokens,output_tokens,"
                        "duration_ms,error_class) VALUES("
                        ":id,:request_id,:run_id,:task,:provider,:model,:prompt_version,"
                        ":schema_version,:attempt,:route_role,:status,:input_tokens,"
                        ":cached_input_tokens,:output_tokens,:duration_ms,:error_class)"
                    ),
                    {
                        "id": uuid4(),
                        "request_id": invocation.request_id,
                        "run_id": invocation.run_id,
                        "task": invocation.task.value,
                        "provider": invocation.provider,
                        "model": invocation.model,
                        "prompt_version": invocation.prompt_version,
                        "schema_version": invocation.schema_version,
                        "attempt": invocation.attempt,
                        "route_role": invocation.route_role,
                        "status": invocation.status,
                        "input_tokens": invocation.input_tokens,
                        "cached_input_tokens": invocation.cached_input_tokens,
                        "output_tokens": invocation.output_tokens,
                        "duration_ms": invocation.duration_ms,
                        "error_class": invocation.error_class,
                    },
                )
                if invocation.run_id is not None and invocation.status == "SUCCEEDED":
                    # 汇总只关联工作流内调用；会话预检没有 run_id，不能错误计入后续 run。
                    # Token 统计覆盖所有实际成功调用，主备模型字段则忽略影子评测。
                    await connection.execute(
                        text(
                            "UPDATE agent_runs SET input_tokens=input_tokens+:input_tokens,"
                            "output_tokens=output_tokens+:output_tokens,"
                            "model_provider=CASE WHEN :public THEN :provider "
                            "ELSE model_provider END,"
                            "model_name=CASE WHEN :public THEN :model ELSE model_name END,"
                            "updated_at=now() "
                            "WHERE id=:run_id"
                        ),
                        {
                            "run_id": invocation.run_id,
                            "input_tokens": invocation.input_tokens,
                            "output_tokens": invocation.output_tokens,
                            "public": invocation.route_role != "SHADOW",
                            "provider": invocation.provider,
                            "model": invocation.model,
                        },
                    )
        except Exception:
            # 可观测性写入不得把已经成功的用户请求改写为失败；日志不含请求正文。
            logger.exception("model invocation audit write failed")
