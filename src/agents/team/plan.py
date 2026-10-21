"""The team lead's staffing plan: parsed from the model, then checked in code.

The model proposes; the code decides what is allowed. The architect and the
security engineer always run (this pipeline exists to produce an audited
design), unknown roles are dropped, and an unreadable plan falls back to the
default quick plan instead of failing the run.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

# Roles this pipeline can staff, in the order they run.
PIPELINE_ROLES = ("ideologist", "business-analyst", "architect", "security-engineer")
ALWAYS_STAFFED = ("architect", "security-engineer")
MODES = ("small", "quick", "full")

# Security reviews allowed per mode: the first review plus the team rules' fix
# rounds (quick: one fix round, full: two). After that the run escalates.
SECURITY_ROUNDS = {"small": 2, "quick": 2, "full": 3}

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


def default_plan(reason: str) -> StaffingPlan:
    return StaffingPlan(
        mode="quick",
        staff=("business-analyst", *ALWAYS_STAFFED),
        skip_reasons={"ideologist": "default plan"},
        decisions=(reason,),
    )


def _strings(value, limit: int) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    items = [str(item).strip() for item in value if str(item).strip()]
    return tuple(items[:limit])


def parse_plan(text: str) -> StaffingPlan:
    """Parse the team lead's JSON reply; never raises."""
    match = _JSON_OBJECT.search(text or "")
    if not match:
        return default_plan("Team lead plan had no JSON object; used the default quick plan.")
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return default_plan("Team lead plan was not valid JSON; used the default quick plan.")
    if not isinstance(data, dict):
        return default_plan("Team lead plan was not a JSON object; used the default quick plan.")

    decisions = list(_strings(data.get("decisions"), MAX_DECISIONS))

    mode = str(data.get("mode", "")).strip().lower()
    if mode not in MODES:
        decisions.append(f"Unknown mode {mode!r}; used quick.")
        mode = "quick"

    requested = _strings(data.get("staff"), len(PIPELINE_ROLES) * 2)
    unknown = [role for role in requested if role not in PIPELINE_ROLES]
    if unknown:
        decisions.append(f"Dropped roles this pipeline cannot staff: {', '.join(unknown)}.")
    missing = [role for role in ALWAYS_STAFFED if role not in requested]
    if missing:
        decisions.append(f"Always staffed: added {', '.join(missing)}.")
    wanted = set(requested) | set(ALWAYS_STAFFED)
    staff = tuple(role for role in PIPELINE_ROLES if role in wanted)

    raw_reasons = data.get("skip_reasons")
    skip_reasons = {}
    if isinstance(raw_reasons, dict):
        skip_reasons = {
            str(role): str(reason).strip()
            for role, reason in raw_reasons.items()
            if role in PIPELINE_ROLES and role not in staff and str(reason).strip()
        }

    return StaffingPlan(
        mode=mode,
        staff=staff,
        skip_reasons=skip_reasons,
        questions=_strings(data.get("questions_for_operator"), MAX_QUESTIONS),
        decisions=tuple(decisions[:MAX_DECISIONS]),
    )
