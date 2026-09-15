import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from agent_team.adapters.discord import (
    apply_persona_for_delivery,
    replace_with_chunked,
    send_chunked,
)
from agent_team.contracts import CoordinationDecision, DiscordSREPlan, SpecialistDecision
from agent_team.discord_delivery import (
    discord_parts,
    specialist_next_step,
    split_discord_text,
    task_next_step,
    task_waits_for_owner,
)
from agent_team.persona import PersonaDefinition, persona_formatter, render_persona_reply


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


def test_coordinator_replaces_placeholder_then_uses_common_marked_retry_boundary():
    class Message:
        def __init__(self, message_id, author_id, content=""):
            self.id = message_id
            self.author = SimpleNamespace(id=author_id)
            self.content = content
            self.edits = []

        async def edit(self, **kwargs):
            self.content = kwargs["content"]
            self.edits.append(kwargs)
            return self

    class Channel:
        def __init__(self, placeholder):
            self.messages = [placeholder]
            self.next_id = 2

        async def history(self, limit):
            for message in reversed(self.messages[-limit:]):
                yield message

        async def send(self, content, **kwargs):
            message = Message(self.next_id, 7, content)
            message.kwargs = kwargs
            self.next_id += 1
            self.messages.append(message)
            return message

    async def scenario():
        placeholder = Message(1, 7, "内容を確認しています…")
        channel = Channel(placeholder)
        text = "A" * 1800 + "\n\n" + "B" * 1800
        sent = await replace_with_chunked(
            channel,
            placeholder,
            text,
            event_id="chat-1-coordinator",
            bot_user_id=7,
        )
        assert sent[0] is placeholder
        assert len(sent) > 1
        assert f"part:1/{len(sent)}" in placeholder.content
        first_mentions = placeholder.edits[0]["allowed_mentions"]
        assert (first_mentions.everyone, first_mentions.users, first_mentions.roles) == (
            False,
            False,
            False,
        )

        missing = sent[1]
        channel.messages.remove(missing)
        retried = await send_chunked(
            channel,
            text,
            event_id="chat-1-coordinator",
            bot_user_id=7,
        )
        assert retried[0] is placeholder
        assert retried[1].id == len(sent) + 1
        retry_mentions = retried[1].kwargs["allowed_mentions"]
        assert (retry_mentions.everyone, retry_mentions.users, retry_mentions.roles) == (
            False,
            False,
            False,
        )

    asyncio.run(scenario())


def test_disabled_persona_preserves_all_six_legacy_delivery_cases_without_formatter_call():
    sre_plan = DiscordSREPlan(
        schema_version=1,
        operation="create_text_channel",
        guild_id="guild",
        target_id="",
        parent_category_id="category",
        name="project-test",
        topic="",
        archive=None,
        reason="検証用チャンネルが必要",
        impact="カテゴリ権限を継承する",
        verification="作成後の親カテゴリを確認する",
        rollback="利用を停止して削除承認を求める",
    )
    cases = [
        CoordinationDecision(action="reply", reply="通常返信", task_summary="", delegations=[]),
        CoordinationDecision(
            action="delegate", reply="担当へ依頼", task_summary="",
            delegations=[{"role": "upstream", "instruction": "要件を確認する"}],
        ),
        decision("clarify", reply="一点確認します？"),
        decision("request_approval", reply="承認待ちです。", approval_reason="外部変更"),
        decision("continue", reply="調査を続けます。", continuation_instruction="次のログを見る"),
        decision(
            "request_approval",
            reply="Discord変更は未実行です。",
            approval_reason="Discord変更",
            sre_plan=sre_plan,
        ),
    ]

    async def scenario():
        calls = []

        def forbidden_formatter(request):
            calls.append(request)
            raise AssertionError("disabled persona called formatter")

        for index, item in enumerate(cases):
            original = f"legacy-body-{index}"
            body, audit = await apply_persona_for_delivery(
                enabled=False,
                original_body=original,
                decision=item,
                formatter=forbidden_formatter,
            )
            assert body == original
            assert audit is None
        assert calls == []

    asyncio.run(scenario())


def test_enabled_four_roles_keep_role_version_and_stable_delivery_markers():
    roles = ("coordinator", "cto", "backend_integrator", "security_sre")

    async def scenario():
        for role in roles:
            content = Path(f"prompts/personas/{role}/v1/PERSONA.md").read_text()
            item = (
                CoordinationDecision(
                    action="reply", reply="確認しました。", task_summary="", delegations=[]
                )
                if role == "coordinator"
                else decision("reply", reply="確認しました。")
            )
            body, audit = await apply_persona_for_delivery(
                enabled=True,
                original_body=item.reply,
                decision=item,
                role_id=role,
                persona=PersonaDefinition(role, "v1", content),
                formatter=persona_formatter,
            )
            assert audit and (audit.role_id, audit.version) == (role, "v1")
            first = discord_parts(body, "same-event-" + role)
            second = discord_parts(body, "same-event-" + role)
            assert [part.marker for part in first] == [part.marker for part in second]

    asyncio.run(scenario())


def test_every_persona_failure_mode_continues_once_through_discord_delivery():
    class Channel:
        def __init__(self):
            self.messages = []

        async def history(self, limit):
            for message in reversed(self.messages[-limit:]):
                yield message

        async def send(self, content, **kwargs):
            message = SimpleNamespace(
                id=len(self.messages) + 1,
                content=content,
                author=SimpleNamespace(id=7),
                kwargs=kwargs,
            )
            self.messages.append(message)
            return message

    async def timeout_formatter(request):
        await asyncio.sleep(0.02)
        return request.safe_source_reply

    async def scenario():
        channel = Channel()
        persona = PersonaDefinition("cto", "v1", "role_id: cto\n## Voice\n簡潔")
        formatters = {
            "empty": lambda request: "",
            "invalid": lambda request: "別内容",
            "over-limit": lambda request: "x" * 3000 + request.safe_source_reply,
            "internal-error": lambda request: 1 / 0,
            "timeout": timeout_formatter,
        }
        for name, formatter in formatters.items():
            rendered = await render_persona_reply(
                decision=decision(),
                role_id="cto",
                persona=persona,
                formatter=formatter,
                timeout_seconds=0.001,
                max_characters=100,
                identifiers={"evidence": "x" * 2500} if name == "over-limit" else None,
            )
            assert rendered.audit.fallback
            before = len(channel.messages)
            first = await send_chunked(
                channel, rendered.text, event_id="fallback-" + name, bot_user_id=7
            )
            after_first = len(channel.messages)
            second = await send_chunked(
                channel, rendered.text, event_id="fallback-" + name, bot_user_id=7
            )
            assert after_first > before
            assert len(channel.messages) == after_first
            assert [message.id for message in first] == [message.id for message in second]

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
