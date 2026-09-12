"""Bot間の表示メッセージとは分離した、型付き依頼の規則。"""

from dataclasses import dataclass

from sqlalchemy import select

from .db import Delegation, Outbox, TopicThread, task_lock
from .policy import GuardError, digest


@dataclass(frozen=True)
class DelegationEnvelope:
    task_id: str
    topic_id: str
    source_role: str
    target_role: str
    purpose: str
    expected_artifact: str
    parent_id: str = ""

    @property
    def content_hash(self) -> str:
        return digest(
            "\n".join(
                (
                    self.task_id,
                    self.topic_id,
                    self.source_role,
                    self.target_role,
                    self.purpose,
                    self.expected_artifact,
                    self.parent_id,
                )
            )
        )


def validate_delegation(
    envelope: DelegationEnvelope,
    *,
    known_roles: set[str],
    allowed_targets: set[str],
    visited_roles: tuple[str, ...] = (),
    round_trip: int = 0,
) -> None:
    if envelope.source_role not in known_roles or envelope.target_role not in known_roles:
        raise GuardError("Unknown delegation role")
    if envelope.source_role == envelope.target_role:
        raise GuardError("A role cannot delegate to itself")
    if envelope.target_role not in allowed_targets:
        raise GuardError("Delegation target is outside the role policy")
    if envelope.target_role in visited_roles:
        raise GuardError("Delegation cycle detected")
    if not envelope.task_id or not envelope.topic_id:
        raise GuardError("Delegation must retain task and topic identity")
    if not envelope.purpose.strip() or not envelope.expected_artifact.strip():
        raise GuardError("Delegation purpose and expected artifact are required")
    if not 0 <= round_trip <= 2:
        raise GuardError("Specialist dialogue is limited to two round trips")


def discord_delegation_projection(
    envelope: DelegationEnvelope, *, source_mention: str, target_mention: str, status: str
) -> str:
    if status not in {"requested", "accepted", "questioned", "answered", "completed", "blocked"}:
        raise GuardError("Unknown delegation status")
    return (
        f"{source_mention} → {target_mention}\n"
        f"依頼: {envelope.purpose}\n期待成果: {envelope.expected_artifact}\n"
        f"状態: {status}\n`topic:{envelope.topic_id}`"
    )


class DelegationService:
    def __init__(self, settings):
        self.settings = settings

    def create(
        self,
        session,
        task_id: str,
        *,
        topic_id: str,
        source_role: str,
        target_role: str,
        purpose: str,
        expected_artifact: str,
        parent_delegation_id: str = "",
        round_trip: int = 0,
    ) -> Delegation:
        task = task_lock(session, task_id)
        source = self.settings.role_registry.role(source_role)
        target = self.settings.role_registry.role(target_role)
        envelope = DelegationEnvelope(
            task_id=task.id,
            topic_id=topic_id,
            source_role=source.id,
            target_role=target.id,
            purpose=purpose,
            expected_artifact=expected_artifact,
            parent_id=parent_delegation_id,
        )
        validate_delegation(
            envelope,
            known_roles=set(self.settings.role_registry.role_ids),
            allowed_targets={
                self.settings.role_registry.resolve(role) for role in source.consultable_roles
            },
            round_trip=round_trip,
        )
        topic = session.scalar(
            select(TopicThread).where(
                TopicThread.task_id == task.id,
                TopicThread.topic_id == topic_id,
            )
        )
        if topic is None:
            raise GuardError("Delegation topic is not bound to the task")
        record = Delegation(
            task_id=task.id,
            topic_id=topic_id,
            source_role=source.id,
            target_role=target.id,
            purpose=purpose,
            expected_artifact=expected_artifact,
            status="pending",
            content_hash=envelope.content_hash,
            parent_delegation_id=parent_delegation_id or None,
        )
        session.add(record)
        session.flush()
        session.add(
            Outbox(
                task_id=task.id,
                data={
                    "role": source.id,
                    "thread_id": topic.thread_id or task.thread_id,
                    "body": discord_delegation_projection(
                        envelope,
                        source_mention=source.display_name,
                        target_mention=target.display_name,
                        status="requested",
                    ),
                    "delegation_log": True,
                    "delegation_id": record.id,
                    "target_role": target.id,
                },
            )
        )
        return record
