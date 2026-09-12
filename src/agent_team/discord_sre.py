import time

from sqlalchemy import select

from .contracts import DiscordSREPlan, DiscordTargetSnapshot
from .db import DiscordChange, uid
from .policy import GuardError, discord_sre_change_digest, validate_discord_sre_plan


class DiscordChangeService:
    def __init__(self, db, settings):
        self.db = db
        self.settings = settings

    def authorize(self, actor: str, guild: str, channel: str):
        if actor not in self.settings.owner_ids or guild != self.settings.guild_id:
            raise GuardError("Unauthorized Discord SRE actor or guild")
        allowed_channels = {self.settings.channel_id}
        if self.settings.discord_sre.audit_channel_id:
            allowed_channels.add(self.settings.discord_sre.audit_channel_id)
        if channel not in allowed_channels:
            raise GuardError("Unauthorized Discord SRE channel")

    @staticmethod
    def serialize(change: DiscordChange):
        return {
            "id": change.id,
            "status": change.status,
            "digest": change.digest,
            "plan": change.plan,
            "before": change.before,
            "expires": change.expires,
            "data": change.data,
        }

    def propose(self, *, event_id, actor, guild, channel, plan, before):
        # Treat the service boundary as untrusted. FastAPI's model_dump() and
        # other callers may provide nested dictionaries even after request
        # validation, while the policy layer requires typed contracts.
        plan = DiscordSREPlan.model_validate(plan)
        before = DiscordTargetSnapshot.model_validate(before) if before is not None else None
        self.authorize(actor, guild, channel)
        digest = validate_discord_sre_plan(plan, before, self.settings)
        with self.db.transaction() as session:
            existing = session.scalar(
                select(DiscordChange).where(DiscordChange.event_id == event_id)
            )
            if existing:
                if existing.digest != digest:
                    raise GuardError("Discord SRE event was reused with a different plan")
                return self.serialize(existing)
            change = DiscordChange(
                id="SRE-" + uid()[:12],
                event_id=event_id,
                actor=actor,
                guild_id=guild,
                channel_id=channel,
                digest=digest,
                plan=plan.model_dump(mode="json"),
                before=before.model_dump(mode="json") if before else None,
                expires=time.time() + self.settings.discord_sre.approval_seconds,
                data={},
            )
            session.add(change)
            session.flush()
            return self.serialize(change)

    def approve(self, change_id, *, event_id, actor, guild, channel, digest):
        self.authorize(actor, guild, channel)
        with self.db.transaction() as session:
            change = session.scalar(
                select(DiscordChange).where(DiscordChange.id == change_id).with_for_update()
            )
            if change is None:
                raise ValueError("Unknown Discord SRE change")
            if change.status == "approved" and change.data.get("approval_event_id") == event_id:
                return self.serialize(change)
            if change.status != "pending" or change.expires <= time.time() or digest != change.digest:
                raise GuardError("Stale Discord SRE approval")
            change.status = "approved"
            change.data = {
                **change.data,
                "approved_by": actor,
                "approved_at": time.time(),
                "approval_event_id": event_id,
            }
            return self.serialize(change)

    def authorize_execution(self, change_id, current_before):
        stale_reason = ""
        with self.db.transaction() as session:
            change = session.scalar(
                select(DiscordChange).where(DiscordChange.id == change_id).with_for_update()
            )
            if change is None:
                raise ValueError("Unknown Discord SRE change")
            if change.status != "approved" or change.expires <= time.time():
                raise GuardError("Discord SRE change is not approved")
            plan = DiscordSREPlan.model_validate(change.plan)
            original_before = (
                DiscordTargetSnapshot.model_validate(change.before) if change.before else None
            )
            if current_before != original_before:
                change.status = "stale"
                stale_reason = "Discord target changed after proposal"
            elif discord_sre_change_digest(plan, current_before) != change.digest:
                change.status = "stale"
                stale_reason = "Discord SRE approval digest mismatch"
            else:
                validate_discord_sre_plan(plan, current_before, self.settings)
                change.status = "executing"
                change.data = {**change.data, "execution_started_at": time.time()}
            result = self.serialize(change)
        if stale_reason:
            raise GuardError(stale_reason)
        return result

    def complete(self, change_id, *, success, result, error=""):
        with self.db.transaction() as session:
            change = session.scalar(
                select(DiscordChange).where(DiscordChange.id == change_id).with_for_update()
            )
            if change is None:
                raise ValueError("Unknown Discord SRE change")
            if change.status != "executing":
                raise GuardError("Discord SRE change is not executing")
            change.status = "completed" if success else "failed"
            change.data = {
                **change.data,
                "finished_at": time.time(),
                "result": result,
                "error": error[:1000],
            }
            return self.serialize(change)
