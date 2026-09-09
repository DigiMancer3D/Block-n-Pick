from __future__ import annotations
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import re

from .alignment import apply_alignment, relation_grade_for_mode
from .hashing import hash_bundle
from .model import RecipeManifest, TargetIdentity
from .picker import (
    build_index,
    pick_fewest_chunks,
    pick_partial,
    pick_partial_weighted,
    pick_partial_weighted_soft,
    pick_weighted,
    pick_weighted_soft,
)
from .recipe_io import write_bnp, write_bnp2, write_bnpm
from .representations import represent_target
from .tolerances import assess_mix, assess_proof_coverage
from .tx_links import resolve_tx_link_pool


def _assessment_dict(obj):
    return dict(obj.__dict__)


def _alignment_context(mode, notes):
    mode = str(mode).upper()
    if mode == "LOCKED_LINK":
        count = notes.get("common_height_count", 0)
        lo = notes.get("common_height_min")
        hi = notes.get("common_height_max")
        recs = notes.get("records_after", 0)
        if count:
            return f" LOCKED_LINK eligible pool: {recs} records across {count} common heights ({lo}..{hi})."
    if mode == "USER_PICKED":
        return f" USER_PICKED matched {notes.get('records_after', 0)} records across {', '.join(notes.get('matched_chains', [])) or 'no chains'}."
    if mode == "EPOCH_LINK":
        count = notes.get("common_epoch_count", 0)
        return f" EPOCH_LINK eligible pool: {notes.get('records_after', 0)} records across {count} common epochs."
    if mode == "TX_LINKED":
        return (
            f" TX_LINKED eligible pool: {notes.get('records_after', 0)} records from "
            f"{notes.get('relation_count', 0)} relationship group(s); "
            f"unresolved members={notes.get('unresolved_members', 0)}."
        )
    return ""


def build_manifest(
    *, raw, safe_name, source_kind, source_value, corpus, chains,
    representation="RAW", mode="SOLOCHAIN", strategy="FEWEST_CHUNKS",
    weights=None, proof_coverage=None, max_chunk=3, progress=None, cancel=None,
    user_pick=None, epoch_seconds=86400, tx_link_selector=None,
):
    representation = representation.upper()
    mode = mode.upper()
    tb = represent_target(raw, representation)
    hb = hash_bundle(raw)
    target = TargetIdentity(
        safe_name, hb.raw_size, len(tb), hb.sha256_raw, hb.sha256_b64,
        representation, source_kind, source_value,
    )

    # USER_PICKED is explicitly allowed to use user-supplied/unverified corpus
    # records.  The association is marked UNVERIFIED-USER-PROOF and every
    # segment still preserves its underlying verified_source flag.
    # USER_PICKED intentionally accepts unverified user-selected data.
    # EPOCH_LINK also needs timestamped single-source records (for example LTC
    # public seeds); the relation grade is downgraded when such records are used.
    records = list(corpus.iter_records(
        chains=chains, network="mainnet",
        verified_only=(mode not in {"USER_PICKED", "EPOCH_LINK", "TX_LINKED"}),
    ))
    if mode == "TX_LINKED":
        pool = resolve_tx_link_pool(corpus, chains=chains, selector=tx_link_selector)
        # TX_LINKED does not index the origin block hash alone.  Each eligible
        # transaction member contributes one synthetic source whose bytes are:
        # tx_raw_hash || origin_block_hash.
        records = list(pool["records"])
        alignment_notes = {
            "mode": "TX_LINKED",
            "records_before": corpus.record_count(chains, verified_only=False),
            "records_after": len(records),
            "relation_grade": pool["relation_grade"],
            "verified_relation": pool["relation_grade"].startswith("VERIFIED-"),
            "relation_ids": [r.relation_id for r in pool["relations"]],
            "relation_count": len(pool["relations"]),
            "unresolved_members": pool["unresolved_members"],
            "missing_tx_raw_hash": pool.get("missing_tx_raw_hash", 0),
            "missing_origin_block": pool.get("missing_origin_block", 0),
            "literal_origin_used": pool.get("literal_origin_used", 0),
            "mapping_material": pool.get("material_kind", "serialized_tx_bytes||origin_block_hash"),
            "tx_link_selector": tx_link_selector or "ALL",
        }
    else:
        records, alignment_notes = apply_alignment(
            records, chains, mode, user_pick=user_pick, epoch_seconds=epoch_seconds,
        )
    idx = build_index(tb, records, representation, max_chunk, progress=progress, cancel=cancel)

    requested_proof = None if proof_coverage is None else float(proof_coverage)
    if requested_proof is not None and requested_proof < 100:
        if weights:
            # Try exact requested quotas first.  If one requested chain cannot
            # supply enough matching bytes, fall back to soft percentage steering
            # instead of sacrificing proof coverage merely to preserve the mix.
            result = pick_partial_weighted(
                tb, idx, weights, representation, requested_proof,
                progress=progress, cancel=cancel,
            )
            proof_assessment = assess_proof_coverage(requested_proof, result.coverage_percent)
            if not proof_assessment.passed:
                result = pick_partial_weighted_soft(
                    tb, idx, weights, representation, requested_proof,
                    progress=progress, cancel=cancel,
                )
        else:
            result = pick_partial(
                tb, idx, chains, representation, requested_proof,
                progress=progress, cancel=cancel,
            )
        proof_assessment = assess_proof_coverage(requested_proof, result.coverage_percent)
        mix_assessment = assess_mix(result.chain_bytes, weights)
        if not proof_assessment.passed:
            raise RuntimeError(
                "requested proof coverage could not be satisfied within accepted proof tolerance: "
                f"coverage={_assessment_dict(proof_assessment)}, mix={_assessment_dict(mix_assessment)}, "
                f"picker={result.diagnostics}." + _alignment_context(mode, alignment_notes)
            )
    elif weights:
        # Exact percentage quotas remain the first attempt because they provide
        # the cleanest requested mix.  Crosschain percentages are advisory,
        # however: if exact quotas block reconstruction, let the selected chains
        # cover each other's unavailable bytes while steering as near the request
        # as the available corpus permits.
        result = pick_weighted(tb, idx, weights, representation, True, progress=progress, cancel=cancel)
        if not result.exact or result.covered_bytes != len(tb):
            result = pick_weighted_soft(
                tb, idx, weights, representation,
                progress=progress, cancel=cancel,
            )
        mix_assessment = assess_mix(result.chain_bytes, weights)
        if result.covered_bytes != len(tb):
            raise RuntimeError(
                "target cannot be fully reconstructed from the selected/aligned crosschain corpus: "
                f"mix={_assessment_dict(mix_assessment)}, picker={result.diagnostics}."
                + _alignment_context(mode, alignment_notes)
            )
        proof_assessment = assess_proof_coverage(100.0, result.coverage_percent)
    else:
        result = pick_fewest_chunks(tb, idx, chains, representation, progress=progress, cancel=cancel)
        if not result.exact:
            raise RuntimeError(
                f"target cannot be fully reconstructed from selected corpus: {result.diagnostics}."
                + _alignment_context(mode, alignment_notes)
            )
        proof_assessment = assess_proof_coverage(100.0, result.coverage_percent)
        mix_assessment = assess_mix(result.chain_bytes, None)

    relation_grade = alignment_notes.get("relation_grade") or relation_grade_for_mode(mode)
    if relation_grade != "VERIFIED-EXPLICIT":
        result.segments = [replace(seg, relation_grade=relation_grade) for seg in result.segments]

    is_partial_request = requested_proof is not None and requested_proof < 100
    notes = {
        "picker": result.diagnostics,
        "chain_bytes": result.chain_bytes,
        "alignment": alignment_notes,
        "acceptance": {
            "crosschain_mix_policy": "nearest-feasible-no-hard-tolerance",
            "proof_under_tolerance_points": 1.0,
            "proof_over_tolerance_points": 2.0,
            "mix": _assessment_dict(mix_assessment),
            "proof": _assessment_dict(proof_assessment),
        },
    }
    m = RecipeManifest(
        "BNP2" if is_partial_request else "BNPM",
        1,
        mode,
        target,
        result.segments,
        {k.upper(): float(v) for k, v in (weights or {}).items()},
        strategy,
        datetime.now(timezone.utc).isoformat(),
        notes,
    )
    return m, result.coverage_percent


_OUTPUT_EXTENSIONS = (".bnp", ".bnpm", ".bnp2")

def _normalize_output_base(output_base):
    """Return a base path without a BnP output extension."""
    base = Path(output_base)
    if base.suffix.lower() in _OUTPUT_EXTENSIONS:
        base = base.with_suffix("")
    return base

def output_artifact_paths(output_base):
    """Return all possible BnP artifact paths for one logical output base."""
    base = _normalize_output_base(output_base)
    return [Path(str(base) + ext) for ext in _OUTPUT_EXTENSIONS]

def output_base_is_free(output_base):
    return not any(path.exists() for path in output_artifact_paths(output_base))

def next_available_output_base(output_base):
    """Choose a non-overwriting serial base.

    GUI browse defaults use ``name_bnp``.  The first result keeps that name;
    later results become ``name_bnp2``, ``name_bnp3`` ... whenever any BnP
    output type already occupies the logical base.  This prevents stale files
    from being mistaken for a newly successful build after a later failure.
    """
    base = _normalize_output_base(output_base)
    if output_base_is_free(base):
        return base
    name = base.name
    m = re.fullmatch(r"(.*_bnp)(\d*)", name, re.IGNORECASE)
    if m:
        root = m.group(1)
        current = int(m.group(2) or "1")
        serial = max(2, current + 1)
        while True:
            candidate = base.with_name(f"{root}{serial}")
            if output_base_is_free(candidate):
                return candidate
            serial += 1
    serial = 2
    while True:
        candidate = base.with_name(f"{name}{serial}")
        if output_base_is_free(candidate):
            return candidate
        serial += 1


def save_outputs(manifest, coverage, output_base, *, progress=None, cancel=None):
    # Resolve again immediately before publication so CLI callers and a second
    # process cannot silently overwrite an earlier recipe/proof.
    base = next_available_output_base(output_base)
    if manifest.format.upper() == "BNP2":
        bnp2 = Path(str(base) + ".bnp2")
        write_bnp2(manifest, coverage, bnp2, progress=progress, cancel=cancel)
        return [bnp2]
    bnp = Path(str(base) + ".bnp")
    bnpm = Path(str(base) + ".bnpm")
    write_bnp(manifest, bnp, progress=progress, cancel=cancel)
    write_bnpm(manifest, bnpm, progress=progress, cancel=cancel)
    return [bnp, bnpm]
