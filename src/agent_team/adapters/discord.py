import asyncio
import base64
import hashlib
import io
import logging
import re

import discord
import httpx
from discord import app_commands

from ..config import load_settings, secret
from ..contracts import CoordinationDecision, SpecialistDecision
from ..coordination import resolve_specialist_handoffs
from ..discord_delivery import discord_parts, specialist_next_step, task_next_step
from ..persona import PersonaDefinition, persona_formatter, render_persona_reply
from ..prompt_context import load_agent_prompt_context
from ..redaction import SecretScanner

log = logging.getLogger(__name__)

DISCORD_MESSAGE_LINK = re.compile(
    r"https://(?:www\.)?discord(?:app)?\.com/channels/(?P<guild>\d+)/(?P<channel>\d+)/(?P<message>\d+)"
)
TASK_ID = re.compile(r"\bTASK-[A-Za-z0-9-]+\b")


def task_id_from_text(text: str) -> str:
    match = TASK_ID.search(text)
    return match.group(0) if match else ""


def asks_task_status(text: str) -> bool:
    compact = "".join(text.casefold().split())
    return any(
        phrase in compact
        for phrase in (
            "状況",
            "状態",
            "進捗",
            "どうなって",
            "どういうこと",
            "何待ち",
            "止まって",
            "status",
            "progress",
        )
    )


def confirms_issue_body_update(text: str) -> bool:
    compact = "".join(text.casefold().split())
    if any(phrase in compact for phrase in ("未反映", "反映してない", "反映していない")):
        return False
    return any(
        phrase in compact
        for phrase in (
            "反映した",
            "反映しました",
            "反映済み",
            "更新した",
            "更新しました",
            "更新済み",
            "コピーした",
            "コピーしました",
            "実行した",
            "実行しました",
        )
    )


def format_task_status(task: dict) -> str:
    state_labels = {
        "Blocked": "停止中",
        "DraftingRequirements": "要件作成中",
        "AwaitingRequirementsConfirmation": "要件確認待ち",
        "PlanningImplementation": "実装計画作成中",
        "AwaitingPlanApproval": "実装計画の承認待ち",
        "Implementing": "実装中",
        "Reviewing": "レビュー中",
        "AwaitingChecks": "CI確認中",
        "AwaitingMergeApproval": "マージ承認待ち",
        "Merged": "完了",
        "Cancelled": "中止",
    }
    data = task.get("data", {})
    state = task["state"]
    lines = [
        f"**{task['id']} の現在状況**",
        f"状態: {state_labels.get(state, state)} (`{state}`)",
    ]
    if reason := data.get("reason"):
        lines.append(f"理由: {reason}")
    lines.append(f"次: {task_next_step(state)}")
    if proposal := data.get("requirements_proposal_url"):
        lines.append(f"最新の要件案: {proposal}")
    return "\n".join(lines)


def owner_message_links(text: str, guild_id: str, limit: int = 3) -> list[tuple[int, int]]:
    """Return distinct same-guild Discord message targets from owner input."""
    links = []
    for match in DISCORD_MESSAGE_LINK.finditer(text):
        if match.group("guild") != guild_id:
            continue
        target = (int(match.group("channel")), int(match.group("message")))
        if target not in links:
            links.append(target)
        if len(links) == limit:
            break
    return links


async def send_chunked(
    channel,
    text: str,
    *,
    event_id: str,
    bot_user_id: int | None = None,
    first_kwargs: dict | None = None,
    known_messages: dict[str, object] | None = None,
):
    """Send every deterministic part once and return messages in content order."""
    parts = discord_parts(text, event_id)
    existing_by_marker = dict(known_messages or {})
    if bot_user_id is not None:
        async for candidate in channel.history(limit=100):
            if candidate.author.id != bot_user_id:
                continue
            for part in parts:
                if part.marker in candidate.content:
                    existing_by_marker[part.marker] = candidate
    posted = []
    for part in parts:
        message = existing_by_marker.get(part.marker)
        if message is None:
            kwargs = dict(first_kwargs or {}) if part.index == 1 else {}
            kwargs.setdefault("allowed_mentions", discord.AllowedMentions.none())
            message = await channel.send(part.content, **kwargs)
        posted.append(message)
    return posted


async def replace_with_chunked(
    channel,
    placeholder,
    text: str,
    *,
    event_id: str,
    bot_user_id: int | None = None,
    allowed_mentions=None,
):
    """Replace a progress message, then use the common idempotent part boundary."""
    parts = discord_parts(text, event_id)
    mentions = allowed_mentions or discord.AllowedMentions.none()
    await placeholder.edit(content=parts[0].content, allowed_mentions=mentions)
    return await send_chunked(
        channel,
        text,
        event_id=event_id,
        bot_user_id=bot_user_id,
        known_messages={parts[0].marker: placeholder},
    )


async def apply_persona_for_delivery(
    *,
    enabled: bool,
    original_body: str,
    decision,
    role_id: str = "",
    persona: PersonaDefinition | None = None,
    formatter=persona_formatter,
    **render_options,
):
    """Single production switch for preserving the legacy delivery path."""
    if not enabled:
        return original_body, None
    if persona is None or not role_id:
        raise ValueError("Enabled persona delivery requires an exact role/version definition")
    rendered = await render_persona_reply(
        decision=decision,
        role_id=role_id,
        persona=persona,
        formatter=formatter,
        **render_options,
    )
    return rendered.text, rendered.audit


async def serve():
    settings = load_settings()
    prompt_context = load_agent_prompt_context(
        settings.role_registry,
        persona_enabled=settings.personas.enabled,
        persona_dir=__import__("pathlib").Path(settings.personas.directory),
        active_versions=settings.personas.active_versions,
        persona_max_characters=settings.personas.definition_max_characters,
    )
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

    async def expand_owner_message_links(content: str, guild_id: str) -> str:
        """Resolve owner-authored same-guild references before task clarification."""
        references = []
        for channel_id, message_id in owner_message_links(content, guild_id):
            try:
                channel = await coordinator.fetch_channel(channel_id)
                referenced = await channel.fetch_message(message_id)
            except Exception:
                log.warning(
                    "Owner Discord reference unavailable channel=%s message=%s",
                    channel_id,
                    message_id,
                    exc_info=True,
                )
                continue
            body = referenced.content.strip()
            if str(referenced.author.id) not in settings.owner_ids or not body:
                continue
            if scanner.scan_text(body).blocked:
                log.warning(
                    "Secret blocked in owner Discord reference channel=%s message=%s",
                    channel_id,
                    message_id,
                )
                continue
            references.append(f"参照したオーナー発言 ({message_id}):\n{body[:3000]}")
        return content if not references else content + "\n\n" + "\n\n".join(references)

    async def task_from_referenced_bot_message(message):
        reference = message.reference
        if reference is None or reference.message_id is None:
            return None
        referenced = reference.resolved
        if not isinstance(referenced, discord.Message):
            try:
                channel = message.channel
                if reference.channel_id and reference.channel_id != message.channel.id:
                    channel = await coordinator.fetch_channel(reference.channel_id)
                referenced = await channel.fetch_message(reference.message_id)
            except Exception:
                log.warning(
                    "Referenced Discord message unavailable message=%s reference=%s",
                    message.id,
                    reference.message_id,
                    exc_info=True,
                )
                return None
        team_bot_ids = {
            client.user.id
            for client in dict.fromkeys(role_clients.values())
            if client.user is not None
        }
        if referenced.author.id not in team_bot_ids:
            return None
        task_id = task_id_from_text(referenced.content)
        if not task_id:
            return None
        response = await api.get(f"/tasks/{task_id}")
        return response.json() if response.is_success else None

    async def reply_with_task_status(message, task, event_kind="status"):
        await send_chunked(
            message.channel,
            format_task_status(task),
            event_id=f"task-{event_kind}-{message.id}",
            bot_user_id=(coordinator.user.id if coordinator.user is not None else None),
            first_kwargs={
                "reference": message,
                "mention_author": False,
                "allowed_mentions": discord.AllowedMentions.none(),
            },
        )

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
        referenced_task = await task_from_referenced_bot_message(message)
        if referenced_task is not None:
            if asks_task_status(content):
                await reply_with_task_status(message, referenced_task)
                return
            action = "answer"
            if (
                referenced_task["state"] == "Blocked"
                and referenced_task["data"].get("reason")
                == "Issueコメントの要件案を本文へ反映後、再試行してください。"
                and confirms_issue_body_update(content)
            ):
                action = "retry"
            response = await api.post(
                "/commands",
                json={
                    "action": action,
                    "event_id": str(message.id),
                    "actor": str(message.author.id),
                    "guild": str(message.guild.id),
                    "channel": str(message.channel.id),
                    "task_id": referenced_task["id"],
                    "text": content if action == "answer" else "",
                },
            )
            if response.is_success:
                await reply_with_task_status(message, response.json(), event_kind="accepted")
            else:
                detail = response.json().get("detail", "案件への返信を処理できませんでした。")
                await send_chunked(
                    message.channel,
                    f"**{referenced_task['id']}**\n{detail}",
                    event_id=f"task-error-{message.id}",
                    bot_user_id=(coordinator.user.id if coordinator.user is not None else None),
                    first_kwargs={
                        "reference": message,
                        "mention_author": False,
                        "allowed_mentions": discord.AllowedMentions.none(),
                    },
                )
            return
        content = await expand_owner_message_links(content, str(message.guild.id))
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
                                "repo": decision["repository_alias"],
                                "text": decision["task_summary"],
                            },
                        )
                        if task.is_error:
                            log.warning("Coordinated task rejected: HTTP %s", task.status_code)
                            await waiting.edit(content="作業依頼の登録に失敗しました。監査ログを確認します。")
                            return
                    validated_coordinator = CoordinationDecision.model_validate(decision)
                    role_id = settings.role_registry.resolve("coordinator")
                    coordinator_body, persona_audit = await apply_persona_for_delivery(
                        enabled=settings.personas.enabled,
                        original_body=decision["reply"],
                        decision=validated_coordinator,
                        role_id=role_id,
                        persona=(
                            PersonaDefinition(
                                role_id,
                                prompt_context.persona_versions[role_id],
                                prompt_context.personas[role_id],
                            )
                            if settings.personas.enabled
                            else None
                        ),
                        execution_state=(
                            "succeeded" if validated_coordinator.action == "task" else "not_run"
                        ),
                        identifiers={"event_id": message.id},
                        next_step=(
                            "task_registered" if validated_coordinator.action == "task" else None
                        ),
                        timeout_seconds=settings.personas.timeout_seconds,
                        max_characters=settings.personas.max_characters,
                    )
                    if persona_audit:
                        log.info(
                            "persona_render role=%s version=%s fallback=%s reason=%s facts=%s validation=%s",
                            persona_audit.role_id, persona_audit.version,
                            persona_audit.fallback, persona_audit.fallback_reason,
                            persona_audit.fact_digest, persona_audit.final_validation,
                        )
                    await replace_with_chunked(
                        message.channel,
                        waiting,
                        coordinator_body,
                        event_id=f"chat-{message.id}-coordinator",
                        bot_user_id=(coordinator.user.id if coordinator.user is not None else None),
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    if decision["action"] == "delegate":
                        bots = role_clients
                        initial_roles = [item["role"] for item in decision["delegations"]]
                        for delegated in decision["delegations"]:
                            target_user = bots[delegated["role"]].user
                            if target_user is not None and coordinator.user is not None:
                                await message.channel.send(
                                    (
                                        f"<@{coordinator.user.id}> → <@{target_user.id}> 依頼\n"
                                        f"{delegated['instruction']}"
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
                            continuation_turn=0,
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
                                                    f"{handoff_depth}-{handoff_round}-"
                                                    f"{continuation_turn}-{attempt}"
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
                                                "continuation_turn": continuation_turn,
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
                                attention_user = None
                                if specialist_decision.action in {"clarify", "request_approval"}:
                                    attention_user = discord.Object(id=int(settings.owner_ids[0]))
                                elif coordinator.user is not None:
                                    attention_user = coordinator.user
                                attention = (
                                    f"<@{attention_user.id}> " if attention_user is not None else ""
                                )
                                next_step = specialist_next_step(
                                    specialist_decision,
                                    owner_mention=(
                                        f"<@{settings.owner_ids[0]}>"
                                        if settings.owner_ids
                                        else "オーナー"
                                    ),
                                    continuation_turn=continuation_turn,
                                    continuation_limit=settings.specialist_continuation_limit,
                                )
                                final_body = f"{attention}{specialist_decision.reply}\n\n{next_step}"
                                role_id = settings.role_registry.resolve(delegated["role"])
                                final_body, persona_audit = await apply_persona_for_delivery(
                                    enabled=settings.personas.enabled,
                                    original_body=final_body,
                                    decision=specialist_decision,
                                    role_id=role_id,
                                    persona=(
                                        PersonaDefinition(
                                            role_id,
                                            prompt_context.persona_versions[role_id],
                                            prompt_context.personas[role_id],
                                        )
                                        if settings.personas.enabled
                                        else None
                                    ),
                                    control_blocks=tuple(
                                        part for part in (attention.strip(), next_step) if part
                                    ),
                                    execution_state="not_run",
                                    identifiers={
                                        "event_id": (
                                            f"{message.id}-{delegated['role']}-"
                                            f"{handoff_depth}-{handoff_round}-{continuation_turn}"
                                        )
                                    },
                                    targets={"role": delegated["role"]},
                                    quantities={"continuation_turn": continuation_turn},
                                    next_step=next_step,
                                    timeout_seconds=settings.personas.timeout_seconds,
                                    max_characters=settings.personas.max_characters,
                                )
                                if persona_audit:
                                    log.info(
                                        "persona_render role=%s version=%s fallback=%s reason=%s facts=%s validation=%s",
                                        persona_audit.role_id,
                                        persona_audit.version,
                                        persona_audit.fallback,
                                        persona_audit.fallback_reason,
                                        persona_audit.fact_digest,
                                        persona_audit.final_validation,
                                    )
                                if not final_body:
                                    log.error("persona final validation blocked delivery role=%s", delegated["role"])
                                    return
                                await send_chunked(
                                    channel,
                                    final_body,
                                    event_id=(
                                        f"chat-{message.id}-{delegated['role']}-"
                                        f"{handoff_depth}-{handoff_round}-{continuation_turn}"
                                    ),
                                    bot_user_id=bot.user.id if bot.user is not None else None,
                                    first_kwargs={
                                        "allowed_mentions": (
                                            discord.AllowedMentions(users=[attention_user])
                                            if attention_user is not None
                                            else discord.AllowedMentions.none()
                                        )
                                    },
                                )
                                if delegated["role"] == "sre" and specialist_decision.sre_plan:
                                    await propose_sre_change(
                                        message, specialist_decision.model_dump(mode="json")
                                    )
                                if specialist_decision.action == "continue":
                                    return await specialist_turn(
                                        {
                                            "role": delegated["role"],
                                            "instruction": specialist_decision.continuation_instruction,
                                        },
                                        handoff_depth=handoff_depth,
                                        handoff_round=handoff_round,
                                        handoff_source_roles=handoff_source_roles,
                                        visited_roles=visited_roles,
                                        handoff_context=(
                                            f"直前の自分の回答: {specialist_decision.reply}"
                                        ),
                                        allow_fallback=allow_fallback,
                                        continuation_turn=continuation_turn + 1,
                                    )
                                return delegated["role"], specialist_decision
                            except Exception:
                                log.exception("Specialist turn failed: role=%s", delegated["role"])
                                return None

                        initial_results = await asyncio.gather(
                            *(specialist_turn(delegated) for delegated in decision["delegations"])
                        )
                        completed = [result for result in initial_results if result is not None]
                        recommendations = [
                            (role, result.task_summary)
                            for role, result in completed
                            if result.action == "recommend_task"
                        ]
                        if recommendations and not decision.get("repository_alias"):
                            await message.channel.send(
                                "正式案件にする前に、変更対象のリポジトリを教えてください。",
                                allowed_mentions=discord.AllowedMentions.none(),
                            )
                            recommendations = []
                        if recommendations:
                            combined_summary = "\n".join(
                                f"{role}: {summary}" for role, summary in recommendations
                            )
                            registered = await api.post(
                                "/commands",
                                json={
                                    "action": "request",
                                    "event_id": f"{message.id}-specialist-recommendation",
                                    "actor": str(message.author.id),
                                    "guild": str(message.guild.id),
                                    "channel": str(message.channel.id),
                                    "repo": decision["repository_alias"],
                                    "text": combined_summary,
                                },
                            )
                            if registered.is_error:
                                log.warning(
                                    "Specialist task recommendation rejected: HTTP %s",
                                    registered.status_code,
                                )
                                owner = discord.Object(id=int(settings.owner_ids[0]))
                                await message.channel.send(
                                    f"<@{owner.id}> 正式案件への登録に失敗しました。次: オーナーの再指示待ち",
                                    allowed_mentions=discord.AllowedMentions(users=[owner]),
                                )
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
                            posted_parts = await send_chunked(
                                thread,
                                f"{mention}{item['body']}",
                                event_id=item["id"],
                                bot_user_id=(
                                    coordinator.user.id if coordinator.user is not None else None
                                ),
                                first_kwargs={"allowed_mentions": allowed},
                            )
                            await api.post(
                                f"/outbox/{item['id']}/ack",
                                json={
                                    "message_id": str(posted_parts[0].id),
                                    "message_ids": [str(part.id) for part in posted_parts],
                                    "thread_id": str(thread.id),
                                },
                            )
                            continue
                        if item.get("project_status"):
                            channel = await coordinator.fetch_channel(int(item["channel_id"]))
                            posted_parts = None
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
                            elif item.get("mention_owner") and settings.owner_ids:
                                owner = discord.Object(id=int(settings.owner_ids[0]))
                                mention = f"<@{owner.id}> "
                                owner_mentions = discord.AllowedMentions(users=[owner])
                            body = f"{mention}**{item['task_id']}**\n{item['body']}"
                            if item.get("next_action"):
                                body += f"\n次: {item['next_action']}"
                            parts = discord_parts(body, item["id"])
                            files = [
                                discord.File(
                                    io.BytesIO(base64.b64decode(attachment["content_base64"])),
                                    filename=attachment["filename"],
                                )
                                for attachment in item.get("attachments", [])
                            ]
                            existing = None
                            # Discord does not notify users when a mention is added by editing
                            # an existing message. Owner-attention notices therefore need a
                            # fresh message instead of reusing the project status message.
                            if len(parts) == 1 and item.get("status_message_id") and not (
                                item.get("approval") or item.get("mention_owner")
                            ):
                                try:
                                    existing = await channel.fetch_message(
                                        int(item["status_message_id"])
                                    )
                                except discord.NotFound:
                                    existing = None
                            if existing is None:
                                posted_parts = await send_chunked(
                                    channel,
                                    body,
                                    event_id=item["id"],
                                    bot_user_id=(
                                        coordinator.user.id if coordinator.user is not None else None
                                    ),
                                    first_kwargs={
                                        "view": view,
                                        "allowed_mentions": owner_mentions,
                                        "files": files,
                                    },
                                )
                                existing = posted_parts[0]
                            else:
                                await existing.edit(
                                    content=parts[0].content,
                                    view=view,
                                    allowed_mentions=owner_mentions,
                                    attachments=files,
                                )
                            await api.post(
                                f"/outbox/{item['id']}/ack",
                                json={
                                    "message_id": str(existing.id),
                                    "message_ids": (
                                        [str(part.id) for part in posted_parts]
                                        if posted_parts is not None
                                        else [str(existing.id)]
                                    ),
                                },
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
                            body_parts = await send_chunked(
                                thread,
                                item["body"],
                                event_id=item["id"] + "-body",
                                bot_user_id=(
                                    coordinator.user.id if coordinator.user is not None else None
                                ),
                            )
                            await api.post(
                                f"/outbox/{item['id']}/ack",
                                json={
                                    "message_id": str(message.id),
                                    "message_ids": [str(part.id) for part in body_parts],
                                    "thread_id": str(thread.id),
                                },
                            )
                            continue
                        if not item.get("thread_id"):
                            continue
                        channel = await bot.fetch_channel(int(item["thread_id"]))
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
                        body = body_prefix + item["body"]
                        if item.get("head_sha"):
                            body += "\nhead: " + item["head_sha"]
                        if item.get("next_action"):
                            body += "\n次: " + item["next_action"]
                        posted_parts = await send_chunked(
                            channel,
                            body,
                            event_id=item["id"],
                            bot_user_id=bot.user.id if bot.user is not None else None,
                            first_kwargs=kwargs,
                        )
                        await api.post(
                            f"/outbox/{item['id']}/ack",
                            json={
                                "message_id": str(posted_parts[0].id),
                                "message_ids": [str(part.id) for part in posted_parts],
                            },
                        )
                    except Exception:
                        log.warning("Notification failed: %s", item["id"], exc_info=True)
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
