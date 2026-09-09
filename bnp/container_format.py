from __future__ import annotations
import hashlib, json, os, struct, tempfile, zlib
from pathlib import Path
from typing import Any, Iterable

MAGICS={"BNPM": b"BNPM\x00\x01", "BNP2": b"BNP2\x00\x01"}
CODEC_ZLIB_JSON=1
STREAM_COMPRESSION_LEVEL=6
class ContainerIntegrityError(ValueError): pass


def _canonical_json(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _is_cancelled(cancel) -> bool:
    return bool(cancel is not None and cancel.is_set())


def pack_container(kind: str, payload: dict[str, Any], path: str | Path) -> None:
    """Compatibility in-memory writer retained for small payloads/tests."""
    kind=kind.upper()
    if kind not in MAGICS: raise ValueError(f"unknown container kind {kind}")
    magic=MAGICS[kind]; packed=zlib.compress(_canonical_json(payload), 9); length=struct.pack(">Q", len(packed))
    payload_hash=hashlib.sha256(packed).digest(); prefix=magic+bytes([CODEC_ZLIB_JSON])+length+packed+payload_hash
    Path(path).write_bytes(prefix+hashlib.sha256(prefix).digest())


def pack_container_stream(
    kind: str,
    json_chunks: Iterable[bytes],
    path: str | Path,
    *,
    cancel=None,
    compression_level: int = STREAM_COMPRESSION_LEVEL,
    final_progress=None,
) -> None:
    """Write a BNPM/BNP2 without materializing JSON or compressed payload in RAM.

    The on-disk format remains byte-layout compatible with v1: magic, codec, packed
    length, zlib JSON, payload SHA-256, container SHA-256.  A temporary compressed
    payload is required because the packed length occurs before the payload.
    """
    kind = kind.upper()
    if kind not in MAGICS:
        raise ValueError(f"unknown container kind {kind}")
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload_tmp = None
    part = out.with_name(out.name + ".part")
    try:
        fd, payload_name = tempfile.mkstemp(prefix=out.name + ".payload-", suffix=".tmp", dir=out.parent)
        os.close(fd)
        payload_tmp = Path(payload_name)
        compressor = zlib.compressobj(int(compression_level))
        packed_hash = hashlib.sha256()
        packed_len = 0
        with payload_tmp.open("wb") as pf:
            for raw in json_chunks:
                if _is_cancelled(cancel):
                    from .picker import BuildCancelled
                    raise BuildCancelled("build cancelled during machine-manifest packing")
                if not raw:
                    continue
                chunk = compressor.compress(raw)
                if chunk:
                    pf.write(chunk); packed_hash.update(chunk); packed_len += len(chunk)
            tail = compressor.flush()
            if tail:
                pf.write(tail); packed_hash.update(tail); packed_len += len(tail)
            pf.flush()
            os.fsync(pf.fileno())

        magic = MAGICS[kind]
        header = magic + bytes([CODEC_ZLIB_JSON]) + struct.pack(">Q", packed_len)
        container_hash = hashlib.sha256()
        copied = 0
        with part.open("wb") as f, payload_tmp.open("rb") as pf:
            f.write(header); container_hash.update(header)
            while True:
                if _is_cancelled(cancel):
                    from .picker import BuildCancelled
                    raise BuildCancelled("build cancelled while finalizing machine manifest")
                data = pf.read(1024 * 1024)
                if not data: break
                f.write(data); container_hash.update(data); copied += len(data)
                if final_progress:
                    final_progress(copied, max(1, packed_len))
            ph = packed_hash.digest()
            f.write(ph); container_hash.update(ph)
            f.write(container_hash.digest())
            f.flush(); os.fsync(f.fileno())
        os.replace(part, out)
    finally:
        if part.exists():
            try: part.unlink()
            except OSError: pass
        if payload_tmp is not None and payload_tmp.exists():
            try: payload_tmp.unlink()
            except OSError: pass


def detect_container(data: bytes) -> str | None:
    return next((k for k,m in MAGICS.items() if data.startswith(m)), None)


def verify_container_stream(path: str | Path) -> dict[str, Any]:
    """Verify binary framing and both integrity seals without decompression."""
    p=Path(path)
    size=p.stat().st_size
    with p.open("rb") as f:
        prefix=f.read(max(len(m) for m in MAGICS.values()))
        kind=detect_container(prefix)
        if not kind: raise ValueError("not a BNPM/BNP2 container")
        magic=MAGICS[kind]
        f.seek(0)
        header=f.read(len(magic)+1+8)
        if len(header) != len(magic)+9: raise ContainerIntegrityError("truncated container")
        if not header.startswith(magic): raise ContainerIntegrityError("container magic mismatch")
        codec=header[len(magic)]
        if codec != CODEC_ZLIB_JSON: raise ValueError(f"unsupported container codec {codec}")
        packed_len=struct.unpack(">Q", header[len(magic)+1:])[0]
        expected=len(header)+packed_len+64
        if size != expected: raise ContainerIntegrityError("container length mismatch")
        payload_hash=hashlib.sha256(); container_hash=hashlib.sha256(); container_hash.update(header)
        remain=packed_len
        while remain:
            data=f.read(min(1024*1024,remain))
            if not data: raise ContainerIntegrityError("truncated payload")
            payload_hash.update(data); container_hash.update(data); remain-=len(data)
        ph=f.read(32); ch=f.read(32)
        if payload_hash.digest()!=ph: raise ContainerIntegrityError("payload SHA-256 mismatch")
        container_hash.update(ph)
        if container_hash.digest()!=ch: raise ContainerIntegrityError("container SHA-256 mismatch")
        return {"kind":kind,"packed_bytes":packed_len,"file_bytes":size,"payload_sha256":ph.hex(),"container_sha256":ch.hex()}


def unpack_container(path: str | Path) -> tuple[str, dict[str, Any]]:
    data=Path(path).read_bytes(); kind=detect_container(data)
    if not kind: raise ValueError("not a BNPM/BNP2 container")
    magic=MAGICS[kind]; header_len=len(magic)+1+8
    if len(data) < header_len+64: raise ContainerIntegrityError("truncated container")
    codec=data[len(magic)]
    if codec != CODEC_ZLIB_JSON: raise ValueError(f"unsupported container codec {codec}")
    packed_len=struct.unpack(">Q", data[len(magic)+1:header_len])[0]; end=header_len+packed_len
    if len(data) != end+64: raise ContainerIntegrityError("container length mismatch")
    packed=data[header_len:end]; ph=data[end:end+32]; ch=data[end+32:end+64]
    if hashlib.sha256(packed).digest() != ph: raise ContainerIntegrityError("payload SHA-256 mismatch")
    if hashlib.sha256(data[:end+32]).digest() != ch: raise ContainerIntegrityError("container SHA-256 mismatch")
    try: payload=json.loads(zlib.decompress(packed).decode("utf-8"))
    except Exception as exc: raise ContainerIntegrityError(f"payload decode failure: {exc}") from exc
    if payload.get("format") != kind: raise ContainerIntegrityError("internal format does not match binary magic")
    return kind, payload


def inspect_container(path: str | Path) -> dict[str, Any]:
    kind,payload=unpack_container(path)
    return {"kind":kind,"version":payload.get("version"),
            "coverage_percent":payload.get("coverage_percent") if kind=="BNP2" else None,
            "proof_class":payload.get("proof_class") if kind=="BNP2" else None,
            "target":payload.get("target",{})}
