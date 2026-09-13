"""Issue正本のv2開発フローを扱うアプリケーションサービス。"""

import time

from sqlalchemy import func, select

from .db import (
    ApprovalGrant,
    Event,
    ExecutionBudget,
    Job,
    Outbox,
    PlanVersion,
    ProjectWorkspace,
    RequirementsReference,
    Task,
    task_lock,
    uid,
)
from .delegations import DelegationService
from .discord_delivery import task_next_step, task_waits_for_owner
from .planning import validate_plan, validate_review_coverage
from .policy import GuardError
from .requirements import bind_requirements

V2_ACTIVE_STATES = {
    "DraftingRequirements",
    "AwaitingRequirementsConfirmation",
    "PlanningImplementation",
    "ReviewingImplementationPlan",
    "AwaitingPlanApproval",
    "Queued",
    "Implementing",
    "Fixing",
    "Reviewing",
    "AwaitingChecks",
    "AwaitingMergeApproval",
    "ReadyToMerge",
    "Merging",
}


def _notify(session, task, body, role="coordinator", **extra):
    extra = {
        "next_action": task_next_step(task.state),
        "mention_owner": task_waits_for_owner(task.state),
        **extra,
    }
    if task.workflow_version == 2 and task.data.get("project_channel_id"):
        extra = {
            "project_status": True,
            "channel_id": task.data["project_channel_id"],
            **extra,
        }
    session.add(
        Outbox(
            task_id=task.id,
            data={"thread_id": task.thread_id, "role": role, "body": body, **extra},
        )
    )


def _transition(session, task, state, reason=""):
    old = task.state
    task.state = state
    task.state_version += 1
    task.data = {**task.data, "reason": reason}
    session.add(
        Event(
            task_id=task.id,
            source="state",
            external_id=uid(),
            data={"from": old, "to": state, "reason": reason, "version": task.state_version},
        )
    )
    _notify(session, task, f"{task.id}: {state}" + (f" — {reason}" if reason else ""))


def _job_identity(task):
    return {
        "workflow_version": 2,
        "requirements_hash": task.data.get("requirements_hash", ""),
        "plan_version": task.data.get("plan_version", 0),
        "plan_hash": task.data.get("plan_hash", ""),
        "base_sha": task.data.get("base_sha", ""),
        "head_sha": task.data.get("head_sha", ""),
    }


def _enqueue(session, task, kind, role):
    session.add(Job(task_id=task.id, role=role, kind=kind, data=_job_identity(task)))


class WorkflowV2Service:
    def __init__(self, db, settings):
        self.db = db
        self.settings = settings

    def selected(self, repo_alias: str) -> bool:
        config = self.settings.workflow_v2
        return config.enabled and repo_alias in config.repository_aliases

    def create_task(
        self, session, *, task_id: str, repo: str, repository: str, summary: str
    ):
        channel_id = self.settings.workflow_v2.project_channels.get(
            repo, self.settings.channel_id
        )
        workspace = session.scalar(
            select(ProjectWorkspace).where(ProjectWorkspace.repository == repository)
        )
        if workspace is None:
            workspace = ProjectWorkspace(
                repository=repository,
                discord_channel_id=channel_id,
            )
            session.add(workspace)
        task = Task(
            id=task_id,
            repo=repo,
            workflow_version=2,
            state="DraftingRequirements",
            data={
                "summary": summary,
                "answers": [],
                "workflow_version": 2,
                "repository": repository,
                "project_channel_id": channel_id,
            },
        )
        session.add(task)
        session.flush()
        session.add(ExecutionBudget(task_id=task.id))
        _notify(
            session,
            task,
            "案件を受け付けました。CTOが目的を整理し、要件Issueを作成します。",
            create_status=True,
        )
        _enqueue(session, task, "draft_requirements", "cto")
        return task

    def register_requirements(
        self,
        task_id: str,
        *,
        repository: str,
        issue_number: int,
        issue_url: str,
        updated_at: str,
        body: str,
        explanation_hash: str,
    ):
        identity = bind_requirements(
            repository=repository,
            issue_number=issue_number,
            issue_url=issue_url,
            updated_at=updated_at,
            body=body,
        )
        if not explanation_hash.startswith("sha256:"):
            raise GuardError("Requirements explanation is required before approval")
        with self.db.transaction() as session:
            task = task_lock(session, task_id)
            if task.workflow_version != 2 or task.state != "DraftingRequirements":
                raise GuardError("Task is not drafting v2 requirements")
            reference = session.scalar(
                select(RequirementsReference).where(
                    RequirementsReference.repository == repository,
                    RequirementsReference.issue_number == issue_number,
                )
            )
            if reference and reference.task_id != task.id:
                previous = session.get(Task, reference.task_id)
                if (
                    previous is None
                    or previous.state != "Cancelled"
                    or task.data.get("previous_task_id") != previous.id
                ):
                    raise GuardError("Issue authority is already bound to another active task")
                reference.task_id = task.id
            if reference is None:
                reference = RequirementsReference(
                    task_id=task.id,
                    repository=repository,
                    issue_number=issue_number,
                    issue_url=issue_url,
                    body_hash=identity.body_hash,
                    github_updated_at=updated_at,
                    status="awaiting_approval",
                )
                session.add(reference)
                session.flush()
            else:
                reference.body_hash = identity.body_hash
                reference.github_updated_at = updated_at
                reference.status = "awaiting_approval"
            task.requirements_reference_id = reference.id
            task.spec_version += 1
            task.data = {
                **task.data,
                "requirements_hash": identity.body_hash,
                "requirements_updated_at": updated_at,
                "requirements_issue": issue_number,
                "requirements_url": issue_url,
                "requirements_acceptance_ids": list(identity.acceptance_ids),
                "requirements_explanation_hash": explanation_hash,
            }
            _transition(session, task, "AwaitingRequirementsConfirmation")
            confirmation_id = "REQ-" + uid().replace("-", "")[:16]
            task.data = {**task.data, "requirements_confirmation_id": confirmation_id}
            _notify(
                session,
                task,
                "要件定義と説明資料が完成しました。内容を確認して承認してください。",
                role="cto",
                approval="requirements",
                hash=identity.body_hash,
                confirmation_id=confirmation_id,
                artifact_ids=(
                    [task.data["requirements_explanation_artifact_id"]]
                    if task.data.get("requirements_explanation_artifact_id")
                    else []
                ),
            )
            return self.serialize(task)

    def restart_cancelled(self, session, previous: Task, *, task_id: str):
        if previous.workflow_version != 2 or previous.state != "Cancelled":
            raise GuardError("Only a cancelled v2 task can be restarted")
        task = self.create_task(
            session,
            task_id=task_id,
            repo=previous.repo,
            repository=previous.data["repository"],
            summary=previous.data["summary"],
        )
        preserved = {
            key: previous.data[key]
            for key in (
                "requirements_issue",
                "requirements_url",
                "issue",
                "provisioned",
            )
            if previous.data.get(key)
        }
        task.data = {
            **task.data,
            **preserved,
            "previous_task_id": previous.id,
            "branch": (
                f"agent/issue-{preserved['requirements_issue']}-{task.id.lower()}-r1"
                if preserved.get("requirements_issue")
                else ""
            ),
        }
        if preserved.get("requirements_issue"):
            from .workspaces import WorkspaceService

            WorkspaceService(self.settings).create_topic(
                session,
                task,
                origin_event_id=f"restart-{previous.id}",
                purpose="中止済み案件を新しいtaskとして再開し、要件承認を取り直します。",
                title=f"{task.id} 再開要件確認",
            )
        return task

    def approve_requirements(
        self, task_id: str, *, actor: str, target_hash: str, confirmation_id: str
    ):
        with self.db.transaction() as session:
            task = task_lock(session, task_id)
            return self.approve_requirements_in_session(
                session,
                task,
                actor=actor,
                target_hash=target_hash,
                confirmation_id=confirmation_id,
            )

    def approve_requirements_in_session(
        self, session, task, *, actor: str, target_hash: str, confirmation_id: str
    ):
        if actor not in self.settings.workflow_v2.requirements_approver_ids:
            raise GuardError("Actor cannot approve requirements")
        expected = (
            task.state == "AwaitingRequirementsConfirmation"
            and target_hash == task.data.get("requirements_hash")
            and confirmation_id == task.data.get("requirements_confirmation_id")
        )
        if not expected:
            raise GuardError("Stale requirements approval")
        reference = session.get(RequirementsReference, task.requirements_reference_id)
        if reference is None or reference.body_hash != target_hash:
            raise GuardError("Requirements authority mismatch")
        if session.scalar(
            select(ApprovalGrant).where(ApprovalGrant.confirmation_id == confirmation_id)
        ):
            raise GuardError("Approval confirmation was already consumed")
        session.add(
            ApprovalGrant(
                task_id=task.id,
                stage="requirements",
                target_hash=target_hash,
                confirmation_id=confirmation_id,
                actor_id=actor,
                consumed_at=time.time(),
            )
        )
        reference.status = "approved"
        task.data = {**task.data, "requirements_approval_id": confirmation_id}
        from .db import TopicThread

        topic = session.scalar(
            select(TopicThread)
            .where(TopicThread.task_id == task.id)
            .order_by(TopicThread.created)
        )
        if topic:
            DelegationService(self.settings).create(
                session,
                task.id,
                topic_id=topic.topic_id,
                source_role="cto",
                target_role="backend_integrator",
                purpose="承認済み要件Issueを基に実装計画を作成する",
                expected_artifact=f"docs/work-items/issue-{reference.issue_number}/plans/vN.md",
            )
        _transition(session, task, "PlanningImplementation")
        _enqueue(session, task, "plan", "backend_integrator")
        return self.serialize(task)

    def register_plan(self, task_id: str, *, body: str, requirements_body: str, base_sha: str):
        with self.db.transaction() as session:
            task = task_lock(session, task_id)
            if task.workflow_version != 2 or task.state != "PlanningImplementation":
                raise GuardError("Task is not planning implementation")
            reference = session.get(RequirementsReference, task.requirements_reference_id)
            if reference is None or reference.status != "approved":
                raise GuardError("Requirements are not approved")
            version = (
                session.scalar(
                    select(func.max(PlanVersion.version)).where(
                        PlanVersion.requirements_reference_id == reference.id
                    )
                )
                or 0
            ) + 1
            identity = validate_plan(
                body=body,
                requirements_body=requirements_body,
                issue_number=reference.issue_number,
                version=version,
                base_sha=base_sha,
            )
            plan = PlanVersion(
                task_id=task.id,
                requirements_reference_id=reference.id,
                version=version,
                path=identity.path,
                content_hash=identity.content_hash,
                base_sha=base_sha,
                review_status="pending",
            )
            session.add(plan)
            session.flush()
            task.current_plan_version_id = plan.id
            task.data = {
                **task.data,
                "plan_version": version,
                "plan_path": identity.path,
                "plan_hash": identity.content_hash,
                "base_sha": base_sha,
            }
            _transition(session, task, "ReviewingImplementationPlan")
            _enqueue(session, task, "review_plan", "cto")
            return self.serialize(task)

    def complete_plan_review(
        self,
        task_id: str,
        *,
        plan_hash: str,
        coverage: list[dict],
        findings: list[dict],
        explanation_hash: str,
    ):
        with self.db.transaction() as session:
            task = task_lock(session, task_id)
            plan = session.get(PlanVersion, task.current_plan_version_id)
            if (
                task.state != "ReviewingImplementationPlan"
                or plan is None
                or plan.content_hash != plan_hash
            ):
                raise GuardError("Stale implementation plan review")
            try:
                validate_review_coverage(
                    tuple(task.data.get("requirements_acceptance_ids", [])), coverage, findings
                )
            except GuardError as error:
                budget = session.scalar(
                    select(ExecutionBudget)
                    .where(ExecutionBudget.task_id == task.id)
                    .with_for_update()
                )
                plan.review_status = "changes_requested"
                task.data = {
                    **task.data,
                    "findings": findings,
                    "plan_review_reason": str(error),
                }
                if budget.plan_revision_reservations >= (
                    self.settings.workflow_v2.plan_revision_limit
                ):
                    from .workspaces import WorkspaceService

                    WorkspaceService(self.settings).create_topic(
                        session,
                        task,
                        origin_event_id=f"plan-review-limit-{plan.id}",
                        purpose=(
                            "実装計画の修正を3回行いましたが、重大な指摘が残っています。"
                            "指摘内容と次の方針を確認してください。"
                        ),
                        title=f"{task.id} 計画レビュー停止",
                        owner_confirmation=True,
                    )
                    _transition(session, task, "Blocked", "実装計画の修正回数上限")
                else:
                    budget.plan_revision_reservations += 1
                    _transition(session, task, "PlanningImplementation", str(error))
                    _enqueue(session, task, "plan", "backend_integrator")
                return self.serialize(task)
            if not explanation_hash.startswith("sha256:"):
                raise GuardError("Plan explanation is required before approval")
            plan.review_status = "approved"
            confirmation_id = "PLAN-" + uid().replace("-", "")[:16]
            task.data = {
                **task.data,
                "plan_explanation_hash": explanation_hash,
                "plan_confirmation_id": confirmation_id,
            }
            _transition(session, task, "AwaitingPlanApproval")
            _notify(
                session,
                task,
                "独立CTOレビューと計画説明が完了しました。実装計画を確認して承認してください。",
                role="cto",
                approval="plan",
                hash=plan_hash,
                base_sha=plan.base_sha,
                confirmation_id=confirmation_id,
                artifact_ids=(
                    [task.data["plan_explanation_artifact_id"]]
                    if task.data.get("plan_explanation_artifact_id")
                    else []
                ),
            )
            return self.serialize(task)

    def approve_plan(
        self,
        task_id: str,
        *,
        actor: str,
        target_hash: str,
        target_sha: str,
        confirmation_id: str,
    ):
        with self.db.transaction() as session:
            task = task_lock(session, task_id)
            return self.approve_plan_in_session(
                session,
                task,
                actor=actor,
                target_hash=target_hash,
                target_sha=target_sha,
                confirmation_id=confirmation_id,
            )

    def approve_plan_in_session(
        self,
        session,
        task,
        *,
        actor: str,
        target_hash: str,
        target_sha: str,
        confirmation_id: str,
    ):
        if actor not in self.settings.workflow_v2.plan_approver_ids:
            raise GuardError("Actor cannot approve implementation plans")
        plan = session.get(PlanVersion, task.current_plan_version_id)
        if (
            task.state != "AwaitingPlanApproval"
            or plan is None
            or plan.review_status != "approved"
            or target_hash != plan.content_hash
            or target_sha != plan.base_sha
            or confirmation_id != task.data.get("plan_confirmation_id")
        ):
            raise GuardError("Stale implementation plan approval")
        if session.scalar(
            select(ApprovalGrant).where(ApprovalGrant.confirmation_id == confirmation_id)
        ):
            raise GuardError("Approval confirmation was already consumed")
        session.add(
            ApprovalGrant(
                task_id=task.id,
                stage="plan",
                target_hash=target_hash,
                target_sha=target_sha,
                confirmation_id=confirmation_id,
                actor_id=actor,
                consumed_at=time.time(),
            )
        )
        task.data = {**task.data, "plan_approval_id": confirmation_id}
        _transition(session, task, "Queued")
        _enqueue(session, task, "implement", "backend_integrator")
        return self.serialize(task)

    def reserve_model_call(self, task_id: str, *, consultation_topic: str = "", revision=""):
        config = self.settings.workflow_v2
        with self.db.transaction() as session:
            task_lock(session, task_id)
            budget = session.scalar(
                select(ExecutionBudget)
                .where(ExecutionBudget.task_id == task_id)
                .with_for_update()
            )
            if budget is None:
                raise GuardError("Missing v2 execution budget")
            if budget.model_reservations >= config.model_calls_per_task:
                raise GuardError("Task model-call limit reached")
            if consultation_topic:
                if budget.consultation_reservations >= config.consultations_per_task:
                    raise GuardError("Internal consultation task limit reached")
                from .db import Consultation

                topic_count = session.scalar(
                    select(func.count()).select_from(Consultation).where(
                        Consultation.task_id == task_id,
                        Consultation.topic_id == consultation_topic,
                    )
                )
                if topic_count >= config.consultations_per_topic:
                    raise GuardError("Internal consultation topic limit reached")
                budget.consultation_reservations += 1
            if revision == "plan":
                if budget.plan_revision_reservations >= config.plan_revision_limit:
                    raise GuardError("Plan revision limit reached")
                budget.plan_revision_reservations += 1
            elif revision == "implementation":
                if budget.implementation_revision_reservations >= config.implementation_revision_limit:
                    raise GuardError("Implementation revision limit reached")
                budget.implementation_revision_reservations += 1
            elif revision:
                raise GuardError("Unknown revision budget")
            budget.model_reservations += 1

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
