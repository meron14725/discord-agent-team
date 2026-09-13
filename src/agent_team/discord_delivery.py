"""Deterministic Discord message rendering and delivery metadata."""

from dataclasses import dataclass

from .contracts import SpecialistDecision

DISCORD_CONTENT_LIMIT = 2000
DEFAULT_CONTENT_LIMIT = 1800


def split_discord_text(text: str, limit: int = DEFAULT_CONTENT_LIMIT) -> list[str]:
    """Split without dropping content, preferring paragraph and line boundaries."""
    if limit < 1:
        raise ValueError("Discord message limit must be positive")
    if not text:
        return [""]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n\n", 0, limit + 1)
        separator_length = 2
        if cut <= 0:
            cut = remaining.rfind("\n", 0, limit + 1)
            separator_length = 1
        if cut <= 0:
            cut = remaining.rfind(" ", 0, limit + 1)
            separator_length = 1
        if cut <= 0:
            cut = limit
            separator_length = 0
        else:
            cut += separator_length
        chunks.append(remaining[:cut])
        remaining = remaining[cut:]
    chunks.append(remaining)
    return chunks


def part_marker(event_id: str, part: int, total: int) -> str:
    return f"[event:{event_id} part:{part}/{total}]"


@dataclass(frozen=True)
class DiscordPart:
    content: str
    marker: str
    index: int
    total: int


def discord_parts(text: str, event_id: str, limit: int = DEFAULT_CONTENT_LIMIT) -> list[DiscordPart]:
    chunks = split_discord_text(text, limit)
    total = len(chunks)
    return [
        DiscordPart(
            content=chunk + "\n" + part_marker(event_id, index, total),
            marker=part_marker(event_id, index, total),
            index=index,
            total=total,
        )
        for index, chunk in enumerate(chunks, 1)
    ]


def specialist_next_step(
    decision: SpecialistDecision,
    *,
    owner_mention: str = "オーナー",
    continuation_turn: int = 0,
    continuation_limit: int = 2,
) -> str:
    if decision.action == "clarify":
        return f"次: {owner_mention}の回答待ち"
    if decision.action == "request_approval":
        return f"次: {owner_mention}の承認待ち"
    if decision.action == "handoff":
        return "次: 統括が指定された担当へ引き継ぎ"
    if decision.action == "continue":
        return f"次: この担当が自動継続（{continuation_turn + 1}/{continuation_limit}）"
    if decision.action == "recommend_task":
        return "次: 統括が正式案件へ登録"
    return "次: この応答で完了"


def task_next_step(state: str) -> str:
    owner_actions = {
        "Clarifying": "オーナーが同じスレッドで質問へ回答",
        "AwaitingSpecApproval": "オーナーが仕様を確認して承認または修正依頼",
        "AwaitingRequirementsConfirmation": "オーナーが要件を確認して承認または修正依頼",
        "AwaitingPlanApproval": "オーナーが実装計画を確認して承認または修正依頼",
        "AwaitingMergeApproval": "オーナーが対象SHAを確認してマージを承認",
        "Paused": "オーナーが再開を指示",
        "Blocked": "停止理由を解消した担当が同じ案件を再試行",
    }
    if state in owner_actions:
        return owner_actions[state]
    if state in {"Merged", "Cancelled"}:
        return "完了（自動継続なし）"
    return "現在の担当が自動継続"


def task_waits_for_owner(state: str) -> bool:
    return state in {
        "Clarifying",
        "AwaitingSpecApproval",
        "AwaitingRequirementsConfirmation",
        "AwaitingPlanApproval",
        "AwaitingMergeApproval",
    }
