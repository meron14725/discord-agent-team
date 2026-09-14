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
from typing import Awaitable, Callable, Mapping

from .contracts import CoordinationDecision, SpecialistDecision

Decision = CoordinationDecision | SpecialistDecision
Formatter = Callable[["PersonaFormatRequest"], Awaitable[str] | str]
QUESTION_RE = re.compile(r"[?？]")
COMPLETED_RE = re.compile(r"(?:実行|変更|作業|送信|公開|マージ).{0,8}(?:済み|完了|成功|しました)")
SECRET_RE = re.compile(r"(?i)(?:authorization:\s*bearer|api[_-]?key\s*[:=]|token\s*[:=])\s*\S+")


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
    next_step: str
    fact_digest: str

    @classmethod
    def from_decision(cls, role_id: str, decision: Decision, *, execution_state: str = "not_run"):
        approval_state = "waiting" if getattr(decision, "action") == "request_approval" else "none"
        handoffs = getattr(decision, "handoffs", ()) or getattr(decision, "delegations", ())
        roles = tuple(item.role for item in handoffs)
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
        facts = {
            "role_id": role_id,
            "action": decision.action,
            "execution_state": execution_state,
            "approval_state": approval_state,
            "approval_reason": getattr(decision, "approval_reason", ""),
            "handoff_roles": roles,
            "next_step": next_steps[decision.action],
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
    }[envelope.next_step]
    return state + next_text


def validate_final_reply(text: str, envelope: FixedFactEnvelope, *, max_characters: int) -> None:
    if not text.strip():
        raise ValueError("empty")
    if len(text) > max_characters:
        raise ValueError("over_limit")
    if SECRET_RE.search(text):
        raise ValueError("secret")
    if len(QUESTION_RE.findall(text)) > 1:
        raise ValueError("multiple_questions")
    if envelope.execution_state == "not_run" and COMPLETED_RE.search(text):
        raise ValueError("not_run_contradiction")
    sentences = [part for part in re.split(r"(?<=[。！？!?])|\n+", text) if part.strip()]
    if len(sentences) > 4 and not any(key in text for key in ("安全", "検証", "承認", "理由:")):
        raise ValueError("too_many_sentences")
    # Deterministic control state must remain visible in every final rendering.
    expected = deterministic_fallback(envelope).split("。")[-2]
    if expected and expected not in text:
        raise ValueError("next_step_missing")


async def render_persona_reply(
    *,
    decision: Decision,
    role_id: str,
    persona: PersonaDefinition,
    formatter: Formatter,
    control_blocks: tuple[str, ...] = (),
    execution_state: str = "not_run",
    timeout_seconds: float = 2.0,
    max_characters: int = 1800,
) -> PersonaRenderResult:
    """Format only after validation, then validate the fully composed Discord body."""
    envelope = FixedFactEnvelope.from_decision(role_id, decision, execution_state=execution_state)
    control = deterministic_fallback(envelope).split("。")[-2] + "。"
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
        validate_final_reply(body, envelope, max_characters=max_characters)
    return PersonaRenderResult(
        body,
        PersonaAudit(role_id, persona.version, bool(reason), reason, envelope.fact_digest, "passed"),
    )
