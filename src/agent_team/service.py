import time

from sqlalchemy import select

from .db import (
    Approval,
    ApprovalGrant,
    Event,
    Job,
    Operation,
    Outbox,
    RepositoryLease,
    Spec,
    Task,
    task_lock,
    uid,
)
from .policy import GuardError
from .workflow import WorkflowV2Service

STOPPED = {"Paused", "Blocked", "Cancelled", "Merged"}


def notify(s, task, body, role="upstream", **extra):
    if task.workflow_version == 2 and task.data.get("project_channel_id"):
        extra = {
            "project_status": True,
            "channel_id": task.data["project_channel_id"],
            **extra,
        }
    s.add(Outbox(task_id=task.id, data={"thread_id": task.thread_id, "role": role, "body": body, **extra}))


def transition(s, task, state, reason=""):
    old = task.state
    task.state, task.state_version = state, task.state_version + 1
    task.data = {**task.data, "reason": reason}
    s.add(
        Event(
            task_id=task.id,
            source="state",
            external_id=uid(),
            data={"from": old, "to": state, "reason": reason, "version": task.state_version},
        )
    )
    notify(s, task, f"{task.id}: {state}" + (f" — {reason}" if reason else ""))


def enqueue(s, task, kind):
    role = "downstream" if kind in {"implement", "fix"} else "upstream"
    s.add(
        Job(
            task_id=task.id,
            role=role,
            kind=kind,
            data={"spec_version": task.spec_version, "spec_hash": task.data.get("spec_hash", "")},
        )
    )


def invalidate(s, task):
    for j in s.scalars(select(Job).where(Job.task_id == task.id, Job.status.in_(["queued", "running"]))):
        j.status, j.fence = "cancelled", j.fence + 1
    task.data = {k: v for k, v in task.data.items() if k not in {"review_approval", "merge_approval"}}


class TaskService:
    def __init__(self, db, settings):
        self.db, self.settings = db, settings
        self.v2 = WorkflowV2Service(db, settings)

    def authorize(self, actor, guild, channel, task=None, bot=False):
        if bot or actor not in self.settings.owner_ids or guild != self.settings.guild_id:
            raise GuardError("Unauthorized actor or guild")
        allowed = {self.settings.channel_id}
        allowed.update(self.settings.workflow_v2.project_channels.values())
        if task and task.thread_id:
            allowed.add(task.thread_id)
        if task:
            allowed.update(task.data.get("topic_thread_ids", []))
        if channel not in allowed:
            raise GuardError("Unauthorized channel/thread")

    def command(
        self,
        *,
        action,
        event_id,
        actor,
        guild,
        channel,
        task_id="",
        text="",
        repo="",
        version=0,
        hash="",
        head_sha="",
        base_sha="",
        confirmation_id="",
        bot=False,
    ):
        with self.db.transaction() as s:
            task = task_lock(s, task_id) if task_id else None
            self.authorize(actor, guild, channel, task, bot)
            previous = s.scalar(select(Event).where(Event.source == "discord", Event.external_id == event_id))
            if previous:
                return self.serialize(s.get(Task, previous.task_id))
            if action == "request":
                if task or repo not in self.settings.repos or not text.strip():
                    raise GuardError("Request requires allowlisted repo and summary")
                task_id = "TASK-" + uid()[:12]
                configured = self.settings.repos[repo]
                owner, prefix = configured.repository.split("/")
                repository = (
                    f"{owner}/{prefix}-{task_id.lower()}" if configured.per_task else configured.repository
                )
                if self.v2.selected(repo):
                    task = self.v2.create_task(
                        s,
                        task_id=task_id,
                        repo=repo,
                        repository=repository,
                        summary=text,
                    )
                else:
                    task = Task(
                        id=task_id,
                        repo=repo,
                        data={"summary": text, "answers": [], "repository": repository},
                    )
                    s.add(task)
                    s.flush()
                    notify(s, task, "案件を受け付けました。要件を整理します。", create_thread=True)
                    enqueue(s, task, "clarify")
            elif task is None:
                raise GuardError("Task is required")
            elif task.workflow_version == 2 and action == "approve_requirements":
                self.v2.approve_requirements_in_session(
                    s,
                    task,
                    actor=actor,
                    target_hash=hash,
                    confirmation_id=confirmation_id,
                )
            elif task.workflow_version == 2 and action == "approve_plan":
                self.v2.approve_plan_in_session(
                    s,
                    task,
                    actor=actor,
                    target_hash=hash,
                    target_sha=base_sha,
                    confirmation_id=confirmation_id,
                )
            elif task.workflow_version == 2 and action in {"answer", "revise"}:
                if task.state in {"Cancelled", "Merged", "Merging"}:
                    raise GuardError("Cannot revise this task")
                if not text.strip():
                    raise GuardError("Answer cannot be empty")
                invalidate(s, task)
                task.data = {
                    **task.data,
                    "answers": task.data.get("answers", []) + [text],
                    "requirements_approval_id": "",
                    "plan_approval_id": "",
                }
                transition(s, task, "DraftingRequirements")
                s.add(
                    Job(
                        task_id=task.id,
                        role="cto",
                        kind="draft_requirements",
                        data={
                            "workflow_version": 2,
                            "requirements_hash": task.data.get("requirements_hash", ""),
                            "plan_version": 0,
                            "plan_hash": "",
                            "base_sha": task.data.get("base_sha", ""),
                        },
                    )
                )
            elif task.workflow_version == 2 and action == "resume" and task.state == "Cancelled":
                raise GuardError("Cancelled v2 work must restart as a new task")
            elif task.workflow_version == 2 and action == "restart":
                previous = task
                task = self.v2.restart_cancelled(
                    s,
                    previous,
                    task_id="TASK-" + uid()[:12],
                )
            elif action == "status":
                pass
            elif action in {"answer", "revise"}:
                if task.state in {"Cancelled", "Merged", "Merging"}:
                    raise GuardError("Cannot revise this task")
                if action == "answer" and task.state not in {"Clarifying", "AwaitingSpecApproval"}:
                    raise GuardError("Use /revise to change approved requirements")
                if not text.strip():
                    raise GuardError("Answer cannot be empty")
                invalidate(s, task)
                task.data = {
                    **task.data,
                    "answers": task.data.get("answers", []) + [text],
                    "spec_approval": None,
                }
                # A generation increment fences even clarification jobs before a new spec is saved.
                task.spec_version += 1
                transition(s, task, "Clarifying")
                enqueue(s, task, "clarify")
            elif action == "approve_spec":
                if (
                    task.state != "AwaitingSpecApproval"
                    or version != task.spec_version
                    or hash != task.data.get("spec_hash")
                ):
                    raise GuardError("Stale specification approval")
                task.data = {**task.data, "spec_approval": hash}
                s.add(
                    Approval(
                        task_id=task.id,
                        data={"kind": "spec", "version": version, "hash": hash, "actor": actor},
                    )
                )
                transition(s, task, "Queued")
                enqueue(s, task, "implement")
            elif action == "approve_merge":
                authority_key = "requirements_hash" if task.workflow_version == 2 else "spec_hash"
                if task.state != "AwaitingMergeApproval" or (head_sha, base_sha, hash) != tuple(
                    task.data.get(k) for k in ("head_sha", "base_sha", authority_key)
                ):
                    raise GuardError("Stale merge approval")
                approval = {
                    "commits": [head_sha, base_sha, hash]
                    + ([task.data.get("plan_hash", "")] if task.workflow_version == 2 else []),
                    "actor": actor,
                    "expires": time.time() + self.settings.approval_seconds,
                }
                task.data = {**task.data, "merge_approval": approval}
                s.add(Approval(task_id=task.id, data={"kind": "merge", **approval}))
                transition(s, task, "ReadyToMerge")
            elif action in {"pause", "cancel"}:
                if task.state in {"Cancelled", "Merged"}:
                    raise GuardError("Task is terminal")
                old = task.state
                invalidate(s, task)
                task.data = {
                    **task.data,
                    "resume_state": old
                    if old not in STOPPED
                    else task.data.get("resume_state", "Clarifying"),
                }
                if task.workflow_version == 2 and action == "cancel":
                    for approval in s.scalars(
                        select(ApprovalGrant).where(ApprovalGrant.task_id == task.id)
                    ):
                        approval.expires = time.time()
                    for outbox in s.scalars(
                        select(Outbox).where(Outbox.task_id == task.id, Outbox.sent.is_(False))
                    ):
                        outbox.sent = True
                        outbox.data = {**outbox.data, "cancelled": True}
                    job_ids = list(s.scalars(select(Job.id).where(Job.task_id == task.id)))
                    if job_ids:
                        for lease in s.scalars(
                            select(RepositoryLease).where(RepositoryLease.job_id.in_(job_ids))
                        ):
                            s.delete(lease)
                    issue_number = task.data.get("requirements_issue")
                    if issue_number:
                        s.add(
                            Operation(
                                task_id=task.id,
                                key=f"issue-close:{task.id}",
                                status="pending",
                                data={
                                    "repository": task.data["repository"],
                                    "issue_number": issue_number,
                                    "attempts": 0,
                                },
                            )
                        )
                transition(s, task, "Paused" if action == "pause" else "Cancelled", text or action)
            elif action in {"resume", "retry"}:
                expected = "Paused" if action == "resume" else "Blocked"
                if task.state != expected:
                    raise GuardError(f"Expected {expected}")
                recoverable = s.scalar(
                    select(Job)
                    .where(Job.task_id == task.id, Job.status == "failed")
                    .order_by(Job.created.desc())
                )
                if (
                    action == "retry"
                    and recoverable
                    and recoverable.data.get("response")
                    and recoverable.data["spec_version"] == task.spec_version
                    and recoverable.data["spec_hash"] == task.data.get("spec_hash")
                ):
                    recoverable.status = "queued"
                    recoverable.attempt = 0
                    transition(
                        s,
                        task,
                        "Reviewing" if recoverable.kind == "review" else "Queued",
                        "保存済み成果物と外部操作を照合",
                    )
                elif task.data.get("pr"):
                    transition(s, task, "Reviewing", "GitHub再照合後に新規レビュー")
                    enqueue(s, task, "review")
                elif task.data.get("spec_approval"):
                    transition(s, task, "Queued")
                    enqueue(s, task, "implement")
                else:
                    transition(s, task, "Clarifying")
                    enqueue(s, task, "clarify")
            else:
                raise GuardError("Unknown command")
            s.add(
                Event(
                    task_id=task.id,
                    source="discord",
                    external_id=event_id,
                    data={"action": action, "actor": actor},
                )
            )
            s.flush()
            return self.serialize(task)

    @staticmethod
    def serialize(task):
        return {
            "id": task.id,
            "repo": task.repo,
            "thread_id": task.thread_id,
            "state": task.state,
            "version": task.spec_version,
            "workflow_version": task.workflow_version,
            "data": task.data,
        }

    def status(self, task_id):
        with self.db.transaction() as s:
            return self.serialize(task_lock(s, task_id))

    def spec(self, task_id):
        with self.db.transaction() as s:
            t = task_lock(s, task_id)
            return s.scalar(select(Spec).where(Spec.task_id == t.id, Spec.version == t.spec_version))
