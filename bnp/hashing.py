from __future__ import annotations
import base64, hashlib
from dataclasses import dataclass

@dataclass(frozen=True)
class HashBundle:
    raw_size: int
    b64_size: int
    sha256_raw: str
    sha256_b64: str
    canonical_b64: bytes

def sha256_hex(data: bytes) -> str: return hashlib.sha256(data).hexdigest()
def canonical_base64(data: bytes) -> bytes: return base64.b64encode(data)
def hash_bundle(data: bytes) -> HashBundle:
    b64 = canonical_base64(data)
    return HashBundle(len(data), len(b64), sha256_hex(data), sha256_hex(b64), b64)
def decode_base64_flexible(text: str | bytes) -> bytes:
    raw = "".join(text.split()).encode("ascii") if isinstance(text, str) else b"".join(text.split())
    return base64.b64decode(raw, validate=True)
