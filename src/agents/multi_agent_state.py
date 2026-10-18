from typing import Literal, TypedDict


class ProjectState(TypedDict):
    requirements: str | None
    architecture: str | None
    api_spec: str | None

    # Security specific
    security_findings: str | None
    revision_count: int
    next_agent: Literal["designer", "security", "FINISH"]
