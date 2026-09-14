import asyncio

from agent_team.contracts import SpecialistDecision
from agent_team.persona import PersonaDefinition, render_persona_reply


def decision(action="reply"):
    return SpecialistDecision(action=action, reply="未実行です。", task_summary="", approval_reason="", continuation_instruction="", sre_plan=None, handoffs=[])


def render(formatter, **kwargs):
    return asyncio.run(render_persona_reply(decision=decision(), role_id="cto", persona=PersonaDefinition("cto", "v1", "x"), formatter=formatter, **kwargs))


def test_valid_format_records_role_version_and_fact_digest():
    result = render(lambda request: "操作は実行していません。")
    assert not result.audit.fallback
    assert (result.audit.role_id, result.audit.version) == ("cto", "v1")
    assert len(result.audit.fact_digest) == 64


def test_empty_invalid_over_limit_and_internal_error_fallback():
    for formatter, kwargs in ((lambda r: "", {}), (lambda r: "実行が完了しました。", {}), (lambda r: "x" * 100, {"max_characters": 50}), (lambda r: 1 / 0, {})):
        assert render(formatter, **kwargs).audit.fallback


def test_timeout_falls_back_without_rerunning_decision():
    calls = []
    async def slow(request):
        calls.append(request.envelope.fact_digest)
        await asyncio.sleep(.02)
        return "遅延"
    result = render(slow, timeout_seconds=.001)
    assert result.audit.fallback and result.audit.fallback_reason == "timeout"
    assert len(calls) == 1
