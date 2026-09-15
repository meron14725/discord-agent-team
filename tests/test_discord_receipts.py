import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_team.adapters.discord import DeliveryReceipts, send_chunked
from agent_team.api import create_app


class Message:
    def __init__(self, message_id, content):
        self.id = message_id
        self.content = content
        self.author = SimpleNamespace(id=7)

    async def edit(self, **kwargs):
        self.content = kwargs["content"]
        return self


class Channel:
    id = 12

    def __init__(self):
        self.messages = []
        self.show_history = True

    async def history(self, limit):
        if self.show_history:
            for message in self.messages[-limit:]:
                yield message

    async def fetch_message(self, message_id):
        return next(m for m in self.messages if m.id == message_id)

    async def send(self, content, **kwargs):
        message = Message(len(self.messages) + 1, content)
        self.messages.append(message)
        return message


def test_clean_delivery_recovers_from_db_without_history_or_memory(team):
    settings, db, *_ = team

    async def scenario():
        channel = Channel()
        text = "A" * 1800 + "B" * 100
        for attempt in range(2):
            # Reconstruct both clients: identity survives process memory loss.
            app = create_app(db=db, settings=settings, token="test-internal")
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test",
                headers={"Authorization": "Bearer test-internal"},
            ) as api:
                posted = await send_chunked(
                    channel, text, event_id="conversation", bot_user_id=7,
                    receipts=DeliveryReceipts(api),
                )
                assert [m.id for m in posted] == [1, 2]
                assert "".join(m.content for m in posted) == text
                assert all("[event:" not in m.content for m in posted)
            channel.show_history = False
        assert len(channel.messages) == 2

    asyncio.run(scenario())


def test_receipt_failure_preserves_recovery_marker_and_retry_cleans_it(team):
    settings, db, *_ = team

    class FailingReceipts(DeliveryReceipts):
        async def save(self, key, message_id):
            raise RuntimeError("database unavailable")

    async def scenario():
        channel = Channel()
        app = create_app(db=db, settings=settings, token="test-internal")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
            headers={"Authorization": "Bearer test-internal"},
        ) as api:
            with pytest.raises(RuntimeError, match="database unavailable"):
                await send_chunked(channel, "こんにちは", event_id="hello", bot_user_id=7,
                                   receipts=FailingReceipts(api))
            assert "[event:" in channel.messages[0].content
            posted = await send_chunked(channel, "こんにちは", event_id="hello", bot_user_id=7,
                                        receipts=DeliveryReceipts(api))
            assert len(channel.messages) == 1
            assert posted[0].content == "こんにちは"

    asyncio.run(scenario())


def test_delivery_receipts_require_internal_auth_and_validate_ids(team):
    settings, db, *_ = team
    client = TestClient(create_app(db=db, settings=settings, token="test-internal"))
    path = "/discord-deliveries/" + "a" * 64
    assert client.get(path).status_code == 401
    assert client.put(path, params={"message_id": "123"}).status_code == 401
    headers = {"Authorization": "Bearer test-internal"}
    assert client.put(path, params={"message_id": "not-an-id"}, headers=headers).status_code == 422
    assert client.put(path, params={"message_id": "123"}, headers=headers).status_code == 200
    assert client.get(path, headers=headers).json() == {"message_id": "123"}


def test_placeholder_reply_is_clean_and_recoverable(team):
    from agent_team.adapters.discord import replace_with_chunked

    settings, db, *_ = team

    async def scenario():
        channel = Channel()
        placeholder = await channel.send("確認しています…")
        app = create_app(db=db, settings=settings, token="test-internal")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
            headers={"Authorization": "Bearer test-internal"},
        ) as api:
            await replace_with_chunked(
                channel, placeholder, "こんにちは。今日はどうしましょう？",
                event_id="chat-1-coordinator", bot_user_id=7, receipts=DeliveryReceipts(api),
            )
            assert placeholder.content == "こんにちは。今日はどうしましょう？"
            channel.show_history = False
            posted = await send_chunked(
                channel, placeholder.content, event_id="chat-1-coordinator", bot_user_id=7,
                receipts=DeliveryReceipts(api),
            )
            assert posted == [placeholder]
            assert len(channel.messages) == 1

    asyncio.run(scenario())
