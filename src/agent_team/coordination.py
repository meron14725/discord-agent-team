from dataclasses import dataclass

from .contracts import SpecialistDecision, SpecialistRole
from .policy import GuardError


@dataclass(frozen=True)
class HandoffDispatch:
    role: SpecialistRole
    source_roles: tuple[SpecialistRole, ...]
    instruction: str
    context: str


def validate_specialist_handoffs(
    decision: SpecialistDecision,
    *,
    source_role: SpecialistRole,
    handoff_depth: int,
    handoff_round: int,
    handoff_source_roles: list[SpecialistRole],
    visited_roles: list[SpecialistRole],
) -> SpecialistDecision:
    visited = set(visited_roles)
    visited.add(source_role)
    if len(visited_roles) != len(set(visited_roles)):
        raise GuardError("Visited specialist roles must be unique")
    if len(handoff_source_roles) != len(set(handoff_source_roles)):
        raise GuardError("Handoff source roles must be unique")
    if handoff_depth == 0 and (handoff_round or handoff_source_roles):
        raise GuardError("Initial specialist turn cannot have handoff dialogue state")
    if handoff_depth == 1 and not handoff_source_roles:
        raise GuardError("A handoff recipient requires at least one source role")
    if handoff_depth == 2 and handoff_source_roles:
        raise GuardError("A handoff answer turn cannot open another dialogue")
    if handoff_depth == 2 and decision.handoffs:
        raise GuardError("A handoff answer cannot delegate")
    if handoff_depth == 1 and handoff_round >= 2 and decision.handoffs:
        raise GuardError("Specialist handoff round-trip limit exceeded")
    for handoff in decision.handoffs:
        if handoff.role == source_role:
            raise GuardError("A specialist cannot hand off to itself")
        if handoff_depth == 0 and handoff.role in visited:
            raise GuardError("Specialist handoff would duplicate an existing assignment")
        if handoff_depth == 1 and handoff.role not in handoff_source_roles:
            raise GuardError("A handoff recipient may only question its source specialists")
    return decision


def resolve_specialist_handoffs(
    initial_roles: list[SpecialistRole],
    decisions: list[tuple[SpecialistRole, SpecialistDecision]],
) -> list[HandoffDispatch]:
    initial = set(initial_roles)
    grouped: dict[SpecialistRole, list[tuple[SpecialistRole, str, str, str]]] = {}
    for source_role, decision in decisions:
        for handoff in decision.handoffs:
            if handoff.role == source_role or handoff.role in initial:
                continue
            grouped.setdefault(handoff.role, []).append(
                (source_role, handoff.instruction, handoff.reason, decision.reply)
            )

    dispatches = []
    for role, requests in grouped.items():
        sources = tuple(source for source, _, _, _ in requests)
        if len(requests) == 1:
            _, instruction, reason, reply = requests[0]
            context = (
                f"{sources[0]}からの引き継ぎ理由: {reason}\n"
                f"{sources[0]}の直前応答: {reply}"
            )
        else:
            instruction = "\n".join(
                f"{source}からの依頼: {request}" for source, request, _, _ in requests
            )[:1500]
            context = "\n".join(
                f"{source}からの引き継ぎ理由: {reason}\n{source}の直前応答: {reply}"
                for source, _, reason, reply in requests
            )[:2000]
        dispatches.append(
            HandoffDispatch(
                role=role,
                source_roles=sources,
                instruction=instruction,
                context=context,
            )
        )
    return dispatches
