from __future__ import annotations

from typing import Any


def candidate_coverage(
    *,
    preview_count: int,
    inventory_count: int | None,
    minimum_candidates: int,
    minimum_exhausted_candidates: int = 10,
) -> dict[str, Any]:
    preview_count = max(0, int(preview_count))
    inventory = None if inventory_count is None else max(0, int(inventory_count))
    exhausted = bool(inventory and preview_count >= inventory)
    coverage_pass = bool(
        preview_count >= int(minimum_candidates)
        or (
            exhausted
            and preview_count >= int(minimum_exhausted_candidates)
        )
    )
    return {
        "pass": coverage_pass,
        "inventory_exhausted": exhausted,
        "candidate_pool_count": preview_count,
        "candidate_inventory_count": inventory,
        "minimum_candidate_count": int(minimum_candidates),
        "minimum_exhausted_candidate_count": int(minimum_exhausted_candidates),
    }
