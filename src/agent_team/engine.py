import asyncio
import json
import logging
import time
from pathlib import Path

from sqlalchemy import select

from .contracts import RunRequest, RunResponse
from .db import Artifact, Job, Operation, Review, Run, Spec, Task, task_lock, uid
from .policy import GuardError, digest, merge_gate, validate_files, validate_review
from .service import STOPPED, enqueue, invalidate, notify, transition


class Engine:
    def __init__(self, db, settings, github, runner, artifacts="artifacts"):
        self.db, self.settings, self.github, self.runner = db, settings, github, runner
        self.owner, self.artifacts = uid(), Path(artifacts)
        self.active = {}

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
            or job.data["spec_version"] != task.spec_version
            or job.data["spec_hash"] != task.data.get("spec_hash", "")
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
            busy = set(s.scalars(select(Job.role).where(Job.status == "running")))
            for job in s.scalars(
                select(Job)
                .where(Job.status == "queued")
                .order_by(Job.created)
                .with_for_update(skip_locked=True)
            ):
                if job.role in busy or (self.settings.auth_mode == "chatgpt" and busy):
                    continue
                task = task_lock(s, job.task_id)
                if task.state in STOPPED:
                    continue
                if job.data["spec_version"] != task.spec_version or job.data["spec_hash"] != task.data.get(
                    "spec_hash", ""
                ):
                    job.status = "cancelled"
                    continue
                if self.settings.mode == "live":
                    runs = list(s.scalars(select(Run)))
                    started_runs = [r for r in runs if "reserved_usd" in r.data]
                    if (
                        sum(r.created >= now - now % 86400 for r in started_runs)
                        >= self.settings.daily_run_limit
                        or sum(r.task_id == task.id for r in started_runs) >= self.settings.task_run_limit
                    ):
                        transition(s, task, "Blocked", "日次/案件の実行回数上限。/retryで再確認")
                        job.status = "cancelled"
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
                job.fence, job.attempt, job.lease = (
                    job.fence + 1,
                    job.attempt + 1,
                    now + self.settings.lease_seconds,
                )
                if job.attempt > 3:
                    job.status = "failed"
                    transition(s, task, "Blocked", "ジョブ再試行上限")
                    continue
                if job.kind in {"implement", "fix"}:
                    transition(s, task, "Implementing" if job.kind == "implement" else "Fixing")
                return job.id, job.fence
        return None

    def heartbeat(self, job_id, fence):
        self.db.check_leader()
        with self.db.transaction() as s:
            _, job = self.current(s, job_id, fence)
            job.lease = time.time() + self.settings.lease_seconds

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

    def finish(self, job_id, fence, request, response):
        response = RunResponse.model_validate(response)
        result = response.result
        for field in ("task_id", "spec_version", "spec_hash", "head_sha", "base_sha"):
            if getattr(result, field) != getattr(request, field):
                raise GuardError(f"Result contract mismatch: {field}")
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

    def fail(self, job_id, fence, error):
        with self.db.transaction() as s:
            try:
                task, job = self.current(s, job_id, fence)
            except GuardError:
                return
            job.status = "failed"
            # Do not echo HTTP bodies, secrets, or raw subprocess output into Discord.
            reason = (
                str(error)[:500]
                if isinstance(error, GuardError)
                else type(error).__name__ + ": 接続・実行失敗。監査を確認して /retry"
            )
            transition(s, task, "Blocked", reason)

    async def execute(self, job_id, fence):
        execution = None
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
            execution = asyncio.create_task(self.runner.run(request)) if not saved else None
            if execution:
                while not execution.done():
                    await asyncio.wait({execution}, timeout=min(15, self.settings.lease_seconds / 3))
                    await asyncio.to_thread(self.heartbeat, job_id, fence)
                response = execution.result()
            else:
                response = RunResponse.model_validate(saved)
            await asyncio.to_thread(self.finish, job_id, fence, request, response)
        except Exception as error:
            if execution and not execution.done():
                execution.cancel()
                await self.runner.cancel(job_id)
            await asyncio.to_thread(self.fail, job_id, fence, error)

    def reconcile(self):
        self.db.check_leader()
        self.recover_after_runtime_change()
        self.recover_invalid_spec_finding()
        with self.db.transaction() as s:
            ids = list(s.scalars(select(Task.id).where(Task.state.not_in(["Merged"]))))
        for task_id in ids:
            try:
                with self.db.transaction() as s:
                    task = task_lock(s, task_id)
                    if not task.data.get("pr"):
                        continue
                    repo = self.settings.repo_for(task)
                    snap = self.github.snapshot(repo, task)
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
                                hash=task.data["spec_hash"],
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
