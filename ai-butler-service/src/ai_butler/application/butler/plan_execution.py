"""计划草案与不可变审批生命周期的兼容门面。"""

from __future__ import annotations

from .bootstrap import BootstrapService
from .events import EventService
from .evidence_execution import EvidenceExecutionService
from .model_plan_draft import ModelPlanDraftService
from .plan_draft import PlanDraftService
from .plan_lifecycle import PlanLifecycleService


class PlanExecutionService:
    """保持现有服务装配接口，同时将计划职责拆分到独立模块。"""

    _work_item_objectives = staticmethod(PlanDraftService._work_item_objectives)

    def __init__(
        self,
        events: EventService,
        evidence: EvidenceExecutionService,
        bootstrap: BootstrapService,
    ) -> None:
        draft = PlanDraftService(events, evidence, bootstrap)
        model_draft = ModelPlanDraftService(events, evidence, bootstrap)
        lifecycle = PlanLifecycleService(events)
        self._create_plan_draft = draft._create_plan_draft
        self._create_model_plan_draft = model_draft._create_model_plan_draft
        self._regenerate_approval = lifecycle._regenerate_approval
        self._publish_revision = lifecycle._publish_revision
        self._materialize_approval = lifecycle._materialize_approval
        self._materialize_model_approval = lifecycle._materialize_model_approval
