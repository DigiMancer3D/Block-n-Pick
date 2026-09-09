from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Iterator

from .recipe_resolver import resolve_recipe
from .representations import represent_segment_source


STREAM_BACKEND = "segment-stream-v2"
IO_CHUNK = 1024 * 1024


@dataclass
class MineResult:
    recipe: str
    kind: str
    action: str
    success: bool
    full_output: str | None = None
    partial_representation_output: str | None = None
    coverage_map_output: str | None = None
    coverage_percent: float = 0.0
    sha256_raw_ok: bool | None = None
    sha256_b64_ok: bool | None = None
    post_write_sha256_ok: bool | None = None
    bytes_written: int = 0
    warnings: list[str] = field(default_factory=list)
    resolution: dict[str, Any] = field(default_factory=dict)
    construction_backend: str = STREAM_BACKEND
    source_cache_entries: int = 0
    source_cache_bytes: int = 0
    stream_peak_buffer_bytes: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _cancelled(cancel) -> bool:
    return bool(cancel is not None and cancel.is_set())


def _emit(progress, stage: str, done: int, total: int, detail: str = ""):
    if progress:
        progress(stage, int(done), int(total), detail)


def _unique_output_path(path: Path) -> Path:
    if not path.exists() and not Path(str(path) + ".part").exists():
        return path
    stem, suffix = path.stem, path.suffix
    root = stem if stem.endswith("_mined") else stem + "_mined"
    candidate = path.with_name(root + suffix)
    if not candidate.exists() and not Path(str(candidate) + ".part").exists():
        return candidate
    serial = 2
    while True:
        candidate = path.with_name(f"{root}{serial}{suffix}")
        if not candidate.exists() and not Path(str(candidate) + ".part").exists():
            return candidate
        serial += 1


def _source_cache_key(seg) -> tuple:
    return (
        getattr(seg, "source_kind", "BLOCK_HASH"),
        getattr(seg, "tx_raw_hash", None),
        seg.block_hash,
        seg.representation,
    )


def _sorted_segments(manifest):
    segs = manifest.segments
    if all(segs[i - 1].target_offset <= segs[i].target_offset for i in range(1, len(segs))):
        return segs
    return sorted(segs, key=lambda s: (s.target_offset, s.source_offset))


class _FragmentStream:
    """Resolve literal source material once per unique source and yield fragments.

    Alpha.6 repeatedly decoded the same 32-byte block hash for every segment. Large
    recipes commonly contain hundreds of thousands or millions of segments but
    only hundreds/thousands of unique sources. Alpha.7 interns represented source
    bytes for the duration of one Mine-Build/Verify job.
    """

    def __init__(self, manifest, *, progress=None, cancel=None):
        self.manifest = manifest
        self.progress = progress
        self.cancel = cancel
        self.cache: dict[tuple, bytes] = {}
        self.cache_bytes = 0
        self.peak_fragment = 0

    def __iter__(self) -> Iterator[tuple[Any, bytes]]:
        segs = _sorted_segments(self.manifest)
        total = len(segs)
        expected = 0
        for i, seg in enumerate(segs, 1):
            if _cancelled(self.cancel):
                raise RuntimeError("Mine-Build cancelled")
            if int(seg.target_offset) != expected:
                raise ValueError(
                    f"full recipe is non-contiguous at target offset {seg.target_offset}; expected {expected}"
                )
            key = _source_cache_key(seg)
            source = self.cache.get(key)
            if source is None:
                source = represent_segment_source(seg)
                self.cache[key] = source
                self.cache_bytes += len(source)
            start = int(seg.source_offset)
            end = start + int(seg.length)
            frag = source[start:end]
            if len(frag) != int(seg.length):
                raise ValueError("recipe segment range exceeds literal source material")
            self.peak_fragment = max(self.peak_fragment, len(frag))
            yield seg, frag
            expected += int(seg.length)
            if i == total or i % 25000 == 0:
                _emit(self.progress, "mine_extract", i, max(1, total), "streaming recipe fragments")


class _PartialFragmentStream(_FragmentStream):
    def __iter__(self) -> Iterator[tuple[Any, bytes]]:
        segs = _sorted_segments(self.manifest)
        total = len(segs)
        previous_end = 0
        for i, seg in enumerate(segs, 1):
            if _cancelled(self.cancel):
                raise RuntimeError("Mine-Build cancelled")
            if int(seg.target_offset) < previous_end:
                raise ValueError(f"proof segments overlap at target offset {seg.target_offset}")
            key = _source_cache_key(seg)
            source = self.cache.get(key)
            if source is None:
                source = represent_segment_source(seg)
                self.cache[key] = source
                self.cache_bytes += len(source)
            start = int(seg.source_offset)
            end = start + int(seg.length)
            frag = source[start:end]
            if len(frag) != int(seg.length):
                raise ValueError("recipe segment range exceeds literal source material")
            self.peak_fragment = max(self.peak_fragment, len(frag))
            yield seg, frag
            previous_end = int(seg.target_offset) + int(seg.length)
            if i == total or i % 25000 == 0:
                _emit(self.progress, "mine_extract", i, max(1, total), "streaming proof fragments")


def _update_b64_hash_from_raw(b64_hash, carry: bytearray, raw: bytes) -> int:
    carry.extend(raw)
    usable = (len(carry) // 3) * 3
    if not usable:
        return len(carry)
    data = bytes(carry[:usable])
    del carry[:usable]
    b64_hash.update(base64.b64encode(data))
    return max(len(data), len(carry))


def _finish_b64_hash_from_raw(b64_hash, carry: bytearray) -> int:
    if carry:
        data = bytes(carry)
        b64_hash.update(base64.b64encode(data))
        carry.clear()
        return len(data)
    return 0


def _stream_full_representation(manifest, *, writer: BinaryIO | None, progress=None, cancel=None):
    """Reconstruct a full recipe without materializing the target in RAM.

    Returns raw/b64 digests, raw byte count and stream/cache diagnostics. RAW
    recipes are written fragment-by-fragment. B64 recipes are hashed as their
    represented stream and decoded in 4-character groups directly to the sink.
    """
    raw_hash = hashlib.sha256()
    b64_hash = hashlib.sha256()
    peak = 0
    raw_count = 0
    stream = _FragmentStream(manifest, progress=progress, cancel=cancel)
    rep = manifest.target.representation.upper()

    if rep == "RAW":
        carry = bytearray()
        for _seg, frag in stream:
            if writer is not None:
                writer.write(frag)
            raw_hash.update(frag)
            raw_count += len(frag)
            peak = max(peak, len(frag), _update_b64_hash_from_raw(b64_hash, carry, frag))
        peak = max(peak, _finish_b64_hash_from_raw(b64_hash, carry))
    elif rep == "B64":
        b64_carry = bytearray()
        saw_padding = False
        for _seg, frag in stream:
            if saw_padding and frag:
                raise ValueError("Base64 recipe contains data after padded terminal quartet")
            b64_hash.update(frag)
            b64_carry.extend(frag)
            usable = (len(b64_carry) // 4) * 4
            if usable:
                encoded = bytes(b64_carry[:usable])
                del b64_carry[:usable]
                try:
                    raw = base64.b64decode(encoded, validate=True)
                except Exception as exc:
                    raise ValueError(f"invalid Base64 representation in recipe: {exc}") from exc
                if b"=" in encoded:
                    saw_padding = True
                if writer is not None:
                    writer.write(raw)
                raw_hash.update(raw)
                raw_count += len(raw)
                peak = max(peak, len(encoded), len(raw), len(b64_carry))
        if b64_carry:
            raise ValueError("Base64 representation length is not a multiple of 4")
    else:
        raise ValueError(f"unsupported Mine-Build representation {manifest.target.representation}")

    return {
        "sha256_raw": raw_hash.hexdigest(),
        "sha256_b64": b64_hash.hexdigest(),
        "raw_bytes": raw_count,
        "source_cache_entries": len(stream.cache),
        "source_cache_bytes": stream.cache_bytes,
        "stream_peak_buffer_bytes": max(peak, stream.peak_fragment),
    }


def _hash_file_dual(path: Path, *, cancel=None):
    raw_hash = hashlib.sha256()
    b64_hash = hashlib.sha256()
    carry = bytearray()
    peak = 0
    total = 0
    with path.open("rb") as f:
        while True:
            if _cancelled(cancel):
                raise RuntimeError("Mine-Build cancelled")
            data = f.read(IO_CHUNK)
            if not data:
                break
            raw_hash.update(data)
            total += len(data)
            peak = max(peak, len(data), _update_b64_hash_from_raw(b64_hash, carry, data))
    peak = max(peak, _finish_b64_hash_from_raw(b64_hash, carry))
    return raw_hash.hexdigest(), b64_hash.hexdigest(), total, peak


def _identity_ok(manifest, hashes) -> tuple[bool | None, bool | None]:
    raw_ok = hashes["sha256_raw"] == manifest.target.sha256_raw if manifest.target.sha256_raw else None
    b64_ok = hashes["sha256_b64"] == manifest.target.sha256_b64 if manifest.target.sha256_b64 else None
    return raw_ok, b64_ok


# Retained as a compatibility helper for older external callers/tests. Alpha.7
# Mine-Build and Verify no longer use this full-target bytearray path.
def _representation_bytes(manifest, *, partial: bool, progress=None, cancel=None) -> bytearray:
    total = int(manifest.target.representation_size)
    out = bytearray(total if partial else 0)
    stream = _PartialFragmentStream(manifest, progress=progress, cancel=cancel) if partial else _FragmentStream(manifest, progress=progress, cancel=cancel)
    expected = 0
    for seg, frag in stream:
        if partial:
            out[int(seg.target_offset):int(seg.target_offset) + int(seg.length)] = frag
        else:
            if int(seg.target_offset) != expected:
                raise ValueError(f"full recipe is non-contiguous at target offset {seg.target_offset}; expected {expected}")
            out.extend(frag)
            expected += int(seg.length)
    return out


def verify_recipe(recipe: str | Path, *, corpus=None, progress=None, cancel=None) -> MineResult:
    manifest, resolution, _payload = resolve_recipe(recipe, corpus, include_sources=False)
    if not resolution.ok:
        return MineResult(
            str(recipe), resolution.detected_kind, "VERIFY", False,
            coverage_percent=resolution.coverage_percent,
            warnings=[*resolution.warnings, *resolution.errors],
            resolution=resolution.to_dict(include_sources=False),
        )
    if resolution.full_recipe:
        hashes = _stream_full_representation(manifest, writer=None, progress=progress, cancel=cancel)
        raw_ok, b64_ok = _identity_ok(manifest, hashes)
        success = raw_ok is not False and b64_ok is not False
        return MineResult(
            str(recipe), resolution.detected_kind, "VERIFY", success,
            coverage_percent=resolution.coverage_percent,
            sha256_raw_ok=raw_ok, sha256_b64_ok=b64_ok,
            bytes_written=0, warnings=list(resolution.warnings),
            resolution=resolution.to_dict(include_sources=False),
            source_cache_entries=hashes["source_cache_entries"],
            source_cache_bytes=hashes["source_cache_bytes"],
            stream_peak_buffer_bytes=hashes["stream_peak_buffer_bytes"],
        )

    # Proof verification without the original target validates literal source
    # extraction, range integrity and coverage while keeping memory bounded.
    stream = _PartialFragmentStream(manifest, progress=progress, cancel=cancel)
    for _seg, _frag in stream:
        pass
    warnings = list(resolution.warnings)
    warnings.append("BNP2 is partial proof material; missing target bytes cannot be SHA-256 verified without the original complete object.")
    return MineResult(
        str(recipe), resolution.detected_kind, "VERIFY", True,
        coverage_percent=resolution.coverage_percent, warnings=warnings,
        resolution=resolution.to_dict(include_sources=False),
        source_cache_entries=len(stream.cache), source_cache_bytes=stream.cache_bytes,
        stream_peak_buffer_bytes=stream.peak_fragment,
    )


def mine_recipe(
    recipe: str | Path,
    *,
    corpus=None,
    output_dir: str | Path | None = None,
    output_path: str | Path | None = None,
    verify_after_write: bool = True,
    progress=None,
    cancel=None,
) -> MineResult:
    recipe = Path(recipe)
    manifest, resolution, payload = resolve_recipe(recipe, corpus, include_sources=False)
    if not resolution.ok:
        raise ValueError("recipe resolution failed: " + "; ".join(resolution.errors or resolution.warnings))

    if resolution.full_recipe:
        if output_path is not None:
            final = _unique_output_path(Path(output_path))
        else:
            directory = Path(output_dir) if output_dir is not None else recipe.parent
            directory.mkdir(parents=True, exist_ok=True)
            final = _unique_output_path(directory / resolution.target_filename)
        final.parent.mkdir(parents=True, exist_ok=True)
        part = Path(str(final) + ".part")
        try:
            _emit(progress, "mine_stream", 0, max(1, len(manifest.segments)), "streaming recipe directly to .part")
            with part.open("wb") as f:
                hashes = _stream_full_representation(manifest, writer=f, progress=progress, cancel=cancel)
                f.flush()
                os.fsync(f.fileno())
            raw_ok, b64_ok = _identity_ok(manifest, hashes)
            if raw_ok is False or b64_ok is False:
                raise ValueError("reconstructed object failed target SHA-256 identity before publication")
            if manifest.target.raw_size and int(hashes["raw_bytes"]) != int(manifest.target.raw_size):
                raise ValueError(
                    f"reconstructed raw size {hashes['raw_bytes']} does not match target {manifest.target.raw_size}"
                )
            _emit(progress, "mine_stream", max(1, len(manifest.segments)), max(1, len(manifest.segments)), "streamed target identity verified")

            post_ok = None
            post_peak = 0
            if verify_after_write:
                _emit(progress, "mine_verify", 0, 1, "streaming .part back through SHA-256 verification")
                post_raw, post_b64, post_size, post_peak = _hash_file_dual(part, cancel=cancel)
                post_ok = (
                    (not manifest.target.sha256_raw or post_raw == manifest.target.sha256_raw)
                    and (not manifest.target.sha256_b64 or post_b64 == manifest.target.sha256_b64)
                    and (not manifest.target.raw_size or post_size == int(manifest.target.raw_size))
                )
                if not post_ok:
                    raise ValueError("post-write Mine-Build verification failed; final file was not published")
                _emit(progress, "mine_verify", 1, 1, "post-write SHA-256 verified")
            os.replace(part, final)
        finally:
            if part.exists():
                try:
                    part.unlink()
                except OSError:
                    pass
        return MineResult(
            str(recipe), resolution.detected_kind,
            "BUILD+VERIFY" if verify_after_write else "BUILD", True,
            full_output=str(final), coverage_percent=100.0,
            sha256_raw_ok=raw_ok, sha256_b64_ok=b64_ok,
            post_write_sha256_ok=post_ok, bytes_written=int(hashes["raw_bytes"]),
            warnings=list(resolution.warnings), resolution=resolution.to_dict(include_sources=False),
            source_cache_entries=hashes["source_cache_entries"],
            source_cache_bytes=hashes["source_cache_bytes"],
            stream_peak_buffer_bytes=max(hashes["stream_peak_buffer_bytes"], post_peak),
        )

    # BNP2: create the represented address space as a sparse/zero-filled file and
    # write only covered fragments. This avoids a representation-sized bytearray.
    directory = Path(output_dir) if output_dir is not None else recipe.parent
    directory.mkdir(parents=True, exist_ok=True)
    stem = Path(resolution.target_filename).name
    rep_suffix = ".partial.raw" if manifest.target.representation.upper() == "RAW" else ".partial.b64rep"
    rep_path = _unique_output_path(directory / (stem + rep_suffix))
    map_path = _unique_output_path(directory / (stem + ".coverage.json"))
    part = Path(str(rep_path) + ".part")
    stream = _PartialFragmentStream(manifest, progress=progress, cancel=cancel)
    try:
        total = int(manifest.target.representation_size)
        with part.open("w+b") as f:
            f.truncate(total)
            for seg, frag in stream:
                if _cancelled(cancel):
                    raise RuntimeError("Mine-Build cancelled")
                f.seek(int(seg.target_offset))
                f.write(frag)
            f.flush()
            os.fsync(f.fileno())
        os.replace(part, rep_path)
    finally:
        if part.exists():
            try:
                part.unlink()
            except OSError:
                pass

    coverage_doc = {
        "format": "BNP-MINE-COVERAGE-MAP",
        "version": 2,
        "construction_backend": STREAM_BACKEND,
        "source_recipe": str(recipe),
        "detected_kind": resolution.detected_kind,
        "target_safe_name": manifest.target.safe_name,
        "target_filename": resolution.target_filename,
        "representation": manifest.target.representation,
        "representation_size": manifest.target.representation_size,
        "coverage_percent": resolution.coverage_percent,
        "claimed_coverage_percent": payload.get("coverage_percent"),
        "covered_ranges": resolution.covered_ranges,
        "hole_ranges": resolution.hole_ranges,
        "zero_fill_semantics": "Bytes outside covered_ranges are 0x00 placeholders and are NOT claimed target data.",
        "sha256_raw_target": manifest.target.sha256_raw,
        "sha256_b64_target": manifest.target.sha256_b64,
        "source_cache_entries": len(stream.cache),
        "source_cache_bytes": stream.cache_bytes,
    }
    map_part = Path(str(map_path) + ".part")
    try:
        map_part.write_text(json.dumps(coverage_doc, indent=2), encoding="utf-8")
        os.replace(map_part, map_path)
    finally:
        if map_part.exists():
            try:
                map_part.unlink()
            except OSError:
                pass
    warnings = list(resolution.warnings)
    warnings.append("BNP2 Mine-Build output is a partial representation plus coverage map; it is not presented as the complete target object.")
    return MineResult(
        str(recipe), resolution.detected_kind, "PARTIAL-BUILD", True,
        partial_representation_output=str(rep_path), coverage_map_output=str(map_path),
        coverage_percent=resolution.coverage_percent,
        bytes_written=int(manifest.target.representation_size), warnings=warnings,
        resolution=resolution.to_dict(include_sources=False),
        source_cache_entries=len(stream.cache), source_cache_bytes=stream.cache_bytes,
        stream_peak_buffer_bytes=stream.peak_fragment,
    )
