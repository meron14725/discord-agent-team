import asyncio
import base64
import hashlib
import io
import logging

import discord
import httpx
from discord import app_commands

from ..config import load_settings, secret
from ..contracts import SpecialistDecision
from ..coordination import resolve_specialist_handoffs
from ..redaction import SecretScanner

log = logging.getLogger(__name__)


async def serve():
    settings = load_settings()
    import os

    internal_token = secret("INTERNAL_TOKEN")
    scanner = SecretScanner(hashlib.sha256(internal_token.encode()).digest())
    api = httpx.AsyncClient(
        base_url=os.environ.get("CONTROL_URL", "http://orchestrator:8080"),
        timeout=httpx.Timeout(settings.coordination_timeout + 120),
        headers={"Authorization": "Bearer " + internal_token},
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
    role_clients = {
        "coordinator": coordinator,
        "cto": upstream,
        "backend_integrator": downstream,
        "security_sre": sre,
    }
    for definition in settings.role_registry.entries:
        if definition.enabled and definition.discord_enabled and definition.id not in role_clients:
            role_clients[definition.id] = discord.Client(
                intents=discord.Intents.default(),
                allowed_mentions=discord.AllowedMentions.none(),
            )
    for alias, canonical in settings.role_registry.aliases.items():
        if canonical in role_clients:
            role_clients[alias] = role_clients[canonical]
    tree = app_commands.CommandTree(upstream)
    guild = discord.Object(id=int(settings.guild_id))
    coordinator_lock = asyncio.Lock()
    specialist_slots = asyncio.Semaphore(settings.specialist_concurrency)

    async def remove_secret_message(message):
        """Use the SRE identity to remove a detected secret and retain metadata only."""
        report = scanner.scan_text(message.content)
        if not report.blocked:
            return False
        deleted = False
        try:
            channel = await sre.fetch_channel(message.channel.id)
            target = await channel.fetch_message(message.id)
            await target.delete(reason="deterministic secret scanner")
            deleted = True
        except Exception:
            log.exception("SRE could not delete detected secret message id=%s", message.id)
        if settings.discord_sre.audit_channel_id:
            try:
                audit = await sre.fetch_channel(int(settings.discord_sre.audit_channel_id))
                kinds = ", ".join(sorted({finding.kind for finding in report.findings}))
                await audit.send(
                    (
                        "秘密情報らしき投稿を検出しました。値は保存していません。\n"
                        f"message_id: `{message.id}` / channel_id: `{message.channel.id}`\n"
                        f"分類: {kinds} / 削除: {'成功' if deleted else '失敗'}\n"
                        f"監査hash: `{report.content_hash}`\n"
                        "該当資格情報を失効・再発行してください。"
                    ),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except Exception:
                log.exception("SRE secret audit notification failed message id=%s", message.id)
        return True

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
        ("restart", "キャンセル済み案件を新しい案件として再開"),
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
        if await remove_secret_message(message):
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
        project_channels = settings.workflow_v2.project_channels
        accepted_channels = {settings.channel_id, *project_channels.values()}
        if settings.natural_language_requests and str(message.channel.id) in accepted_channels:
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
                                "repo": next(
                                    (
                                        alias
                                        for alias, channel_id in project_channels.items()
                                        if channel_id == str(message.channel.id)
                                    ),
                                    settings.default_repo,
                                ),
                                "text": decision["task_summary"],
                            },
                        )
                        if task.is_error:
                            log.warning("Coordinated task rejected: HTTP %s", task.status_code)
                            await waiting.edit(content="作業依頼の登録に失敗しました。監査ログを確認します。")
                            return
                    await waiting.edit(content=decision["reply"])
                    if decision["action"] == "delegate":
                        bots = role_clients
                        initial_roles = [item["role"] for item in decision["delegations"]]
                        for delegated in decision["delegations"]:
                            target_user = bots[delegated["role"]].user
                            if target_user is not None and coordinator.user is not None:
                                await message.channel.send(
                                    (
                                        f"<@{coordinator.user.id}> → <@{target_user.id}> 依頼\n"
                                        f"{delegated['instruction'][:1400]}"
                                    ),
                                    allowed_mentions=discord.AllowedMentions(users=[target_user]),
                                )

                        async def specialist_turn(
                            delegated,
                            *,
                            handoff_depth=0,
                            handoff_round=0,
                            handoff_source_roles=None,
                            visited_roles=None,
                            handoff_context="",
                            allow_fallback=True,
                        ):
                            bot = bots[delegated["role"]]
                            try:
                                async with specialist_slots:
                                    channel = bot.get_channel(message.channel.id)
                                    if channel is None:
                                        channel = await bot.fetch_channel(message.channel.id)
                                    specialist = None
                                    turn_history = list(history)
                                    if handoff_context:
                                        turn_history.append(handoff_context)
                                    while sum(len(item) for item in turn_history) > 30_000:
                                        turn_history.pop(0)
                                    for attempt in range(settings.specialist_retry_attempts):
                                        specialist = await api.post(
                                            "/specialist-turn",
                                            json={
                                                "event_id": (
                                                    f"{message.id}-{delegated['role']}-"
                                                    f"{handoff_depth}-{handoff_round}-{attempt}"
                                                ),
                                                "actor": str(message.author.id),
                                                "guild": str(message.guild.id),
                                                "channel": str(message.channel.id),
                                                "text": content,
                                                "history": turn_history,
                                                "role": delegated["role"],
                                                "instruction": delegated["instruction"],
                                                "handoff_depth": handoff_depth,
                                                "handoff_round": handoff_round,
                                                "handoff_source_roles": handoff_source_roles or [],
                                                "visited_roles": visited_roles or initial_roles,
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
                                    role_definition = settings.role_registry.role(
                                        delegated["role"]
                                    )
                                    fallback = role_definition.fallback_role
                                    if (
                                        allow_fallback
                                        and specialist.status_code in {502, 503}
                                        and fallback
                                        and fallback in bots
                                    ):
                                        fallback_bot = bots[fallback]
                                        if fallback_bot.user is not None:
                                            await channel.send(
                                                (
                                                    f"<@{fallback_bot.user.id}> "
                                                    f"{role_definition.display_name}への接続が3回失敗したため、"
                                                    "同じ能力範囲の代替担当へ依頼します。"
                                                ),
                                                allowed_mentions=discord.AllowedMentions(
                                                    users=[fallback_bot.user]
                                                ),
                                            )
                                        return await specialist_turn(
                                            {
                                                "role": fallback,
                                                "instruction": (
                                                    delegated["instruction"]
                                                    + "\n接続不能だった元担当: "
                                                    + role_definition.id
                                                ),
                                            },
                                            handoff_depth=handoff_depth,
                                            handoff_round=handoff_round,
                                            handoff_source_roles=handoff_source_roles,
                                            visited_roles=visited_roles,
                                            handoff_context=handoff_context,
                                            allow_fallback=False,
                                        )
                                    await channel.send(
                                        "一時的に応答できませんでした。少し待ってから、もう一度呼んでください。",
                                        allowed_mentions=discord.AllowedMentions.none(),
                                    )
                                    return
                                specialist_decision = SpecialistDecision.model_validate(
                                    specialist.json()
                                )
                                await channel.send(
                                    (
                                        f"<@{coordinator.user.id}> {specialist_decision.reply}"
                                        if coordinator.user is not None
                                        else specialist_decision.reply
                                    ),
                                    allowed_mentions=(
                                        discord.AllowedMentions(users=[coordinator.user])
                                        if coordinator.user is not None
                                        else discord.AllowedMentions.none()
                                    ),
                                )
                                if delegated["role"] == "sre" and specialist_decision.sre_plan:
                                    await propose_sre_change(
                                        message, specialist_decision.model_dump(mode="json")
                                    )
                                return delegated["role"], specialist_decision
                            except Exception:
                                log.exception("Specialist turn failed: role=%s", delegated["role"])
                                return None

                        initial_results = await asyncio.gather(
                            *(specialist_turn(delegated) for delegated in decision["delegations"])
                        )
                        completed = [result for result in initial_results if result is not None]
                        followups = resolve_specialist_handoffs(initial_roles, completed)
                        if followups:
                            all_visited = [
                                *initial_roles,
                                *(followup.role for followup in followups),
                            ]

                            async def run_handoff_dialogue(followup):
                                context = followup.context
                                source_roles = list(followup.source_roles)
                                for round_trip in range(2):
                                    recipient_result = await specialist_turn(
                                        {
                                            "role": followup.role,
                                            "instruction": followup.instruction,
                                        },
                                        handoff_depth=1,
                                        handoff_round=round_trip,
                                        handoff_source_roles=source_roles,
                                        visited_roles=all_visited,
                                        handoff_context=context,
                                    )
                                    if recipient_result is None:
                                        return
                                    _, recipient_decision = recipient_result
                                    if recipient_decision.action != "handoff":
                                        return
                                    answers = await asyncio.gather(
                                        *(
                                            specialist_turn(
                                                {
                                                    "role": question.role,
                                                    "instruction": question.instruction,
                                                },
                                                handoff_depth=2,
                                                handoff_round=round_trip,
                                                visited_roles=all_visited,
                                                handoff_context=(
                                                    f"{followup.role}からの確認理由: "
                                                    f"{question.reason}"
                                                ),
                                            )
                                            for question in recipient_decision.handoffs
                                        )
                                    )
                                    answer_context = []
                                    for answer in answers:
                                        if answer is not None:
                                            answer_role, answer_dec = answer
                                            answer_context.append(
                                                f"{answer_role}からの回答: {answer_dec.reply}"
                                            )
                                    if not answer_context:
                                        return
                                    context = "\n".join(
                                        [context, recipient_decision.reply, *answer_context]
                                    )[-20_000:]

                            await asyncio.gather(
                                *(run_handoff_dialogue(followup) for followup in followups)
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
        for client in dict.fromkeys(role_clients.values()):
            await client.wait_until_ready()
        while True:
            try:
                response = await api.get("/outbox")
                response.raise_for_status()
                for item in response.json():
                    try:
                        outbound = str(item.get("body", "")) + "\n" + str(item.get("spec", ""))
                        if scanner.scan_text(outbound).blocked:
                            log.error("Secret blocked at Discord outbox boundary event=%s", item["id"])
                            await api.post(f"/outbox/{item['id']}/fail")
                            continue
                        bot = role_clients.get(item["role"], upstream)
                        marker = f"[event:{item['id']}]"
                        if item.get("archive_topic_thread"):
                            thread = await sre.fetch_channel(int(item["thread_id"]))
                            await thread.edit(
                                archived=True,
                                reason=f"resolved topic {item['topic_record_id']}",
                            )
                            await api.post(
                                f"/outbox/{item['id']}/ack",
                                json={"message_id": item["thread_id"]},
                            )
                            continue
                        if item.get("create_topic_thread"):
                            channel = await coordinator.fetch_channel(int(item["channel_id"]))
                            thread = await channel.create_thread(
                                name=item["thread_name"],
                                type=discord.ChannelType.public_thread,
                                reason=f"agent topic {item['topic_id']}",
                            )
                            mention = ""
                            allowed = discord.AllowedMentions.none()
                            if item.get("owner_confirmation") and settings.owner_ids:
                                owner = discord.Object(id=int(settings.owner_ids[0]))
                                mention = f"<@{owner.id}> "
                                allowed = discord.AllowedMentions(users=[owner])
                            posted = await thread.send(
                                f"{mention}{item['body'][:1700]}\n{marker}",
                                allowed_mentions=allowed,
                            )
                            await api.post(
                                f"/outbox/{item['id']}/ack",
                                json={
                                    "message_id": str(posted.id),
                                    "thread_id": str(thread.id),
                                },
                            )
                            continue
                        if item.get("project_status"):
                            channel = await coordinator.fetch_channel(int(item["channel_id"]))
                            view = None
                            owner_mentions = discord.AllowedMentions.none()
                            mention = ""
                            if item.get("approval"):
                                view = discord.ui.View(timeout=None)
                                labels = {
                                    "requirements": "要件を承認",
                                    "plan": "実装計画を承認",
                                    "merge": "このSHAのマージを承認",
                                }
                                view.add_item(
                                    discord.ui.Button(
                                        label=labels[item["approval"]],
                                        custom_id="team:" + item["id"],
                                        style=discord.ButtonStyle.success,
                                    )
                                )
                                approvers = (
                                    settings.workflow_v2.requirements_approver_ids
                                    if item["approval"] == "requirements"
                                    else settings.workflow_v2.plan_approver_ids
                                )
                                if approvers:
                                    owner = discord.Object(id=int(approvers[0]))
                                    mention = f"<@{owner.id}> "
                                    owner_mentions = discord.AllowedMentions(users=[owner])
                            body = (
                                f"{mention}**{item['task_id']}**\n{item['body'][:1500]}\n{marker}"
                            )
                            files = [
                                discord.File(
                                    io.BytesIO(base64.b64decode(attachment["content_base64"])),
                                    filename=attachment["filename"],
                                )
                                for attachment in item.get("attachments", [])
                            ]
                            existing = None
                            if item.get("status_message_id"):
                                try:
                                    existing = await channel.fetch_message(
                                        int(item["status_message_id"])
                                    )
                                except discord.NotFound:
                                    existing = None
                            if existing is None:
                                existing = await channel.send(
                                    body,
                                    view=view,
                                    allowed_mentions=owner_mentions,
                                    files=files,
                                )
                            else:
                                await existing.edit(
                                    content=body,
                                    view=view,
                                    allowed_mentions=owner_mentions,
                                    attachments=files,
                                )
                            await api.post(
                                f"/outbox/{item['id']}/ack",
                                json={"message_id": str(existing.id)},
                            )
                            continue
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
                            body_prefix = ""
                            if item.get("mention_owner") and settings.owner_ids:
                                owner = discord.Object(id=int(settings.owner_ids[0]))
                                body_prefix = f"<@{owner.id}> "
                                kwargs["allowed_mentions"] = discord.AllowedMentions(users=[owner])
                            if item.get("delegation_log"):
                                target_bot = role_clients.get(item["target_role"])
                                if target_bot and target_bot.user:
                                    body_prefix = f"<@{target_bot.user.id}> "
                                    kwargs["allowed_mentions"] = discord.AllowedMentions(
                                        users=[target_bot.user]
                                    )
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
                            body = body_prefix + item["body"][:1500]
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
        starts = []
        for role_id, client in {
            role.id: role_clients[role.id]
            for role in settings.role_registry.entries
            if role.enabled and role.discord_enabled
        }.items():
            starts.append(
                client.start(secret(settings.discord_bot_key(role_id).upper()))
            )
        await asyncio.gather(*starts, notifications())
    finally:
        for client in dict.fromkeys(role_clients.values()):
            await client.close()
        await api.aclose()
