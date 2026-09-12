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
    visited_roles: list[SpecialistRole],
) -> SpecialistDecision:
    visited = set(visited_roles)
    visited.add(source_role)
    if len(visited_roles) != len(set(visited_roles)):
        raise GuardError("Visited specialist roles must be unique")
    if handoff_depth >= 1 and decision.handoffs:
        raise GuardError("Specialist handoff depth exceeded")
    for handoff in decision.handoffs:
        if handoff.role == source_role:
            raise GuardError("A specialist cannot hand off to itself")
        if handoff.role in visited:
            raise GuardError("Specialist handoff would duplicate an existing assignment")
    return decision


def resolve_specialist_handoffs(
    initial_roles: list[SpecialistRole],
    decisions: list[tuple[SpecialistRole, SpecialistDecision]],
) -> list[HandoffDispatch]:
    initial = set(initial_roles)
    grouped: dict[SpecialistRole, list[tuple[SpecialistRole, str, str]]] = {}
    for source_role, decision in decisions:
        for handoff in decision.handoffs:
            if handoff.role == source_role or handoff.role in initial:
                continue
            grouped.setdefault(handoff.role, []).append(
                (source_role, handoff.instruction, handoff.reason)
            )

    dispatches = []
    for role, requests in grouped.items():
        sources = tuple(source for source, _, _ in requests)
        if len(requests) == 1:
            _, instruction, reason = requests[0]
            context = f"{sources[0]}からの引き継ぎ理由: {reason}"
        else:
            instruction = "\n".join(
                f"{source}からの依頼: {request}" for source, request, _ in requests
            )[:1500]
            context = "\n".join(
                f"{source}からの引き継ぎ理由: {reason}" for source, _, reason in requests
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
