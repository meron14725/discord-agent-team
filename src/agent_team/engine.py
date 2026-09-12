import asyncio
import json
import logging
import time
from pathlib import Path

from sqlalchemy import select

from .contracts import RunRequest, RunResponse
from .db import (
    Artifact,
    Consultation,
    Delegation,
    Event,
    ExecutionBudget,
    Job,
    Operation,
    PlanVersion,
    RepositoryLease,
    RequirementsReference,
    Review,
    Run,
    Spec,
    Task,
    TopicThread,
    task_lock,
    uid,
)
from .explanations import (
    LocalExplanationRenderer,
    RemoteExplanationRenderer,
    bind_explanation,
)
from .planning import validate_plan, validate_review_coverage
from .policy import GuardError, digest, merge_gate, validate_files, validate_review
from .prompt_context import load_agent_prompt_context, load_vendor_skill_context
from .service import STOPPED, enqueue, invalidate, notify, transition
from .workflow import WorkflowV2Service
from .workspaces import TopicLink, WorkItemIndex, WorkspaceService, render_index


class Engine:
    def __init__(self, db, settings, github, runner, artifacts="artifacts", explanation_renderer=None):
        self.db, self.settings, self.github, self.runner = db, settings, github, runner
        self.owner, self.artifacts = uid(), Path(artifacts)
        self.active = {}
        self.explanation_renderer = explanation_renderer or (
            LocalExplanationRenderer()
            if settings.mode == "mock"
            else RemoteExplanationRenderer(settings.workflow_v2.renderer_url)
        )
        self.prompt_context = load_agent_prompt_context(settings.role_registry)
        self.vendor_skills = load_vendor_skill_context()

    @staticmethod
    def identity_current(task, job):
        if task.workflow_version == 1:
            return job.data.get("spec_version") == task.spec_version and job.data.get(
                "spec_hash"
            ) == task.data.get("spec_hash", "")
        expected = job.data
        return expected.get("workflow_version") == 2 and all(
            not expected.get(key) or expected.get(key) == task.data.get(key, default)
            for key, default in (
                ("requirements_hash", ""),
                ("plan_version", 0),
                ("plan_hash", ""),
                ("base_sha", ""),
                ("head_sha", ""),
            )
        )

    def release_repository_lease(self, session, job):
        lease = session.scalar(
            select(RepositoryLease).where(RepositoryLease.job_id == job.id).with_for_update()
        )
        if lease and lease.owner == self.owner:
            session.delete(lease)

    @staticmethod
    def enqueue_v2(session, task, kind, role):
        session.add(
            Job(
                task_id=task.id,
                role=role,
                kind=kind,
                data={
                    "workflow_version": 2,
                    "requirements_hash": task.data.get("requirements_hash", ""),
                    "plan_version": task.data.get("plan_version", 0),
                    "plan_hash": task.data.get("plan_hash", ""),
                    "base_sha": task.data.get("base_sha", ""),
                    "head_sha": task.data.get("head_sha", ""),
                },
            )
        )

    def current(self, s, job_id, fence):
        job = s.get(Job, job_id)
        if not job:
            raise GuardError("Unknown job")
        task = task_lock(s, job.task_id)
        if (
            job.status != "running"
            or job.fence != fence
            or job.owner != self.owner
            or job.lease <= time.time()
            or task.state in STOPPED
            or not self.identity_current(task, job)
        ):
            raise GuardError("Expired lease, stopped task or stale result")
        return task, job

    def claim(self):
        self.db.check_leader()
        with self.db.transaction() as s:
            now = time.time()
            for expired in s.scalars(
                select(Job).where(Job.status == "running", Job.lease < now).with_for_update()
            ):
                expired.fence += 1
                expired.status = "queued"
            running_jobs = list(s.scalars(select(Job).where(Job.status == "running")))
            busy = {item.role for item in running_jobs}
            for job in s.scalars(
                select(Job)
                .where(Job.status == "queued")
                .order_by(Job.created)
                .with_for_update(skip_locked=True)
            ):
                task = task_lock(s, job.task_id)
                if task.state in STOPPED:
                    continue
                if not self.identity_current(task, job):
                    job.status = "cancelled"
                    continue
                if task.workflow_version == 1:
                    if job.role in busy or (self.settings.auth_mode == "chatgpt" and busy):
                        continue
                else:
                    role = self.settings.role_registry.role(job.role)
                    parallel_class = (
                        "read_only" if job.kind == "consult" else role.parallel_class
                    )
                    running_classes = [
                        self.settings.role_registry.role(item.role).parallel_class
                        for item in running_jobs
                        if s.get(Task, item.task_id).workflow_version == 2
                    ]
                    if parallel_class == "privileged":
                        if running_classes.count("privileged") >= self.settings.workflow_v2.privileged_concurrency:
                            continue
                    elif sum(value != "privileged" for value in running_classes) >= (
                        self.settings.workflow_v2.normal_concurrency
                    ):
                        continue
                    if parallel_class == "repository_write":
                        repository = self.settings.repo_for(task).repository
                        lease = s.scalar(
                            select(RepositoryLease)
                            .where(RepositoryLease.repository == repository)
                            .with_for_update()
                        )
                        if lease and lease.lease_expires >= now and lease.job_id != job.id:
                            continue
                        if lease:
                            lease.job_id = job.id
                            lease.owner = self.owner
                            lease.fence += 1
                            lease.lease_expires = now + self.settings.lease_seconds
                        else:
                            s.add(
                                RepositoryLease(
                                    repository=repository,
                                    job_id=job.id,
                                    owner=self.owner,
                                    fence=1,
                                    lease_expires=now + self.settings.lease_seconds,
                                )
                            )
                    budget = s.scalar(
                        select(ExecutionBudget)
                        .where(ExecutionBudget.task_id == task.id)
                        .with_for_update()
                    )
                    if budget is None or budget.model_reservations >= (
                        self.settings.workflow_v2.model_calls_per_task
                    ):
                        transition(s, task, "Blocked", "案件のモデル実行上限")
                        job.status = "cancelled"
                        self.release_repository_lease(s, job)
                        continue
                    budget.model_reservations += 1
                if self.settings.mode == "live":
                    runs = list(s.scalars(select(Run)))
                    started_runs = [r for r in runs if "reserved_usd" in r.data]
                    if task.workflow_version == 1 and (
                        sum(r.created >= now - now % 86400 for r in started_runs)
                        >= self.settings.daily_run_limit
                        or sum(r.task_id == task.id for r in started_runs) >= self.settings.task_run_limit
                    ):
                        transition(s, task, "Blocked", "日次/案件の実行回数上限。/retryで再確認")
                        job.status = "cancelled"
                        self.release_repository_lease(s, job)
                        continue
                    daily = sum(r.data.get("reserved_usd", 0) for r in runs if r.created >= now - now % 86400)
                    total = sum(r.data.get("reserved_usd", 0) for r in runs if r.task_id == task.id)
                    reserve = self.settings.run_reservation_usd
                    if self.settings.auth_mode == "api_key" and (
                        daily + reserve > self.settings.daily_budget_usd
                        or total + reserve > self.settings.task_budget_usd
                    ):
                        transition(s, task, "Blocked", "予算予約上限。設定確認後 /retry")
                        job.status = "cancelled"
                        self.release_repository_lease(s, job)
                        continue
                s.add(
                    Run(
                        task_id=task.id,
                        data={
                            "job_id": job.id,
                            "model": self.settings.model,
                            "prompt_version": 1,
                            "reserved_usd": self.settings.run_reservation_usd
                            if self.settings.mode == "live"
                            else 0,
                        },
                    )
                )
                job.status, job.owner = "running", self.owner
                job.data = {
                    **job.data,
                    "queued_at": job.data.get("queued_at", job.created),
                    "started_at": now,
                }
                job.fence, job.attempt, job.lease = (
                    job.fence + 1,
                    job.attempt + 1,
                    now + self.settings.lease_seconds,
                )
                if job.attempt > 3:
                    job.status = "failed"
                    self.release_repository_lease(s, job)
                    transition(s, task, "Blocked", "ジョブ再試行上限")
                    continue
                if job.kind in {"implement", "fix"}:
                    transition(s, task, "Implementing" if job.kind == "implement" else "Fixing")
                return job.id, job.fence
        return None

    def heartbeat(self, job_id, fence):
        self.db.check_leader()
        with self.db.transaction() as s:
            task, job = self.current(s, job_id, fence)
            now = time.time()
            job.lease = now + self.settings.lease_seconds
            lease = s.scalar(select(RepositoryLease).where(RepositoryLease.job_id == job.id))
            if lease and lease.owner == self.owner:
                lease.lease_expires = job.lease
            last_status = job.data.get("status_heartbeat_at", 0)
            if task.workflow_version == 2 and now - last_status >= (
                self.settings.workflow_v2.heartbeat_seconds
            ):
                job.data = {**job.data, "status_heartbeat_at": now}
                job.data = {**job.data, "heartbeat_at": now}
                notify(
                    s,
                    task,
                    f"{task.id}: {task.state} — {job.role}が処理を継続しています。",
                    role=job.role,
                )

    def external(self, job_id, fence, kind, payload_key, call):
        key = f"{job_id}:{kind}:{payload_key}"
        with self.db.transaction() as s:
            task, _ = self.current(s, job_id, fence)
            op = s.scalar(select(Operation).where(Operation.key == key))
            if op and op.status == "done":
                return op.data["result"]
            if not op:
                s.add(Operation(task_id=task.id, key=key, data={"kind": kind}))
        # Task row lock serializes pause/cancel against each bounded external operation.
        with self.db.transaction() as s:
            task, _ = self.current(s, job_id, fence)
            result = call(task, key)
            op = s.scalar(select(Operation).where(Operation.key == key))
            op.status, op.data = "done", {"kind": kind, "result": result}
            return result

    def prepare(self, job_id, fence):
        with self.db.transaction() as s:
            task, job = self.current(s, job_id, fence)
            is_v2 = task.workflow_version == 2
        if is_v2:
            return self.prepare_v2(job_id, fence, task, job)
        with self.db.transaction() as s:
            task, job = self.current(s, job_id, fence)
            repo = self.settings.repo_for(task)
            kind, role = job.kind, job.role
            spec = s.scalar(select(Spec).where(Spec.task_id == task.id, Spec.version == task.spec_version))
            body = spec.body if spec else ""
        provisioned_now = repo.per_task and not task.data.get("provisioned") and kind != "clarify"
        if provisioned_now:
            self.external(
                job_id, fence, "repository", task.id, lambda t, key: self.github.ensure_repo(repo, t)
            )
            with self.db.transaction() as s:
                task, _ = self.current(s, job_id, fence)
                task.data = {**task.data, "provisioned": True}
        fresh = repo.per_task and not task.data.get("provisioned")
        base = "" if fresh else self.github.base(repo, attempts=6 if provisioned_now else 1)
        if kind != "clarify" and task.data.get("pr"):
            snap = self.github.snapshot(repo, task)
            if snap["merged"] or snap["state"] != "open":
                raise GuardError("PR already merged or closed; reconcile required")
            if snap["base_sha"] != task.data["base_sha"]:
                raise GuardError("Base changed: human rebase/integration test required before /retry")
            with self.db.transaction() as s:
                current, _ = self.current(s, job_id, fence)
                current.data = {**current.data, "head_sha": snap["head_sha"]}
                task = current
        elif kind != "clarify":
            issue = self.external(
                job_id,
                fence,
                "issue",
                str(task.spec_version),
                lambda t, key: self.github.issue(repo, t, body),
            )
            with self.db.transaction() as s:
                task, _ = self.current(s, job_id, fence)
                task.data = {
                    **task.data,
                    "issue": issue,
                    "base_sha": base,
                    "branch": f"agent/{task.id}/implementation",
                }
        head = task.data.get("head_sha", "")
        files = {} if fresh else self.github.source(repo, head or base)
        context = {
            "request": task.data["summary"],
            "answers": task.data.get("answers", []),
            "approved_spec": body,
            "findings": task.data.get("findings", []),
            "base_source": self.github.source(repo, base) if kind == "review" else {},
            "controller_managed_spec": f"docs/tasks/{task.id}/spec.md",
        }
        return RunRequest(
            auth_mode=self.settings.auth_mode,
            job_id=job_id,
            role=role,
            kind=kind,
            task_id=task.id,
            spec_version=task.spec_version,
            spec_hash=task.data.get("spec_hash", ""),
            base_sha=task.data.get("base_sha", base),
            head_sha=head,
            prompt=json.dumps(context, ensure_ascii=False),
            files=files,
            test_commands=repo.test_commands if kind in {"implement", "fix"} else [],
            model=self.settings.model,
            timeout=self.settings.run_timeout,
        )

    def prepare_v2(self, job_id, fence, task, job):
        repo = self.settings.repo_for(task)
        kind, role = job.kind, job.role
        provisioned_now = repo.per_task and not task.data.get("provisioned")
        if provisioned_now:
            self.external(
                job_id,
                fence,
                "repository",
                task.id,
                lambda current, key: self.github.ensure_repo(repo, current),
            )
            with self.db.transaction() as session:
                current, _ = self.current(session, job_id, fence)
                current.data = {**current.data, "provisioned": True}
                task = current
        base = task.data.get("base_sha") or self.github.base(
            repo, attempts=6 if provisioned_now else 1
        )
        with self.db.transaction() as session:
            current, _ = self.current(session, job_id, fence)
            current.data = {**current.data, "base_sha": base}
            task = current
        if kind == "draft_requirements":
            issue_number = task.data.get("requirements_issue")
            if not issue_number:
                issue_number = self.external(
                    job_id,
                    fence,
                    "requirements_issue",
                    task.id,
                    lambda current, key: self.github.issue(
                        repo,
                        current,
                        "# 要件定義（作成中）\n\nCTOが要件を整理しています。",
                    ),
                )
                with self.db.transaction() as session:
                    current, _ = self.current(session, job_id, fence)
                    current.data = {
                        **current.data,
                        "requirements_issue": issue_number,
                        "issue": issue_number,
                        "branch": f"agent/issue-{issue_number}-{current.id.lower()}-r1",
                    }
                    WorkspaceService(self.settings).create_topic(
                        session,
                        current,
                        origin_event_id=f"requirements-issue-{issue_number}",
                        purpose="要件定義、オーナー確認、承認の会話をこのスレッドにまとめます。",
                        title=f"{current.id} 要件定義",
                        owner_confirmation=True,
                    )
                    task = current
            authority = self.github.issue_reference(repo, issue_number)
            context = {
                "owner_request": task.data["summary"],
                "owner_answers": task.data.get("answers", []),
                "requirements_issue": authority,
                "required_sections": "背景、目的、対象ユーザー、スコープ、対象外、機能要件、非機能要件、受入条件、テスト、未解決事項",
                "trusted_skills": {
                    path: content
                    for path, content in self.vendor_skills.items()
                    if any(
                        name in path
                        for name in ("/grilling/", "/grill-with-docs/", "/domain-modeling/")
                    )
                },
            }
            files = {}
        else:
            issue_number = task.data.get("requirements_issue")
            if not issue_number:
                raise GuardError("v2 task has no requirements Issue")
            authority = self.github.issue_reference(repo, issue_number)
            if authority["body_hash"] != task.data.get("requirements_hash") or authority[
                "updated_at"
            ] != task.data.get("requirements_updated_at"):
                raise GuardError("GitHub Issue changed after requirements approval")
            plan_body = ""
            head = task.data.get("head_sha", "")
            if task.data.get("plan_path"):
                plan_source = self.github.source(repo, head or base)
                plan_body = plan_source.get(task.data["plan_path"], "")
                if task.data.get("plan_hash") and digest(plan_body) != task.data["plan_hash"]:
                    raise GuardError("Implementation plan changed after approval")
            context = {
                "owner_request": task.data["summary"],
                "approved_requirements": authority["body"],
                "required_acceptance_ids": task.data.get("requirements_acceptance_ids", []),
                "implementation_plan": plan_body,
                "findings": task.data.get("findings", []),
            }
            if kind == "consult":
                with self.db.transaction() as session:
                    consultation = session.get(Consultation, job.data["consultation_id"])
                    if consultation is None:
                        raise GuardError("Unknown consultation")
                    consultation_context = {
                        "topic_id": consultation.topic_id,
                        "requester_role": consultation.requester_role,
                        "question": consultation.question_summary,
                    }
                context = {
                    **consultation_context,
                    "approved_requirements": authority["body"],
                    "implementation_plan": plan_body,
                    "required_output": "判断材料、選択肢、推奨案、未解決事項、参照資料",
                }
            if kind in {"plan", "review_plan"}:
                files = {}
            else:
                model_snapshot = self.github.source_context(repo, head or base)
                files = model_snapshot["files"]
                context["repository_manifest"] = model_snapshot["manifest"]
        context = {
            "trusted_company_policy": self.prompt_context.company_policy,
            "trusted_role_policy": self.prompt_context.role_policies[role],
            **context,
        }
        return RunRequest(
            auth_mode=self.settings.auth_mode,
            job_id=job_id,
            role=role,
            kind=kind,
            task_id=task.id,
            spec_version=task.spec_version,
            spec_hash=task.data.get("requirements_hash", ""),
            base_sha=base,
            head_sha=task.data.get("head_sha", ""),
            prompt=json.dumps(context, ensure_ascii=False),
            files=files,
            test_commands=repo.test_commands if kind in {"implement", "fix"} else [],
            model=self.settings.model,
            timeout=self.settings.run_timeout,
        )

    def finish(self, job_id, fence, request, response):
        response = RunResponse.model_validate(response)
        result = response.result
        for field in ("task_id", "spec_version", "spec_hash", "head_sha", "base_sha"):
            if getattr(result, field) != getattr(request, field):
                raise GuardError(f"Result contract mismatch: {field}")
        with self.db.transaction() as session:
            task, _ = self.current(session, job_id, fence)
            is_v2 = task.workflow_version == 2
        if is_v2:
            return self.finish_v2(job_id, fence, request, response)
        with self.db.transaction() as s:
            task, job = self.current(s, job_id, fence)
            # Persist result before any publication; recovery can re-use the exact result.
            job.data = {**job.data, "request": request.model_dump(), "response": response.model_dump()}
            self.artifacts.mkdir(parents=True, exist_ok=True)
            relative = f"{job_id}-{fence}.json"
            encoded = response.model_dump_json()
            (self.artifacts / relative).write_text(encoded)
            s.add(
                Artifact(
                    task_id=task.id, data={"path": relative, "hash": digest(encoded), "kind": "run_result"}
                )
            )
            s.add(
                Run(
                    task_id=task.id,
                    data={
                        "job_id": job_id,
                        "usage": response.usage,
                        "cli_version": response.cli_version,
                        "elapsed_seconds": response.elapsed_seconds,
                        "result": relative,
                        "finished": time.time(),
                    },
                )
            )
            if result.status != "completed":
                job.status = "done"
                self.release_repository_lease(s, job)
                if request.kind == "clarify" and result.status == "needs_clarification":
                    notify(s, task, "\n".join(result.questions[:5]))
                else:
                    transition(s, task, "Blocked", result.summary[:1000])
                return
            if request.kind == "clarify":
                if result.questions or "AC-" not in result.spec_markdown or "FR-" not in result.spec_markdown:
                    raise GuardError(
                        "Specification has unresolved questions or lacks requirement/acceptance IDs"
                    )
                version = task.spec_version + 1
                body = f"<!-- {task.id} v{version} -->\n" + result.spec_markdown
                task.spec_version = version
                task.data = {**task.data, "spec_hash": digest(body)}
                s.add(Spec(task_id=task.id, version=version, body=body, hash=digest(body)))
                transition(s, task, "AwaitingSpecApproval")
                notify(
                    s, task, result.summary, approval="spec", version=version, hash=digest(body), spec=body
                )
                job.status = "done"
                self.release_repository_lease(s, job)
                return
            repo = self.settings.repo_for(task)
            spec = s.scalar(select(Spec).where(Spec.task_id == task.id, Spec.version == task.spec_version))
        if request.kind in {"implement", "fix"}:
            files = dict(response.files)
            validate_files(files, self.settings, task.id, task.data["spec_hash"])
            files[f"docs/tasks/{task.id}/spec.md"] = spec.body
            if not response.tests or any(t.exit_code != 0 for t in response.tests):
                raise GuardError("Configured tests did not pass")
            if [t.command for t in response.tests] != repo.test_commands:
                raise GuardError("Test evidence does not match configured commands")
            with self.db.transaction() as s:
                task, _ = self.current(s, job_id, fence)
                task.data = {
                    **task.data,
                    "implementation_summary": result.summary
                    + "\n"
                    + json.dumps([c.model_dump() for c in result.coverage], ensure_ascii=False),
                }
            published = self.external(
                job_id,
                fence,
                "publish",
                digest(json.dumps(files, sort_keys=True)),
                lambda t, key: self.github.publish(repo, t, files, key),
            )
            with self.db.transaction() as s:
                task, job = self.current(s, job_id, fence)
                task.data = {**task.data, **published, "review_approval": None, "merge_approval": None}
                job.status = "done"
                self.release_repository_lease(s, job)
                transition(s, task, "Reviewing")
                notify(
                    s,
                    task,
                    f"PR #{published['pr']} を確認しました。上流へレビュー依頼。{published['pr_url']}",
                    "downstream",
                )
                enqueue(s, task, "review")
        else:
            if any(f.file == f"docs/tasks/{task.id}/spec.md" for f in result.findings):
                raise GuardError("Reviewer targeted the controller-managed specification")
            validate_review(result, spec.body)
            snap = self.github.snapshot(repo, task)
            if (snap["head_sha"], snap["base_sha"]) != (result.head_sha, result.base_sha):
                raise GuardError("PR changed during review")
            review_id = self.external(
                job_id,
                fence,
                "review",
                result.head_sha,
                lambda t, key: self.github.review(repo, t, result, key),
            )
            with self.db.transaction() as s:
                task, job = self.current(s, job_id, fence)
                s.add(Review(task_id=task.id, data={**result.model_dump(), "github_review_id": review_id}))
                job.status = "done"
                self.release_repository_lease(s, job)
                if result.decision == "approve":
                    task.data = {
                        **task.data,
                        "review_approval": [result.head_sha, result.base_sha, result.spec_hash],
                    }
                    transition(s, task, "AwaitingChecks")
                elif result.decision == "request_changes":
                    rounds = task.data.get("rounds", 0) + 1
                    task.data = {
                        **task.data,
                        "rounds": rounds,
                        "findings": [f.model_dump() for f in result.findings],
                    }
                    if rounds > self.settings.max_rounds:
                        transition(s, task, "Blocked", "自動修正回数上限")
                    else:
                        transition(s, task, "Fixing", result.summary)
                        enqueue(s, task, "fix")
                else:
                    transition(s, task, "Blocked", result.summary)

    def persist_v2_result(self, session, task, job, fence, response):
        job.data = {**job.data, "request": response[0].model_dump(), "response": response[1].model_dump()}
        self.artifacts.mkdir(parents=True, exist_ok=True)
        relative = f"{job.id}-{fence}.json"
        encoded = response[1].model_dump_json()
        (self.artifacts / relative).write_text(encoded)
        session.add(
            Artifact(task_id=task.id, data={"path": relative, "hash": digest(encoded), "kind": "run_result"})
        )

    def store_explanation(self, session, task, source_kind, source_hash, markdown):
        html, png = self.explanation_renderer.render(source_kind, source_hash, markdown)
        identity = bind_explanation(
            source_kind=source_kind,
            source_hash=source_hash,
            html=html,
            png=png,
        )
        root = self.artifacts / task.id / "explanations" / source_kind
        root.mkdir(parents=True, exist_ok=True)
        html_path, png_path = root / f"{source_hash.removeprefix('sha256:')}.html", root / (
            source_hash.removeprefix("sha256:") + ".png"
        )
        html_path.write_text(html)
        png_path.write_bytes(png)
        artifact = Artifact(
            task_id=task.id,
            data={
                "kind": "explanation",
                "source_kind": source_kind,
                "source_hash": source_hash,
                "html_path": str(html_path),
                "html_hash": identity.html_hash,
                "png_path": str(png_path),
                "png_hash": identity.png_hash,
            },
        )
        session.add(artifact)
        session.flush()
        task.data = {
            **task.data,
            f"{source_kind}_explanation_artifact_id": artifact.id,
        }
        return identity.html_hash

    def finish_v2(self, job_id, fence, request, response):
        result = response.result
        service = WorkflowV2Service(self.db, self.settings)
        with self.db.transaction() as session:
            task, job = self.current(session, job_id, fence)
            self.persist_v2_result(session, task, job, fence, (request, response))
            if result.status != "completed":
                job.status = "done"
                self.release_repository_lease(session, job)
                if result.status == "needs_clarification":
                    notify(session, task, "\n".join(result.questions[:5]), role=job.role)
                else:
                    transition(session, task, "Blocked", result.summary[:1000])
                return
            repo = self.settings.repo_for(task)
            kind = job.kind
        if kind == "draft_requirements":
            body = f"<!-- agent-task:{request.task_id} -->\n" + result.spec_markdown
            from .requirements import validate_requirements_body

            validate_requirements_body(body)
            if task.data.get("previous_task_id"):
                self.external(
                    job_id,
                    fence,
                    "requirements_issue_reopen",
                    str(task.data["requirements_issue"]),
                    lambda task, key: self.github.set_issue_state(
                        repo,
                        task.data["requirements_issue"],
                        "open",
                    ),
                )
            current = self.github.issue_reference(repo, task.data["requirements_issue"])
            if not self.settings.workflow_v2.github_issue_conditional_updates:
                if current["body_hash"] != digest(body):
                    proposal = self.external(
                        job_id,
                        fence,
                        "requirements_proposal",
                        digest(body),
                        lambda task, key: self.github.comment_issue(
                            repo,
                            task.data["requirements_issue"],
                            f"要件本文の変更案 `{digest(body)}`\n\n{body}",
                        ),
                    )
                    with self.db.transaction() as session:
                        task, job = self.current(session, job_id, fence)
                        task.data = {
                            **task.data,
                            "requirements_proposal_hash": digest(body),
                            "requirements_proposal_url": proposal["url"],
                        }
                        job.status = "done"
                        self.release_repository_lease(session, job)
                        transition(
                            session,
                            task,
                            "Blocked",
                            "Issueコメントの要件案を本文へ反映後、再試行してください。",
                        )
                        notify(
                            session,
                            task,
                            "GitHubはIssue更新の安全な条件付きPATCHに対応していません。"
                            "コメントの要件案をIssue本文へ反映し、Discordで再試行してください。",
                            role="cto",
                            mention_owner=True,
                        )
                    return
                updated = current
            else:
                updated = self.external(
                    job_id,
                    fence,
                    "requirements_update",
                    digest(body),
                    lambda task, key: self.github.update_issue_body(
                        repo,
                        task.data["requirements_issue"],
                        body,
                        expected_etag=current["etag"],
                        preflight_confirmed=True,
                    ),
                )
            with self.db.transaction() as session:
                task, job = self.current(session, job_id, fence)
                explanation_hash = self.store_explanation(
                    session, task, "requirements", updated["body_hash"], body
                )
                job.status = "done"
                self.release_repository_lease(session, job)
            service.register_requirements(
                request.task_id,
                repository=repo.repository,
                issue_number=updated["number"],
                issue_url=updated["url"],
                updated_at=updated["updated_at"],
                body=updated["body"],
                explanation_hash=explanation_hash,
            )
            return
        if kind == "plan":
            authority = self.github.issue_reference(repo, task.data["requirements_issue"])
            with self.db.transaction() as session:
                task, _ = self.current(session, job_id, fence)
                reference = session.get(RequirementsReference, task.requirements_reference_id)
                version = (
                    session.scalar(
                        select(PlanVersion.version)
                        .where(PlanVersion.requirements_reference_id == reference.id)
                        .order_by(PlanVersion.version.desc())
                    )
                    or 0
                ) + 1
                identity = validate_plan(
                    body=result.plan,
                    requirements_body=authority["body"],
                    issue_number=reference.issue_number,
                    version=version,
                    base_sha=request.base_sha,
                )
            published = self.external(
                job_id,
                fence,
                "publish_plan",
                identity.content_hash,
                lambda task, key: self.github.publish(repo, task, {identity.path: result.plan}, key),
            )
            with self.db.transaction() as session:
                task, job = self.current(session, job_id, fence)
                task.data = {**task.data, **published}
                delegation = session.scalar(
                    select(Delegation)
                    .where(
                        Delegation.task_id == task.id,
                        Delegation.target_role == "backend_integrator",
                        Delegation.status == "pending",
                    )
                    .order_by(Delegation.created.desc())
                )
                if delegation:
                    delegation.status = "completed"
                job.status = "done"
                self.release_repository_lease(session, job)
            service.register_plan(
                request.task_id,
                body=result.plan,
                requirements_body=authority["body"],
                base_sha=request.base_sha,
            )
            return
        if kind == "review_plan":
            with self.db.transaction() as session:
                task, job = self.current(session, job_id, fence)
                plan = session.get(PlanVersion, task.current_plan_version_id)
                source = self.github.source(repo, task.data["head_sha"])
                body = source.get(plan.path, "")
                try:
                    validate_review_coverage(
                        tuple(task.data.get("requirements_acceptance_ids", [])),
                        [item.model_dump() for item in result.coverage],
                        [item.model_dump() for item in result.findings],
                    )
                except GuardError:
                    explanation_hash = ""
                else:
                    explanation_hash = self.store_explanation(
                        session, task, "plan", plan.content_hash, body
                    )
                job.status = "done"
                self.release_repository_lease(session, job)
            service.complete_plan_review(
                request.task_id,
                plan_hash=plan.content_hash,
                coverage=[item.model_dump() for item in result.coverage],
                findings=[item.model_dump() for item in result.findings],
                explanation_hash=explanation_hash,
            )
            return
        if kind == "consult":
            consultation_id = job.data["consultation_id"]
            with self.db.transaction() as session:
                consultation = session.get(Consultation, consultation_id)
                topic_id = consultation.topic_id if consultation else "unknown"
            comment_body = (
                f"内部相談 `{consultation_id}` / topic `{topic_id}`\n\n"
                f"結論: {result.summary}\n\n"
                f"判断材料・リスク:\n"
                + "\n".join(f"- {risk}" for risk in result.risks)
            )
            comment = self.external(
                job_id,
                fence,
                "consultation_comment",
                consultation_id,
                lambda task, key: self.github.comment_issue(
                    repo, task.data["requirements_issue"], comment_body
                ),
            )
            with self.db.transaction() as session:
                task, job = self.current(session, job_id, fence)
                consultation = session.get(Consultation, consultation_id)
                consultation.conclusion_summary = result.summary
                consultation.decision_criteria = "\n".join(result.risks) or "記録済み結論"
                consultation.issue_comment_url = comment["url"]
                job.status = "done"
                self.release_repository_lease(session, job)
                notify(
                    session,
                    task,
                    f"{job.role}への内部相談を完了し、Issueコメントへ判断材料を記録しました。",
                    role=job.role,
                )
            return
        if kind in {"implement", "fix"}:
            files = dict(response.files)
            validate_files(files, self.settings, task.id, task.data["requirements_hash"])
            protected_plan = task.data["plan_path"]
            if protected_plan in files:
                raise GuardError("Approved implementation plan is immutable")
            if not response.tests or any(test.exit_code != 0 for test in response.tests):
                raise GuardError("Configured tests did not pass")
            if [test.command for test in response.tests] != repo.test_commands:
                raise GuardError("Test evidence does not match configured commands")
            published = self.external(
                job_id,
                fence,
                "publish_implementation",
                digest(json.dumps(files, sort_keys=True)),
                lambda task, key: self.github.publish(repo, task, files, key),
            )
            with self.db.transaction() as session:
                task, job = self.current(session, job_id, fence)
                task.data = {**task.data, **published}
                job.data = {**job.data, "head_sha": published["head_sha"]}
                explanation_hashes = tuple(
                    artifact.data["html_hash"]
                    for artifact in session.scalars(
                        select(Artifact).where(Artifact.task_id == task.id)
                    )
                    if artifact.data.get("kind") == "explanation"
                )
                topics = tuple(
                    TopicLink(
                        topic_id=topic.topic_id,
                        title=topic.purpose_hash[:12],
                        discord_url=(
                            f"https://discord.com/channels/{self.settings.guild_id}/{topic.thread_id}"
                            if topic.thread_id
                            else ""
                        ),
                        issue_comment_url=topic.issue_comment_url,
                    )
                    for topic in session.scalars(
                        select(TopicThread).where(TopicThread.task_id == task.id)
                    )
                )
                index_path = f"docs/work-items/issue-{task.data['requirements_issue']}/README.md"
                index_body = render_index(
                    WorkItemIndex(
                        issue_number=task.data["requirements_issue"],
                        issue_url=task.data["requirements_url"],
                        requirements_hash=task.data["requirements_hash"],
                        task_id=task.id,
                        previous_task_id=task.data.get("previous_task_id", ""),
                        state="Reviewing",
                        branch=task.data["branch"],
                        commit_sha=published["head_sha"],
                        plan_version=task.data["plan_version"],
                        plan_hash=task.data["plan_hash"],
                        explanation_hashes=explanation_hashes,
                        topics=topics,
                    )
                )
            indexed = self.external(
                job_id,
                fence,
                "publish_final_index",
                digest(index_body),
                lambda task, key: self.github.publish(repo, task, {index_path: index_body}, key),
            )
            self.external(
                job_id,
                fence,
                "ready_for_review",
                indexed["head_sha"],
                lambda task, key: self.github.ready_for_review(repo, task),
            )
            with self.db.transaction() as session:
                task, job = self.current(session, job_id, fence)
                task.data = {
                    **task.data,
                    **indexed,
                    "implementation_summary": result.summary,
                    "review_approval": None,
                    "merge_approval": None,
                }
                job.status = "done"
                self.release_repository_lease(session, job)
                transition(session, task, "Reviewing")
                notify(
                    session,
                    task,
                    f"PR #{indexed['pr']} の実装と最終索引を更新しました。CTOへ独立レビューを依頼します。",
                    "backend_integrator",
                )
                self.enqueue_v2(session, task, "review", "cto")
            return
        if kind == "review":
            authority = self.github.issue_reference(repo, task.data["requirements_issue"])
            if authority["body_hash"] != task.data["requirements_hash"]:
                raise GuardError("GitHub Issue changed during implementation review")
            validate_review(result, authority["body"])
            snapshot = self.github.snapshot(repo, task)
            if (snapshot["head_sha"], snapshot["base_sha"]) != (
                result.head_sha,
                result.base_sha,
            ):
                raise GuardError("PR changed during implementation review")
            review_id = self.external(
                job_id,
                fence,
                "review",
                result.head_sha,
                lambda task, key: self.github.review(repo, task, result, key),
            )
            with self.db.transaction() as session:
                task, job = self.current(session, job_id, fence)
                session.add(
                    Review(
                        task_id=task.id,
                        data={**result.model_dump(), "github_review_id": review_id},
                    )
                )
                job.status = "done"
                self.release_repository_lease(session, job)
                if result.decision == "approve":
                    task.data = {
                        **task.data,
                        "review_approval": [
                            result.head_sha,
                            result.base_sha,
                            task.data["requirements_hash"],
                            task.data["plan_hash"],
                        ],
                    }
                    transition(session, task, "AwaitingChecks")
                elif result.decision == "request_changes":
                    budget = session.scalar(
                        select(ExecutionBudget)
                        .where(ExecutionBudget.task_id == task.id)
                        .with_for_update()
                    )
                    if budget.implementation_revision_reservations >= (
                        self.settings.workflow_v2.implementation_revision_limit
                    ):
                        transition(session, task, "Blocked", "実装修正回数上限")
                    else:
                        budget.implementation_revision_reservations += 1
                        task.data = {
                            **task.data,
                            "findings": [finding.model_dump() for finding in result.findings],
                        }
                        transition(session, task, "Fixing", result.summary)
                        self.enqueue_v2(session, task, "fix", "backend_integrator")
                else:
                    transition(session, task, "Blocked", result.summary)
            return
        raise GuardError(f"Unknown v2 execution kind: {kind}")

    def fail(self, job_id, fence, error, phase="run"):
        with self.db.transaction() as s:
            try:
                task, job = self.current(s, job_id, fence)
            except GuardError:
                return
            self.release_repository_lease(s, job)
            if task.workflow_version == 2 and not isinstance(error, GuardError):
                failure_target = {
                    "prepare": "GitHub準備処理",
                    "finish": "成果物の検証・保存処理",
                }.get(phase, f"{job.role}への接続・実行")
                if job.attempt < 3:
                    job.status = "queued"
                    notify(
                        s,
                        task,
                        f"{failure_target}に失敗しました。{job.attempt + 1}回目を再試行します。",
                        role="coordinator",
                    )
                    return
                definition = self.settings.role_registry.role(job.role)
                fallback = definition.fallback_role
                if fallback and definition.parallel_class != "privileged":
                    job.status = "failed"
                    s.add(
                        Job(
                            task_id=task.id,
                            role=self.settings.role_registry.resolve(fallback),
                            kind=job.kind,
                            data={**job.data, "fallback_from": definition.id},
                        )
                    )
                    notify(
                        s,
                        task,
                        f"{definition.display_name}への接続が3回失敗したため、能力を満たす代替担当へ移管します。",
                        role="coordinator",
                    )
                    return
            job.status = "failed"
            # Do not echo HTTP bodies, secrets, or raw subprocess output into Discord.
            reason = (
                str(error)[:500]
                if isinstance(error, GuardError)
                else type(error).__name__
                + f": {failure_target if task.workflow_version == 2 else '接続・実行'}失敗。"
                "監査を確認して /retry"
            )
            if task.workflow_version == 2:
                task.data = {
                    **task.data,
                    "retry_state": task.state,
                    "failed_phase": phase,
                }
            transition(s, task, "Blocked", reason)

    async def execute(self, job_id, fence):
        execution = None
        phase = "prepare"
        try:
            with self.db.transaction() as s:
                _, job = self.current(s, job_id, fence)
                saved = job.data.get("response")
                saved_request = job.data.get("request")
            request = (
                RunRequest.model_validate(saved_request)
                if saved
                else await asyncio.to_thread(self.prepare, job_id, fence)
            )
            phase = "run"
            execution = asyncio.create_task(self.runner.run(request)) if not saved else None
            if execution:
                while not execution.done():
                    await asyncio.wait({execution}, timeout=min(15, self.settings.lease_seconds / 3))
                    await asyncio.to_thread(self.heartbeat, job_id, fence)
                response = execution.result()
            else:
                response = RunResponse.model_validate(saved)
            phase = "finish"
            await asyncio.to_thread(self.finish, job_id, fence, request, response)
        except Exception as error:
            if execution and not execution.done():
                execution.cancel()
                await self.runner.cancel(job_id)
            await asyncio.to_thread(self.fail, job_id, fence, error, phase)

    async def cancel_task(self, task_id):
        with self.db.transaction() as session:
            job_ids = set(
                session.scalars(
                    select(Job.id).where(Job.task_id == task_id)
                )
            )
        for job_id in job_ids:
            future = self.active.get(job_id)
            if future is None or future.done():
                continue
            await self.runner.cancel(job_id)
            future.cancel()

    def reconcile(self):
        self.db.check_leader()
        self.recover_after_runtime_change()
        self.recover_invalid_spec_finding()
        self.report_slow_v2_starts()
        self.recover_stalled_v2()
        self.reconcile_cancelled_issue_operations()
        with self.db.transaction() as s:
            WorkspaceService.enqueue_due_archives(s, time.time())
            WorkspaceService.enqueue_due_owner_reminders(s, time.time())
            ids = list(s.scalars(select(Task.id).where(Task.state.not_in(["Merged"]))))
        for task_id in ids:
            try:
                with self.db.transaction() as s:
                    task = task_lock(s, task_id)
                    if not task.data.get("pr"):
                        continue
                    repo = self.settings.repo_for(task)
                    snap = self.github.snapshot(repo, task)
                    if task.workflow_version == 2:
                        authority = self.github.issue_reference(
                            repo, task.data["requirements_issue"]
                        )
                        if (
                            authority["body_hash"] != task.data.get("requirements_hash")
                            or authority["updated_at"]
                            != task.data.get("requirements_updated_at")
                        ):
                            invalidate(s, task)
                            task.data = {
                                **task.data,
                                "requirements_approval_id": "",
                                "plan_approval_id": "",
                                "review_approval": None,
                                "merge_approval": None,
                            }
                            transition(
                                s,
                                task,
                                "DraftingRequirements",
                                "要件Issue更新により承認を失効",
                            )
                            self.enqueue_v2(s, task, "draft_requirements", "cto")
                            continue
                        plan_source = self.github.source(repo, snap["head_sha"])
                        if digest(plan_source.get(task.data.get("plan_path", ""), "")) != task.data.get(
                            "plan_hash"
                        ):
                            invalidate(s, task)
                            task.data = {
                                **task.data,
                                "plan_approval_id": "",
                                "review_approval": None,
                                "merge_approval": None,
                            }
                            transition(
                                s,
                                task,
                                "PlanningImplementation",
                                "実装計画更新により承認を失効",
                            )
                            self.enqueue_v2(s, task, "plan", "backend_integrator")
                            continue
                    if task.data.get("api_failures"):
                        task.data = {**task.data, "api_failures": 0}
                    if snap["merged"] and snap.get("merge_sha"):
                        task.data = {
                            **task.data,
                            "merge_sha": snap["merge_sha"],
                            "merged_externally": not task.data.get("merge_started", False),
                        }
                        invalidate(s, task)
                        transition(s, task, "Merged", "GitHubでマージ確定")
                        continue
                    if task.state in STOPPED:
                        continue
                    if snap["state"] != "open":
                        invalidate(s, task)
                        transition(s, task, "Blocked", "PRが手動で閉じられました")
                        continue
                    if snap["base_sha"] != task.data["base_sha"]:
                        invalidate(s, task)
                        transition(s, task, "Blocked", "base更新。最新base統合と再検証が必要")
                        continue
                    if snap["head_sha"] != task.data["head_sha"]:
                        invalidate(s, task)
                        task.data = {**task.data, "head_sha": snap["head_sha"]}
                        transition(s, task, "Reviewing", "head更新により承認を失効")
                        enqueue(s, task, "review")
                        continue
                    if task.state not in {
                        "AwaitingChecks",
                        "AwaitingMergeApproval",
                        "ReadyToMerge",
                        "Merging",
                    }:
                        continue
                    gate = merge_gate(task, snap, repo, self.settings)
                    if gate == "failed_checks":
                        invalidate(s, task)
                        transition(
                            s,
                            task,
                            "Blocked",
                            "必須CI失敗。インフラ/コード原因を確認して /retry または /revise",
                        )
                        continue
                    if gate in {"waiting_checks", "disabled"}:
                        continue
                    if gate == "needs_human":
                        if task.state != "AwaitingMergeApproval":
                            transition(s, task, "AwaitingMergeApproval")
                            notify(
                                s,
                                task,
                                "CI・レビュー成功。対象SHAのマージ承認をお願いします。",
                                approval="merge",
                                head_sha=task.data["head_sha"],
                                base_sha=task.data["base_sha"],
                                hash=task.data.get(
                                    "requirements_hash", task.data.get("spec_hash", "")
                                ),
                            )
                        continue
                    transition(s, task, "Merging")
                    task.data = {**task.data, "merge_started": True}
                    key = f"merge:{task.id}:{task.data['head_sha']}"
                    if not s.scalar(select(Operation).where(Operation.key == key)):
                        s.add(Operation(task_id=task.id, key=key, data={"head_sha": task.data["head_sha"]}))
                # Durable merge intent above. Re-fetch all gates under the task lock.
                with self.db.transaction() as s:
                    task = task_lock(s, task_id)
                    snap = self.github.snapshot(repo, task)
                    if snap["merged"]:
                        continue
                    if merge_gate(task, snap, repo, self.settings) != "ready":
                        continue
                    self.github.merge(repo, task)
                    confirmed = self.github.snapshot(repo, task)
                    if confirmed["merged"] and confirmed.get("merge_sha"):
                        task.data = {
                            **task.data,
                            "merge_sha": confirmed["merge_sha"],
                            "merged_externally": False,
                        }
                        transition(s, task, "Merged", "GitHubでマージ確定")
                        op = s.scalar(select(Operation).where(Operation.key == key))
                        op.status, op.data = "done", {"merge_sha": confirmed["merge_sha"]}
            except GuardError as e:
                with self.db.transaction() as s:
                    task = task_lock(s, task_id)
                    if task.state not in STOPPED:
                        invalidate(s, task)
                        transition(s, task, "Blocked", str(e))
            except Exception as error:
                logging.getLogger(__name__).warning(
                    "Reconcile failed for %s: %s", task_id, type(error).__name__
                )
                with self.db.transaction() as s:
                    task = task_lock(s, task_id)
                    failures = task.data.get("api_failures", 0) + 1
                    task.data = {**task.data, "api_failures": failures}
                    if failures >= 3 and task.state not in STOPPED:
                        invalidate(s, task)
                        transition(
                            s,
                            task,
                            "Blocked",
                            "GitHub照合が3回失敗。資格情報・レート制限を確認。外部結果は引き続き照合",
                        )
                continue

    def report_slow_v2_starts(self, now=None):
        """Record a 30-second start-SLA breach only when worker capacity is available."""
        now = time.time() if now is None else now
        with self.db.transaction() as session:
            running = list(session.scalars(select(Job).where(Job.status == "running")))
            running_classes = [
                self.settings.role_registry.role(job.role).parallel_class
                for job in running
                if session.get(Task, job.task_id).workflow_version == 2
            ]
            for job in session.scalars(
                select(Job).where(Job.status == "queued", Job.created <= now - 30)
            ):
                task = session.get(Task, job.task_id)
                if task is None or task.workflow_version != 2 or job.data.get("start_sla_notified_at"):
                    continue
                role = self.settings.role_registry.role(job.role)
                parallel_class = "read_only" if job.kind == "consult" else role.parallel_class
                if parallel_class == "privileged":
                    capacity_available = running_classes.count("privileged") < (
                        self.settings.workflow_v2.privileged_concurrency
                    )
                else:
                    capacity_available = sum(value != "privileged" for value in running_classes) < (
                        self.settings.workflow_v2.normal_concurrency
                    )
                if parallel_class == "repository_write":
                    repository = self.settings.repo_for(task).repository
                    lease = session.scalar(
                        select(RepositoryLease).where(RepositoryLease.repository == repository)
                    )
                    capacity_available = capacity_available and not (
                        lease and lease.lease_expires >= now
                    )
                if not capacity_available:
                    continue
                job.data = {**job.data, "start_sla_notified_at": now}
                notify(
                    session,
                    task,
                    "空きworkerがある状態で受付から30秒以内に処理を開始できませんでした。SREが実行基盤を確認します。",
                    role="security_sre",
                )

    def recover_stalled_v2(self, now=None):
        """Fence and retry a silent v2 job once, then stop and notify SRE."""
        now = time.time() if now is None else now
        recoverable_states = {
            "DraftingRequirements": ("draft_requirements", "cto"),
            "PlanningImplementation": ("plan", "backend_integrator"),
            "ReviewingImplementationPlan": ("review_plan", "cto"),
            "Queued": ("implement", "backend_integrator"),
            "Implementing": ("implement", "backend_integrator"),
            "Fixing": ("fix", "backend_integrator"),
            "Reviewing": ("review", "cto"),
        }
        with self.db.transaction() as session:
            tasks = list(
                session.scalars(
                    select(Task).where(
                        Task.workflow_version == 2,
                        Task.state.in_(recoverable_states),
                    )
                )
            )
            for task in tasks:
                active = list(
                    session.scalars(
                        select(Job)
                        .where(Job.task_id == task.id, Job.status.in_(["queued", "running"]))
                        .order_by(Job.created.desc())
                    )
                )
                latest_event = session.scalar(
                    select(Event)
                    .where(Event.task_id == task.id, Event.source == "state")
                    .order_by(Event.created.desc())
                )
                activity = max(
                    [latest_event.created if latest_event else 0]
                    + [
                        max(job.created, float(job.data.get("status_heartbeat_at", 0)))
                        for job in active
                    ]
                )
                if not activity or now - activity < self.settings.workflow_v2.stalled_seconds:
                    continue
                for job in active:
                    job.status = "cancelled"
                    job.fence += 1
                    self.release_repository_lease(session, job)
                if task.data.get("stalled_retry_version") == task.state_version:
                    transition(session, task, "Blocked", "10分無更新の再実行後も処理が停止")
                    notify(
                        session,
                        task,
                        "自動復旧後も処理が停止しました。SREが監査ログとworker状態を確認してください。",
                        role="security_sre",
                        mention_owner=True,
                    )
                    continue
                kind, role = (
                    (active[0].kind, active[0].role)
                    if active
                    else recoverable_states[task.state]
                )
                task.data = {**task.data, "stalled_retry_version": task.state_version}
                self.enqueue_v2(session, task, kind, role)
                notify(
                    session,
                    task,
                    "10分間更新がなかったため、古い実行を無効化して1回だけ再実行します。",
                    role="coordinator",
                )

    def reconcile_cancelled_issue_operations(self):
        with self.db.transaction() as session:
            operation_ids = list(
                session.scalars(
                    select(Operation.id).where(
                        Operation.key.like("issue-close:%"),
                        Operation.status == "pending",
                    )
                )
            )
        for operation_id in operation_ids:
            with self.db.transaction() as session:
                operation = session.get(Operation, operation_id)
                if operation is None or operation.status != "pending":
                    continue
                task = task_lock(session, operation.task_id)
                if task.state != "Cancelled":
                    operation.status = "cancelled"
                    continue
                repo = self.settings.repo_for(task)
                issue_number = operation.data["issue_number"]
            try:
                self.github.set_issue_state(repo, issue_number, "closed")
            except Exception as error:
                with self.db.transaction() as session:
                    operation = session.get(Operation, operation_id)
                    task = task_lock(session, operation.task_id)
                    attempts = operation.data.get("attempts", 0) + 1
                    operation.data = {
                        **operation.data,
                        "attempts": attempts,
                        "error_class": type(error).__name__,
                    }
                    if attempts >= 3:
                        operation.status = "failed"
                        notify(
                            session,
                            task,
                            "中止済み案件のIssue closeが3回失敗しました。案件は再開せず、SRE確認が必要です。",
                            role="security_sre",
                        )
            else:
                with self.db.transaction() as session:
                    operation = session.get(Operation, operation_id)
                    operation.status = "done"
                    operation.data = {**operation.data, "attempts": operation.data.get("attempts", 0) + 1}

    def recover_after_runtime_change(self):
        """Requeue only when a missing executable was fixed by trusted configuration."""
        with self.db.transaction() as s:
            for task in s.scalars(select(Task).where(Task.state == "Blocked").with_for_update()):
                if task.data.get("reason") != "Configured tests did not pass":
                    continue
                job = s.scalar(
                    select(Job)
                    .where(Job.task_id == task.id, Job.status == "failed")
                    .order_by(Job.created.desc())
                )
                if not job or job.kind not in {"implement", "fix"}:
                    continue
                saved_request = job.data.get("request") or {}
                saved_response = job.data.get("response") or {}
                old_commands = saved_request.get("test_commands")
                new_commands = self.settings.repo_for(task).test_commands
                tests = saved_response.get("tests") or []
                missing_executable = any(test.get("exit_code") in {126, 127} for test in tests)
                same_spec = (
                    job.data.get("spec_version") == task.spec_version
                    and job.data.get("spec_hash") == task.data.get("spec_hash")
                )
                if not (same_spec and missing_executable and old_commands and old_commands != new_commands):
                    continue
                job.status = "cancelled"
                transition(s, task, "Queued" if job.kind == "implement" else "Fixing",
                           "統括が実行環境設定の修正を検出し、自動で担当へ再依頼")
                enqueue(s, task, job.kind)

    def recover_invalid_spec_finding(self):
        """Discard a review finding that only asks to remove the valid audit copy."""
        with self.db.transaction() as s:
            candidates = [
                task.id
                for task in s.scalars(select(Task).where(Task.state == "Blocked"))
                if task.data.get("reason") == "Approved specification was modified"
                and task.data.get("pr")
                and task.data.get("findings")
                and all(
                    finding.get("file") == f"docs/tasks/{task.id}/spec.md"
                    for finding in task.data["findings"]
                )
            ]
        for task_id in candidates:
            with self.db.transaction() as s:
                task = task_lock(s, task_id)
                repo = self.settings.repo_for(task)
            snapshot = self.github.snapshot(repo, task)
            with self.db.transaction() as s:
                task = task_lock(s, task_id)
                if task.state != "Blocked" or snapshot.get("spec_hash") != task.data.get("spec_hash"):
                    continue
                task.data = {**task.data, "findings": []}
                transition(s, task, "Reviewing", "統括が管理対象仕様への無効な指摘を破棄し、再レビュー")
                enqueue(s, task, "review")

    async def loop(self):
        last_poll = 0
        while True:
            self.db.check_leader()
            if time.monotonic() - last_poll >= self.settings.poll_seconds:
                await asyncio.to_thread(self.reconcile)
                last_poll = time.monotonic()
            for job_id, future in list(self.active.items()):
                if future.done():
                    await future
                    del self.active[job_id]
            claimed = await asyncio.to_thread(self.claim)
            if claimed:
                self.active[claimed[0]] = asyncio.create_task(self.execute(*claimed))
            await asyncio.sleep(0.5)
