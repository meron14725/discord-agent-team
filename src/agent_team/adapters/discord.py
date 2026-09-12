import asyncio
import io
import logging

import discord
import httpx
from discord import app_commands

from ..config import load_settings, secret

log = logging.getLogger(__name__)


async def serve():
    settings = load_settings()
    import os

    api = httpx.AsyncClient(
        base_url=os.environ.get("CONTROL_URL", "http://orchestrator:8080"),
        timeout=httpx.Timeout(settings.coordination_timeout + 120),
        headers={"Authorization": "Bearer " + secret("INTERNAL_TOKEN")},
    )
    coordinator_intents = discord.Intents.default()
    coordinator_intents.message_content = settings.message_content
    coordinator = discord.Client(
        intents=coordinator_intents, allowed_mentions=discord.AllowedMentions.none()
    )
    upstream = discord.Client(
        intents=discord.Intents.default(), allowed_mentions=discord.AllowedMentions.none()
    )
    downstream = discord.Client(
        intents=discord.Intents.default(), allowed_mentions=discord.AllowedMentions.none()
    )
    sre = discord.Client(intents=discord.Intents.default(), allowed_mentions=discord.AllowedMentions.none())
    tree = app_commands.CommandTree(upstream)
    guild = discord.Object(id=int(settings.guild_id))
    coordinator_lock = asyncio.Lock()
    specialist_slots = asyncio.Semaphore(settings.specialist_concurrency)

    async def sre_platform_snapshot(guild_id: int):
        guild = sre.get_guild(guild_id)
        if guild is None or sre.user is None:
            raise RuntimeError("SRE guild cache is unavailable")
        member = guild.me or await guild.fetch_member(sre.user.id)
        permissions = member.guild_permissions
        protected_channels = set(settings.discord_sre.protected_channel_ids)
        managed_categories = set(settings.discord_sre.managed_category_ids)
        protected_roles = set(settings.discord_sre.protected_role_ids)

        channels = []
        for channel in [*guild.channels, *guild.threads]:
            if isinstance(channel, discord.CategoryChannel):
                kind = "category"
                category_id = channel.id
            elif isinstance(channel, discord.TextChannel):
                kind = "text"
                category_id = channel.category_id or 0
            elif isinstance(channel, discord.VoiceChannel):
                kind = "voice"
                category_id = channel.category_id or 0
            elif isinstance(channel, discord.Thread):
                kind = "thread"
                category_id = channel.parent.category_id if channel.parent else 0
            else:
                kind = "other"
                category_id = getattr(channel, "category_id", 0) or 0
            channel_id = str(channel.id)
            channels.append(
                {
                    "id": channel_id,
                    "name": channel.name,
                    "kind": kind,
                    "category_id": str(category_id) if category_id else "",
                    "topic": getattr(channel, "topic", "") or "",
                    "protected": channel_id in protected_channels,
                    "managed": str(category_id) in managed_categories
                    or (kind == "category" and channel_id in managed_categories),
                }
            )

        audit = []
        if permissions.view_audit_log:
            async for entry in guild.audit_logs(limit=10):
                audit.append(
                    {
                        "action": entry.action.name,
                        "target_id": str(getattr(entry.target, "id", "") or ""),
                        "actor_id": str(getattr(entry.user, "id", "") or ""),
                        "reason": entry.reason or "",
                        "created_at": entry.created_at.isoformat(),
                    }
                )

        return {
            "guild_id": str(guild.id),
            "bot_id": str(sre.user.id),
            "permissions": {
                name: getattr(permissions, name)
                for name in (
                    "administrator",
                    "view_audit_log",
                    "manage_guild",
                    "manage_channels",
                    "manage_roles",
                    "manage_messages",
                    "manage_threads",
                    "create_public_threads",
                    "create_private_threads",
                )
            },
            "roles": [
                {
                    "id": str(role.id),
                    "name": role.name,
                    "position": role.position,
                    "managed": role.managed,
                    "protected": str(role.id) in protected_roles,
                }
                for role in guild.roles
            ],
            "channels": channels,
            "recent_audit": audit,
        }

    def channel_snapshot(channel):
        if isinstance(channel, discord.CategoryChannel):
            kind = "category"
            category_id = channel.id
            parent_id = 0
        elif isinstance(channel, discord.TextChannel):
            kind = "text"
            category_id = channel.category_id or 0
            parent_id = channel.category_id or 0
        elif isinstance(channel, discord.Thread):
            kind = "thread"
            category_id = channel.parent.category_id if channel.parent else 0
            parent_id = channel.parent_id
        else:
            raise ValueError("Unsupported Discord target type")
        return {
            "guild_id": str(channel.guild.id),
            "target_id": str(channel.id),
            "kind": kind,
            "name": channel.name,
            "parent_id": str(parent_id) if parent_id else "",
            "category_id": str(category_id) if category_id else "",
            "topic": getattr(channel, "topic", "") or "",
            "archived": getattr(channel, "archived", False),
        }

    async def current_sre_target(plan):
        guild = sre.get_guild(int(plan["guild_id"]))
        if guild is None:
            raise ValueError("SRE guild is unavailable")
        if plan["operation"] == "create_text_channel":
            category = guild.get_channel(int(plan["parent_category_id"]))
            if not isinstance(category, discord.CategoryChannel):
                raise ValueError("Managed category is unavailable")
            existing = next(
                (channel for channel in category.channels if channel.name == plan["name"]), None
            )
            return channel_snapshot(existing) if existing else None
        target = guild.get_channel_or_thread(int(plan["target_id"]))
        if target is None:
            target = await sre.fetch_channel(int(plan["target_id"]))
        return channel_snapshot(target)

    async def execute_sre_plan(change):
        plan = change["plan"]
        guild = sre.get_guild(int(plan["guild_id"]))
        if guild is None:
            raise ValueError("SRE guild is unavailable")
        reason = f"{change['id']} {change['digest']}"
        if plan["operation"] == "create_text_channel":
            category = guild.get_channel(int(plan["parent_category_id"]))
            if not isinstance(category, discord.CategoryChannel):
                raise ValueError("Managed category is unavailable")
            target = await guild.create_text_channel(
                plan["name"], category=category, topic=plan["topic"] or None, reason=reason
            )
        else:
            target = guild.get_channel_or_thread(int(plan["target_id"]))
            if target is None:
                target = await sre.fetch_channel(int(plan["target_id"]))
            if plan["operation"] == "update_channel_topic" and isinstance(
                target, discord.TextChannel
            ):
                await target.edit(topic=plan["topic"], reason=reason)
            elif plan["operation"] == "archive_thread" and isinstance(target, discord.Thread):
                await target.edit(archived=True, reason=reason)
            else:
                raise ValueError("Discord target no longer matches the approved operation")
        refreshed = await sre.fetch_channel(target.id)
        return channel_snapshot(refreshed)

    async def propose_sre_change(message, decision):
        plan = decision.get("sre_plan")
        if not plan:
            return
        before = await current_sre_target(plan)
        response = await api.post(
            "/discord-changes",
            json={
                "event_id": str(message.id),
                "actor": str(message.author.id),
                "guild": str(message.guild.id),
                "channel": str(message.channel.id),
                "plan": plan,
                "before": before,
            },
        )
        if response.is_error:
            await message.channel.send(
                "Discord変更案は安全検査を通過しませんでした。監査チャンネルを確認します。",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            log.warning("Discord SRE proposal rejected: HTTP %s", response.status_code)
            return
        change = response.json()
        audit_channel = await sre.fetch_channel(int(settings.discord_sre.audit_channel_id))
        operation = {
            "create_text_channel": "テキストチャンネル作成",
            "update_channel_topic": "チャンネルトピック更新",
            "archive_thread": "スレッドのアーカイブ",
        }[plan["operation"]]
        view = discord.ui.View(timeout=None)
        view.add_item(
            discord.ui.Button(
                label="このDiscord変更を承認",
                style=discord.ButtonStyle.danger,
                custom_id=f"sre-change:{change['id']}:{change['digest']}",
            )
        )
        await audit_channel.send(
            (
                f"**{change['id']} — {operation}**\n"
                f"理由: {plan['reason']}\n影響: {plan['impact']}\n"
                f"確認方法: {plan['verification']}\nロールバック: {plan['rollback']}\n"
                f"対象: `{plan.get('target_id') or plan.get('parent_category_id')}`\n"
                f"承認ハッシュ: `{change['digest']}`"
            )[:1900],
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def handle_sre_interaction(interaction):
        custom_id = (interaction.data or {}).get("custom_id", "")
        if not custom_id.startswith("sre-change:"):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        _, change_id, digest = custom_id.split(":", 2)
        approval = await api.post(
            f"/discord-changes/{change_id}/approve",
            json={
                **identity(interaction),
                "digest": digest,
            },
        )
        if approval.is_error:
            await interaction.followup.send("承認できませんでした。期限または対象を確認してください。")
            return
        change = approval.json()
        try:
            current = await current_sre_target(change["plan"])
            authorization = await api.post(
                f"/discord-changes/{change_id}/authorize",
                json={"current_before": current},
            )
            if authorization.is_error:
                await interaction.followup.send(
                    "提案後に対象が変わったため、承認を失効しました。変更案を作り直してください。"
                )
                return
            result = await execute_sre_plan(authorization.json())
            await api.post(
                f"/discord-changes/{change_id}/complete",
                json={"success": True, "result": result},
            )
            await interaction.channel.send(
                f"{change_id}: Discord変更を実行し、変更後の状態を確認しました。",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            await interaction.followup.send("承認したDiscord変更を実行しました。")
        except Exception as error:
            log.exception("Discord SRE execution failed: change=%s", change_id)
            await api.post(
                f"/discord-changes/{change_id}/complete",
                json={"success": False, "result": {}, "error": type(error).__name__},
            )
            await interaction.followup.send("Discord変更に失敗しました。権限と監査ログを確認します。")

    handle_sre_interaction.__name__ = "on_interaction"
    sre.event(handle_sre_interaction)

    def identity(interaction):
        return {
            "event_id": str(interaction.id),
            "actor": str(interaction.user.id),
            "guild": str(interaction.guild_id),
            "channel": str(interaction.channel_id),
        }

    async def execute(interaction, action, **values):
        await interaction.response.defer(ephemeral=True, thinking=True)
        response = await api.post("/commands", json={**identity(interaction), "action": action, **values})
        if response.is_error:
            message = response.json().get("detail", "受付に失敗しました")
        else:
            task = response.json()
            d = task["data"]
            message = f"{task['id']}: {task['state']}\n仕様v{task['version']}\n{d.get('pr_url', '')}\n{d.get('reason', '')}"
        await interaction.followup.send(
            str(message)[:1800], ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
        )

    @tree.command(name="request", description="新規案件を依頼", guild=guild)
    async def request(interaction: discord.Interaction, repo: str, summary: str):
        await execute(interaction, "request", repo=repo, text=summary)

    @tree.command(name="answer", description="要件への回答", guild=guild)
    async def answer(interaction: discord.Interaction, task: str, text: str):
        await execute(interaction, "answer", task_id=task, text=text)

    @tree.command(name="revise", description="仕様変更・承認取り直し", guild=guild)
    async def revise(interaction: discord.Interaction, task: str, text: str):
        await execute(interaction, "revise", task_id=task, text=text)

    def register(name, description):
        async def handler(interaction: discord.Interaction, task: str):
            await execute(interaction, name, task_id=task)

        tree.add_command(
            app_commands.Command(name=name, description=description, callback=handler), guild=guild
        )

    for name, description in [
        ("status", "案件状態"),
        ("pause", "一時停止"),
        ("resume", "再開"),
        ("cancel", "キャンセル"),
        ("retry", "停止原因解消後に再試行"),
    ]:
        register(name, description)

    @upstream.event
    async def on_interaction(interaction):
        custom_id = (interaction.data or {}).get("custom_id", "")
        if not custom_id.startswith("team:"):
            return
        await interaction.response.defer(ephemeral=True)
        r = await api.post(
            "/buttons/" + custom_id.removeprefix("team:"), json={**identity(interaction), "action": "button"}
        )
        text = (
            f"{r.json()['id']}: {r.json()['state']}"
            if r.is_success
            else str(r.json().get("detail", "承認失敗"))
        )
        await interaction.followup.send(text[:1800], ephemeral=True)

    @coordinator.event
    async def on_message(message):
        if not settings.message_content or message.author.bot or message.webhook_id or not message.guild:
            return
        if str(message.author.id) not in settings.owner_ids or str(message.guild.id) != settings.guild_id:
            return
        content = message.content.strip()
        if not content:
            return
        if isinstance(message.channel, discord.Thread):
            r = await api.get("/threads")
            r.raise_for_status()
            task = next((t for t in r.json() if t["thread_id"] == str(message.channel.id)), None)
            if not task:
                return
            await api.post(
                "/commands",
                json={
                    "action": "answer",
                    "event_id": str(message.id),
                    "actor": str(message.author.id),
                    "guild": str(message.guild.id),
                    "channel": str(message.channel.id),
                    "task_id": task["task_id"],
                    "text": content,
                },
            )
            return
        if settings.natural_language_requests and str(message.channel.id) == settings.channel_id:
            waiting = await message.reply(
                "内容を確認しています…",
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            try:
                async with coordinator_lock:
                    history = []
                    async for prior in message.channel.history(limit=15, before=message):
                        if prior.content.strip():
                            history.append(f"{prior.author.display_name}: {prior.content.strip()[:1800]}")
                    history.reverse()
                    response = await api.post(
                        "/coordinate",
                        json={
                            "event_id": str(message.id),
                            "actor": str(message.author.id),
                            "guild": str(message.guild.id),
                            "channel": str(message.channel.id),
                            "text": content,
                            "history": history,
                        },
                    )
                    if response.is_error:
                        log.warning("Coordinator decision rejected: HTTP %s", response.status_code)
                        await waiting.edit(content="判断処理に失敗しました。少し待ってからもう一度送ってください。")
                        return
                    decision = response.json()
                    if decision["action"] == "task":
                        task = await api.post(
                            "/commands",
                            json={
                                "action": "request",
                                "event_id": str(message.id),
                                "actor": str(message.author.id),
                                "guild": str(message.guild.id),
                                "channel": str(message.channel.id),
                                "repo": settings.default_repo,
                                "text": decision["task_summary"],
                            },
                        )
                        if task.is_error:
                            log.warning("Coordinated task rejected: HTTP %s", task.status_code)
                            await waiting.edit(content="作業依頼の登録に失敗しました。監査ログを確認します。")
                            return
                    await waiting.edit(content=decision["reply"])
                    if decision["action"] == "delegate":
                        bots = {"upstream": upstream, "downstream": downstream, "sre": sre}

                        async def specialist_turn(delegated):
                            bot = bots[delegated["role"]]
                            try:
                                async with specialist_slots:
                                    channel = bot.get_channel(message.channel.id)
                                    if channel is None:
                                        channel = await bot.fetch_channel(message.channel.id)
                                    specialist = None
                                    for attempt in range(settings.specialist_retry_attempts):
                                        specialist = await api.post(
                                            "/specialist-turn",
                                            json={
                                                "event_id": f"{message.id}-{attempt}",
                                                "actor": str(message.author.id),
                                                "guild": str(message.guild.id),
                                                "channel": str(message.channel.id),
                                                "text": content,
                                                "history": history,
                                                "role": delegated["role"],
                                                "instruction": delegated["instruction"],
                                                "discord_snapshot": (
                                                    await sre_platform_snapshot(message.guild.id)
                                                    if delegated["role"] == "sre"
                                                    else None
                                                ),
                                            },
                                        )
                                        if specialist.is_success or specialist.status_code not in {
                                            409,
                                            502,
                                            503,
                                        }:
                                            break
                                        if attempt + 1 < settings.specialist_retry_attempts:
                                            await asyncio.sleep(2)
                                if specialist.is_error:
                                    log.warning(
                                        "Specialist response rejected: role=%s HTTP %s",
                                        delegated["role"],
                                        specialist.status_code,
                                    )
                                    await channel.send(
                                        "一時的に応答できませんでした。少し待ってから、もう一度呼んでください。",
                                        allowed_mentions=discord.AllowedMentions.none(),
                                    )
                                    return
                                decision = specialist.json()
                                await channel.send(
                                    decision["reply"],
                                    allowed_mentions=discord.AllowedMentions.none(),
                                )
                                if delegated["role"] == "sre" and decision.get("sre_plan"):
                                    await propose_sre_change(message, decision)
                            except Exception:
                                log.exception("Specialist turn failed: role=%s", delegated["role"])

                        await asyncio.gather(
                            *(specialist_turn(delegated) for delegated in decision["delegations"])
                        )
            except Exception:
                log.exception("Coordinator message handling failed")
                await waiting.edit(content="判断処理に失敗しました。少し待ってからもう一度送ってください。")

    @upstream.event
    async def on_ready():
        await tree.sync(guild=guild)
        if settings.message_content:
            # Bounded catch-up. The durable /answer command is the recovery fallback for older gaps.
            r = await api.get("/threads")
            r.raise_for_status()
            for item in r.json():
                channel = await upstream.fetch_channel(int(item["thread_id"]))
                async for message in channel.history(limit=100, oldest_first=False):
                    await on_message(message)

    async def notifications():
        await coordinator.wait_until_ready()
        await upstream.wait_until_ready()
        await downstream.wait_until_ready()
        await sre.wait_until_ready()
        while True:
            try:
                response = await api.get("/outbox")
                response.raise_for_status()
                for item in response.json():
                    try:
                        bot = {
                            "downstream": downstream,
                            "sre": sre,
                            "coordinator": coordinator,
                        }.get(item["role"], upstream)
                        marker = f"[event:{item['id']}]"
                        if item.get("create_thread"):
                            channel = await coordinator.fetch_channel(int(settings.channel_id))
                            message = None
                            async for candidate in channel.history(limit=100):
                                if candidate.author.id == coordinator.user.id and marker in candidate.content:
                                    message = candidate
                                    break
                            if message is None:
                                message = await channel.send(f"{item['task_id']}\n{marker}")
                            thread = message.thread or await message.create_thread(name=item["task_id"])
                            await thread.send(item["body"][:1800])
                            await api.post(
                                f"/outbox/{item['id']}/ack",
                                json={"message_id": str(message.id), "thread_id": str(thread.id)},
                            )
                            continue
                        if not item.get("thread_id"):
                            continue
                        channel = await bot.fetch_channel(int(item["thread_id"]))
                        existing = None
                        async for candidate in channel.history(limit=100):
                            if candidate.author.id == bot.user.id and marker in candidate.content:
                                existing = candidate
                                break
                        if existing is None:
                            kwargs = {}
                            if item.get("approval"):
                                view = discord.ui.View(timeout=None)
                                view.add_item(
                                    discord.ui.Button(
                                        label="仕様を承認"
                                        if item["approval"] == "spec"
                                        else "このSHAのマージを承認",
                                        custom_id="team:" + item["id"],
                                        style=discord.ButtonStyle.success,
                                    )
                                )
                                kwargs["view"] = view
                            if item.get("spec"):
                                kwargs["file"] = discord.File(
                                    io.BytesIO(item["spec"].encode()), filename="spec.md"
                                )
                            body = item["body"][:1500]
                            if item.get("head_sha"):
                                body += "\nhead: " + item["head_sha"]
                            existing = await channel.send(body + "\n" + marker, **kwargs)
                        await api.post(f"/outbox/{item['id']}/ack", json={"message_id": str(existing.id)})
                    except Exception:
                        log.warning("Notification failed: %s", item["id"])
                        await api.post(f"/outbox/{item['id']}/fail")
            except Exception:
                log.warning("Control connection unavailable; retrying")
            await asyncio.sleep(2)

    try:
        await asyncio.gather(
            coordinator.start(secret("DISCORD_COORDINATOR_TOKEN")),
            upstream.start(secret("DISCORD_UPSTREAM_TOKEN")),
            downstream.start(secret("DISCORD_DOWNSTREAM_TOKEN")),
            sre.start(secret("DISCORD_SRE_TOKEN")),
            notifications(),
        )
    finally:
        await coordinator.close()
        await upstream.close()
        await downstream.close()
        await sre.close()
        await api.aclose()
