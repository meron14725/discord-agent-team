import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from test_v2_domain import PLAN, REQUIREMENTS
from test_workflow import step

from agent_team.api import create_app
from agent_team.consultations import ConsultationService
from agent_team.db import (
    ApprovalGrant,
    Consultation,
    Delegation,
    Event,
    ExecutionBudget,
    Job,
    Operation,
    Outbox,
    PlanVersion,
    ProjectWorkspace,
    RepositoryLease,
    RequirementsReference,
    Task,
    TaskProjection,
    TopicThread,
)
from agent_team.policy import GuardError, digest
from agent_team.workflow import WorkflowV2Service
from agent_team.workspaces import WorkspaceService


def service_for(team):
    settings, db, *_ = team
    settings.workflow_v2.enabled = True
    settings.workflow_v2.repository_aliases = ["demo"]
    settings.workflow_v2.requirements_approver_ids = ["demo-owner"]
    settings.workflow_v2.plan_approver_ids = ["demo-owner"]
    return db, WorkflowV2Service(db, settings)


def create_v2(db, service):
    with db.transaction() as session:
        task = service.create_task(
            session,
            task_id="TASK-V2",
            repo="demo",
            repository="example/demo",
            summary="新しい開発フロー",
        )
        assert task.workflow_version == 2
    return task.id


def register_and_approve_requirements(db, service):
    task_id = create_v2(db, service)
    result = service.register_requirements(
        task_id,
        repository="owner/repo",
        issue_number=2,
        issue_url="https://github.com/owner/repo/issues/2",
        updated_at="2026-09-12T00:00:00Z",
        body=REQUIREMENTS,
        explanation_hash="sha256:explanation",
    )
    result = service.approve_requirements(
        task_id,
        actor="demo-owner",
        target_hash=result["data"]["requirements_hash"],
        confirmation_id=result["data"]["requirements_confirmation_id"],
    )
    return result


def test_v2_requirements_and_plan_have_two_separate_owner_gates(team):
    db, service = service_for(team)
    requirements = register_and_approve_requirements(db, service)
    assert requirements["state"] == "PlanningImplementation"

    plan = service.register_plan(
        requirements["id"], body=PLAN, requirements_body=REQUIREMENTS, base_sha="a" * 40
    )
    assert plan["state"] == "ReviewingImplementationPlan"
    reviewed = service.complete_plan_review(
        plan["id"],
        plan_hash=plan["data"]["plan_hash"],
        coverage=[
            {"acceptance_id": "AC-01", "status": "met", "evidence": "test a"},
            {"acceptance_id": "AC-02", "status": "met", "evidence": "test b"},
        ],
        findings=[],
        explanation_hash="sha256:plan-explanation",
    )
    assert reviewed["state"] == "AwaitingPlanApproval"
    approved = service.approve_plan(
        plan["id"],
        actor="demo-owner",
        target_hash=plan["data"]["plan_hash"],
        target_sha="a" * 40,
        confirmation_id=reviewed["data"]["plan_confirmation_id"],
    )
    assert approved["state"] == "Queued"
    with db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(ApprovalGrant)) == 2
        assert session.scalar(select(func.count()).select_from(PlanVersion)) == 1
        kinds = list(session.scalars(select(Job.kind).where(Job.task_id == plan["id"])))
        assert kinds == ["draft_requirements", "plan", "review_plan", "implement"]


def test_v2_rejects_stale_or_unauthorized_approval(team):
    db, service = service_for(team)
    task_id = create_v2(db, service)
    result = service.register_requirements(
        task_id,
        repository="owner/repo",
        issue_number=2,
        issue_url="https://github.com/owner/repo/issues/2",
        updated_at="2026-09-12T00:00:00Z",
        body=REQUIREMENTS,
        explanation_hash="sha256:explanation",
    )
    with pytest.raises(GuardError, match="cannot approve"):
        service.approve_requirements(
            task_id,
            actor="someone-else",
            target_hash=result["data"]["requirements_hash"],
            confirmation_id=result["data"]["requirements_confirmation_id"],
        )
    with pytest.raises(GuardError, match="Stale"):
        service.approve_requirements(
            task_id,
            actor="demo-owner",
            target_hash=digest("changed"),
            confirmation_id=result["data"]["requirements_confirmation_id"],
        )


def test_plan_review_requests_at_most_three_revision_rounds(team):
    db, service = service_for(team)
    requirements = register_and_approve_requirements(db, service)
    current = service.register_plan(
        requirements["id"], body=PLAN, requirements_body=REQUIREMENTS, base_sha="a" * 40
    )
    finding = {
        "id": "PLAN-01",
        "severity": "medium",
        "requirement_id": "AC-01",
        "file": current["data"]["plan_path"],
        "line": 1,
        "reason": "競合時の検証が不足",
        "requested_change": "競合試験を追加する",
    }
    revised = service.complete_plan_review(
        current["id"],
        plan_hash=current["data"]["plan_hash"],
        coverage=[],
        findings=[finding],
        explanation_hash="",
    )
    assert revised["state"] == "PlanningImplementation"
    with db.transaction() as session:
        stored_task = session.get(Task, current["id"])
        budget = session.scalar(
            select(ExecutionBudget).where(ExecutionBudget.task_id == current["id"])
        )
        assert budget.plan_revision_reservations == 1
        assert session.scalar(
            select(PlanVersion).where(PlanVersion.id == stored_task.current_plan_version_id)
        ).review_status == "changes_requested"


def test_v2_budget_reservations_are_strict_and_persistent(team):
    db, service = service_for(team)
    task_id = create_v2(db, service)
    for _ in range(30):
        service.reserve_model_call(task_id)
    with pytest.raises(GuardError, match="model-call"):
        service.reserve_model_call(task_id)
    with db.transaction() as session:
        budget = session.scalar(select(ExecutionBudget).where(ExecutionBudget.task_id == task_id))
        assert budget.model_reservations == 30


def test_v2_task_creation_does_not_change_v1_default(team):
    db, service = service_for(team)
    create_v2(db, service)
    with db.transaction() as session:
        assert session.get(Task, "TASK-V2").workflow_version == 2


def test_task_service_routes_allowlisted_new_request_to_v2(team):
    settings, _, _, service, _, command = team
    settings.workflow_v2.enabled = True
    settings.workflow_v2.repository_aliases = ["demo"]
    created = command("request", repo="demo", text="新しい開発フロー")
    assert created["workflow_version"] == 2
    assert created["state"] == "DraftingRequirements"
    assert service.status(created["id"])["workflow_version"] == 2


def test_v2_per_task_repository_is_provisioned_before_base_and_issue(team, monkeypatch):
    settings, _, github, service, engine, command = team
    settings.repos["demo"].per_task = True
    settings.workflow_v2.enabled = True
    settings.workflow_v2.repository_aliases = ["demo"]
    settings.workflow_v2.requirements_approver_ids = ["demo-owner"]
    settings.workflow_v2.github_issue_conditional_updates = True

    task = command("request", repo="demo", text="案件repoでIssue正本を作る")
    original_base = github.base

    def base_after_provisioning(repo, attempts=1):
        assert github.read("repository:" + task["id"])["repository"] == repo.repository
        return original_base(repo, attempts)

    monkeypatch.setattr(github, "base", base_after_provisioning)
    step(engine)

    prepared = service.status(task["id"])
    assert prepared["state"] == "AwaitingRequirementsConfirmation"
    assert prepared["data"]["provisioned"] is True
    assert prepared["data"]["requirements_issue"] > 0


def test_cancel_closes_issue_and_restart_creates_new_task_with_fresh_approval(team):
    settings, db, github, service, engine, command = team
    settings.workflow_v2.enabled = True
    settings.workflow_v2.repository_aliases = ["demo"]
    settings.workflow_v2.requirements_approver_ids = ["demo-owner"]
    settings.workflow_v2.github_issue_conditional_updates = True

    original = command("request", repo="demo", text="中止と再開を検証する")
    step(engine)
    original = service.status(original["id"])
    issue_number = original["data"]["requirements_issue"]
    old_confirmation = original["data"]["requirements_confirmation_id"]
    cancelled = command("cancel", task_id=original["id"], text="方針を見直す")
    assert cancelled["state"] == "Cancelled"
    with db.transaction() as session:
        close = session.scalar(
            select(Operation).where(Operation.key == f"issue-close:{original['id']}")
        )
        assert close.status == "pending"

    engine.reconcile()
    assert github.read(github._issue_key(issue_number))["state"] == "closed"
    restarted = command("restart", task_id=original["id"])
    assert restarted["id"] != original["id"]
    assert restarted["data"]["previous_task_id"] == original["id"]
    assert restarted["data"]["requirements_issue"] == issue_number
    with pytest.raises(GuardError, match="Stale"):
        service.v2.approve_requirements(
            restarted["id"],
            actor="demo-owner",
            target_hash=original["data"]["requirements_hash"],
            confirmation_id=old_confirmation,
        )

    step(engine)
    restarted = service.status(restarted["id"])
    assert restarted["state"] == "AwaitingRequirementsConfirmation"
    assert restarted["data"]["requirements_confirmation_id"] != old_confirmation
    assert github.read(github._issue_key(issue_number))["state"] == "open"
    with db.transaction() as session:
        authority = session.scalar(
            select(RequirementsReference).where(
                RequirementsReference.repository == "example/demo",
                RequirementsReference.issue_number == issue_number,
            )
        )
        assert authority.task_id == restarted["id"]


def test_engine_runs_v2_end_to_end_with_two_early_approval_gates(team):
    settings, db, github, service, engine, command = team
    settings.workflow_v2.enabled = True
    settings.workflow_v2.repository_aliases = ["demo"]
    settings.workflow_v2.requirements_approver_ids = ["demo-owner"]
    settings.workflow_v2.plan_approver_ids = ["demo-owner"]
    settings.workflow_v2.github_issue_conditional_updates = True

    task = command("request", repo="demo", text="承認付きで実装する")
    step(engine)
    task = service.status(task["id"])
    assert task["state"] == "AwaitingRequirementsConfirmation"
    command(
        "approve_requirements",
        task_id=task["id"],
        hash=task["data"]["requirements_hash"],
        confirmation_id=task["data"]["requirements_confirmation_id"],
    )
    step(engine)
    assert service.status(task["id"])["state"] == "ReviewingImplementationPlan"
    step(engine)
    task = service.status(task["id"])
    assert task["state"] == "AwaitingPlanApproval"
    command(
        "approve_plan",
        task_id=task["id"],
        hash=task["data"]["plan_hash"],
        base_sha=task["data"]["base_sha"],
        confirmation_id=task["data"]["plan_confirmation_id"],
    )
    assert service.status(task["id"])["state"] == "Queued"
    step(engine)
    step(engine)
    engine.reconcile()
    task = service.status(task["id"])
    assert task["state"] == "AwaitingMergeApproval"
    command(
        "approve_merge",
        task_id=task["id"],
        hash=task["data"]["requirements_hash"],
        head_sha=task["data"]["head_sha"],
        base_sha=task["data"]["base_sha"],
    )
    engine.reconcile()
    merged = service.status(task["id"])
    assert merged["state"] == "Merged"
    source = github.source(settings.repos["demo"], merged["data"]["head_sha"])
    assert merged["data"]["plan_path"] in source
    assert f"docs/work-items/issue-{merged['data']['requirements_issue']}/README.md" in source
    with db.transaction() as session:
        budget = session.scalar(
            select(ExecutionBudget).where(ExecutionBudget.task_id == task["id"])
        )
        assert budget.model_reservations == 5
        delegation = session.scalar(
            select(Delegation).where(Delegation.task_id == task["id"])
        )
        assert delegation.source_role == "cto"
        assert delegation.target_role == "backend_integrator"
        assert delegation.status == "completed"


def test_v2_safe_issue_proposal_resumes_from_saved_result_after_owner_applies_it(team):
    settings, db, github, service, engine, command = team
    settings.workflow_v2.enabled = True
    settings.workflow_v2.repository_aliases = ["demo"]
    settings.workflow_v2.requirements_approver_ids = ["demo-owner"]
    settings.workflow_v2.github_issue_conditional_updates = False

    task = command("request", repo="demo", text="安全なIssue更新")
    step(engine)
    blocked = service.status(task["id"])
    assert blocked["state"] == "Blocked"
    assert blocked["data"]["requirements_proposal_hash"]
    with db.transaction() as session:
        notices = list(session.scalars(select(Outbox).where(Outbox.task_id == task["id"])))
        assert any(
            blocked["data"]["requirements_proposal_url"] in notice.data.get("body", "")
            for notice in notices
        )

    issue_number = blocked["data"]["requirements_issue"]
    comments = github.read(f"issue-comments:{issue_number}")
    proposed_body = comments[-1]["body"].split("\n\n", 1)[1]
    current = github.issue_reference(settings.repos["demo"], issue_number)
    github.update_issue_body(
        settings.repos["demo"],
        issue_number,
        proposed_body,
        expected_etag=current["etag"],
        preflight_confirmed=True,
    )

    command("retry", task_id=task["id"])
    step(engine)
    resumed = service.status(task["id"])
    assert resumed["state"] == "AwaitingRequirementsConfirmation"
    with db.transaction() as session:
        jobs = list(session.scalars(select(Job).where(Job.task_id == task["id"])))
        assert len(jobs) == 1
        assert jobs[0].attempt == 1


def test_v2_outbox_projects_one_status_message_and_topic_thread(team):
    settings, db, _, service, engine, command = team
    settings.workflow_v2.enabled = True
    settings.workflow_v2.repository_aliases = ["demo"]
    settings.workflow_v2.project_channels = {"demo": "123456789012345678"}
    settings.workflow_v2.github_issue_conditional_updates = True
    task = command("request", repo="demo", text="案件チャンネルで進める")
    step(engine)

    app = create_app(db, settings, "internal-test-token-that-is-long-enough")
    client = TestClient(app)
    headers = {"Authorization": "Bearer internal-test-token-that-is-long-enough"}
    items = client.get("/outbox", headers=headers).json()
    status = next(item for item in items if item.get("create_status"))
    topic = next(item for item in items if item.get("create_topic_thread"))
    approval = next(item for item in items if item.get("approval") == "requirements")
    assert {item["filename"] for item in approval["attachments"]} == {
        "requirements.html",
        "requirements.png",
    }
    assert status["channel_id"] == topic["channel_id"] == "123456789012345678"
    assert client.post(
        f"/outbox/{status['id']}/ack", json={"message_id": "status-1"}, headers=headers
    ).is_success
    assert client.post(
        f"/outbox/{topic['id']}/ack",
        json={"message_id": "topic-message", "thread_id": "thread-1"},
        headers=headers,
    ).is_success
    with db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(ProjectWorkspace)) == 1
        assert session.scalar(select(TaskProjection).where(TaskProjection.task_id == task["id"]))
        saved_topic = session.scalar(select(TopicThread).where(TopicThread.task_id == task["id"]))
        assert saved_topic.thread_id == "thread-1"
        assert "thread-1" in session.get(Task, task["id"]).data["topic_thread_ids"]


def test_v2_read_only_jobs_run_four_at_once_even_with_subscription_auth(team):
    settings, db, _, _, engine, _ = team
    settings.workflow_v2.enabled = True
    settings.workflow_v2.repository_aliases = ["demo"]
    workflow = WorkflowV2Service(db, settings)
    with db.transaction() as session:
        for number in range(5):
            workflow.create_task(
                session,
                task_id=f"TASK-PARALLEL-{number}",
                repo="demo",
                repository=f"example/demo-{number}",
                summary="parallel",
            )
    claims = [engine.claim() for _ in range(5)]
    assert all(claims[:4])
    assert claims[4] is None


def test_v2_repository_write_jobs_are_serialized_per_repository(team):
    settings, db, _, _, engine, _ = team
    settings.workflow_v2.enabled = True
    settings.workflow_v2.repository_aliases = ["demo"]
    workflow = WorkflowV2Service(db, settings)
    with db.transaction() as session:
        for number in range(2):
            task = workflow.create_task(
                session,
                task_id=f"TASK-WRITE-{number}",
                repo="demo",
                repository="example/demo",
                summary="write",
            )
            task.state = "Queued"
        for job in session.scalars(select(Job)):
            job.role = "backend_integrator"
            job.kind = "implement"
    assert engine.claim()
    assert engine.claim() is None
    with db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(RepositoryLease)) == 1


def test_internal_advisor_is_limited_to_five_calls_per_immutable_topic(team):
    settings, db, _, _, _, _ = team
    settings.workflow_v2.enabled = True
    settings.workflow_v2.repository_aliases = ["demo"]
    workflow = WorkflowV2Service(db, settings)
    with db.transaction() as session:
        task = workflow.create_task(
            session,
            task_id="TASK-CONSULT",
            repo="demo",
            repository="example/demo",
            summary="consult",
        )
        topic = WorkspaceService(settings).create_topic(
            session,
            task,
            origin_event_id="consult-origin",
            purpose="認証方式を判断する",
            title="認証方式",
        )
        topic_id = topic.topic_id
    advisors = ConsultationService(db, settings)
    for number in range(5):
        advisors.request(
            "TASK-CONSULT",
            topic_id=topic_id,
            requester_role="cto",
            consultant_role="analyst",
            question=f"選択肢{number}を評価する",
        )
    with pytest.raises(GuardError, match="topic limit"):
        advisors.request(
            "TASK-CONSULT",
            topic_id=topic_id,
            requester_role="cto",
            consultant_role="analyst",
            question="追加評価",
        )
    with db.transaction() as session:
        records = list(
            session.scalars(
                select(Consultation)
                .where(Consultation.task_id == "TASK-CONSULT")
                .order_by(Consultation.ordinal)
            )
        )
        assert [record.ordinal for record in records] == [1, 2, 3, 4, 5]
        assert all(record.consultant_role == "analyst" for record in records)
    assert not settings.discord_role_enabled("analyst")


def test_v2_connection_failure_retries_three_times_then_uses_safe_fallback(team):
    settings, db, _, _, engine, _ = team
    settings.workflow_v2.enabled = True
    settings.workflow_v2.repository_aliases = ["demo"]
    workflow = WorkflowV2Service(db, settings)
    with db.transaction() as session:
        workflow.create_task(
            session,
            task_id="TASK-FALLBACK",
            repo="demo",
            repository="example/demo",
            summary="fallback",
        )
        job = session.scalar(select(Job).where(Job.task_id == "TASK-FALLBACK"))
        job.role = "analyst"
        job.kind = "consult"

    for _ in range(3):
        claim = engine.claim()
        assert claim is not None
        engine.fail(*claim, RuntimeError("temporary connection failure"))

    with db.transaction() as session:
        jobs = list(
            session.scalars(
                select(Job).where(Job.task_id == "TASK-FALLBACK").order_by(Job.created)
            )
        )
        assert jobs[0].status == "failed"
        assert jobs[0].attempt == 3
        assert jobs[1].role == "cto"
        assert jobs[1].data["fallback_from"] == "analyst"
        assert session.get(Task, "TASK-FALLBACK").state != "Blocked"


def test_v2_retry_requeues_failed_requirements_job(team):
    settings, db, _, service, engine, command = team
    settings.workflow_v2.enabled = True
    settings.workflow_v2.repository_aliases = ["demo"]
    with db.transaction() as session:
        service.v2.create_task(
            session,
            task_id="TASK-RETRY-V2",
            repo="demo",
            repository="example/demo",
            summary="retry",
        )

    for _ in range(3):
        claim = engine.claim()
        assert claim is not None
        engine.fail(*claim, RuntimeError("GitHub unavailable"), phase="prepare")

    blocked = service.status("TASK-RETRY-V2")
    assert blocked["state"] == "Blocked"
    assert blocked["data"]["retry_state"] == "DraftingRequirements"
    assert "GitHub準備処理失敗" in blocked["data"]["reason"]

    retried = command("retry", task_id="TASK-RETRY-V2")
    assert retried["state"] == "DraftingRequirements"
    with db.transaction() as session:
        job = session.scalar(select(Job).where(Job.task_id == "TASK-RETRY-V2"))
        assert (job.status, job.attempt, job.owner, job.lease) == ("queued", 0, "", 0)


def test_v2_stalled_job_retries_once_then_blocks_and_notifies_sre(team):
    settings, db, _, _, engine, _ = team
    settings.workflow_v2.enabled = True
    settings.workflow_v2.repository_aliases = ["demo"]
    workflow = WorkflowV2Service(db, settings)
    with db.transaction() as session:
        workflow.create_task(
            session,
            task_id="TASK-STALL",
            repo="demo",
            repository="example/demo",
            summary="stall",
        )
        first = session.scalar(select(Job).where(Job.task_id == "TASK-STALL"))
        started = first.created

    engine.recover_stalled_v2(started + settings.workflow_v2.stalled_seconds)
    with db.transaction() as session:
        jobs = list(session.scalars(select(Job).where(Job.task_id == "TASK-STALL")))
        assert [job.status for job in jobs].count("queued") == 1
        retry = next(job for job in jobs if job.status == "queued")
        retry.created = started + settings.workflow_v2.stalled_seconds

    engine.recover_stalled_v2(started + settings.workflow_v2.stalled_seconds * 2)
    with db.transaction() as session:
        task = session.get(Task, "TASK-STALL")
        assert task.state == "Blocked"
        notices = list(session.scalars(select(Outbox).where(Outbox.task_id == task.id)))
        assert any(item.data.get("role") == "security_sre" for item in notices)
        assert any(item.data.get("mention_owner") for item in notices)


def test_v2_owner_clarification_wait_is_not_recovered_as_a_stalled_job(team):
    settings, db, _, _, engine, _ = team
    settings.workflow_v2.enabled = True
    settings.workflow_v2.repository_aliases = ["demo"]
    workflow = WorkflowV2Service(db, settings)
    with db.transaction() as session:
        workflow.create_task(
            session,
            task_id="TASK-WAITING-OWNER",
            repo="demo",
            repository="example/demo",
            summary="waiting for an owner decision",
        )
        job = session.scalar(select(Job).where(Job.task_id == "TASK-WAITING-OWNER"))
        job.status = "done"
        started = job.created

    engine.recover_stalled_v2(started + settings.workflow_v2.stalled_seconds * 3)

    with db.transaction() as session:
        task = session.get(Task, "TASK-WAITING-OWNER")
        jobs = list(session.scalars(select(Job).where(Job.task_id == task.id)))
        assert task.state == "DraftingRequirements"
        assert len(jobs) == 1
        assert jobs[0].status == "done"


def test_v2_reports_slow_start_once_when_worker_capacity_is_available(team):
    settings, db, _, _, engine, _ = team
    settings.workflow_v2.enabled = True
    settings.workflow_v2.repository_aliases = ["demo"]
    workflow = WorkflowV2Service(db, settings)
    with db.transaction() as session:
        workflow.create_task(
            session,
            task_id="TASK-SLOW-START",
            repo="demo",
            repository="example/demo",
            summary="slow start",
        )
        job = session.scalar(select(Job).where(Job.task_id == "TASK-SLOW-START"))
        job.created = 1000

    engine.report_slow_v2_starts(1029)
    engine.report_slow_v2_starts(1030)
    engine.report_slow_v2_starts(1060)
    with db.transaction() as session:
        notices = [
            out
            for out in session.scalars(
                select(Outbox).where(Outbox.task_id == "TASK-SLOW-START")
            )
            if "30秒以内" in out.data.get("body", "")
        ]
        assert len(notices) == 1
        assert notices[0].data["role"] == "security_sre"


def test_owner_decision_topic_is_reminded_once_after_24_hours(team):
    settings, db, *_ = team
    settings.workflow_v2.enabled = True
    settings.workflow_v2.repository_aliases = ["demo"]
    workflow = WorkflowV2Service(db, settings)
    with db.transaction() as session:
        task = workflow.create_task(
            session,
            task_id="TASK-REMINDER",
            repo="demo",
            repository="example/demo",
            summary="reminder",
        )
        task.state = "AwaitingRequirementsConfirmation"
        topic = WorkspaceService(settings).create_topic(
            session,
            task,
            origin_event_id="owner-question",
            purpose="オーナー判断が必要です",
            title="owner decision",
            owner_confirmation=True,
        )
        topic.thread_id = "123"
        marker = session.scalar(
            select(Event).where(Event.source == "owner-topic", Event.external_id == topic.id)
        )
        marker.created = 1000

    with db.transaction() as session:
        assert WorkspaceService.enqueue_due_owner_reminders(session, 1000 + 86400 - 1) == 0
        assert WorkspaceService.enqueue_due_owner_reminders(session, 1000 + 86400) == 1
        assert WorkspaceService.enqueue_due_owner_reminders(session, 1000 + 86400) == 0
        reminder = session.scalar(
            select(Outbox).where(
                Outbox.task_id == "TASK-REMINDER",
                Outbox.data["mention_owner"].as_boolean().is_(True),
            )
        )
        assert reminder.data["thread_id"] == "123"
