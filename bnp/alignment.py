from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable

from .model import BlockRecord


class AlignmentError(ValueError):
    pass


@dataclass(frozen=True)
class UserPickRule:
    heights: frozenset[int] = frozenset()
    ranges: tuple[tuple[int, int], ...] = ()
    hashes: tuple[str, ...] = ()
    short_handles: tuple[tuple[str, str], ...] = ()

    def matches(self, record: BlockRecord) -> bool:
        h = record.block_hash.lower()
        if record.height in self.heights:
            return True
        if any(lo <= record.height <= hi for lo, hi in self.ranges):
            return True
        if any(h == item for item in self.hashes):
            return True
        if any(h.startswith(prefix) and h.endswith(suffix) for prefix, suffix in self.short_handles):
            return True
        return False


@dataclass(frozen=True)
class UserPickSpec:
    by_chain: dict[str, UserPickRule]
    source_text: str

    def matches(self, record: BlockRecord) -> bool:
        rules = []
        chain_rule = self.by_chain.get(record.chain.upper())
        all_rule = self.by_chain.get("ALL")
        if chain_rule:
            rules.append(chain_rule)
        if all_rule:
            rules.append(all_rule)
        return any(rule.matches(record) for rule in rules)


def _split_items(text: str) -> list[str]:
    return [part.strip() for part in text.split(",") if part.strip()]


def _parse_height_token(token: str) -> tuple[str, object] | None:
    raw = token.strip()
    if raw.startswith("#"):
        raw = raw[1:]
    elif raw.lower().startswith("height:"):
        raw = raw.split(":", 1)[1].strip()
    elif raw.lower().startswith("block:"):
        raw = raw.split(":", 1)[1].strip()
    else:
        return None
    if re.fullmatch(r"\d+", raw):
        return "height", int(raw)
    m = re.fullmatch(r"(\d+)\s*-\s*(\d+)", raw)
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        if hi < lo:
            lo, hi = hi, lo
        return "range", (lo, hi)
    raise AlignmentError(f"invalid user-picked height selector: {token}")


def parse_user_pick(text: str) -> UserPickSpec:
    """Parse a compact user-picked block selector.

    Examples::

        BTC:#0-9,#20;LTC:#0,#5
        ALL:#0-9
        BTC:hash:0000...full64hex
        XMR:short:abc123...def456

    User-pick relationships are deliberately treated as unverified association
    choices.  The underlying block records may still be independently verified.
    """
    text = (text or "").strip()
    if not text:
        raise AlignmentError(
            "USER_PICKED mode needs selectors, e.g. BTC:#0-9;LTC:#0,#5 or ALL:#0-9"
        )
    builders: dict[str, dict[str, list]] = {}
    for clause in [c.strip() for c in text.split(";") if c.strip()]:
        if ":" not in clause:
            raise AlignmentError(f"user-picked clause needs CHAIN:selectors: {clause}")
        chain, body = clause.split(":", 1)
        chain = chain.strip().upper()
        if not chain:
            raise AlignmentError(f"missing chain in user-picked clause: {clause}")
        b = builders.setdefault(chain, {"heights": [], "ranges": [], "hashes": [], "short": []})
        # hash:/short: contain a colon, but items remain comma-delimited.
        for token in _split_items(body):
            parsed = _parse_height_token(token)
            if parsed:
                kind, value = parsed
                b["heights" if kind == "height" else "ranges"].append(value)
                continue
            low = token.lower()
            if low.startswith("hash:"):
                value = token.split(":", 1)[1].strip().lower()
                if not re.fullmatch(r"[0-9a-f]{64}", value):
                    raise AlignmentError("hash: selector must contain one full 64-hex block hash")
                b["hashes"].append(value)
                continue
            if low.startswith("short:"):
                value = token.split(":", 1)[1].strip().lower()
                if "..." not in value:
                    raise AlignmentError("short: selector uses short:<prefix>...<suffix>")
                prefix, suffix = value.split("...", 1)
                if not prefix and not suffix:
                    raise AlignmentError("short handle needs a prefix, suffix, or both")
                if (prefix and not re.fullmatch(r"[0-9a-f]+", prefix)) or (suffix and not re.fullmatch(r"[0-9a-f]+", suffix)):
                    raise AlignmentError("short handle prefix/suffix must be hexadecimal")
                b["short"].append((prefix, suffix))
                continue
            # Convenient bare full hash.
            if re.fullmatch(r"[0-9a-fA-F]{64}", token):
                b["hashes"].append(token.lower())
                continue
            raise AlignmentError(f"unknown user-picked selector: {token}")

    rules = {
        chain: UserPickRule(
            heights=frozenset(v["heights"]),
            ranges=tuple(v["ranges"]),
            hashes=tuple(v["hashes"]),
            short_handles=tuple(v["short"]),
        )
        for chain, v in builders.items()
    }
    return UserPickSpec(rules, text)


def relation_grade_for_mode(mode: str) -> str:
    mode = mode.upper()
    if mode == "USER_PICKED":
        return "UNVERIFIED-USER-PROOF"
    if mode == "LOCKED_LINK":
        return "VERIFIED-HEIGHT-ALIGNMENT"
    if mode == "EPOCH_LINK":
        return "VERIFIED-EPOCH-ALIGNMENT"
    return "VERIFIED-EXPLICIT"


def apply_alignment(
    records: Iterable[BlockRecord],
    chains: Iterable[str],
    mode: str,
    *,
    user_pick: str | None = None,
    epoch_seconds: int = 86400,
) -> tuple[list[BlockRecord], dict]:
    records = list(records)
    chains = [c.upper() for c in chains]
    mode = mode.upper()
    base = {
        "mode": mode,
        "records_before": len(records),
        "relation_grade": relation_grade_for_mode(mode),
    }

    if mode in {"SOLOCHAIN", "CROSSCHAIN"}:
        return records, {**base, "records_after": len(records)}

    if mode == "LOCKED_LINK":
        if len(chains) < 2:
            raise AlignmentError("LOCKED_LINK needs at least two selected chains")
        heights_by_chain = {
            c: {r.height for r in records if r.chain.upper() == c}
            for c in chains
        }
        if any(not heights_by_chain[c] for c in chains):
            missing = [c for c in chains if not heights_by_chain[c]]
            raise AlignmentError(f"LOCKED_LINK has no eligible records for: {', '.join(missing)}")
        common = set.intersection(*(heights_by_chain[c] for c in chains))
        if not common:
            raise AlignmentError("LOCKED_LINK found no common block heights across all selected chains")
        out = [r for r in records if r.chain.upper() in chains and r.height in common]
        return out, {
            **base,
            "records_after": len(out),
            "common_height_count": len(common),
            "common_height_min": min(common),
            "common_height_max": max(common),
            "common_heights_sample": sorted(common)[:64],
        }

    if mode == "EPOCH_LINK":
        if len(chains) < 2:
            raise AlignmentError("EPOCH_LINK needs at least two selected chains")
        epoch_seconds = int(epoch_seconds)
        if epoch_seconds <= 0:
            raise AlignmentError("epoch size must be greater than zero seconds")

        coverage = {}
        epochs_by_chain = {}
        for c in chains:
            chain_records = [r for r in records if r.chain.upper() == c]
            stamped = [r for r in chain_records if r.timestamp is not None]
            epochs = {int(r.timestamp) // epoch_seconds for r in stamped}
            epochs_by_chain[c] = epochs
            coverage[c] = {
                "total": len(chain_records),
                "timestamped": len(stamped),
                "verified_timestamped": sum(1 for r in stamped if r.verified),
                "unverified_timestamped": sum(1 for r in stamped if not r.verified),
                "epoch_min": min(epochs) if epochs else None,
                "epoch_max": max(epochs) if epochs else None,
            }

        if any(not epochs_by_chain[c] for c in chains):
            missing = [c for c in chains if not epochs_by_chain[c]]
            detail = "; ".join(
                f"{c}={coverage[c]['timestamped']}/{coverage[c]['total']} timestamped" for c in chains
            )
            raise AlignmentError(
                "EPOCH_LINK needs timestamped records for every selected chain; missing timestamp coverage for: "
                + ", ".join(missing) + f". Timestamp coverage: {detail}. "
                "The offline 0–9 starter hashes intentionally do not carry timestamp provenance."
            )

        common = set.intersection(*(epochs_by_chain[c] for c in chains))
        if not common:
            ranges = "; ".join(
                f"{c}=epochs {coverage[c]['epoch_min']}..{coverage[c]['epoch_max']}" for c in chains
            )
            raise AlignmentError(
                "EPOCH_LINK found no normalized epochs shared by every selected chain. "
                + ranges + ". Acquire overlapping-time records or choose a larger epoch size."
            )
        out = [
            r for r in records
            if r.chain.upper() in chains and r.timestamp is not None and int(r.timestamp) // epoch_seconds in common
        ]
        relation_grade = (
            "VERIFIED-EPOCH-ALIGNMENT" if all(r.verified for r in out)
            else "UNVERIFIED-EPOCH-ALIGNMENT"
        )
        return out, {
            **base,
            "relation_grade": relation_grade,
            "verified_relation": relation_grade == "VERIFIED-EPOCH-ALIGNMENT",
            "records_after": len(out),
            "epoch_seconds": epoch_seconds,
            "timestamp_coverage": coverage,
            "common_epoch_count": len(common),
            "common_epoch_min": min(common),
            "common_epoch_max": max(common),
            "common_epochs_sample": sorted(common)[:64],
        }

    if mode == "USER_PICKED":
        spec = parse_user_pick(user_pick or "")
        out = [r for r in records if spec.matches(r)]
        if not out:
            raise AlignmentError("USER_PICKED selectors did not match any selected corpus records")
        matched_chains = sorted({r.chain.upper() for r in out})
        return out, {
            **base,
            "records_after": len(out),
            "user_pick": spec.source_text,
            "matched_chains": matched_chains,
            "verified_relation": False,
        }

    raise AlignmentError(f"mode {mode} is not implemented in this Alpha.3 build")
