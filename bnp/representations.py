from __future__ import annotations
import base64

TX_LINKED_SOURCE_KIND = "TX_RAW_PLUS_ORIGIN_BLOCK"


def block_hash_bytes(block_hash: str) -> bytes:
    h=block_hash.strip().lower()
    if len(h) != 64: raise ValueError("expected 64-character hash")
    return bytes.fromhex(h)


def _hash64(value: str, label: str) -> str:
    h=str(value).strip().lower()
    if len(h) != 64:
        raise ValueError(f"{label} must be 64 hexadecimal characters")
    bytes.fromhex(h)
    return h


def _raw_tx_hex(value: str, label: str = "tx_raw_hash") -> str:
    """Normalize serialized transaction hex used as TX-linked source material.

    ``tx_raw_hash`` is retained as the historical external field name, but the
    value is the complete raw transaction serialization in hexadecimal, not a
    fixed 32-byte digest.  Legacy 64-hex Alpha.5-r1 values remain accepted.
    """
    h=str(value).strip().lower()
    if not h:
        raise ValueError(f"{label} cannot be empty")
    if len(h) % 2:
        raise ValueError(f"{label} must contain an even number of hexadecimal characters")
    try:
        bytes.fromhex(h)
    except ValueError as exc:
        raise ValueError(f"{label} must contain only hexadecimal characters") from exc
    return h


def tx_link_material_bytes(tx_raw_hash: str, origin_block_hash: str) -> bytes:
    """Return raw transaction bytes followed by the origin block hash bytes.

    Canonical material:
        serialized transaction bytes || 32-byte origin block hash

    The transaction half is intentionally variable-length.
    """
    txh=_raw_tx_hex(tx_raw_hash,"tx_raw_hash")
    bh=_hash64(origin_block_hash,"origin_block_hash")
    return bytes.fromhex(txh) + bytes.fromhex(bh)


def represent_bytes(raw: bytes, representation: str) -> bytes:
    rep=representation.upper()
    if rep == "RAW": return bytes(raw)
    if rep == "B64": return base64.b64encode(bytes(raw))
    raise ValueError(f"unsupported representation: {representation}")


def represent_hash(block_hash: str, representation: str) -> bytes:
    return represent_bytes(block_hash_bytes(block_hash), representation)


def represent_record(record, representation: str) -> bytes:
    if getattr(record,"source_kind","BLOCK_HASH") == TX_LINKED_SOURCE_KIND:
        tx_raw_hash=getattr(record,"tx_raw_hash",None)
        if not tx_raw_hash:
            raise ValueError("TX-linked source record is missing tx_raw_hash")
        return represent_bytes(tx_link_material_bytes(tx_raw_hash, record.block_hash), representation)
    return represent_hash(record.block_hash, representation)


def represent_segment_source(segment) -> bytes:
    if getattr(segment,"source_kind","BLOCK_HASH") == TX_LINKED_SOURCE_KIND:
        tx_raw_hash=getattr(segment,"tx_raw_hash",None)
        if not tx_raw_hash:
            raise ValueError("TX-linked recipe segment is missing tx_raw_hash")
        return represent_bytes(tx_link_material_bytes(tx_raw_hash, segment.block_hash), segment.representation)
    return represent_hash(segment.block_hash, segment.representation)


def represent_target(raw: bytes, representation: str) -> bytes:
    return represent_bytes(raw, representation)
