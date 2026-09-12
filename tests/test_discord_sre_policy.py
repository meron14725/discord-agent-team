import pytest
from pydantic import ValidationError

from agent_team.config import DiscordSRE, Settings
from agent_team.contracts import DiscordSREPlan, DiscordTargetSnapshot
from agent_team.policy import GuardError, validate_discord_sre_plan


def settings():
    return Settings(
        guild_id="100",
        channel_id="200",
        discord_sre=DiscordSRE(
            enabled=True,
            managed_category_ids=["300"],
            protected_channel_ids=["200", "201"],
            protected_role_ids=["400", "401"],
            audit_channel_id="201",
        ),
    )


def plan(operation, **values):
    defaults = {
        "schema_version": 1,
        "target_id": "",
        "parent_category_id": "",
        "name": "",
        "topic": "",
        "archive": None,
    }
    return DiscordSREPlan(
        operation=operation,
        guild_id="100",
        reason="案件用の会話場所を用意する",
        impact="管理対象カテゴリ内に限定される",
        verification="Discord APIから変更後の状態を再取得する",
        rollback="作成チャンネルを読み取り専用化して監査へ記録する",
        **{**defaults, **values},
    )


def snapshot(**values):
    defaults = {
        "guild_id": "100",
        "target_id": "500",
        "kind": "text",
        "name": "project-demo",
        "parent_id": "300",
        "category_id": "300",
    }
    return DiscordTargetSnapshot(**{**defaults, **values})


def test_managed_channel_creation_is_bound_to_a_stable_hash():
    change = plan("create_text_channel", parent_category_id="300", name="project-demo")
    first = validate_discord_sre_plan(change, None, settings())
    second = validate_discord_sre_plan(change, None, settings())
    assert first == second and first.startswith("sha256:")


@pytest.mark.parametrize("name", ["Project Demo", "-project", "project-", "project--demo", "../demo"])
def test_channel_creation_rejects_unsafe_names(name):
    change = plan("create_text_channel", parent_category_id="300", name=name)
    with pytest.raises(GuardError, match="channel name"):
        validate_discord_sre_plan(change, None, settings())


def test_change_cannot_escape_managed_category_or_touch_protected_channel():
    unmanaged = plan("create_text_channel", parent_category_id="999", name="project-demo")
    with pytest.raises(GuardError, match="outside the managed area"):
        validate_discord_sre_plan(unmanaged, None, settings())

    protected = plan("update_channel_topic", target_id="200", topic="new")
    with pytest.raises(GuardError, match="Protected"):
        validate_discord_sre_plan(protected, snapshot(target_id="200"), settings())


def test_existing_change_requires_matching_live_snapshot():
    change = plan("update_channel_topic", target_id="500", topic="repository: owner/demo")
    with pytest.raises(GuardError, match="snapshot mismatch"):
        validate_discord_sre_plan(change, snapshot(target_id="501"), settings())
    assert validate_discord_sre_plan(change, snapshot(), settings()).startswith("sha256:")


def test_archive_is_thread_only_and_requires_true():
    with pytest.raises(ValidationError, match="archive=true"):
        plan("archive_thread", target_id="500", archive=False)
    change = plan("archive_thread", target_id="500", archive=True)
    with pytest.raises(GuardError, match="Only threads"):
        validate_discord_sre_plan(change, snapshot(), settings())
    thread = snapshot(kind="thread", parent_id="510")
    assert validate_discord_sre_plan(change, thread, settings()).startswith("sha256:")


def test_feature_flag_and_guild_fence_are_mandatory():
    change = plan("create_text_channel", parent_category_id="300", name="project-demo")
    disabled = settings()
    disabled.discord_sre.enabled = False
    with pytest.raises(GuardError, match="disabled"):
        validate_discord_sre_plan(change, None, disabled)
    wrong_guild = change.model_copy(update={"guild_id": "999"})
    with pytest.raises(GuardError, match="guild mismatch"):
        validate_discord_sre_plan(wrong_guild, None, settings())
