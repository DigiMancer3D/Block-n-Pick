from __future__ import annotations

import math
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Callable, Iterable

from .model import BlockRecord, Segment
from .representations import represent_record


class BuildCancelled(RuntimeError):
    """Raised when a caller-requested build cancellation is observed."""


@dataclass(frozen=True)
class SourceMatch:
    record: BlockRecord
    source_offset: int
    length: int


@dataclass(frozen=True)
class _SourcePoint:
    record: BlockRecord
    source_offset: int
    data: bytes


@dataclass
class PickResult:
    segments: list[Segment]
    covered_bytes: int
    total_bytes: int
    chain_bytes: dict[str, int]
    exact: bool
    diagnostics: dict

    @property
    def coverage_percent(self):
        return 100.0 if not self.total_bytes else 100.0 * self.covered_bytes / self.total_bytes


ProgressFn = Callable[[str, int, int, str], None]


def _cancelled(cancel) -> bool:
    return bool(cancel is not None and cancel.is_set())


def _emit(progress: ProgressFn | None, stage: str, done: int, total: int, detail: str = ""):
    if progress:
        progress(stage, int(done), int(total), detail)


class TargetSpecificIndex:
    """Low-memory source-prefix index for matching target bytes.

    Alpha.2 built a set of every target substring for every length up to
    ``max_chunk``.  That scales as roughly target_size * max_chunk Python objects
    and can consume gigabytes for ordinary images.  This replacement indexes the
    much smaller source corpus instead:

    * exact maps for 1, 2 and 3 byte prefixes;
    * a 4-byte prefix fan-out for possible long matches;
    * direct byte comparison only when a 4-byte prefix actually collides.

    Memory therefore follows corpus size, not target size, while preserving all
    chunk lengths through 19.
    """

    SHORT_MAX = 3
    LONG_PREFIX = 4

    def __init__(self, target: bytes, max_chunk: int = 3):
        self.target = target
        self.max_chunk = max(1, int(max_chunk))
        self.records_scanned = 0
        self.source_bytes = 0
        self.short_by_chain = defaultdict(lambda: defaultdict(dict))
        self.long_by_chain = defaultdict(dict)
        self.long_points = 0
        self.long_collision_points = 0

    def add_record(self, record: BlockRecord, representation: str):
        data = represent_record(record, representation)
        self.records_scanned += 1
        self.source_bytes += len(data)
        chain = record.chain.upper()
        short = self.short_by_chain[chain]
        longmap = self.long_by_chain[chain]

        for offset in range(len(data)):
            avail = min(self.max_chunk, len(data) - offset)
            for length in range(1, min(self.SHORT_MAX, avail) + 1):
                frag = data[offset:offset + length]
                dest = short[length]
                if frag not in dest:
                    dest[frag] = SourceMatch(record, offset, length)

            if avail >= self.LONG_PREFIX:
                key = data[offset:offset + self.LONG_PREFIX]
                point = _SourcePoint(record, offset, data)
                existing = longmap.get(key)
                if existing is None:
                    longmap[key] = point
                elif isinstance(existing, list):
                    existing.append(point)
                    self.long_collision_points += 1
                else:
                    longmap[key] = [existing, point]
                    self.long_collision_points += 1
                self.long_points += 1

    def diagnostics(self) -> dict:
        return {
            "records_scanned": self.records_scanned,
            "source_bytes": self.source_bytes,
            "long_prefix_points": self.long_points,
            "long_prefix_collisions": self.long_collision_points,
            "index_kind": "source-prefix-v1",
        }

    def _long_candidate(self, chain: str, pos: int, cap: int) -> SourceMatch | None:
        if cap < self.LONG_PREFIX or pos + self.LONG_PREFIX > len(self.target):
            return None
        entry = self.long_by_chain.get(chain, {}).get(
            self.target[pos:pos + self.LONG_PREFIX]
        )
        if entry is None:
            return None
        points = entry if isinstance(entry, list) else (entry,)
        best = None
        for point in points:
            limit = min(cap, len(point.data) - point.source_offset, len(self.target) - pos)
            length = self.LONG_PREFIX
            # Four bytes are already known equal from the dictionary key.  Long
            # prefix hits are sparse, so this short direct comparison is cheap.
            while length < limit and self.target[pos + length] == point.data[point.source_offset + length]:
                length += 1
            if best is None or length > best.length:
                best = SourceMatch(point.record, point.source_offset, length)
                if length >= cap:
                    break
        return best

    def candidate(self, chain: str, pos: int, max_len: int | None = None):
        if pos >= len(self.target):
            return None
        chain = chain.upper()
        cap = min(self.max_chunk, len(self.target) - pos, max_len or self.max_chunk)
        if cap <= 0:
            return None

        if cap >= self.LONG_PREFIX:
            long_match = self._long_candidate(chain, pos, cap)
            if long_match is not None:
                return long_match

        short = self.short_by_chain.get(chain, {})
        for length in range(min(self.SHORT_MAX, cap), 0, -1):
            match = short.get(length, {}).get(self.target[pos:pos + length])
            if match:
                return match
        return None

    def candidates(self, chains: Iterable[str], pos: int, max_len: int | None = None):
        return [m for c in chains if (m := self.candidate(c, pos, max_len))]


def build_index(
    target,
    records,
    representation,
    max_chunk=3,
    *,
    progress: ProgressFn | None = None,
    cancel=None,
):
    idx = TargetSpecificIndex(target, max_chunk)
    try:
        total = len(records)
    except TypeError:
        records = list(records)
        total = len(records)
    for i, record in enumerate(records, 1):
        if _cancelled(cancel):
            raise BuildCancelled("build cancelled while indexing corpus")
        idx.add_record(record, representation)
        if i == total or i == 1 or i % 128 == 0:
            _emit(progress, "index", i, total, f"{record.chain} height {record.height}")
    return idx


def _segment(match, representation, target_offset, length=None):
    use_len = match.length if length is None else length
    record = match.record
    return Segment(
        record.chain,
        record.network,
        record.height,
        record.branch_index,
        record.block_hash,
        representation.upper(),
        match.source_offset,
        use_len,
        target_offset,
        record.verified,
        getattr(record, "relation_grade", "VERIFIED-EXPLICIT"),
        getattr(record, "source_kind", "BLOCK_HASH"),
        getattr(record, "tx_raw_hash", None),
        getattr(record, "relation_id", None),
    )


def _progress_stride(total: int) -> int:
    # At most roughly 250 queue/UI updates per search stage.
    return max(1, int(total) // 250)


def pick_fewest_chunks(
    target,
    idx,
    chains,
    representation,
    *,
    progress: ProgressFn | None = None,
    cancel=None,
):
    """Exact minimum-chunk segmentation with O(target_size) bytes of DP memory.

    The former implementation allocated Python ``cost`` and ``choice`` object
    lists proportional to the target.  Here only one byte of choice is retained
    per target position; future costs fit in a rolling window because no edge is
    longer than ``max_chunk``.
    """
    n = len(target)
    if n == 0:
        return PickResult([], 0, 0, {}, True, {**idx.diagnostics(), "chunks": 0})

    inf = n + 1
    choice = bytearray(n)
    future = deque([0] + [inf] * (idx.max_chunk - 1), maxlen=idx.max_chunk)
    stride = _progress_stride(n)
    current_cost = inf

    for processed, pos in enumerate(range(n - 1, -1, -1), 1):
        if _cancelled(cancel):
            raise BuildCancelled("build cancelled while searching target")
        opts = idx.candidates(chains, pos)
        longest = max((m.length for m in opts), default=0)
        best_cost = inf
        best_len = 0
        # If a source contains a prefix of length L, its prefixes 1..L are also
        # valid matches.  We therefore only need the longest match to enumerate
        # every legal edge from this target position.
        for length in range(1, longest + 1):
            future_cost = future[length - 1]
            candidate_cost = 1 + future_cost
            if candidate_cost < best_cost or (candidate_cost == best_cost and length > best_len):
                best_cost = candidate_cost
                best_len = length
        current_cost = best_cost
        choice[pos] = best_len
        future.appendleft(current_cost)
        if processed == n or processed % stride == 0:
            _emit(progress, "search", processed, n, f"target offset {pos:,}")

    if current_cost >= inf or choice[0] == 0:
        return PickResult(
            [], 0, n, {}, False,
            {**idx.diagnostics(), "reason": "target cannot be fully covered"},
        )

    segments = []
    chain_bytes = defaultdict(int)
    pos = 0
    segment_count = 0
    while pos < n:
        if _cancelled(cancel):
            raise BuildCancelled("build cancelled while materializing recipe")
        length = int(choice[pos])
        if length <= 0:
            return PickResult(
                segments, pos, n, dict(chain_bytes), False,
                {**idx.diagnostics(), "reason": f"missing DP choice at target offset {pos}"},
            )
        candidates = idx.candidates(chains, pos, length)
        matches = [m for m in candidates if m.length >= length]
        if not matches:
            return PickResult(
                segments, pos, n, dict(chain_bytes), False,
                {**idx.diagnostics(), "reason": f"source match vanished at target offset {pos}"},
            )
        match = max(matches, key=lambda m: (m.length, m.record.chain))
        segments.append(_segment(match, representation, pos, length))
        chain_bytes[match.record.chain] += length
        pos += length
        segment_count += 1

    return PickResult(
        segments, n, n, dict(chain_bytes), True,
        {**idx.diagnostics(), "chunks": segment_count, "dp_choice_bytes": len(choice)},
    )


def _byte_quotas(total, weights):
    total_weight = sum(weights.values())
    raw = {k.upper(): total * value / total_weight for k, value in weights.items()}
    quotas = {k: int(math.floor(value)) for k, value in raw.items()}
    left = total - sum(quotas.values())
    for key, _ in sorted(
        raw.items(), key=lambda item: item[1] - math.floor(item[1]), reverse=True
    )[:left]:
        quotas[key] += 1
    return quotas


def pick_weighted(
    target,
    idx,
    weights,
    representation,
    strict=True,
    *,
    progress: ProgressFn | None = None,
    cancel=None,
):
    chains = [c.upper() for c in weights]
    quotas = _byte_quotas(len(target), weights)
    used = {c: 0 for c in chains}
    segments = []
    pos = 0
    stride = _progress_stride(len(target))
    while pos < len(target):
        if _cancelled(cancel):
            raise BuildCancelled("build cancelled while searching weighted target")
        opts = []
        for chain in chains:
            remaining = quotas[chain] - used[chain]
            if remaining <= 0:
                continue
            match = idx.candidate(chain, pos, min(idx.max_chunk, remaining))
            if match:
                opts.append((remaining / max(1, quotas[chain]), match.length, chain, match))
        if not opts:
            return PickResult(
                segments, pos, len(target), dict(used), False,
                {**idx.diagnostics(), "reason": f"weighted coverage blocked at {pos}", "quotas": quotas, "used": used},
            )
        _, _, chain, match = max(opts, key=lambda item: (item[0], item[1], item[2]))
        length = min(match.length, quotas[chain] - used[chain])
        segments.append(_segment(match, representation, pos, length))
        used[chain] += length
        pos += length
        if pos == len(target) or pos % stride == 0:
            _emit(progress, "search", pos, len(target), f"weighted target offset {pos:,}")
    exact = all(used[c] == quotas[c] for c in quotas)
    return PickResult(
        segments, len(target), len(target), used, exact or not strict,
        {**idx.diagnostics(), "chunks": len(segments), "quotas": quotas, "exact_mix": exact},
    )


def pick_partial(
    target,
    idx,
    chains,
    representation,
    minimum_coverage,
    *,
    progress: ProgressFn | None = None,
    cancel=None,
):
    if not 0 < minimum_coverage <= 100:
        raise ValueError("minimum coverage must be >0 and <=100")
    needed = math.ceil(len(target) * minimum_coverage / 100)
    covered = 0
    pos = 0
    segments = []
    chain_bytes = defaultdict(int)
    stride = _progress_stride(len(target))
    while pos < len(target) and covered < needed:
        if _cancelled(cancel):
            raise BuildCancelled("build cancelled while searching proof target")
        opts = idx.candidates(chains, pos)
        if not opts:
            pos += 1
        else:
            match = max(opts, key=lambda item: (item.length, item.record.chain))
            length = min(match.length, needed - covered)
            segments.append(_segment(match, representation, pos, length))
            chain_bytes[match.record.chain] += length
            covered += length
            pos += length
        if pos == len(target) or pos % stride == 0:
            _emit(progress, "search", pos, len(target), f"proof target offset {pos:,}")
    return PickResult(
        segments, covered, len(target), dict(chain_bytes), covered >= needed,
        {**idx.diagnostics(), "requested_coverage": minimum_coverage, "chunks": len(segments)},
    )


def pick_partial_weighted(
    target,
    idx,
    weights,
    representation,
    minimum_coverage,
    *,
    progress: ProgressFn | None = None,
    cancel=None,
):
    if not 0 < minimum_coverage < 100:
        raise ValueError("weighted partial coverage must be >0 and <100")
    needed = math.ceil(len(target) * minimum_coverage / 100)
    quotas = _byte_quotas(needed, weights)
    chains = [c.upper() for c in weights]
    used = {c: 0 for c in chains}
    segments = []
    pos = 0
    stride = _progress_stride(len(target))
    while pos < len(target) and sum(used.values()) < needed:
        if _cancelled(cancel):
            raise BuildCancelled("build cancelled while searching weighted proof target")
        opts = []
        for chain in chains:
            remaining = quotas[chain] - used[chain]
            if remaining <= 0:
                continue
            match = idx.candidate(chain, pos, min(idx.max_chunk, remaining))
            if match:
                opts.append((remaining / max(1, quotas[chain]), match.length, chain, match))
        if not opts:
            pos += 1
        else:
            _, _, chain, match = max(opts, key=lambda item: (item[0], item[1], item[2]))
            length = min(match.length, quotas[chain] - used[chain])
            segments.append(_segment(match, representation, pos, length))
            used[chain] += length
            pos += length
        if pos == len(target) or pos % stride == 0:
            _emit(progress, "search", pos, len(target), f"weighted proof offset {pos:,}")
    covered = sum(used.values())
    exact_mix = all(used[c] == quotas[c] for c in quotas)
    return PickResult(
        segments, covered, len(target), used, covered >= needed and exact_mix,
        {
            **idx.diagnostics(),
            "requested_coverage": minimum_coverage,
            "chunks": len(segments),
            "quotas": quotas,
            "exact_mix": exact_mix,
        },
    )



def _soft_weight_score(used: dict[str, int], quotas: dict[str, int], chain: str) -> float:
    """Higher score means this chain is more under its requested share.

    The quota is only a steering target.  Once a chain reaches/exceeds it the
    score becomes zero/negative, but the chain remains usable if it is the only
    source capable of covering the current target bytes.
    """
    q = int(quotas.get(chain, 0))
    if q <= 0:
        return -float(used.get(chain, 0) + 1)
    return (q - int(used.get(chain, 0))) / q


def pick_weighted_soft(
    target,
    idx,
    weights,
    representation,
    *,
    progress: ProgressFn | None = None,
    cancel=None,
):
    """Full reconstruction with crosschain percentages as soft preferences.

    Unlike ``pick_weighted``/``pick_weighted_tolerant``, no chain is hard-capped
    at a requested percentage.  This lets another selected chain cover bytes
    when the preferred chain has no matching fragment, while still steering
    toward the requested distribution whenever multiple chains can satisfy the
    same target position.
    """
    chains = [c.upper() for c in weights]
    quotas = _byte_quotas(len(target), weights)
    used = {c: 0 for c in chains}
    segments = []
    pos = 0
    stride = _progress_stride(len(target))
    while pos < len(target):
        if _cancelled(cancel):
            raise BuildCancelled("build cancelled while searching soft-weighted target")
        opts = []
        for chain in chains:
            match = idx.candidate(chain, pos, idx.max_chunk)
            if match:
                opts.append((_soft_weight_score(used, quotas, chain), match.length, chain, match))
        if not opts:
            return PickResult(
                segments, pos, len(target), dict(used), False,
                {
                    **idx.diagnostics(),
                    "reason": f"soft weighted coverage blocked at {pos}",
                    "quotas": quotas,
                    "used": used,
                    "mix_policy": "nearest-feasible-no-hard-tolerance",
                },
            )
        _, _, chain, match = max(opts, key=lambda item: (item[0], item[1], item[2]))
        length = min(match.length, len(target) - pos)
        segments.append(_segment(match, representation, pos, length))
        used[chain] += length
        pos += length
        if pos == len(target) or pos % stride == 0:
            _emit(progress, "search", pos, len(target), f"soft-weighted target offset {pos:,}")
    return PickResult(
        segments, len(target), len(target), used, True,
        {
            **idx.diagnostics(),
            "chunks": len(segments),
            "quotas": quotas,
            "used": used,
            "mix_policy": "nearest-feasible-no-hard-tolerance",
        },
    )


def pick_partial_weighted_soft(
    target,
    idx,
    weights,
    representation,
    minimum_coverage,
    *,
    progress: ProgressFn | None = None,
    cancel=None,
):
    """Partial proof picker with soft crosschain percentage steering."""
    if not 0 < minimum_coverage < 100:
        raise ValueError("weighted partial coverage must be >0 and <100")
    needed = math.ceil(len(target) * minimum_coverage / 100)
    quotas = _byte_quotas(needed, weights)
    chains = [c.upper() for c in weights]
    used = {c: 0 for c in chains}
    segments = []
    covered = 0
    pos = 0
    stride = _progress_stride(len(target))
    while pos < len(target) and covered < needed:
        if _cancelled(cancel):
            raise BuildCancelled("build cancelled while searching soft-weighted proof target")
        opts = []
        for chain in chains:
            match = idx.candidate(chain, pos, idx.max_chunk)
            if match:
                opts.append((_soft_weight_score(used, quotas, chain), match.length, chain, match))
        if not opts:
            pos += 1
        else:
            _, _, chain, match = max(opts, key=lambda item: (item[0], item[1], item[2]))
            length = min(match.length, needed - covered)
            segments.append(_segment(match, representation, pos, length))
            used[chain] += length
            covered += length
            pos += length
        if pos == len(target) or pos % stride == 0:
            _emit(progress, "search", pos, len(target), f"soft-weighted proof offset {pos:,}")
    return PickResult(
        segments, covered, len(target), used, covered >= needed,
        {
            **idx.diagnostics(),
            "requested_coverage": minimum_coverage,
            "chunks": len(segments),
            "quotas": quotas,
            "used": used,
            "mix_policy": "nearest-feasible-no-hard-tolerance",
        },
    )

def _byte_tolerance_bounds(total: int, weights: dict[str, float], tolerance_points: float = 1.0):
    """Return nominal/low/high byte quotas for percentage-point tolerance."""
    total = int(total)
    total_weight = float(sum(weights.values()))
    normalized = {c.upper(): 100.0 * float(v) / total_weight for c, v in weights.items()}
    nominal = _byte_quotas(total, normalized)
    low = {
        c: max(0, int(math.ceil(total * max(0.0, pct - tolerance_points) / 100.0 - 1e-12)))
        for c, pct in normalized.items()
    }
    high = {
        c: min(total, int(math.floor(total * min(100.0, pct + tolerance_points) / 100.0 + 1e-12)))
        for c, pct in normalized.items()
    }
    return normalized, nominal, low, high


def pick_weighted_tolerant(
    target,
    idx,
    weights,
    representation,
    *,
    tolerance_points: float = 1.0,
    progress: ProgressFn | None = None,
    cancel=None,
):
    """Full weighted reconstruction allowing bounded mix deviation.

    Exact quotas remain the primary path.  This fallback is used only when an
    alignment-constrained corpus cannot hit the exact requested distribution.
    Every selected chain must finish within ``tolerance_points`` percentage
    points of its request.  When two choices are otherwise equivalent, the
    picker favors a chain still below its nominal quota, which tends to keep
    small positive deviations preferable to premature undershoot.
    """
    chains = [c.upper() for c in weights]
    normalized, nominal, low, high = _byte_tolerance_bounds(len(target), weights, tolerance_points)
    used = {c: 0 for c in chains}
    segments = []
    pos = 0
    stride = _progress_stride(len(target))
    while pos < len(target):
        if _cancelled(cancel):
            raise BuildCancelled("build cancelled while searching tolerant weighted target")
        opts = []
        remaining_total = len(target) - pos
        for chain in chains:
            cap = high[chain] - used[chain]
            if cap <= 0:
                continue
            match = idx.candidate(chain, pos, min(idx.max_chunk, cap))
            if not match:
                continue
            below_low = max(0, low[chain] - used[chain])
            below_nominal = max(0, nominal[chain] - used[chain])
            # A chain that still needs bytes to satisfy its lower bound outranks
            # one already inside tolerance.  Nominal deficit is the secondary
            # target, then longer fragments reduce recipe size.
            opts.append((1 if below_low > 0 else 0, below_low, below_nominal, match.length, chain, match))
        if not opts:
            return PickResult(
                segments, pos, len(target), dict(used), False,
                {
                    **idx.diagnostics(),
                    "reason": f"tolerant weighted coverage blocked at {pos}",
                    "nominal": nominal, "low": low, "high": high, "used": used,
                    "tolerance_points": tolerance_points,
                },
            )
        _, _, _, _, chain, match = max(opts, key=lambda item: item[:-2] + (item[-2],))
        length = min(match.length, high[chain] - used[chain], remaining_total)
        segments.append(_segment(match, representation, pos, length))
        used[chain] += length
        pos += length
        if pos == len(target) or pos % stride == 0:
            _emit(progress, "search", pos, len(target), f"tolerant weighted target offset {pos:,}")
    within = all(low[c] <= used[c] <= high[c] for c in chains)
    return PickResult(
        segments, len(target), len(target), used, within,
        {
            **idx.diagnostics(),
            "chunks": len(segments),
            "nominal": nominal, "low": low, "high": high, "used": used,
            "tolerance_points": tolerance_points,
            "tolerant_mix": True,
        },
    )
