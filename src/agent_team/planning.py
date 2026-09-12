"""承認済みIssueから作る実装計画の検証。"""
import re
from dataclasses import dataclass

from .policy import GuardError, digest
from .requirements import extract_acceptance_criteria

REQUIRED_PLAN_SECTIONS = (
    "変更予定ファイル",
    "受入条件",
    "テストコマンド",
    "セキュリティ",
    "冪等",
    "競合",
    "権限",
    "ロールアウト",
    "マイグレーション",
    "ロールバック",
    "未確認事項",
)
HEADING_PATTERN = re.compile(r"(?m)^#{1,6}\s+(.+?)\s*$")


@dataclass(frozen=True)
class PlanIdentity:
    version: int
    path: str
    content_hash: str
    requirements_hash: str
    base_sha: str
    acceptance_ids: tuple[str, ...]


def plan_path(issue_number: int, version: int) -> str:
    if issue_number <= 0 or version <= 0:
        raise GuardError("Issue and plan versions must be positive")
    return f"docs/work-items/issue-{issue_number}/plans/v{version}.md"


def validate_plan(
    *, body: str, requirements_body: str, issue_number: int, version: int, base_sha: str
) -> PlanIdentity:
    if not body.strip() or not re.fullmatch(r"[0-9a-f]{40}", base_sha):
        raise GuardError("Plan body and a full base SHA are required")
    headings = [heading.casefold() for heading in HEADING_PATTERN.findall(body)]
    missing_sections = [
        section for section in REQUIRED_PLAN_SECTIONS if not any(section.casefold() in h for h in headings)
    ]
    if missing_sections:
        raise GuardError("Missing plan sections: " + ", ".join(missing_sections))
    required = extract_acceptance_criteria(requirements_body)
    mentioned = set(re.findall(r"\bAC-\d+\b", body))
    missing_ac = set(required) - mentioned
    unknown_ac = mentioned - set(required)
    if missing_ac or unknown_ac:
        raise GuardError(
            "Plan acceptance coverage mismatch"
            + (f"; missing={sorted(missing_ac)}" if missing_ac else "")
            + (f"; unknown={sorted(unknown_ac)}" if unknown_ac else "")
        )
    return PlanIdentity(
        version=version,
        path=plan_path(issue_number, version),
        content_hash=digest(body),
        requirements_hash=digest(requirements_body),
        base_sha=base_sha,
        acceptance_ids=tuple(required),
    )


def validate_review_coverage(required_ids: tuple[str, ...], coverage: list[dict], findings: list[dict]) -> None:
    blocking = [f for f in findings if f.get("severity") in {"critical", "high", "medium"}]
    if blocking:
        raise GuardError("Implementation plan has blocking findings")
    evidence = {
        item.get("acceptance_id")
        for item in coverage
        if item.get("status") == "met" and str(item.get("evidence", "")).strip()
    }
    if evidence != set(required_ids):
        raise GuardError("Implementation plan review does not cover the exact acceptance set")
