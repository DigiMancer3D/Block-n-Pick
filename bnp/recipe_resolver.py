from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .container_format import detect_container, verify_container_stream
from .model import RecipeManifest
from .recipe_io import parse_bnp, read_machine
from .representations import represent_segment_source
from .safe_notation import decode_bnp_safe


@dataclass
class SourceResolution:
    chain: str
    network: str
    block_hash: str
    source_kind: str
    relation_id: str | None
    embedded_material_ok: bool
    corpus_state: str
    verified: bool | None
    segments: int = 0
    represented_bytes: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RecipeResolutionReport:
    artifact_path: str
    detected_kind: str
    declared_mode: str
    representation: str
    target_safe_name: str
    target_filename: str
    target_raw_size: int
    target_representation_size: int
    segment_count: int
    unique_source_count: int
    coverage_percent: float
    proof_class: str | None
    full_recipe: bool
    structurally_valid: bool
    constructible_offline: bool
    container_integrity_ok: bool | None
    corpus_sources_found: int
    corpus_sources_verified: int
    corpus_sources_unverified: int
    literal_only_sources: int
    deferred_mode: bool
    source_resolutions: list[SourceResolution] = field(default_factory=list)
    covered_ranges: list[list[int]] = field(default_factory=list)
    hole_ranges: list[list[int]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    machine_meta: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.structurally_valid and self.constructible_offline and not self.errors

    def to_dict(self, *, include_sources: bool = True) -> dict[str, Any]:
        out = asdict(self)
        out["ok"] = self.ok
        if not include_sources:
            out.pop("source_resolutions", None)
        return out


DEFERRED_MODES = {
    "LOCKED_LINK", "EPOCH_LINK", "TX_LINKED", "USER_PICKED",
    "BINARY_CROSSCHAIN", "TRINARY_CROSSCHAIN", "GENERIC_CROSSCHAIN",
}


def load_recipe_artifact(path: str | Path) -> tuple[str, RecipeManifest, dict[str, Any]]:
    """Load a BnP artifact by content, not by filename extension.

    Binary BNPM/BNP2 magic always wins.  Otherwise the file is parsed as the
    human-readable .bnp language.  This preserves the passive anti-mislabeling
    behavior: renaming a BNPM/BNP2 does not change its detected type.
    """
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(p)
    with p.open("rb") as f:
        prefix = f.read(16)
    kind = detect_container(prefix)
    if kind:
        detected, manifest, payload = read_machine(p)
        return detected, manifest, payload
    manifest = parse_bnp(p)
    return "BNP", manifest, {}


def _source_key(segment):
    return (
        segment.chain.upper(), segment.network.lower(), segment.block_hash.lower(),
        getattr(segment, "source_kind", "BLOCK_HASH"),
        getattr(segment, "tx_raw_hash", None), getattr(segment, "relation_id", None),
    )


def _merge_ranges(ranges: list[tuple[int, int]], total: int) -> tuple[list[list[int]], list[list[int]]]:
    if total < 0:
        raise ValueError("target representation size cannot be negative")
    if not ranges:
        return [], ([[0, total]] if total else [])
    ranges = sorted(ranges)
    merged: list[list[int]] = []
    for start, end in ranges:
        if start < 0 or end < start or end > total:
            raise ValueError(f"target range [{start},{end}) is outside representation size {total}")
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    holes: list[list[int]] = []
    cursor = 0
    for start, end in merged:
        if start > cursor:
            holes.append([cursor, start])
        cursor = max(cursor, end)
    if cursor < total:
        holes.append([cursor, total])
    return merged, holes


def resolve_recipe(path: str | Path, corpus=None, *, include_sources: bool = True) -> tuple[RecipeManifest, RecipeResolutionReport, dict[str, Any]]:
    p = Path(path)
    errors: list[str] = []
    warnings: list[str] = []
    machine_meta: dict[str, Any] = {}
    kind, manifest, payload = load_recipe_artifact(p)

    integrity_ok: bool | None = None
    if kind in {"BNPM", "BNP2"}:
        try:
            machine_meta = verify_container_stream(p)
            integrity_ok = True
        except Exception as exc:
            integrity_ok = False
            errors.append(f"container integrity failure: {exc}")

    suffix = p.suffix.lower()
    expected_suffix = {"BNP": ".bnp", "BNPM": ".bnpm", "BNP2": ".bnp2"}[kind]
    if suffix != expected_suffix:
        warnings.append(f"filename extension {suffix or '(none)'} does not match detected {kind} content ({expected_suffix})")

    total = int(manifest.target.representation_size)
    ranges: list[tuple[int, int]] = []
    unique: dict[tuple, dict[str, Any]] = {}
    structural = True

    seg_list = manifest.segments
    if all(int(seg_list[i-1].target_offset) <= int(seg_list[i].target_offset) for i in range(1, len(seg_list))):
        sorted_segments = seg_list
    else:
        sorted_segments = sorted(seg_list, key=lambda s: (int(s.target_offset), int(s.source_offset)))
    previous_end = 0
    full_kind = kind in {"BNP", "BNPM"}

    for idx, seg in enumerate(sorted_segments):
        try:
            source = represent_segment_source(seg)
            start = int(seg.source_offset)
            end = start + int(seg.length)
            if start < 0 or int(seg.length) <= 0 or end > len(source):
                raise ValueError(
                    f"source range [{start},{end}) exceeds represented source length {len(source)}"
                )
            tstart = int(seg.target_offset)
            tend = tstart + int(seg.length)
            if tstart < 0 or tend > total:
                raise ValueError(f"target range [{tstart},{tend}) exceeds representation size {total}")
            if full_kind:
                if tstart != previous_end:
                    raise ValueError(
                        f"full recipe target offsets are not contiguous at segment {idx}: expected {previous_end}, got {tstart}"
                    )
                previous_end = tend
            elif idx and tstart < previous_end:
                raise ValueError(f"proof segments overlap at target offset {tstart}")
            else:
                previous_end = max(previous_end, tend)
            ranges.append((tstart, tend))
            key = _source_key(seg)
            item = unique.setdefault(key, {"segment": seg, "segments": 0, "bytes": 0, "embedded_ok": True})
            item["segments"] += 1
            item["bytes"] += int(seg.length)
        except Exception as exc:
            structural = False
            errors.append(f"segment {idx}: {exc}")

    if full_kind and previous_end != total:
        structural = False
        errors.append(f"full recipe ends at representation byte {previous_end}, expected {total}")

    try:
        covered, holes = _merge_ranges(ranges, total)
    except Exception as exc:
        covered, holes = [], []
        structural = False
        errors.append(str(exc))

    covered_bytes = sum(end - start for start, end in covered)
    coverage = 100.0 * covered_bytes / total if total else 100.0
    claimed_coverage = payload.get("coverage_percent") if kind == "BNP2" else None
    if claimed_coverage is not None and abs(float(claimed_coverage) - coverage) > 1e-5:
        warnings.append(
            f"BNP2 claims {float(claimed_coverage):.8f}% coverage but resolved ranges cover {coverage:.8f}%"
        )

    source_reports: list[SourceResolution] = []
    found = verified = unverified = literal_only = 0
    constructible = structural
    for item in unique.values():
        seg = item["segment"]
        embedded_ok = True
        try:
            represent_segment_source(seg)
        except Exception:
            embedded_ok = False
            constructible = False
        record = corpus.resolve(seg.chain, seg.network, seg.block_hash) if corpus is not None else None
        if record is None:
            state = "LITERAL-ONLY"
            v = None
            literal_only += 1
        elif record.verified:
            state = "CORPUS-VERIFIED"
            v = True
            found += 1
            verified += 1
        else:
            state = "CORPUS-UNVERIFIED"
            v = False
            found += 1
            unverified += 1
        source_reports.append(SourceResolution(
            chain=seg.chain, network=seg.network, block_hash=seg.block_hash,
            source_kind=getattr(seg, "source_kind", "BLOCK_HASH"),
            relation_id=getattr(seg, "relation_id", None), embedded_material_ok=embedded_ok,
            corpus_state=state, verified=v, segments=item["segments"], represented_bytes=item["bytes"],
        ))

    if corpus is None:
        warnings.append("No local corpus was supplied; source bytes are resolved from literal material embedded in the recipe.")
    elif literal_only:
        warnings.append(
            f"{literal_only} unique source(s) are not present in the local corpus; they remain constructible from literal recipe material but are not locally corroborated."
        )
    if manifest.mode.upper() in DEFERRED_MODES:
        warnings.append(
            f"{manifest.mode.upper()} is a deferred/legacy experimental selector mode in Alpha.6; the recipe remains readable/buildable for compatibility."
        )

    filename = Path(decode_bnp_safe(manifest.target.safe_name)).name or "bnp-output.bin"
    report = RecipeResolutionReport(
        artifact_path=str(p), detected_kind=kind, declared_mode=manifest.mode.upper(),
        representation=manifest.target.representation.upper(), target_safe_name=manifest.target.safe_name,
        target_filename=filename, target_raw_size=int(manifest.target.raw_size),
        target_representation_size=total, segment_count=len(manifest.segments),
        unique_source_count=len(unique), coverage_percent=coverage,
        proof_class=payload.get("proof_class") if kind == "BNP2" else None,
        full_recipe=full_kind, structurally_valid=structural,
        constructible_offline=constructible, container_integrity_ok=integrity_ok,
        corpus_sources_found=found, corpus_sources_verified=verified,
        corpus_sources_unverified=unverified, literal_only_sources=literal_only,
        deferred_mode=manifest.mode.upper() in DEFERRED_MODES,
        source_resolutions=source_reports if include_sources else [], covered_ranges=covered,
        hole_ranges=holes, warnings=warnings, errors=errors, machine_meta=machine_meta,
    )
    return manifest, report, payload
