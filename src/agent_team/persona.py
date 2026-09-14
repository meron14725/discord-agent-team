"""Versioned persona presentation boundary.

Personas may alter presentation only.  Control flow always consumes the original
validated decision; this module deliberately has no tool or transport access.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping

from .contracts import CoordinationDecision, SpecialistDecision

Decision = CoordinationDecision | SpecialistDecision
Formatter = Callable[["PersonaFormatRequest"], Awaitable[str] | str]
QUESTION_RE = re.compile(r"[?？]")
COMPLETED_RE = re.compile(r"(?:実行|変更|作業|送信|公開|マージ).{0,8}(?:済み|完了|成功|しました)")
FAILED_RE = re.compile(r"(?:失敗|失敗しました|failed)", re.IGNORECASE)
NOT_RUN_RE = re.compile(r"(?:未実行|実行していません|not[_ -]?run)", re.IGNORECASE)
SECRET_RE = re.compile(r"(?i)(?:authorization:\s*bearer|api[_-]?key\s*[:=]|token\s*[:=])\s*\S+")
CONTROL_PREFIX = "[fixed-facts]"
DISCORD_CONTENT_LIMIT = 2000


@dataclass(frozen=True)
class PersonaDefinition:
    role_id: str
    version: str
    content: str


@dataclass(frozen=True)
class FixedFactEnvelope:
    role_id: str
    action: str
    execution_state: str
    approval_state: str
    approval_reason: str
    handoff_roles: tuple[str, ...]
    handoff_instructions: tuple[str, ...]
    next_step: str
    identifiers: tuple[tuple[str, str], ...]
    targets: tuple[tuple[str, str], ...]
    quantities: tuple[tuple[str, str], ...]
    task_summary: str
    continuation_instruction: str
    change_plan: tuple[tuple[str, str], ...]
    fact_digest: str

    @classmethod
    def from_decision(
        cls, role_id: str, decision: Decision, *, execution_state: str = "not_run",
        identifiers: Mapping[str, Any] | None = None,
        targets: Mapping[str, Any] | None = None,
        quantities: Mapping[str, Any] | None = None,
        next_step: str | None = None,
    ):
        approval_state = "waiting" if getattr(decision, "action") == "request_approval" else "none"
        handoffs = getattr(decision, "handoffs", ()) or getattr(decision, "delegations", ())
        roles = tuple(item.role for item in handoffs)
        instructions = tuple(
            getattr(item, "instruction", "") for item in handoffs
            if getattr(item, "instruction", "")
        )
        next_steps = {
            "clarify": "owner_answer",
            "request_approval": "owner_approval",
            "handoff": "control_handoff",
            "delegate": "control_delegation",
            "continue": "specialist_continuation",
            "recommend_task": "coordinator_task_registration",
            "task": "task_registration",
            "reply": "complete",
        }
        plan = getattr(decision, "sre_plan", None)
        plan_facts = plan.model_dump(mode="json") if plan is not None else {}
        # Only explicit, already validated values enter the envelope.  No fact is
        # inferred from free-form reply text.
        stable = lambda values: tuple(sorted((str(k), str(v)) for k, v in (values or {}).items()))
        facts = {
            "role_id": role_id,
            "action": decision.action,
            "execution_state": execution_state,
            "approval_state": approval_state,
            "approval_reason": getattr(decision, "approval_reason", ""),
            "handoff_roles": roles,
            "handoff_instructions": instructions,
            "next_step": next_step or next_steps[decision.action],
            "identifiers": stable(identifiers),
            "targets": stable(targets),
            "quantities": stable(quantities),
            "task_summary": getattr(decision, "task_summary", ""),
            "continuation_instruction": getattr(decision, "continuation_instruction", ""),
            "change_plan": stable(plan_facts),
        }
        digest = hashlib.sha256(
            json.dumps(facts, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
        return cls(**facts, fact_digest=digest)


@dataclass(frozen=True)
class PersonaFormatRequest:
    persona: PersonaDefinition
    envelope: FixedFactEnvelope
    safe_source_reply: str


@dataclass(frozen=True)
class PersonaAudit:
    role_id: str
    version: str
    fallback: bool
    fallback_reason: str
    fact_digest: str
    final_validation: str


@dataclass(frozen=True)
class PersonaRenderResult:
    text: str
    audit: PersonaAudit


class PersonaRegistry:
    """Immutable, exact role/version mapping; there is intentionally no fallback role."""

    def __init__(self, definitions: Mapping[tuple[str, str], PersonaDefinition]):
        self._definitions = dict(definitions)

    def get(self, role_id: str, version: str) -> PersonaDefinition:
        try:
            return self._definitions[(role_id, version)]
        except KeyError as exc:
            raise ValueError(f"No persona for role/version: {role_id}/{version}") from exc

    def select(self, active_versions: Mapping[str, str]) -> Mapping[str, PersonaDefinition]:
        return {role: self.get(role, version) for role, version in active_versions.items()}


def deterministic_fallback(envelope: FixedFactEnvelope) -> str:
    state = {
        "not_run": "操作は実行していません。",
        "succeeded": "操作は成功しました。",
        "failed": "操作は失敗しました。",
    }.get(envelope.execution_state, f"実行状態: {envelope.execution_state}。")
    next_text = {
        "owner_answer": "次はオーナーの回答待ちです。",
        "owner_approval": "次はオーナーの承認待ちです。",
        "control_handoff": "次は制御層が担当へ引き継ぎます。",
        "control_delegation": "次は制御層が担当へ委任します。",
        "specialist_continuation": "次は同じ担当が継続します。",
        "coordinator_task_registration": "次は統括が正式案件を提案します。",
        "task_registration": "次は正式案件の登録です。",
        "complete": "次の自動操作はありません。",
    }.get(envelope.next_step, "次工程: " + envelope.next_step + "。")
    details = []
    if envelope.approval_reason:
        details.append("承認理由: " + envelope.approval_reason + "。")
    if envelope.handoff_roles:
        details.append("引き継ぎ先: " + ", ".join(envelope.handoff_roles) + "。")
    if envelope.handoff_instructions:
        details.append("引き継ぎ内容: " + " / ".join(envelope.handoff_instructions) + "。")
    if envelope.task_summary:
        details.append("案件要約: " + envelope.task_summary + "。")
    if envelope.continuation_instruction:
        details.append("継続内容: " + envelope.continuation_instruction + "。")
    if envelope.change_plan:
        details.append("変更計画: " + json.dumps(dict(envelope.change_plan), ensure_ascii=False, sort_keys=True) + "。")
    for label, values in (("識別子", envelope.identifiers), ("対象", envelope.targets), ("数量", envelope.quantities)):
        if values:
            details.append(label + ": " + json.dumps(dict(values), ensure_ascii=False, sort_keys=True) + "。")
    return state + "".join(details) + next_text + "\n" + fixed_fact_block(envelope)


def _fact_payload(envelope: FixedFactEnvelope) -> dict[str, Any]:
    """Return the complete authoritative payload used at the transport boundary."""
    return {
        "action": envelope.action,
        "approval_reason": envelope.approval_reason,
        "approval_state": envelope.approval_state,
        "change_plan": dict(envelope.change_plan),
        "continuation_instruction": envelope.continuation_instruction,
        "execution_state": envelope.execution_state,
        "handoff_instructions": list(envelope.handoff_instructions),
        "handoff_roles": list(envelope.handoff_roles),
        "identifiers": dict(envelope.identifiers),
        "next_step": envelope.next_step,
        "quantities": dict(envelope.quantities),
        "role_id": envelope.role_id,
        "targets": dict(envelope.targets),
        "task_summary": envelope.task_summary,
    }


def fixed_fact_block(envelope: FixedFactEnvelope) -> str:
    return CONTROL_PREFIX + json.dumps(
        _fact_payload(envelope), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def persona_formatter(request: PersonaFormatRequest) -> str:
    """Deterministic presentation formatter using the selected definition.

    Persona text is treated as data: it must identify the selected role and
    contain the required Voice section.  It cannot introduce facts or control
    instructions.  The role-specific lead makes all four presentations
    distinguishable while the source reply remains intact.
    """
    if f"role_id: {request.persona.role_id}" not in request.persona.content:
        raise ValueError("persona_role_mismatch")
    if "## Voice" not in request.persona.content:
        raise ValueError("persona_voice_missing")
    leads = {
        "coordinator": "結論と次の担当を整理します。",
        "cto": "要件と技術判断を分けて示します。",
        "backend_integrator": "実装結果と検証点を示します。",
        "security_sre": "安全条件と承認状態を先に示します。",
    }
    try:
        marker = re.search(r"^presentation_marker:\s*([^\n]{1,32})$", request.persona.content, re.MULTILINE)
        style = (marker.group(1) + " ") if marker else leads[request.persona.role_id]
        return style + request.safe_source_reply
    except KeyError as exc:
        raise ValueError("unsupported_persona_role") from exc


def validate_final_reply(text: str, envelope: FixedFactEnvelope, *, max_characters: int) -> None:
    if not text.strip():
        raise ValueError("empty")
    if len(text) > max_characters:
        raise ValueError("over_limit")
    if SECRET_RE.search(text):
        raise ValueError("secret")
    lines = text.splitlines()
    control_lines = [line for line in lines if line.startswith(CONTROL_PREFIX)]
    if control_lines != [fixed_fact_block(envelope)]:
        raise ValueError("fixed_fact_block_mismatch")
    presentation = "\n".join(line for line in lines if not line.startswith(CONTROL_PREFIX))
    if len(QUESTION_RE.findall(presentation)) > 1:
        raise ValueError("multiple_questions")
    if envelope.execution_state == "not_run" and COMPLETED_RE.search(presentation):
        raise ValueError("not_run_contradiction")
    if envelope.execution_state == "succeeded" and (FAILED_RE.search(presentation) or NOT_RUN_RE.search(presentation)):
        raise ValueError("succeeded_contradiction")
    if envelope.execution_state == "failed" and (COMPLETED_RE.search(presentation) or NOT_RUN_RE.search(presentation)):
        raise ValueError("failed_contradiction")
    sentences = [part for part in re.split(r"(?<=[。！？!?])|\n+", presentation) if part.strip()]
    fallback_presentation = deterministic_fallback(envelope).split("\n" + CONTROL_PREFIX, 1)[0]
    if len(sentences) > 4 and presentation != fallback_presentation:
        safety_detail = "[detail:safety]" in presentation and any(
            value in presentation for value in (envelope.approval_reason, "承認", "安全") if value
        )
        verification_detail = "[detail:verification]" in presentation and any(
            value in presentation for value in ("検証", envelope.execution_state) if value
        )
        if not safety_detail and not verification_detail:
            raise ValueError("too_many_sentences")


async def render_persona_reply(
    *,
    decision: Decision,
    role_id: str,
    persona: PersonaDefinition,
    formatter: Formatter,
    control_blocks: tuple[str, ...] = (),
    execution_state: str = "not_run",
    identifiers: Mapping[str, Any] | None = None,
    targets: Mapping[str, Any] | None = None,
    quantities: Mapping[str, Any] | None = None,
    next_step: str | None = None,
    timeout_seconds: float = 2.0,
    max_characters: int = 1800,
) -> PersonaRenderResult:
    """Format only after validation, then validate the fully composed Discord body."""
    envelope = FixedFactEnvelope.from_decision(
        role_id, decision, execution_state=execution_state,
        identifiers=identifiers, targets=targets, quantities=quantities,
        next_step=next_step,
    )
    control = deterministic_fallback(envelope)
    reason = ""
    try:
        request = PersonaFormatRequest(persona, envelope, decision.reply)
        candidate = formatter(request)
        if hasattr(candidate, "__await__"):
            candidate = await asyncio.wait_for(candidate, timeout_seconds)
        if not isinstance(candidate, str) or not candidate.strip():
            raise ValueError("empty_or_invalid_formatter_result")
        body = "\n\n".join(part for part in (str(candidate), *control_blocks, control) if part)
        validate_final_reply(body, envelope, max_characters=max_characters)
    except asyncio.TimeoutError:
        reason = "timeout"
    except Exception as exc:
        reason = str(exc) or "internal_error"
    if reason:
        body = deterministic_fallback(envelope)
        # The configured bound limits model presentation.  A complete factual
        # fallback may exceed it, but can never exceed Discord's hard limit.
        validate_final_reply(
            body, envelope, max_characters=max(max_characters, len(body))
            if len(body) <= DISCORD_CONTENT_LIMIT else DISCORD_CONTENT_LIMIT,
        )
    return PersonaRenderResult(
        body,
        PersonaAudit(role_id, persona.version, bool(reason), reason, envelope.fact_digest, "passed"),
    )
