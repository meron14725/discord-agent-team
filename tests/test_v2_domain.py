import pytest

from agent_team.consultations import ConsultationLimits, check_budget, stable_topic_id
from agent_team.delegations import DelegationEnvelope, validate_delegation
from agent_team.explanations import bind_explanation, render_explanation
from agent_team.planning import plan_path, validate_plan, validate_review_coverage
from agent_team.policy import GuardError
from agent_team.renderer import create_renderer
from agent_team.requirements import bind_requirements
from agent_team.workflow import WorkflowV2Service
from agent_team.workspaces import TopicLink, WorkItemIndex, WorkspaceService, render_index

REQUIREMENTS = """# 要件
## 背景
x
## 目的
x
## 対象ユーザー
x
## スコープ
x
## 対象外
x
## 機能要件
x
## 非機能要件
x
## 受入条件
- AC-01: オーナーが自然文から案件を開始できる
- AC-02: 承認済みの計画だけ実装を開始できる
## テスト
x
## 未解決事項
なし
"""


PLAN = """# 実装計画
## 変更予定ファイル
x
## 受入条件との対応
- AC-01: 自動試験
- AC-02: 自動試験
## テストコマンド
x
## セキュリティ
x
## 冪等性
x
## 競合
x
## 権限
x
## ロールアウト
x
## マイグレーション
x
## ロールバック
x
## 未確認事項
なし
"""


def test_requirements_and_plan_bind_to_exact_issue_acceptance_set():
    requirements = bind_requirements(
        repository="owner/repo",
        issue_number=2,
        issue_url="https://github.com/owner/repo/issues/2",
        updated_at="2026-09-12T00:00:00Z",
        body=REQUIREMENTS,
    )
    plan = validate_plan(
        body=PLAN,
        requirements_body=REQUIREMENTS,
        issue_number=2,
        version=3,
        base_sha="a" * 40,
    )
    assert requirements.acceptance_ids == plan.acceptance_ids == ("AC-01", "AC-02")
    assert plan.path == plan_path(2, 3)
    validate_review_coverage(
        plan.acceptance_ids,
        [
            {"acceptance_id": "AC-01", "status": "met", "evidence": "test a"},
            {"acceptance_id": "AC-02", "status": "met", "evidence": "test b"},
        ],
        [],
    )


def test_plan_rejects_unknown_or_missing_acceptance_criteria():
    with pytest.raises(GuardError, match="coverage mismatch"):
        validate_plan(
            body=PLAN.replace("AC-02", "AC-03"),
            requirements_body=REQUIREMENTS,
            issue_number=2,
            version=1,
            base_sha="a" * 40,
        )


def test_topic_identity_is_stable_and_limits_are_strict():
    first = stable_topic_id("event-1", " API の認証方式 ")
    second = stable_topic_id("event-1", "api の認証方式")
    assert first == second
    check_budget(topic_count=4, task_consultations=14, model_calls=29, limits=ConsultationLimits())
    with pytest.raises(GuardError, match="topic limit"):
        check_budget(topic_count=5, task_consultations=14, model_calls=29, limits=ConsultationLimits())


def test_delegation_keeps_typed_identity_and_prevents_cycles():
    envelope = DelegationEnvelope(
        task_id="TASK-1",
        topic_id=stable_topic_id("e", "設計"),
        source_role="cto",
        target_role="backend_integrator",
        purpose="実装計画を作る",
        expected_artifact="plans/v1.md",
    )
    validate_delegation(
        envelope,
        known_roles={"cto", "backend_integrator"},
        allowed_targets={"backend_integrator"},
    )
    with pytest.raises(GuardError, match="cycle"):
        validate_delegation(
            envelope,
            known_roles={"cto", "backend_integrator"},
            allowed_targets={"backend_integrator"},
            visited_roles=("backend_integrator",),
        )


def test_work_item_index_is_deterministic():
    item = WorkItemIndex(
        issue_number=2,
        issue_url="https://github.com/o/r/issues/2",
        requirements_hash="sha256:req",
        task_id="TASK-2",
        topics=(
            TopicLink("topic-bbbbbbbbbbbbbbbb", "B", "https://discord.com/b"),
            TopicLink("topic-aaaaaaaaaaaaaaaa", "A", "https://discord.com/a"),
        ),
    )
    assert render_index(item) == render_index(item)
    assert render_index(item).index("topic-a") < render_index(item).index("topic-b")


def test_explanation_rejects_active_content_and_binds_source():
    labels = " ".join(
        ("利用価値", "対象範囲", "対象外", "通常フロー", "権限", "データ境界", "復元困難", "失敗", "受入条件", "ロールバック", "未解決事項")
    )
    html = (
        '<!doctype html><meta http-equiv="Content-Security-Policy" '
        'content="default-src \'none\'; style-src \'unsafe-inline\'">' + labels
    )
    artifact = bind_explanation(
        source_kind="plan", source_hash="sha256:source", html=html, png=b"\x89PNG\r\n\x1a\nbody"
    )
    assert artifact.source_hash == "sha256:source"
    with pytest.raises(GuardError, match="active"):
        bind_explanation(
            source_kind="plan",
            source_hash="sha256:source",
            html=html + "<script>alert(1)</script>",
            png=b"\x89PNG\r\n\x1a\nbody",
        )


def test_fixed_explanation_renderer_has_no_external_or_script_content():
    html, png = render_explanation("requirements", "sha256:source", "# <目的>\n安全に動かす")
    artifact = bind_explanation(
        source_kind="requirements",
        source_hash="sha256:source",
        html=html,
        png=png,
    )
    assert "&lt;目的&gt;" in html
    assert "<script" not in html and "https://" not in html
    assert artifact.png_hash.startswith("sha256:")


def test_renderer_process_is_credential_free_and_rejects_secret_input():
    from fastapi.testclient import TestClient

    client = TestClient(create_renderer())
    assert client.get("/health").json() == {
        "status": "ok",
        "credentials": False,
        "network_assets": False,
    }
    valid = client.post(
        "/render",
        json={
            "source_kind": "requirements",
            "source_hash": "sha256:source",
            "markdown": "# 説明",
        },
    )
    assert valid.is_success
    assert "Content-Security-Policy" in valid.json()["html"]
    blocked = client.post(
        "/render",
        json={
            "source_kind": "requirements",
            "source_hash": "sha256:source",
            "markdown": "Authorization: Bearer " + "A" * 40,
        },
    )
    assert blocked.status_code == 422


def test_topic_archives_only_after_resolved_plus_24_hours(team):
    settings, db, *_ = team
    settings.workflow_v2.enabled = True
    settings.workflow_v2.repository_aliases = ["demo"]
    service = WorkflowV2Service(db, settings)
    with db.transaction() as session:
        task = service.create_task(
            session,
            task_id="TASK-ARCHIVE",
            repo="demo",
            repository="example/demo",
            summary="archive",
        )
        topic = WorkspaceService(settings).create_topic(
            session,
            task,
            origin_event_id="event-archive",
            purpose="完了後に閉じる",
            title="archive test",
        )
        topic.thread_id = "123"
        WorkspaceService.resolve_topic(session, topic, 1000)
    with db.transaction() as session:
        assert WorkspaceService.enqueue_due_archives(session, 1000 + 86400 - 1) == 0
        assert WorkspaceService.enqueue_due_archives(session, 1000 + 86400) == 1
        assert WorkspaceService.enqueue_due_archives(session, 1000 + 86400) == 0
