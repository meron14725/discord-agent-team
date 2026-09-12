import pytest

from agent_team.config import DiscordSRE
from agent_team.contracts import DiscordSREPlan, DiscordTargetSnapshot
from agent_team.db import DiscordChange
from agent_team.discord_sre import DiscordChangeService
from agent_team.policy import GuardError


def configure(settings):
    settings.discord_sre = DiscordSRE(
        enabled=True,
        managed_category_ids=["category"],
        protected_channel_ids=["demo-channel", "audit"],
        protected_role_ids=["sre-role"],
        audit_channel_id="audit",
        approval_seconds=900,
    )


def create_plan(name="project-demo"):
    return DiscordSREPlan(
        schema_version=1,
        operation="create_text_channel",
        guild_id="demo-guild",
        target_id="",
        parent_category_id="category",
        name=name,
        topic="repository: owner/demo",
        archive=None,
        reason="案件チャンネルを作成する",
        impact="検証カテゴリ内にチャンネルが増える",
        verification="作成後のIDと親カテゴリを確認する",
        rollback="チャンネルを読み取り専用化する",
    )


def update_plan():
    return DiscordSREPlan(
        schema_version=1,
        operation="update_channel_topic",
        guild_id="demo-guild",
        target_id="target",
        parent_category_id="",
        name="",
        topic="repository: owner/new",
        archive=None,
        reason="topicを更新する",
        impact="topicだけが変わる",
        verification="変更後のtopicを確認する",
        rollback="以前のtopicへ戻す",
    )


def target(topic="repository: owner/old"):
    return DiscordTargetSnapshot(
        guild_id="demo-guild",
        target_id="target",
        kind="text",
        name="project-demo",
        parent_id="category",
        category_id="category",
        topic=topic,
    )


def test_change_is_idempotent_approved_once_and_completed(team):
    settings, db = team[:2]
    configure(settings)
    service = DiscordChangeService(db, settings)
    values = {
        "event_id": "proposal-1",
        "actor": "demo-owner",
        "guild": "demo-guild",
        "channel": "demo-channel",
        "plan": create_plan(),
        "before": None,
    }
    proposed = service.propose(**values)
    assert service.propose(**values)["id"] == proposed["id"]
    with pytest.raises(GuardError, match="different plan"):
        service.propose(**{**values, "plan": create_plan("project-other")})
    with pytest.raises(GuardError, match="Stale"):
        service.approve(
            proposed["id"],
            event_id="approval-1",
            actor="demo-owner",
            guild="demo-guild",
            channel="audit",
            digest="wrong",
        )
    approved = service.approve(
        proposed["id"],
        event_id="approval-1",
        actor="demo-owner",
        guild="demo-guild",
        channel="audit",
        digest=proposed["digest"],
    )
    assert approved["status"] == "approved"
    assert service.authorize_execution(proposed["id"], None)["status"] == "executing"
    completed = service.complete(
        proposed["id"], success=True, result={"channel_id": "created"}
    )
    assert completed["status"] == "completed"
    with pytest.raises(GuardError, match="not approved"):
        service.authorize_execution(proposed["id"], None)


def test_proposal_revalidates_nested_dicts_from_api_model_dump(team):
    settings, db = team[:2]
    configure(settings)
    service = DiscordChangeService(db, settings)

    proposed = service.propose(
        event_id="proposal-dumped",
        actor="demo-owner",
        guild="demo-guild",
        channel="demo-channel",
        plan=create_plan().model_dump(mode="json"),
        before=None,
    )

    assert proposed["status"] == "pending"
    assert proposed["plan"]["guild_id"] == "demo-guild"


def test_changed_target_invalidates_approval_and_persists_stale_state(team):
    settings, db = team[:2]
    configure(settings)
    service = DiscordChangeService(db, settings)
    proposed = service.propose(
        event_id="proposal-2",
        actor="demo-owner",
        guild="demo-guild",
        channel="demo-channel",
        plan=update_plan(),
        before=target(),
    )
    service.approve(
        proposed["id"],
        event_id="approval-2",
        actor="demo-owner",
        guild="demo-guild",
        channel="audit",
        digest=proposed["digest"],
    )
    with pytest.raises(GuardError, match="changed after proposal"):
        service.authorize_execution(proposed["id"], target("changed elsewhere"))
    with db.transaction() as session:
        assert session.get(DiscordChange, proposed["id"]).status == "stale"


def test_only_owner_and_allowed_channels_can_approve(team):
    settings, db = team[:2]
    configure(settings)
    service = DiscordChangeService(db, settings)
    proposed = service.propose(
        event_id="proposal-3",
        actor="demo-owner",
        guild="demo-guild",
        channel="demo-channel",
        plan=create_plan(),
        before=None,
    )
    for actor, channel in (("intruder", "audit"), ("demo-owner", "other")):
        with pytest.raises(GuardError, match="Unauthorized"):
            service.approve(
                proposed["id"],
                event_id="approval-3",
                actor=actor,
                guild="demo-guild",
                channel=channel,
                digest=proposed["digest"],
            )
