import asyncio

from agent_team.contracts import SpecialistDecision
from agent_team.persona import PersonaDefinition, render_persona_reply


def test_composed_original_block_is_validated_before_delivery():
    decision = SpecialistDecision(action="reply", reply="未実行", task_summary="", approval_reason="", continuation_instruction="", sre_plan=None, handoffs=[])
    persona = PersonaDefinition("backend_integrator", "v1", "x")
    bad_blocks = ("変更は完了しました。質問一？質問二？ 五。六。七。八。九。",)
    result = asyncio.run(render_persona_reply(decision=decision, role_id="backend_integrator", persona=persona, formatter=lambda r: "候補です。", control_blocks=bad_blocks))
    assert result.audit.fallback
    assert "完了しました" not in result.text
    assert "？" not in result.text


def test_persona_never_changes_authoritative_decision():
    decision = SpecialistDecision(action="request_approval", reply="承認待ち", task_summary="", approval_reason="危険な変更", continuation_instruction="", sre_plan=None, handoffs=[])
    result = asyncio.run(render_persona_reply(decision=decision, role_id="security_sre", persona=PersonaDefinition("security_sre", "v1", "x"), formatter=lambda r: "実行済みです。"))
    assert decision.action == "request_approval"
    assert result.audit.fallback
    assert "承認待ち" in result.text
