from typing import Literal, TypedDict


class ProjectState(TypedDict):
    requirements: str | None
    architecture: str | None
    api_spec: str | None

    # Security specific
    security_findings: str | None
    revision_count: int
    # Optional per-run cap, never above settings.MAX_REVISIONS.
    max_revisions: int | None
    # Critical + High findings per audit round (None: report unparsed).
    blocking_history: list[int | None]
    # Why the loop ended early: "no_progress", or None.
    stop_reason: str | None
    # The latest audit had the brief and found no required capability missing.
    brief_verified: bool
    # The audited round with the fewest blocking findings, and the round number
    # returned when that is not the last one (None: the last round).
    best_round: dict | None
    selected_round: int | None
    next_agent: Literal["designer", "security", "FINISH"]
