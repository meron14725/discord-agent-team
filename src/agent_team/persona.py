"""Versioned persona presentation boundary.

Personas may alter presentation only.  Control flow always consumes the original
validated decision; this module deliberately has no tool or transport access.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
from dataclasses import dataclass, replace
from typing import Any, Awaitable, Callable, Mapping

from .contracts import CoordinationDecision, SpecialistDecision
from .redaction import SecretScanner

Decision = CoordinationDecision | SpecialistDecision
Formatter = Callable[["PersonaFormatRequest"], Awaitable[str]]
QUESTION_RE = re.compile(r"[?？]")
COMPLETED_RE = re.compile(
    r"(?:"
    # Match action predicates, not arbitrary text between a domain noun and
    # 'しています' (e.g. '実装とテストを担当しています' describes a role).
    r"(?:実行|変更|作業|送信|公開|マージ|対応|処理|デプロイ|配備|反映|修正|実装|設定|更新|作成|削除|復旧)"
    r"(?:は|が|を)?(?:すべて|全て|既に|すでに|無事に)?"
    r"(?:済み|完了|終了|成功|終わ(?:り|った)|終えました|しました|しています|できました)"
    r"|(?:完了|終了|成功|対応済み|処理済み|デプロイ済み|配備済み)(?:です|しました|しています)"
    r"|(?:^|[。！？\n])\s*(?:完了|終了|成功|対応済み|処理済み|デプロイ済み|配備済み)(?=[。！\n]|$)"
    r")"
)
FAILED_RE = re.compile(r"(?:失敗|失敗しました|failed)", re.IGNORECASE)
NOT_RUN_RE = re.compile(r"(?:未実行|実行していません|not[_ -]?run)", re.IGNORECASE)
APPROVAL_WAIT_RE = re.compile(r"(?:承認待ち|承認が必要|approval\s+required)", re.IGNORECASE)
APPROVAL_DONE_RE = re.compile(r"(?:承認済み|承認は不要|承認不要|approved)", re.IGNORECASE)
HANDOFF_RE = re.compile(r"(?:引き継ぎ|委任)(?:ます|ました|済み)")
TASK_REGISTERED_RE = re.compile(r"(?:案件|タスク|TASK).{0,12}(?:登録|作成)(?:済み|しました)")
CONTROL_PREFIX = "[fixed-facts]"
PERSONA_SECRET_SCANNER = SecretScanner(
    b"persona-boundary-v1",
    {"persona_token_assignment": r"(?i)(?<![\w-])token\s*[:=]\s*\S+"},
)


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
        def stable(values):
            return tuple(sorted((str(k), str(v)) for k, v in (values or {}).items()))
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
    invariant_checks: tuple[str, ...]


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
    }.get(envelope.execution_state, "実行状態を確認できません。")
    next_text = {
        "owner_answer": "次はオーナーの回答待ちです。",
        "owner_approval": "次はオーナーの承認待ちです。",
        "control_handoff": "次は制御層が担当へ引き継ぎます。",
        "control_delegation": "次は制御層が担当へ委任します。",
        "specialist_continuation": "次は同じ担当が継続します。",
        "coordinator_task_registration": "次は統括が正式案件を提案します。",
        "task_registration": "次は正式案件の登録です。",
        "complete": "次の自動操作はありません。",
    }.get(envelope.next_step, "次工程は制御層で確認してください。")
    details = []
    if envelope.handoff_roles:
        details.append("引き継ぎ先は固定事実ブロックに記録されています。")
    return state + "".join(details) + next_text + "\n" + fixed_fact_block(envelope)


def _fact_payload(envelope: FixedFactEnvelope) -> dict[str, Any]:
    """Return the complete authoritative payload used at the transport boundary."""
    payload = {
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
    # Decision fields are authoritative, but many are still user/model supplied
    # free text.  They must never cross the Discord boundary with a credential.
    # Keep the field and its shape for invariant checks while replacing only a
    # detected secret value with a stable marker.
    def safe(value: Any) -> Any:
        if isinstance(value, str):
            return PERSONA_SECRET_SCANNER.redact_text(value)
        if isinstance(value, list):
            return [safe(item) for item in value]
        if isinstance(value, dict):
            return {safe(str(key)): safe(item) for key, item in value.items()}
        return value

    return safe(payload)


def fixed_fact_block(envelope: FixedFactEnvelope) -> str:
    encoded = json.dumps(
        _fact_payload(envelope), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    # This JSON is machine-readable transport metadata. Escaping question
    # punctuation preserves the decoded value while preventing metadata from
    # adding user-facing questions to the Discord message.
    encoded = encoded.replace("?", r"\u003f").replace("？", r"\uff1f")
    return CONTROL_PREFIX + encoded


async def persona_formatter(request: PersonaFormatRequest) -> str:
    """Deterministic presentation formatter using the selected definition.

    Persona text is treated as data: it must identify the selected role and
    contain the required Voice section.  It cannot introduce facts or control
    instructions. Role-specific language is supplied by the model context;
    the presentation boundary preserves that source reply.
    """
    if f"role_id: {request.persona.role_id}" not in request.persona.content:
        raise ValueError("persona_role_mismatch")
    if "## Voice" not in request.persona.content:
        raise ValueError("persona_voice_missing")
    if request.persona.role_id not in {"coordinator", "cto", "backend_integrator", "security_sre"}:
        raise ValueError("unsupported_persona_role")
    # The model already receives the trusted persona. Do not add a canned
    # introduction, or consume its four-sentence allowance at delivery time.
    marker = re.search(r"^presentation_marker:\s*([^\n]{1,32})$", request.persona.content, re.MULTILINE)
    return ((marker.group(1) + " ") if marker else "") + request.safe_source_reply


def _validate_candidate(candidate: str, source_reply: str) -> None:
    """Prove the persona layer only adds a short, non-controlling role marker."""
    if not source_reply or candidate.count(source_reply) != 1 or not candidate.endswith(source_reply):
        raise ValueError("source_reply_changed")
    prefix = candidate[: -len(source_reply)]
    if len(prefix) > 64 or QUESTION_RE.search(prefix):
        raise ValueError("persona_prefix_invalid")
    if any(pattern.search(prefix) for pattern in (
        COMPLETED_RE, FAILED_RE, NOT_RUN_RE, APPROVAL_WAIT_RE,
        APPROVAL_DONE_RE, HANDOFF_RE, TASK_REGISTERED_RE,
    )):
        raise ValueError("persona_prefix_contains_control_claim")


def validate_final_reply(
    text: str, envelope: FixedFactEnvelope, *, max_characters: int
) -> tuple[str, ...]:
    checks = []
    if not text.strip():
        raise ValueError("empty")
    if len(text) > max_characters:
        raise ValueError("over_limit")
    if PERSONA_SECRET_SCANNER.scan_text(text).blocked:
        raise ValueError("secret")
    checks.append("secret_absent")
    lines = text.splitlines()
    control_lines = [line for line in lines if line.startswith(CONTROL_PREFIX)]
    if control_lines != [fixed_fact_block(envelope)]:
        raise ValueError("fixed_fact_block_mismatch")
    checks.append("control_block_exact")
    presentation = "\n".join(line for line in lines if not line.startswith(CONTROL_PREFIX))
    if len(QUESTION_RE.findall(text)) > 1:
        raise ValueError("multiple_questions")
    checks.append("question_limit")
    if envelope.execution_state == "not_run" and COMPLETED_RE.search(presentation):
        raise ValueError("not_run_contradiction")
    if envelope.execution_state == "succeeded" and (FAILED_RE.search(presentation) or NOT_RUN_RE.search(presentation)):
        raise ValueError("succeeded_contradiction")
    if envelope.execution_state == "failed" and (COMPLETED_RE.search(presentation) or NOT_RUN_RE.search(presentation)):
        raise ValueError("failed_contradiction")
    checks.append("execution_consistent")
    if envelope.approval_state == "waiting":
        if APPROVAL_DONE_RE.search(presentation):
            raise ValueError("approval_waiting_contradiction")
    elif APPROVAL_WAIT_RE.search(presentation):
        raise ValueError("approval_state_contradiction")
    checks.append("approval_consistent")
    if envelope.action not in {"handoff", "delegate"} and HANDOFF_RE.search(presentation):
        raise ValueError("handoff_contradiction")
    if envelope.action not in {"task", "recommend_task"} and TASK_REGISTERED_RE.search(presentation):
        raise ValueError("task_registration_contradiction")
    checks.append("action_consistent")
    next_step_conflicts = {
        "owner_answer": ("次の自動操作はありません", "承認待ち"),
        "owner_approval": ("次の自動操作はありません", "回答待ち"),
        "control_handoff": ("次の自動操作はありません",),
        "control_delegation": ("次の自動操作はありません",),
        "complete": ("回答待ち", "承認待ち", "引き継ぎます", "委任します"),
    }
    if any(value in presentation for value in next_step_conflicts.get(envelope.next_step, ())):
        raise ValueError("next_step_contradiction")
    checks.append("next_step_consistent")
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
    checks.append("sentence_policy")
    return tuple(checks)


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
    control = fixed_fact_block(envelope)
    reason = ""
    try:
        # The formatter gets a separately redacted copy of every textual
        # input. The authoritative envelope stays unchanged for its digest and
        # invariant checks; its transport representation is also redacted.
        redact = PERSONA_SECRET_SCANNER.redact_text
        safe_envelope = replace(
            envelope,
            role_id=redact(envelope.role_id),
            action=redact(envelope.action),
            execution_state=redact(envelope.execution_state),
            approval_state=redact(envelope.approval_state),
            approval_reason=redact(envelope.approval_reason),
            handoff_roles=tuple(redact(value) for value in envelope.handoff_roles),
            handoff_instructions=tuple(redact(value) for value in envelope.handoff_instructions),
            next_step=redact(envelope.next_step),
            identifiers=tuple((redact(key), redact(value)) for key, value in envelope.identifiers),
            targets=tuple((redact(key), redact(value)) for key, value in envelope.targets),
            quantities=tuple((redact(key), redact(value)) for key, value in envelope.quantities),
            task_summary=redact(envelope.task_summary),
            continuation_instruction=redact(envelope.continuation_instruction),
            change_plan=tuple((redact(key), redact(value)) for key, value in envelope.change_plan),
        )
        safe_reply = redact(decision.reply)
        safe_persona = replace(persona, content=redact(persona.content))
        request = PersonaFormatRequest(safe_persona, safe_envelope, safe_reply)
        # Reject synchronous callbacks before invoking them. This keeps a
        # blocking callback from stalling Discord fallback delivery.
        if not inspect.iscoroutinefunction(formatter):
            raise TypeError("formatter_contract_requires_async")
        candidate = await asyncio.wait_for(formatter(request), timeout_seconds)
        if not isinstance(candidate, str) or not candidate.strip():
            raise ValueError("empty_or_invalid_formatter_result")
        _validate_candidate(candidate, safe_reply)
        body = "\n\n".join(part for part in (str(candidate), *control_blocks, control) if part)
        checks = validate_final_reply(body, envelope, max_characters=max_characters)
    except asyncio.TimeoutError:
        reason = "timeout"
    except Exception as exc:
        reason = str(exc) or "internal_error"
    if reason:
        body = deterministic_fallback(envelope)
        # The configured bound limits model presentation. A complete factual
        # fallback is allowed to exceed one Discord message because the common
        # delivery boundary deterministically splits it into marked parts.
        try:
            checks = validate_final_reply(body, envelope, max_characters=max(max_characters, len(body)))
        except Exception as exc:
            # A fallback must not reopen formatting or control flow.  This last
            # response contains enumerated state only; the exact, redacted fact
            # block remains available to the transport/audit boundary.
            reason = reason + ";fallback_validation:" + (str(exc) or "internal_error")
            body = "返信を安全に整形できませんでした。次工程は制御層で確認してください。\n" + fixed_fact_block(envelope)
            checks = validate_final_reply(body, envelope, max_characters=max(max_characters, len(body)))
    return PersonaRenderResult(
        body,
        PersonaAudit(
            role_id,
            persona.version,
            bool(reason),
            reason,
            envelope.fact_digest,
            "passed",
            checks,
        ),
    )
