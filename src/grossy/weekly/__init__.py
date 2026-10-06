"""Weekly planning foundation (PRD FR-113..FR-123, FR-163..FR-176, sections 11, 15, 18).

Task 09 implements the read-only weekly-run foundation:

- ``plan-prepare``: one-active-run lifecycle creation, lightweight history
  sync check, and initial ``planning`` state.
- ``baseline-apply``: validated cart-observation persistence by stable
  product identity, YAML/learned recurring-item resolution with precedence,
  positive-delta preview, budget calculation, and sanitized report generation.

No cart mutation occurs (PRD 20 task 09: "no cart mutation"). The run stays
in the ``planning`` lifecycle state throughout.
"""

from __future__ import annotations

from grossy.weekly.mutation import run_action_check, run_advance, run_consent_revoke
from grossy.weekly.plan import (
    WeeklyPlanError,
    run_baseline_apply,
    run_plan_prepare,
)

__all__ = [
    "WeeklyPlanError",
    "run_action_check",
    "run_advance",
    "run_baseline_apply",
    "run_consent_revoke",
    "run_plan_prepare",
]
