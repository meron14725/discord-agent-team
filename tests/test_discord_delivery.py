import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from agent_team.adapters.discord import send_chunked
from agent_team.contracts import SpecialistDecision
from agent_team.discord_delivery import (
    discord_parts,
    specialist_next_step,
    split_discord_text,
    task_next_step,
    task_waits_for_owner,
)


def test_approval_buttons_work_on_coordinator_and_upstream(monkeypatch):
    from agent_team.adapters import discord as gateway
    from agent_team.config import Settings

    class SetupComplete(Exception):
        pass

    clients = []
    client_type = gateway.discord.Client

    def client_factory(**kwargs):
        client = client_type(**kwargs)
        client.start = AsyncMock(side_effect=SetupComplete)
        client.wait_until_ready = AsyncMock(side_effect=SetupComplete)
        clients.append(client)
        return client

    api = SimpleNamespace(post=AsyncMock(), aclose=AsyncMock())
    api.post.return_value = SimpleNamespace(
        is_success=True, json=lambda: {"id": "TASK-test", "state": "PlanningImplementation"}
    )
    monkeypatch.setattr(gateway.discord, "Client", client_factory)
    monkeypatch.setattr(gateway, "load_settings", lambda: Settings(guild_id="123"))
    monkeypatch.setattr(gateway, "secret", lambda _: "test-token-not-a-real-credential")
    monkeypatch.setattr(gateway.httpx, "AsyncClient", lambda **_: api)

    async def scenario():
        with pytest.raises(SetupComplete):
            await gateway.serve()
        for client in clients[:2]:
            response = SimpleNamespace(defer=AsyncMock())
            interaction = SimpleNamespace(
                data={"custom_id": "team:approval-event"}, id=456,
                user=SimpleNamespace(id=789), guild_id=123, channel_id=321,
                response=response, followup=SimpleNamespace(send=AsyncMock()),
            )

            async def post(path, *, json):
                response.defer.assert_awaited_once_with(ephemeral=True)
                assert path == "/buttons/approval-event"
                assert json == {
                    "event_id": "456", "actor": "789", "guild": "123", "channel": "321",
                    "action": "button",
                }
                return api.post.return_value

            api.post.side_effect = post
            await client.on_interaction(interaction)
            interaction.followup.send.assert_awaited_once_with(
                "TASK-test: PlanningImplementation", ephemeral=True
            )
        assert api.post.await_count == 2

    asyncio.run(scenario())


def decision(action="reply", **changes):
    values = {
        "action": action,
        "reply": "状況を確認しました。",
        "task_summary": "",
        "approval_reason": "",
        "continuation_instruction": "",
        "sre_plan": None,
        "handoffs": [],
    }
    values.update(changes)
    return SpecialistDecision(**values)


def test_discord_text_is_split_without_loss_at_readable_boundaries():
    text = "A" * 20 + "\n\n" + "B" * 20 + "\n" + "C" * 20
    chunks = split_discord_text(text, limit=25)

    assert "".join(chunks) == text
    assert all(len(chunk) <= 25 for chunk in chunks)
    assert chunks[0].endswith("\n\n")


def test_discord_parts_have_stable_ordered_markers_below_platform_limit():
    parts = discord_parts("段落\n\n" * 700, "OUTBOX-1")

    assert len(parts) > 1
    assert [part.index for part in parts] == list(range(1, len(parts) + 1))
    assert all(len(part.content) <= 2000 for part in parts)
    assert all(part.marker in part.content for part in parts)


def test_chunked_retry_only_sends_a_missing_part():
    class Channel:
        def __init__(self):
            self.messages = []
            self.next_id = 1

        async def history(self, limit):
            for message in reversed(self.messages[-limit:]):
                yield message

        async def send(self, content, **kwargs):
            message = SimpleNamespace(
                id=self.next_id,
                content=content,
                author=SimpleNamespace(id=7),
                kwargs=kwargs,
            )
            self.next_id += 1
            self.messages.append(message)
            return message

    async def scenario():
        channel = Channel()
        text = "A" * 1800 + "\n\n" + "B" * 1800 + "\n\n" + "C" * 100
        first = await send_chunked(channel, text, event_id="evt", bot_user_id=7)
        assert len(first) == 3
        missing = channel.messages.pop(1)
        retried = await send_chunked(channel, text, event_id="evt", bot_user_id=7)
        assert len(channel.messages) == 3
        assert [message.id for message in retried] == [first[0].id, 4, first[2].id]
        assert missing.id == 2

    asyncio.run(scenario())


def test_specialist_next_step_is_explicit_for_every_action():
    assert specialist_next_step(decision()).endswith("完了")
    assert "回答待ち" in specialist_next_step(decision("clarify"), owner_mention="@owner")
    assert "承認待ち" in specialist_next_step(
        decision("request_approval", approval_reason="権限変更"), owner_mention="@owner"
    )
    assert "自動継続" in specialist_next_step(
        decision("continue", continuation_instruction="ログを確認する"),
        continuation_turn=0,
        continuation_limit=2,
    )
    assert "引き継ぎ" in specialist_next_step(
        decision(
            "handoff",
            handoffs=[{"role": "sre", "reason": "障害調査", "instruction": "ログを見る"}],
        )
    )


def test_self_continuation_requires_a_next_instruction():
    with pytest.raises(ValidationError, match="concrete next instruction"):
        decision("continue")


def test_task_next_step_distinguishes_owner_wait_and_automatic_work():
    assert "オーナー" in task_next_step("AwaitingPlanApproval")
    assert "自動継続" in task_next_step("Implementing")
    assert "自動継続なし" in task_next_step("Merged")
    assert task_waits_for_owner("AwaitingPlanApproval")
    assert not task_waits_for_owner("Implementing")
