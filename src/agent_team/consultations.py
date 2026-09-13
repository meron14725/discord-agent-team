"""内部相談の識別子と回数制限。"""

import re
from dataclasses import dataclass

from sqlalchemy import func, select

from .db import Consultation, ExecutionBudget, Job, TopicThread, task_lock
from .policy import GuardError, digest

TOPIC_ID = re.compile(r"^topic-[0-9a-f]{16}$")


@dataclass(frozen=True)
class ConsultationLimits:
    per_topic: int = 25
    per_task: int = 75
    model_calls_per_task: int = 150
    plan_fixes: int = 15
    implementation_fixes: int = 15


def stable_topic_id(origin_event_id: str, purpose: str) -> str:
    if not origin_event_id.strip() or not purpose.strip():
        raise GuardError("Topic origin and purpose are required")
    normalized = " ".join(purpose.casefold().split())
    return "topic-" + digest(origin_event_id + "\n" + normalized).removeprefix("sha256:")[:16]


def validate_topic_id(topic_id: str) -> None:
    if not TOPIC_ID.fullmatch(topic_id):
        raise GuardError("Invalid immutable topic ID")


def check_budget(
    *, topic_count: int, task_consultations: int, model_calls: int, limits: ConsultationLimits
) -> None:
    if topic_count >= limits.per_topic:
        raise GuardError("Internal consultation topic limit reached")
    if task_consultations >= limits.per_task:
        raise GuardError("Internal consultation task limit reached")
    if model_calls >= limits.model_calls_per_task:
        raise GuardError("Task model-call limit reached")


class ConsultationService:
    def __init__(self, db, settings):
        self.db = db
        self.settings = settings

    def request(
        self,
        task_id: str,
        *,
        topic_id: str,
        requester_role: str,
        consultant_role: str,
        question: str,
    ) -> str:
        validate_topic_id(topic_id)
        requester = self.settings.role_registry.role(requester_role)
        consultant = self.settings.role_registry.role(consultant_role)
        allowed = {
            self.settings.role_registry.resolve(role) for role in requester.consultable_roles
        }
        if not consultant.enabled or consultant.id not in allowed:
            raise GuardError("Consultation target is unavailable or outside role policy")
        if not question.strip():
            raise GuardError("Consultation question is required")
        with self.db.transaction() as session:
            task = task_lock(session, task_id)
            if task.workflow_version != 2:
                raise GuardError("Consultations require a v2 task")
            if not session.scalar(
                select(TopicThread).where(
                    TopicThread.task_id == task.id,
                    TopicThread.topic_id == topic_id,
                )
            ):
                raise GuardError("Consultation topic is not bound to the task")
            budget = session.scalar(
                select(ExecutionBudget)
                .where(ExecutionBudget.task_id == task.id)
                .with_for_update()
            )
            topic_count = session.scalar(
                select(func.count()).select_from(Consultation).where(
                    Consultation.task_id == task.id,
                    Consultation.topic_id == topic_id,
                )
            )
            if budget.consultation_reservations >= self.settings.workflow_v2.consultations_per_task:
                raise GuardError("Internal consultation task limit reached")
            if topic_count >= self.settings.workflow_v2.consultations_per_topic:
                raise GuardError("Internal consultation topic limit reached")
            record = Consultation(
                task_id=task.id,
                topic_id=topic_id,
                ordinal=topic_count + 1,
                requester_role=requester.id,
                consultant_role=consultant.id,
                question_summary=question,
                conclusion_summary="pending",
            )
            session.add(record)
            session.flush()
            budget.consultation_reservations += 1
            session.add(
                Job(
                    task_id=task.id,
                    role=consultant.id,
                    kind="consult",
                    data={
                        "workflow_version": 2,
                        "requirements_hash": task.data.get("requirements_hash", ""),
                        "plan_version": task.data.get("plan_version", 0),
                        "plan_hash": task.data.get("plan_hash", ""),
                        "base_sha": task.data.get("base_sha", ""),
                        "head_sha": task.data.get("head_sha", ""),
                        "consultation_id": record.id,
                    },
                )
            )
            return record.id
