"""Separate bounded removal adapter; no live implementation or implicit launch."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

from grossy.browser.observation import CartObservation
from grossy.paths import GrossyPaths
from grossy.weekly.mutation import _cart_lines
from grossy.weekly.plan import WeeklyPlanError
from grossy.weekly.removal import run_removal_intent, run_removal_observe, run_removal_preflight


class RemovalCartAdapter(ABC):
    @abstractmethod
    def read_cart(self) -> CartObservation:
        """Read the entire rendered cart, fail closed on unsafe UI/identity."""

    @abstractmethod
    def remove_bounded(self, product_id: str, canonical_url: str, quantity: int) -> None:
        """Decrement exactly this positive quantity, never remove unrelated lines."""


def apply_removal(
    paths: GrossyPaths,
    run_id: str,
    adapter: RemovalCartAdapter,
    confirm: Callable[[dict[str, Any]], bool],
    *,
    product: str,
    quantity: int | None = None,
    include_preexisting: bool = False,
) -> dict[str, Any]:
    before = adapter.read_cart()
    prepared = run_removal_preflight(
        paths,
        run_id,
        before.model_dump(mode="json"),
        product=product,
        quantity=quantity,
        include_preexisting=include_preexisting,
    )
    manifest = prepared["manifest"]
    if not confirm(manifest):
        return {"status": "cancelled"}
    current = adapter.read_cart()
    refreshed = run_removal_preflight(
        paths,
        run_id,
        current.model_dump(mode="json"),
        product=product,
        quantity=quantity,
        include_preexisting=include_preexisting,
    )["manifest"]
    if _cart_lines(before) != _cart_lines(current) or any(
        refreshed[key] != manifest[key]
        for key in (
            "removals",
            "run_attributed_quantity",
            "preexisting_quantity",
            "include_preexisting",
        )
    ):
        raise WeeklyPlanError("confirmed removal changed; renewed confirmation required")
    # Check revocation after the immediate reread, before durable authorization.
    if not confirm(refreshed):
        return {"status": "cancelled"}
    final_cart = adapter.read_cart()
    final_manifest = run_removal_preflight(
        paths,
        run_id,
        final_cart.model_dump(mode="json"),
        product=product,
        quantity=quantity,
        include_preexisting=include_preexisting,
    )["manifest"]
    if _cart_lines(final_cart) != _cart_lines(current) or any(
        final_manifest[key] != refreshed[key]
        for key in (
            "removals",
            "run_attributed_quantity",
            "preexisting_quantity",
            "include_preexisting",
        )
    ):
        raise WeeklyPlanError("cart changed during confirmation; renewed confirmation required")
    current, refreshed = final_cart, final_manifest
    authorization = run_removal_intent(paths, run_id, refreshed, confirmed=True)
    removal = authorization["removal"]
    error: BaseException | None = None
    try:
        adapter.remove_bounded(removal["product_id"], removal["canonical_url"], removal["quantity"])
    except BaseException as exc:
        error = exc
    try:
        observed = adapter.read_cart().model_dump(mode="json")
        outcome = "failed" if error else "readback"
    except BaseException as exc:
        observed = current.model_dump(mode="json")
        outcome = "incomplete"
        error = error or exc
    observed["mutation_result"] = {"manifest_checksum": refreshed["checksum"], "outcome": outcome}
    result = run_removal_observe(paths, run_id, observed)
    if isinstance(error, (KeyboardInterrupt, SystemExit)):
        raise error
    return result
