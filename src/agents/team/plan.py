"""The team lead's staffing plan: parsed from the model, then checked in code.

The model proposes; the code decides what is allowed. The designer and the
security engineer always run (this team always produces an audited design).
In a build run there is always at least one engineer, and every build is
tested by QA and reviewed by the reviewer. Outside a build run, build roles are
dropped. Unknown roles are dropped, and an unreadable plan falls back to the
default quick plan instead of failing the run.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

# Every role the pipeline can staff, in the order they work.
PIPELINE_ROLES = (
    "business-analyst",
    "designer",
    "security-engineer",
    "backend-engineer",
    "frontend-engineer",
    "qa-analyst",
    "reviewer",
    "devops-engineer",
)
ALWAYS_STAFFED = ("designer", "security-engineer")
ENGINEERS = ("backend-engineer", "frontend-engineer")
BUILD_ROLES = (*ENGINEERS, "qa-analyst", "reviewer", "devops-engineer")
ALWAYS_IN_BUILD = ("qa-analyst", "reviewer")
MODES = ("small", "quick", "full")

MAX_QUESTIONS = 5
MAX_DECISIONS = 10

_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


@dataclass(frozen=True)
class StaffingPlan:
    mode: str
    staff: tuple[str, ...]
    skip_reasons: dict[str, str] = field(default_factory=dict)
    questions: tuple[str, ...] = ()
    decisions: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "mode": self.mode,
            "staff": list(self.staff),
            "skip_reasons": dict(self.skip_reasons),
            "questions": list(self.questions),
            "decisions": list(self.decisions),
        }


def _ordered(roles) -> tuple[str, ...]:
    wanted = set(roles)
    return tuple(role for role in PIPELINE_ROLES if role in wanted)


def default_plan(reason: str, build: bool = False) -> StaffingPlan:
    staff = ["business-analyst", *ALWAYS_STAFFED]
    if build:
        staff += ["backend-engineer", *ALWAYS_IN_BUILD]
    return StaffingPlan(mode="quick", staff=_ordered(staff), decisions=(reason,))


def _strings(value, limit: int) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    items = [str(item).strip() for item in value if str(item).strip()]
    return tuple(items[:limit])


def parse_plan(text: str, build: bool = False) -> StaffingPlan:
    """Parse the team lead's JSON reply and apply the staffing rules; never raises."""
    match = _JSON_OBJECT.search(text or "")
    if not match:
        return default_plan("Team lead plan had no JSON object; used the default quick plan.", build)
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return default_plan("Team lead plan was not valid JSON; used the default quick plan.", build)
    if not isinstance(data, dict):
        return default_plan("Team lead plan was not a JSON object; used the default quick plan.", build)

    decisions = list(_strings(data.get("decisions"), MAX_DECISIONS))

    mode = str(data.get("mode", "")).strip().lower()
    if mode not in MODES:
        decisions.append(f"Unknown mode {mode!r}; used quick.")
        mode = "quick"

    requested = list(_strings(data.get("staff"), len(PIPELINE_ROLES) * 2))
    unknown = [role for role in requested if role not in PIPELINE_ROLES]
    if unknown:
        decisions.append(f"Dropped roles this team does not have: {', '.join(unknown)}.")
    staff = set(requested) & set(PIPELINE_ROLES)

    missing = [role for role in ALWAYS_STAFFED if role not in staff]
    if missing:
        decisions.append(f"Always staffed: added {', '.join(missing)}.")
    staff |= set(ALWAYS_STAFFED)

    if build:
        if not staff & set(ENGINEERS):
            decisions.append("Build run without an engineer: added backend-engineer.")
            staff.add("backend-engineer")
        added = [role for role in ALWAYS_IN_BUILD if role not in staff]
        if added:
            decisions.append(f"Every build is tested and reviewed: added {', '.join(added)}.")
        staff |= set(ALWAYS_IN_BUILD)
    else:
        dropped = [role for role in BUILD_ROLES if role in staff]
        if dropped:
            decisions.append(f"Design-only run: dropped {', '.join(dropped)}.")
        staff -= set(BUILD_ROLES)

    staffed = _ordered(staff)
    raw_reasons = data.get("skip_reasons")
    skip_reasons = {}
    if isinstance(raw_reasons, dict):
        skip_reasons = {
            str(role): str(reason).strip()
            for role, reason in raw_reasons.items()
            if role in PIPELINE_ROLES and role not in staffed and str(reason).strip()
        }

    return StaffingPlan(
        mode=mode,
        staff=staffed,
        skip_reasons=skip_reasons,
        questions=_strings(data.get("questions_for_operator"), MAX_QUESTIONS),
        decisions=tuple(decisions[:MAX_DECISIONS]),
    )
