"""Separate additive browser boundary; doctor adapters remain read-only.

Implementations operate only on rendered stable IDs. There is deliberately no
remove/decrement/checkout method and no live implementation or browser launch.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

from grossy.browser.observation import CartObservation
from grossy.paths import GrossyPaths
from grossy.weekly.mutation import (
    run_recurring_intent,
    run_recurring_observe,
    run_recurring_preflight,
    run_resume_prepare,
)
from grossy.weekly.plan import WeeklyPlanError


class AdditiveCartAdapter(ABC):
    @abstractmethod
    def read_cart(self) -> CartObservation:
        """Read complete cart and current facts; fail on unsafe/ambiguous UI."""

    @abstractmethod
    def add_positive(self, product_id: str, canonical_url: str, quantity: int) -> None:
        """Add a strictly positive quantity to this exact stable identity."""


def apply_recurring(
    paths: GrossyPaths,
    run_id: str,
    adapter: AdditiveCartAdapter,
    confirm: Callable[[dict[str, Any]], bool],
    *,
    resume: bool = False,
) -> dict[str, Any]:
    """Offline-testable skill orchestration, never implicitly invoked by CLI.

    Each accepted manifest has at most one action. After any failure this
    invocation stops; another invocation must explicitly use resume and reread.
    """
    before = adapter.read_cart().model_dump(mode="json")
    prepared = (run_resume_prepare if resume else run_recurring_preflight)(paths, run_id, before)
    while True:
        manifest = prepared["manifest"]
        if manifest["additions"] and not confirm(manifest):
            return {"status": "cancelled"}
        # Confirmation does not waive the immediately-before-mutation reread.
        current = adapter.read_cart().model_dump(mode="json")
        refreshed = run_recurring_preflight(paths, run_id, current)
        if refreshed["manifest"]["additions"] != manifest["additions"] or current.get(
            "recurring_product_facts"
        ) != before.get("recurring_product_facts"):
            raise WeeklyPlanError("confirmed delta changed; renewed owner confirmation required")
        manifest = refreshed["manifest"]
        authorization = run_recurring_intent(paths, run_id, manifest)
        addition = authorization["addition"]
        if addition is None:
            return {"status": "success"}
        error: BaseException | None = None
        try:
            adapter.add_positive(
                addition["product_id"], addition["canonical_url"], addition["quantity"]
            )
        except BaseException as exc:
            error = exc
        try:
            observed = adapter.read_cart().model_dump(mode="json")
            outcome = "failed" if error else "readback"
        except BaseException as exc:
            observed = current
            outcome = "incomplete"
            error = error or exc
        observed["mutation_result"] = {
            "manifest_checksum": manifest["checksum"],
            "outcome": outcome,
        }
        result = run_recurring_observe(paths, run_id, observed)
        if error is not None or result["status"] != "success":
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise error
            return {"status": "error", "lifecycle": "partial-failure"}
        before = adapter.read_cart().model_dump(mode="json")
        prepared = run_recurring_preflight(paths, run_id, before)
