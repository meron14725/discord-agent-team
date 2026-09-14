import asyncio
import time

from agent_team.contracts import SpecialistDecision
from agent_team.persona import PersonaDefinition, persona_formatter, render_persona_reply


def decision(action="reply"):
    return SpecialistDecision(action=action, reply="未実行です。", task_summary="", approval_reason="", continuation_instruction="", sre_plan=None, handoffs=[])


def render(formatter, **kwargs):
    return asyncio.run(render_persona_reply(decision=decision(), role_id="cto", persona=PersonaDefinition("cto", "v1", "x"), formatter=formatter, **kwargs))


def async_formatter(callback):
    async def wrapped(request):
        return callback(request)

    return wrapped


def test_valid_format_records_role_version_and_fact_digest():
    result = render(async_formatter(lambda request: "要件面です。" + request.safe_source_reply))
    assert not result.audit.fallback
    assert (result.audit.role_id, result.audit.version) == ("cto", "v1")
    assert len(result.audit.fact_digest) == 64
    assert set(result.audit.invariant_checks) == {
        "secret_absent",
        "control_block_exact",
        "question_limit",
        "execution_consistent",
        "approval_consistent",
        "action_consistent",
        "next_step_consistent",
        "sentence_policy",
    }


def test_empty_invalid_over_limit_and_internal_error_fallback():
    for formatter, kwargs in ((lambda r: "", {}), (lambda r: "実行が完了しました。", {}), (lambda r: "x" * 100, {"max_characters": 50}), (lambda r: 1 / 0, {})):
        assert render(async_formatter(formatter), **kwargs).audit.fallback


def test_formatter_cannot_rewrite_or_surround_the_validated_source_reply():
    for formatter in (
        lambda request: "別の回答です。",
        lambda request: request.safe_source_reply.replace("未実行", "実行済み"),
        lambda request: request.safe_source_reply + " 承認済みです。",
    ):
        result = render(async_formatter(formatter))
        assert result.audit.fallback
        assert result.text.startswith("操作は実行していません。")


def test_timeout_falls_back_without_rerunning_decision():
    calls = []
    async def slow(request):
        calls.append(request.envelope.fact_digest)
        await asyncio.sleep(.02)
        return "遅延"
    result = render(slow, timeout_seconds=.001)
    assert result.audit.fallback and result.audit.fallback_reason == "timeout"
    assert len(calls) == 1


def test_synchronous_blocking_formatter_is_rejected_without_invocation():
    calls = []

    def blocking(request):
        calls.append(request)
        time.sleep(1)
        return request.safe_source_reply

    started = time.monotonic()
    result = render(blocking, timeout_seconds=.001)

    assert time.monotonic() - started < .2
    assert result.audit.fallback
    assert result.audit.fallback_reason == "formatter_contract_requires_async"
    assert calls == []


def test_real_formatter_uses_selected_persona_and_differs_by_role():
    outputs = set()
    for role in ("coordinator", "cto", "backend_integrator", "security_sre"):
        persona = PersonaDefinition(role, "v1", f"role_id: {role}\n## Voice\n簡潔")
        result = asyncio.run(render_persona_reply(
            decision=decision(), role_id=role, persona=persona, formatter=persona_formatter
        ))
        assert not result.audit.fallback
        outputs.add(result.text.split("未実行です。", 1)[0])
    assert len(outputs) == 4


def test_fallback_is_delivered_with_the_smallest_configured_limit():
    result = render(async_formatter(lambda request: ""), max_characters=100)
    assert result.text
    assert result.audit.fallback
    assert result.audit.final_validation == "passed"


def test_large_complete_fallback_is_preserved_for_chunked_delivery():
    large = "x" * 2500
    continuing = SpecialistDecision(
        action="continue",
        reply="未実行です。",
        task_summary="",
        approval_reason="",
        continuation_instruction="検証を継続する",
        sre_plan=None,
        handoffs=[],
    )
    result = asyncio.run(render_persona_reply(
        decision=continuing,
        role_id="cto",
        persona=PersonaDefinition("cto", "v1", "x"),
        formatter=async_formatter(lambda request: ""),
        identifiers={"evidence": large},
        max_characters=100,
    ))
    assert result.audit.fallback
    assert len(result.text) > 2000
    assert large in result.text


def test_secret_in_authoritative_free_text_is_redacted_and_fallback_is_delivered():
    waiting = SpecialistDecision(
        action="request_approval",
        reply="承認待ちです。",
        task_summary="",
        approval_reason="token: secret-value",
        continuation_instruction="",
        sre_plan=None,
        handoffs=[],
    )
    result = asyncio.run(render_persona_reply(
        decision=waiting,
        role_id="security_sre",
        persona=PersonaDefinition("security_sre", "v1", "x"),
        formatter=async_formatter(lambda request: 1 / 0),
    ))
    assert result.audit.fallback
    assert result.audit.final_validation == "passed"
    assert "secret-value" not in result.text
    assert '"approval_reason":"[redacted]"' in result.text
    assert "承認待ち" in result.text


def test_all_formatter_inputs_use_the_shared_secret_redaction_boundary():
    captured = []

    async def capture(request):
        captured.append(request)
        return "確認します。" + request.safe_source_reply

    secrets = {
        "reply": "password" + "=" + "abcdefghijk",
        "approval": "Authorization: Basic " + "QWxhZGRpbjpvcGVuIHNlc2FtZQ==",
        "identifier": "ghp_" + "D" * 36,
        "persona": "sk-proj-" + "F" * 32,
    }
    waiting = SpecialistDecision(
        action="request_approval",
        reply="確認します。" + secrets["reply"],
        task_summary="",
        approval_reason=secrets["approval"],
        continuation_instruction="",
        sre_plan=None,
        handoffs=[],
    )
    persona = PersonaDefinition(
        "security_sre", "v1", "role_id: security_sre\n## Voice\n" + secrets["persona"]
    )
    result = asyncio.run(render_persona_reply(
        decision=waiting,
        role_id="security_sre",
        persona=persona,
        formatter=capture,
        identifiers={"credential": secrets["identifier"]},
    ))

    assert len(captured) == 1
    boundary = repr(captured[0])
    for secret in secrets.values():
        assert secret not in boundary
        assert secret not in result.text
    assert "[redacted]" in boundary
