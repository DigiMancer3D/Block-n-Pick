from __future__ import annotations

from collections.abc import Iterable


def equal_split(chains: Iterable[str], total: int = 100) -> dict[str, int]:
    """Split *total* as evenly as possible, assigning remainder left-to-right."""
    ordered = [str(c).upper() for c in chains]
    if not ordered:
        return {}
    base, remainder = divmod(int(total), len(ordered))
    return {c: base + (1 if i < remainder else 0) for i, c in enumerate(ordered)}


def rebalance_percentages(
    values: dict[str, int],
    selected: Iterable[str],
    changed: str | None = None,
    manual_order: list[str] | None = None,
    total: int = 100,
) -> tuple[dict[str, int], list[str]]:
    """Return an integer mix that totals *total*.

    Newly selected/unedited chains share the unclaimed percentage left-to-right.
    Editing a chain makes it manual.  If manual values over-claim the pool, older
    manual chains are reduced before the value the user is currently editing.
    This gives the common two-chain behavior BTC=80 -> LTC=20 while still
    allowing a user to progressively pin three or more values.
    """
    selected_order = [str(c).upper() for c in selected]
    values = {str(k).upper(): max(0, min(total, int(v))) for k, v in values.items()}
    order = [c for c in (manual_order or []) if c in selected_order]
    changed = changed.upper() if changed else None
    if changed and changed in selected_order:
        if changed in order:
            order.remove(changed)
        order.append(changed)

    # No manual values yet: ordinary equal split.
    if not order:
        return equal_split(selected_order, total), []

    out = {c: values.get(c, 0) for c in selected_order}
    manual_sum = sum(out[c] for c in order)
    if manual_sum > total:
        overflow = manual_sum - total
        # Preserve the value currently being edited if possible; reduce older
        # manual values from newest to oldest.
        reducible = [c for c in reversed(order) if c != changed]
        for c in reducible:
            take = min(out[c], overflow)
            out[c] -= take
            overflow -= take
            if overflow <= 0:
                break
        if overflow > 0 and changed:
            out[changed] = max(0, out[changed] - overflow)
        manual_sum = sum(out[c] for c in order)

    auto = [c for c in selected_order if c not in order]
    remaining = max(0, total - manual_sum)
    if auto:
        split = equal_split(auto, remaining)
        for c in auto:
            out[c] = split[c]
    elif selected_order:
        # When every chain was manually touched, keep the edited value and let
        # one companion chain absorb any remainder so the invariant stays 100%.
        delta = total - sum(out.values())
        if delta:
            companions = [c for c in reversed(selected_order) if c != changed]
            sink = companions[0] if companions else selected_order[0]
            out[sink] = max(0, min(total, out[sink] + delta))

    # Correct any rounding/edge remainder deterministically left-to-right.
    diff = total - sum(out.values())
    if diff and selected_order:
        candidates = [c for c in selected_order if c != changed] or selected_order
        step = 1 if diff > 0 else -1
        for _ in range(abs(diff)):
            for c in candidates:
                nv = out[c] + step
                if 0 <= nv <= total:
                    out[c] = nv
                    break
    return out, order


def mix_string(values: dict[str, int], selected: Iterable[str]) -> str:
    return "!".join(f"{int(values[c.upper()])}%{c.upper()}" for c in selected)
