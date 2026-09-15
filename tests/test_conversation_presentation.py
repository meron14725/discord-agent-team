"""Acceptance checks across API decisions, production presentation and delivery.

Only model/network boundaries are doubled. Discord edits return a new object,
as discord.py does; final assertions inspect server content, not input strings.
"""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from agent_team.adapters.codex import MockRunner
from agent_team.adapters.discord import (
    DeliveryReceipts,
    render_specialist_for_delivery,
    replace_with_chunked,
    send_chunked,
)
from agent_team.api import create_app
from agent_team.contracts import SpecialistDecision
from agent_team.persona import PersonaDefinition


def persona(role):
    return PersonaDefinition(
        role, "v1", Path(f"prompts/personas/{role}/v1/PERSONA.md").read_text()
    )


def decision(action="reply", reply="担当です。要件を整理します。"):
    return SpecialistDecision(
        action=action, reply=reply, task_summary="実装の検討" if action == "recommend_task" else "",
        approval_reason="変更の確認" if action == "request_approval" else "",
        continuation_instruction="調査を続ける" if action == "continue" else "",
        sre_plan=None,
        handoffs=[{"role": "security_sre", "reason": "安全確認", "instruction": "調査して"}]
        if action == "handoff" else [],
    )


class Message:
    def __init__(self, channel, message_id, content):
        self.channel, self.id, self.content = channel, message_id, content
        self.author = SimpleNamespace(id=7)

    async def edit(self, *, content, **kwargs):
        replacement = Message(self.channel, self.id, content)
        self.channel.messages[self.id] = replacement
        return replacement


class Channel:
    id = 12

    def __init__(self):
        self.messages = {}

    async def history(self, limit):
        # Exercise durable receipts, not the in-memory/history fallback.
        for message in []:
            yield message

    async def fetch_message(self, message_id):
        return self.messages[message_id]

    async def send(self, content, **kwargs):
        message = Message(self, len(self.messages) + 1, content)
        self.messages[message.id] = message
        return message


def test_team_introductions_api_to_visible_discord_messages(team, monkeypatch):
    settings, db, *_ = team
    original_run = MockRunner.run
    introduction = "担当です。目的を確認します。要件を整理します。設計の相談に応じます。"

    async def model(self, request):
        result = await original_run(self, request)
        if request.kind == "respond":
            result.result.specialist = decision(reply=introduction)
        return result

    monkeypatch.setattr(MockRunner, "run", model)

    async def scenario():
        app = create_app(db=db, settings=settings, token="test-token")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
            headers={"Authorization": "Bearer test-token"},
        ) as api:
            command = dict(
                event_id="introduction", actor="demo-owner", guild="demo-guild",
                channel="demo-channel", text="みんな自己紹介して", history=[],
            )
            coordinated = await api.post("/coordinate", json=command)
            assert coordinated.status_code == 200
            roles = set()
            for delegation in coordinated.json()["delegations"]:
                role = settings.role_registry.resolve(delegation["role"])
                roles.add(role)
                response = await api.post("/specialist-turn", json={**command, **delegation})
                assert response.status_code == 200, response.text
                result = SpecialistDecision.model_validate(response.json())
                assert result.reply == introduction
                body, audit = await render_specialist_for_delivery(
                    decision=result, role_id=role, persona=persona(role), enabled=True,
                    owner_id=123, attention_user_id=7,
                    identifiers={"event_id": f"introduction-{role}"},
                    targets={"role": role}, quantities={"continuation_turn": 0},
                )
                assert not audit.fallback, audit.fallback_reason
                channel = Channel()
                # A second delivery survives missing history and doesn't duplicate.
                for _ in range(2):
                    posted = await send_chunked(
                        channel, body, event_id=role, bot_user_id=7,
                        receipts=DeliveryReceipts(api),
                    )
                    assert posted[0].content == channel.messages[1].content == body
                assert len(channel.messages) == 1
                assert introduction in body
                assert "[fixed-facts]" not in body and "[event:" not in body
                assert "次:" not in body and "操作は実行していません" not in body
            assert roles == {"cto", "backend_integrator", "security_sre"}

    asyncio.run(scenario())


@pytest.mark.parametrize("action", [
    "reply", "clarify", "request_approval", "continue", "handoff", "recommend_task",
])
@pytest.mark.parametrize("enabled", [True, False])
def test_system_ui_does_not_change_workflow_facts_or_count_as_model_prose(action, enabled):
    result = decision(action, "要点を確認します。目的があります。制約があります。根拠があります。")
    before = result.model_dump()
    body, audit = asyncio.run(render_specialist_for_delivery(
        decision=result, role_id="cto", persona=persona("cto"), enabled=enabled,
        owner_id=123, attention_user_id=123,
    ))
    assert result.model_dump() == before
    assert result.reply in body
    assert body.startswith("<@123>")
    assert ("次:" in body) == (action != "reply")
    assert "[fixed-facts]" not in body
    if audit:
        assert not audit.fallback, audit.fallback_reason
    if action == "request_approval":
        assert "<@123>の承認待ち" in body


@pytest.mark.parametrize("claim", [
    "変更を完了しました。", "実装しました。", "反映しています。",
    "変更はすべて完了です。", "処理は完了です。", "完了",
])
def test_unexecuted_completion_claim_is_still_blocked_in_model_prose(claim):
    body, audit = asyncio.run(render_specialist_for_delivery(
        decision=decision(reply=claim), role_id="cto",
        persona=persona("cto"), enabled=True, owner_id=123,
    ))
    assert audit.fallback and audit.fallback_reason == "not_run_contradiction"
    assert claim not in body


@pytest.mark.parametrize("description", [
    "実装とテストを担当しています。",
    "変更統合と回帰テストを担当しています。",
    "復旧や監視を専門にしています。",
    "完了条件の整理や設計について相談できます。",
])
def test_responsibilities_are_not_mistaken_for_executed_operations(description):
    body, audit = asyncio.run(render_specialist_for_delivery(
        decision=decision(reply=description), role_id="backend_integrator",
        persona=persona("backend_integrator"), enabled=True, owner_id=123,
    ))
    assert not audit.fallback, audit.fallback_reason
    assert body == description


def test_placeholder_delivery_uses_edit_return_value_and_removes_marker(team):
    settings, db, *_ = team

    async def scenario():
        app = create_app(db=db, settings=settings, token="test-token")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
            headers={"Authorization": "Bearer test-token"},
        ) as api:
            channel = Channel()
            placeholder = await channel.send("確認しています…")
            posted = await replace_with_chunked(
                channel, placeholder, "こんにちは。", event_id="placeholder",
                bot_user_id=7, receipts=DeliveryReceipts(api),
            )
            assert placeholder.content == "確認しています…"
            assert posted[0].content == channel.messages[1].content == "こんにちは。"
            assert len(channel.messages) == 1

    asyncio.run(scenario())
