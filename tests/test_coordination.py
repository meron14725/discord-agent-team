import pytest
from pydantic import ValidationError

from agent_team.contracts import SpecialistDecision
from agent_team.coordination import (
    resolve_specialist_handoffs,
    validate_specialist_handoffs,
)
from agent_team.policy import GuardError


def handoff(target, *, reason="追加調査が必要", instruction="対象を調査して結果を返す"):
    return SpecialistDecision(
        action="handoff",
        reply=f"{target}へ確認を引き継ぎます。",
        task_summary="",
        approval_reason="",
        sre_plan=None,
        handoffs=[{"role": target, "reason": reason, "instruction": instruction}],
    )


def test_handoff_contract_requires_handoff_action_and_unique_targets():
    values = {
        "action": "reply",
        "reply": "確認します。",
        "task_summary": "",
        "approval_reason": "",
        "sre_plan": None,
        "handoffs": [
            {"role": "downstream", "reason": "実装確認", "instruction": "コードを確認する"}
        ],
    }
    with pytest.raises(ValidationError, match="Only handoff decisions"):
        SpecialistDecision(**values)
    values["action"] = "handoff"
    values["handoffs"].append(values["handoffs"][0])
    with pytest.raises(ValidationError, match="at most one"):
        SpecialistDecision(**values)


def test_handoff_contract_discards_unused_task_and_approval_text():
    decision = handoff("downstream").model_copy(
        update={"task_summary": "unused", "approval_reason": "unused"}
    )
    normalized = SpecialistDecision.model_validate(decision.model_dump())
    assert normalized.task_summary == ""
    assert normalized.approval_reason == ""


def test_control_layer_rejects_self_duplicate_and_second_level_handoffs():
    with pytest.raises(GuardError, match="itself"):
        validate_specialist_handoffs(
            handoff("upstream"),
            source_role="upstream",
            handoff_depth=0,
            visited_roles=["upstream"],
        )
    with pytest.raises(GuardError, match="duplicate"):
        validate_specialist_handoffs(
            handoff("downstream"),
            source_role="upstream",
            handoff_depth=0,
            visited_roles=["upstream", "downstream"],
        )
    with pytest.raises(GuardError, match="depth"):
        validate_specialist_handoffs(
            handoff("downstream"),
            source_role="upstream",
            handoff_depth=1,
            visited_roles=["upstream"],
        )


def test_control_layer_combines_competing_requests_into_one_specialist_run():
    upstream = handoff(
        "sre",
        reason="実行基盤の状態が必要",
        instruction="workerの稼働状態を確認する",
    )
    downstream = handoff(
        "sre",
        reason="障害原因の確認が必要",
        instruction="直近のエラーを調査する",
    )

    dispatches = resolve_specialist_handoffs(
        ["upstream", "downstream"],
        [("upstream", upstream), ("downstream", downstream)],
    )

    assert len(dispatches) == 1
    assert dispatches[0].role == "sre"
    assert dispatches[0].source_roles == ("upstream", "downstream")
    assert "workerの稼働状態" in dispatches[0].instruction
    assert "直近のエラー" in dispatches[0].instruction
    assert "実行基盤の状態" in dispatches[0].context
    assert "障害原因" in dispatches[0].context


def test_control_layer_does_not_schedule_a_role_already_selected_by_coordinator():
    dispatches = resolve_specialist_handoffs(
        ["upstream", "downstream"],
        [("upstream", handoff("downstream"))],
    )
    assert dispatches == []
