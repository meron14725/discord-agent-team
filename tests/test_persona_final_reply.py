import asyncio
import json

import pytest

from agent_team.contracts import SpecialistDecision
from agent_team.persona import (
    FixedFactEnvelope,
    PersonaDefinition,
    fixed_fact_block,
    render_persona_reply,
    validate_final_reply,
)


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


def test_all_authoritative_specialist_fields_survive_fallback_and_digest():
    decision = SpecialistDecision(
        action="continue", reply="続けます", task_summary="", approval_reason="",
        continuation_instruction="検証を実行", sre_plan=None, handoffs=[]
    )
    result = asyncio.run(render_persona_reply(
        decision=decision, role_id="backend_integrator",
        persona=PersonaDefinition("backend_integrator", "v1", "bad"),
        formatter=lambda r: "", identifiers={"event_id": "evt-1"},
        targets={"repository": "sample"}, quantities={"attempt": 1},
    ))
    assert result.audit.fallback
    assert "検証を実行" in result.text
    assert "evt-1" in result.text and "sample" in result.text
    assert len(result.audit.fact_digest) == 64


def test_handoff_instruction_and_actual_next_step_are_fixed_facts():
    decision = SpecialistDecision(
        action="handoff", reply="引き継ぎます", task_summary="", approval_reason="",
        continuation_instruction="", sre_plan=None,
        handoffs=[{"role": "security_sre", "reason": "安全確認", "instruction": "監査ログを確認"}],
    )
    result = asyncio.run(render_persona_reply(
        decision=decision, role_id="backend_integrator",
        persona=PersonaDefinition("backend_integrator", "v1", "bad"),
        formatter=lambda request: "", next_step="次: 統括が安全担当へ引き継ぎ",
    ))
    assert result.audit.fallback
    assert "security_sre" in result.text
    assert "監査ログを確認" in result.text
    assert "次: 統括が安全担当へ引き継ぎ" in result.text


def test_control_block_is_a_complete_exact_envelope_not_substring_evidence():
    decision = SpecialistDecision(action="request_approval", reply="待機", task_summary="", approval_reason="実行しますか？", continuation_instruction="", sre_plan=None, handoffs=[])
    result = asyncio.run(render_persona_reply(
        decision=decision, role_id="security_sre",
        persona=PersonaDefinition("security_sre", "v1", "bad"), formatter=lambda request: "",
        identifiers={"event_id": "evt-9"}, targets={"channel": "42"}, quantities={"count": 2},
    ))
    assert result.audit.fallback and "実行しますか？" not in result.text
    control = next(line for line in result.text.splitlines() if line.startswith("[fixed-facts]"))
    assert json.loads(control.removeprefix("[fixed-facts]"))["approval_reason"] == "実行しますか？"
    tampered = result.text.replace('"approval_state":"waiting"', '"approval_state":"none"')
    with pytest.raises(ValueError, match="fixed_fact_block_mismatch"):
        validate_final_reply(tampered, FixedFactEnvelope.from_decision(
            "security_sre", decision, identifiers={"event_id": "evt-9"},
            targets={"channel": "42"}, quantities={"count": 2}), max_characters=2000)


def test_fixed_fact_questions_cannot_exceed_the_discord_question_limit():
    decision = SpecialistDecision(
        action="request_approval",
        reply="どちらにしますか？",
        task_summary="",
        approval_reason="案Aですか？案Bですか？",
        continuation_instruction="",
        sre_plan=None,
        handoffs=[],
    )
    result = asyncio.run(render_persona_reply(
        decision=decision,
        role_id="security_sre",
        persona=PersonaDefinition("security_sre", "v1", "bad"),
        formatter=lambda request: "",
    ))

    assert result.text.count("?") + result.text.count("？") <= 1
    control = next(line for line in result.text.splitlines() if line.startswith("[fixed-facts]"))
    assert json.loads(control.removeprefix("[fixed-facts]"))["approval_reason"] == "案Aですか？案Bですか？"


@pytest.mark.parametrize(
    "decision,text,error",
    [
        (
            SpecialistDecision(action="request_approval", reply="待機", task_summary="", approval_reason="危険", continuation_instruction="", sre_plan=None, handoffs=[]),
            "承認済みなので進めます。",
            "approval_waiting_contradiction",
        ),
        (
            SpecialistDecision(action="reply", reply="回答", task_summary="", approval_reason="", continuation_instruction="", sre_plan=None, handoffs=[]),
            "次は担当へ引き継ぎます。",
            "handoff_contradiction",
        ),
        (
            SpecialistDecision(action="reply", reply="回答", task_summary="", approval_reason="", continuation_instruction="", sre_plan=None, handoffs=[]),
            "TASKを登録しました。",
            "task_registration_contradiction",
        ),
    ],
)
def test_presentation_semantic_control_claims_cannot_conflict(decision, text, error):
    envelope = FixedFactEnvelope.from_decision("security_sre", decision)
    with pytest.raises(ValueError, match=error):
        validate_final_reply(
            text + "\n" + fixed_fact_block(envelope),
            envelope,
            max_characters=2000,
        )
