from __future__ import annotations

from dataclasses import dataclass


# Crosschain percentages are now a soft optimization target rather than a
# reconstruction gate.  BnP records the requested and achieved mix, but does
# not reject an otherwise valid recipe merely because an aligned/source-limited
# corpus cannot stay inside an arbitrary percentage-point window.
CROSSCHAIN_TOLERANCE_POINTS = None
PROOF_UNDER_TOLERANCE_POINTS = 1.0
PROOF_OVER_TOLERANCE_POINTS = 2.0


@dataclass(frozen=True)
class MixAssessment:
    passed: bool
    actual: dict[str, float]
    deviations: dict[str, float]
    max_abs_deviation: float
    granularity_limited: bool = False
    max_abs_byte_error: float = 0.0
    policy: str = "nearest-feasible-no-hard-tolerance"


@dataclass(frozen=True)
class ProofAssessment:
    passed: bool
    requested: float
    actual: float
    lower: float
    upper: float
    direction: str


def assess_mix(chain_bytes: dict[str, int], requested_weights: dict[str, float] | None) -> MixAssessment:
    """Report requested-vs-actual crosschain distribution.

    The result is intentionally advisory.  A Crosschain recipe succeeds when
    the requested target/proof coverage can be reconstructed; mix deviation is
    recorded for transparency and optimization, not used as a hard failure.
    """
    if not requested_weights:
        return MixAssessment(True, {}, {}, 0.0)
    total = sum(int(v) for v in chain_bytes.values())
    total_weight = float(sum(requested_weights.values())) or 100.0
    requested_pct = {c.upper(): 100.0 * float(w) / total_weight for c, w in requested_weights.items()}
    actual = {
        c: (100.0 * int(chain_bytes.get(c, 0)) / total if total else 0.0)
        for c in requested_pct
    }
    deviations = {c: actual[c] - requested_pct[c] for c in requested_pct}
    max_dev = max((abs(v) for v in deviations.values()), default=0.0)
    byte_errors = {
        c: abs(int(chain_bytes.get(c, 0)) - (total * requested_pct[c] / 100.0))
        for c in requested_pct
    }
    max_byte_error = max(byte_errors.values(), default=0.0)
    # Retain the old granularity diagnostic because it is useful on tiny
    # targets even though it no longer controls pass/fail.
    granularity_limited = bool(total and max_byte_error <= 0.5000000001 and max_dev > 0.0)
    return MixAssessment(True, actual, deviations, max_dev, granularity_limited, max_byte_error)


def assess_proof_coverage(requested: float, actual: float) -> ProofAssessment:
    requested = float(requested)
    actual = float(actual)
    if requested >= 100.0:
        return ProofAssessment(actual >= 100.0 - 1e-12, requested, actual, 100.0, 100.0, "full")
    lower = max(0.0, requested - PROOF_UNDER_TOLERANCE_POINTS)
    upper = min(100.0, requested + PROOF_OVER_TOLERANCE_POINTS)
    if actual >= requested:
        direction = "over" if actual > requested + 1e-12 else "on-target"
    else:
        direction = "under"
    return ProofAssessment(lower - 1e-12 <= actual <= upper + 1e-12, requested, actual, lower, upper, direction)


def proof_preference_key(requested: float, actual: float) -> tuple[int, float]:
    """Smaller is preferred: on/over target first, then allowed under-target."""
    requested = float(requested)
    actual = float(actual)
    if actual >= requested:
        return (0, actual - requested)
    return (1, requested - actual)
