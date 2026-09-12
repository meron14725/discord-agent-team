"""リポジトリ別Discord作業空間と案件索引の決定論的な投影。"""

from dataclasses import dataclass, field

from sqlalchemy import select

from .consultations import stable_topic_id
from .db import Event, Outbox, Task, TopicThread
from .policy import GuardError


@dataclass(frozen=True)
class TopicLink:
    topic_id: str
    title: str
    discord_url: str
    issue_comment_url: str = ""


@dataclass(frozen=True)
class WorkItemIndex:
    issue_number: int
    issue_url: str
    requirements_hash: str
    task_id: str
    previous_task_id: str = ""
    state: str = ""
    branch: str = ""
    commit_sha: str = ""
    plan_version: int = 0
    plan_hash: str = ""
    explanation_hashes: tuple[str, ...] = ()
    topics: tuple[TopicLink, ...] = field(default_factory=tuple)


def render_index(item: WorkItemIndex) -> str:
    if item.issue_number <= 0 or not item.issue_url.startswith("https://github.com/"):
        raise GuardError("Invalid work-item Issue")
    lines = [
        f"# Issue #{item.issue_number} 作業索引",
        "",
        "## 正本",
        "",
        f"- 要件Issue: {item.issue_url}",
        f"- 要件本文hash: `{item.requirements_hash}`",
        "- 要件本文はGitHub Issueだけを正本とし、ここには複製しない。",
        "",
        "## 現在の実行",
        "",
        "| task | 前task | 状態 | branch | commit | 計画版 | 計画hash |",
        "| --- | --- | --- | --- | --- | ---: | --- |",
        (
            f"| {item.task_id} | {item.previous_task_id or '-'} | {item.state or '-'} | "
            f"{item.branch or '-'} | {item.commit_sha or '-'} | {item.plan_version or '-'} | "
            f"{item.plan_hash or '-'} |"
        ),
        "",
        "## 話題スレッド",
        "",
    ]
    if item.topics:
        lines.extend(
            f"- `{topic.topic_id}` [{topic.title}]({topic.discord_url})"
            + (f" ([Issue記録]({topic.issue_comment_url}))" if topic.issue_comment_url else "")
            for topic in sorted(item.topics, key=lambda value: value.topic_id)
        )
    else:
        lines.append("- なし")
    lines.extend(("", "## 説明成果物hash", ""))
    lines.extend(f"- `{value}`" for value in item.explanation_hashes)
    if not item.explanation_hashes:
        lines.append("- なし")
    return "\n".join(lines) + "\n"


class WorkspaceService:
    def __init__(self, settings):
        self.settings = settings

    def create_topic(
        self,
        session,
        task,
        *,
        origin_event_id: str,
        purpose: str,
        title: str,
        owner_confirmation: bool = False,
    ) -> TopicThread:
        topic_id = stable_topic_id(origin_event_id, purpose)
        existing = session.scalar(
            select(TopicThread).where(
                TopicThread.task_id == task.id,
                TopicThread.topic_id == topic_id,
            )
        )
        if existing:
            return existing
        from .policy import digest

        topic = TopicThread(
            task_id=task.id,
            topic_id=topic_id,
            origin_event_id=origin_event_id,
            purpose_hash=digest(" ".join(purpose.casefold().split())),
        )
        session.add(topic)
        session.flush()
        if owner_confirmation:
            session.add(
                Event(
                    task_id=task.id,
                    source="owner-topic",
                    external_id=topic.id,
                    data={"topic_record_id": topic.id, "reminded_at": 0},
                )
            )
        session.add(
            Outbox(
                task_id=task.id,
                data={
                    "role": "coordinator",
                    "body": purpose,
                    "channel_id": task.data["project_channel_id"],
                    "create_topic_thread": True,
                    "topic_record_id": topic.id,
                    "topic_id": topic_id,
                    "thread_name": title[:90],
                    "owner_confirmation": owner_confirmation,
                },
            )
        )
        return topic

    @staticmethod
    def enqueue_due_owner_reminders(session, now: float) -> int:
        """Remind the owner once when an unresolved decision topic is 24 hours old."""
        queued = 0
        waiting_states = {"AwaitingRequirementsConfirmation", "AwaitingPlanApproval", "Blocked"}
        for marker in session.scalars(
            select(Event).where(
                Event.source == "owner-topic",
                Event.created <= now - 86400,
            )
        ):
            if marker.data.get("reminded_at"):
                continue
            topic = session.get(TopicThread, marker.data["topic_record_id"])
            task = session.get(Task, marker.task_id)
            if topic is None or task is None or topic.status != "open" or task.state not in waiting_states:
                continue
            session.add(
                Outbox(
                    task_id=task.id,
                    data={
                        "role": "coordinator",
                        "body": "この論点はオーナー判断待ちのまま24時間経過しました。確認をお願いします。",
                        "thread_id": topic.thread_id or task.thread_id,
                        "mention_owner": True,
                    },
                )
            )
            marker.data = {**marker.data, "reminded_at": now}
            queued += 1
        return queued

    @staticmethod
    def resolve_topic(session, topic: TopicThread, now: float) -> None:
        if topic.status != "open":
            return
        topic.status = "resolved"
        topic.resolved_at = now
        topic.archive_after = now + 86400

    @staticmethod
    def enqueue_due_archives(session, now: float) -> int:
        queued = 0
        for topic in session.scalars(
            select(TopicThread).where(
                TopicThread.status == "resolved",
                TopicThread.archive_after <= now,
                TopicThread.thread_id.is_not(None),
            )
        ):
            session.add(
                Outbox(
                    task_id=topic.task_id,
                    data={
                        "role": "security_sre",
                        "body": "解決から24時間経過した話題スレッドをアーカイブします。",
                        "archive_topic_thread": True,
                        "topic_record_id": topic.id,
                        "thread_id": topic.thread_id,
                    },
                )
            )
            topic.status = "archive_queued"
            queued += 1
        return queued
