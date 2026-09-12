import pytest
from pydantic import ValidationError

from agent_team.api import enforce_explicit_audience, explicitly_addresses_all, requests_discord_change
from agent_team.config import Check, Repo, Settings
from agent_team.contracts import CoordinationDecision


def live_settings(**overrides):
    values = {
        "mode": "live",
        "guild_id": "1",
        "channel_id": "2",
        "owner_ids": ["3"],
        "repos": {"project": Repo(repository="owner/project", checks=[Check(name="tests", app_id=1)])},
    }
    values.update(overrides)
    return Settings(**values)


def test_natural_language_requests_require_message_content_and_known_default_repo():
    with pytest.raises(ValidationError, match="message content and a default repo"):
        live_settings(natural_language_requests=True, default_repo="project")
    with pytest.raises(ValidationError, match="message content and a default repo"):
        live_settings(natural_language_requests=True, message_content=True, default_repo="unknown")
    settings = live_settings(
        natural_language_requests=True, message_content=True, default_repo="project"
    )
    assert settings.default_repo == "project"


def test_specialist_role_endpoints_can_be_split():
    settings = Settings(
        specialist_url="http://fallback:8092",
        specialist_urls={
            "upstream": "http://upstream:8092",
            "downstream": "http://downstream:8094",
            "sre": "http://sre:8093",
        },
        specialist_concurrency=3,
        specialist_retry_attempts=3,
    )
    assert settings.specialist_endpoint("upstream") == "http://upstream:8092"
    assert settings.specialist_endpoint("cto") == "http://upstream:8092"
    assert settings.specialist_endpoint("downstream") == "http://downstream:8094"
    assert settings.specialist_endpoint("backend_integrator") == "http://downstream:8094"
    assert settings.specialist_endpoint("sre") == "http://sre:8093"
    assert settings.specialist_endpoint("security_sre") == "http://sre:8093"
    assert settings.specialist_concurrency == 3
    assert settings.specialist_retry_attempts == 3


def test_explicit_team_audience_is_detected_without_matching_definition_questions():
    assert explicitly_addresses_all("みんなこんにちは")
    assert explicitly_addresses_all("全員、意見を聞かせて")
    assert explicitly_addresses_all("他のメンバーにもこんにちはって言わせて")
    assert not explicitly_addresses_all("みんなとはどういう意味？")
    assert not explicitly_addresses_all("挨拶して")


def test_explicit_team_audience_can_expand_beyond_normal_four_role_limit():
    decision = CoordinationDecision(
        action="reply",
        reply="全員へ依頼します。",
        task_summary="",
        delegations=[],
    )
    expanded = enforce_explicit_audience(
        decision,
        "みんな自己紹介して",
        ("cto", "backend_integrator", "security_sre", "frontend_ux", "qa", "evaluation_manager", "analyst"),
    )
    assert len(expanded.delegations) == 7


def test_explicit_discord_changes_require_a_typed_plan_without_matching_howto_questions():
    assert requests_discord_change(
        "情シス、AIエージェント検証カテゴリに project-sre-test チャンネルを作る変更案を出して"
    )
    assert requests_discord_change("このスレッドをアーカイブして")
    assert requests_discord_change("チャンネルのtopicを更新して")
    assert not requests_discord_change("Discordチャンネルの作り方を教えて")
    assert not requests_discord_change("チャンネル作成は可能ですか？")
    assert not requests_discord_change("現在のDiscord構成を確認して")
