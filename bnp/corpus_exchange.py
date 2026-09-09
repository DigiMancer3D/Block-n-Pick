from __future__ import annotations

import csv
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .model import BlockRecord, SUPPORTED_CHAINS
from .tx_links import list_relations

HASH_RE = re.compile(r"(?i)(?<![0-9a-f])([0-9a-f]{64})(?![0-9a-f])")
HEIGHT_RE = re.compile(r"(?<!\d)#?(\d{1,12})(?!\d)")

HEIGHT_KEYS = ("height", "block_height", "blockheight", "block_number", "blocknumber", "number")
HASH_KEYS = ("block_hash", "blockhash", "hash", "id")
TIME_KEYS = ("timestamp", "block_timestamp", "block_time", "blocktime", "time", "date", "datetime")
CHAIN_KEYS = ("chain", "ticker", "coin", "currency", "symbol")
NETWORK_KEYS = ("network", "net")


def _first(mapping: dict[str, Any], keys: Iterable[str]):
    lower = {str(k).lower(): v for k, v in mapping.items()}
    for key in keys:
        if key in lower and lower[key] not in (None, ""):
            return lower[key]
    return None


def _parse_timestamp(value: Any) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        n = int(value)
        # Millisecond epoch values occasionally appear in explorer exports.
        return n // 1000 if n > 10_000_000_000 else n
    s = str(value).strip()
    if not s:
        return None
    if re.fullmatch(r"\d+(?:\.\d+)?", s):
        n = int(float(s))
        return n // 1000 if n > 10_000_000_000 else n
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except ValueError:
        return None


def _normalize_chain(value: Any, fallback: str | None) -> str:
    raw = str(value or fallback or "").strip().upper()
    if not raw:
        raise ValueError("chain missing; choose a chain for this explorer/manual import")
    aliases = {
        "BITCOIN": "BTC", "LITECOIN": "LTC", "MONERO": "XMR",
        "BITCOIN CASH": "BCH", "BITCOINCASH": "BCH", "DIGIBYTE": "DGB",
    }
    return aliases.get(raw, raw)


def _row_hash(mapping: dict[str, Any]) -> str | None:
    value = _first(mapping, HASH_KEYS)
    if value is None:
        return None
    s = str(value).strip().lower()
    return s if re.fullmatch(r"[0-9a-f]{64}", s) else None


def _row_height(mapping: dict[str, Any]) -> int | None:
    value = _first(mapping, HEIGHT_KEYS)
    if value is None:
        # Some explorer exports use numeric "id" for height while Esplora uses
        # a 64-hex "id" for block hash.  Accept only a clearly numeric id here.
        raw_id = mapping.get("id")
        if raw_id is not None and re.fullmatch(r"\d+", str(raw_id).strip()):
            value = raw_id
    if value is None:
        return None
    try:
        return int(str(value).replace(",", "").strip())
    except ValueError:
        return None


def _record_from_explorer_mapping(
    mapping: dict[str, Any], *, default_chain: str | None, default_network: str,
    fallback_height: int | None, source: str,
) -> BlockRecord | None:
    block_hash = _row_hash(mapping)
    if block_hash is None:
        return None
    height = _row_height(mapping)
    if height is None:
        height = fallback_height
    if height is None:
        raise ValueError(
            "a block hash had no height; provide Start height for hash-only explorer/manual data"
        )
    chain = _normalize_chain(_first(mapping, CHAIN_KEYS), default_chain)
    network = str(_first(mapping, NETWORK_KEYS) or default_network).strip().lower()
    timestamp = _parse_timestamp(_first(mapping, TIME_KEYS))
    status = str(mapping.get("status") or "canonical").strip().lower()
    branch = int(mapping.get("branch_index", mapping.get("branch", 0)) or 0)
    return BlockRecord(
        chain=chain, network=network, height=height, block_hash=block_hash,
        branch_index=branch, status=status, source=source, verified=False,
        timestamp=timestamp,
    ).normalized()


def _walk_json_rows(obj: Any):
    """Yield mapping-like candidate block rows from common explorer JSON shapes."""
    if isinstance(obj, list):
        for item in obj:
            yield from _walk_json_rows(item)
        return
    if not isinstance(obj, dict):
        return
    if _row_hash(obj) is not None:
        yield obj
        return
    # Common wrappers: blocks/results/data/items/records/result.  Fall back to
    # recursively walking nested dict/list values so copied explorer API results
    # remain useful without writing provider-specific adapters for every site.
    preferred = ("blocks", "results", "data", "items", "records", "result")
    visited = set()
    for key in preferred:
        if key in obj:
            visited.add(key)
            yield from _walk_json_rows(obj[key])
    for key, value in obj.items():
        if key in visited:
            continue
        if isinstance(value, (dict, list)):
            yield from _walk_json_rows(value)


def _records_from_json(
    data: Any, *, default_chain: str | None, default_network: str,
    start_height: int | None, descending: bool, source: str,
) -> list[BlockRecord]:
    rows = list(_walk_json_rows(data))
    if not rows:
        # Chainz/other simple APIs may be saved as a JSON string containing only
        # one block hash.
        if isinstance(data, str) and re.fullmatch(r"[0-9a-fA-F]{64}", data.strip()):
            rows = [{"hash": data.strip()}]
        else:
            raise ValueError("no block hash rows were recognized in this JSON")
    out: list[BlockRecord] = []
    direction = -1 if descending else 1
    fallback = start_height
    for idx, row in enumerate(rows):
        rec = _record_from_explorer_mapping(
            row, default_chain=default_chain, default_network=default_network,
            fallback_height=None if fallback is None else fallback + direction * idx,
            source=source,
        )
        if rec:
            out.append(rec)
    return out


def _records_from_delimited_text(
    text: str, *, default_chain: str | None, default_network: str,
    start_height: int | None, descending: bool, source: str,
) -> list[BlockRecord]:
    out: list[BlockRecord] = []
    pending_hash_only: list[tuple[str, int | None]] = []
    direction = -1 if descending else 1
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("//") or line.startswith(";"):
            continue
        # JSONL copied from APIs is common.
        if line.startswith("{") and line.endswith("}"):
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                obj = None
            if isinstance(obj, dict):
                rec = _record_from_explorer_mapping(
                    obj, default_chain=default_chain, default_network=default_network,
                    fallback_height=None, source=source,
                )
                if rec:
                    out.append(rec)
                    continue
        hm = HASH_RE.search(line)
        if not hm:
            continue
        block_hash = hm.group(1).lower()
        scrubbed = line[:hm.start()] + " " + line[hm.end():]
        height = None
        # Prefer an explicit #height if present, then any remaining integer-like
        # column.  This handles "123 hash", "hash,123", "#123: hash" and copied
        # table rows containing additional text.
        explicit = re.search(r"#(\d{1,12})", scrubbed)
        if explicit:
            height = int(explicit.group(1))
        else:
            nums = HEIGHT_RE.findall(scrubbed)
            if nums:
                height = int(nums[0])
        timestamp = None
        # Parse an ISO-looking timestamp if one survived beside the hash.  For
        # plain copied tables, also accept a second large integer as Unix time.
        iso = re.search(r"\d{4}-\d{2}-\d{2}[T ][0-9:]+(?:\.\d+)?(?:Z|[+-]\d\d:?\d\d)?", line)
        if iso:
            timestamp = _parse_timestamp(iso.group(0).replace(" ", "T", 1))
        elif height is not None:
            ints = [int(x) for x in HEIGHT_RE.findall(scrubbed)]
            for n in ints:
                if n != height and n >= 500_000_000:
                    timestamp = _parse_timestamp(n)
                    break
        if height is None:
            pending_hash_only.append((block_hash, timestamp))
            continue
        out.append(BlockRecord(
            _normalize_chain(None, default_chain), default_network, height, block_hash,
            source=source, verified=False, timestamp=timestamp,
        ).normalized())

    if pending_hash_only:
        if start_height is None:
            raise ValueError(
                f"{len(pending_hash_only)} hash-only line(s) need a Start height; "
                "set Start height and choose ascending/descending before import"
            )
        for idx, (block_hash, timestamp) in enumerate(pending_hash_only):
            out.append(BlockRecord(
                _normalize_chain(None, default_chain), default_network,
                start_height + direction * idx, block_hash,
                source=source, verified=False, timestamp=timestamp,
            ).normalized())
    if not out:
        raise ValueError("no 64-hex block hashes were recognized in this text/list")
    return out


def load_manual_explorer_records(
    path: str | Path, *, default_chain: str | None,
    default_network: str = "mainnet", start_height: int | None = None,
    descending: bool = False,
) -> list[BlockRecord]:
    """Load copied/exported block explorer data as deliberately unverified rows.

    Supported inputs include common JSON block objects (height/hash or Esplora
    height/id, optional timestamp/time), JSON arrays/wrappers, JSONL, CSV-like or
    copied text table rows, and one-hash-per-line lists when Start height is given.
    """
    path = Path(path)
    source = f"manual-explorer:{path.name}"
    text = path.read_text(encoding="utf-8", errors="replace")
    suffix = path.suffix.lower()
    if suffix == ".json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = None
        if data is not None:
            return _dedupe(_records_from_json(
                data, default_chain=default_chain,
                default_network=default_network, start_height=start_height,
                descending=descending, source=source,
            ))
        # Some explorer endpoints return plain text even when the user saves the
        # result with a .json filename.  Fall through to the text/hash parser.
    if suffix in {".jsonl", ".ndjson"}:
        data = [json.loads(line) for line in text.splitlines() if line.strip()]
        return _dedupe(_records_from_json(
            data, default_chain=default_chain, default_network=default_network,
            start_height=start_height, descending=descending, source=source,
        ))
    if suffix in {".csv", ".tsv"}:
        dialect = "excel-tab" if suffix == ".tsv" else "excel"
        try:
            rows = list(csv.DictReader(text.splitlines(), dialect=dialect))
            parsed = []
            direction = -1 if descending else 1
            for idx, row in enumerate(rows):
                rec = _record_from_explorer_mapping(
                    row, default_chain=default_chain, default_network=default_network,
                    fallback_height=None if start_height is None else start_height + direction * idx,
                    source=source,
                )
                if rec:
                    parsed.append(rec)
            if parsed:
                return _dedupe(parsed)
        except (csv.Error, ValueError):
            pass
    return _dedupe(_records_from_delimited_text(
        text, default_chain=default_chain, default_network=default_network,
        start_height=start_height, descending=descending, source=source,
    ))


def _dedupe(records: Iterable[BlockRecord]) -> list[BlockRecord]:
    seen = set(); out = []
    for rec in records:
        key = (rec.chain, rec.network, rec.block_hash)
        if key in seen:
            continue
        seen.add(key); out.append(rec)
    return out


def _record_dict(rec: BlockRecord) -> dict[str, Any]:
    return {
        "chain": rec.chain, "network": rec.network, "height": rec.height,
        "branch_index": rec.branch_index, "block_hash": rec.block_hash,
        "status": rec.status, "source": rec.source, "verified": bool(rec.verified),
        "timestamp": rec.timestamp,
    }


def export_corpus_json(db, path: str | Path, *, chains: Iterable[str] | None = None, network: str = "mainnet") -> int:
    path = Path(path)
    records = list(db.iter_records(chains=chains, network=network, verified_only=False))
    payload = {
        "format": "BNP-CORPUS-EXPORT", "version": 1,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "network": network, "record_count": len(records),
        "blocks": [_record_dict(r) for r in records],
    }
    # Cross-chain relationship groups are included only for a full/all-chain
    # backup.  A single-chain export cannot faithfully contain a multi-chain
    # relationship and therefore deliberately omits them.
    if chains is None:
        payload["tx_links"] = [r.to_dict() for r in list_relations(db)]
        payload["tx_link_count"] = len(payload["tx_links"])
    path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return len(records)


def _compress_heights(values: Iterable[int]) -> str:
    nums = sorted(set(int(v) for v in values))
    if not nums:
        return ""
    parts = []
    start = prev = nums[0]
    for n in nums[1:]:
        if n == prev + 1:
            prev = n
            continue
        parts.append(str(start) if start == prev else f"{start}-{prev}")
        start = prev = n
    parts.append(str(start) if start == prev else f"{start}-{prev}")
    return ",".join(parts)


def export_height_list(db, path: str | Path, *, chains: Iterable[str] | None = None, network: str = "mainnet") -> dict[str, int]:
    path = Path(path)
    selected = [c.upper() for c in chains] if chains else list(SUPPORTED_CHAINS)
    records = list(db.iter_records(chains=selected, network=network, verified_only=False))
    by_chain = {c: [] for c in selected}
    for rec in records:
        if rec.chain in by_chain:
            by_chain[rec.chain].append(rec.height)
    lines = ["# BNP-BLOCK-HEIGHT-LIST v1", f"# network={network}"]
    counts = {}
    for chain in selected:
        vals = sorted(set(by_chain[chain])); counts[chain] = len(vals)
        lines.append(f"{chain}:{_compress_heights(vals)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return counts


def hash_stream(db, *, chains: Iterable[str] | None = None, network: str = "mainnet") -> tuple[str, int]:
    selected = [c.upper() for c in chains] if chains else list(SUPPORTED_CHAINS)
    records = list(db.iter_records(chains=selected, network=network, verified_only=False))
    order = {c: i for i, c in enumerate(selected)}
    records.sort(key=lambda r: (order.get(r.chain, 999), r.height, r.branch_index, r.block_hash))
    return "".join(r.block_hash for r in records), len(records)


def export_hash_stream(db, path: str | Path, *, chains: Iterable[str] | None = None, network: str = "mainnet") -> int:
    stream, count = hash_stream(db, chains=chains, network=network)
    Path(path).write_text(stream, encoding="ascii")
    return count


def export_per_chain_hash_streams(db, directory: str | Path, *, network: str = "mainnet") -> dict[str, int]:
    directory = Path(directory); directory.mkdir(parents=True, exist_ok=True)
    out = {}
    for chain in SUPPORTED_CHAINS:
        count = export_hash_stream(db, directory / f"{chain.lower()}_block_hash_stream.txt", chains=[chain], network=network)
        out[chain] = count
    return out


# ---------------------------------------------------------------------------
# Alpha.5 Clean Stream String
# ---------------------------------------------------------------------------

def _encode_zero_run(length: int) -> str:
    """Encode one positive zero run using the BnP o0 additive-nonary joke.

    1..9 are encoded directly (o01..o09).  Larger runs are represented as an
    additive sequence of single decimal digits limited to 1..9 and terminated
    with '+'.  Examples: 10 -> o091+, 18 -> o099+, 27 -> o0999+.
    """
    length = int(length)
    if length <= 0:
        return ""
    if length <= 9:
        return f"o0{length}"
    parts = []
    remaining = length
    while remaining > 9:
        parts.append("9")
        remaining -= 9
    if remaining:
        parts.append(str(remaining))
    return "o0" + "".join(parts) + "+"


def _decode_zero_token(text: str, start: int) -> tuple[int, int]:
    if not text.startswith("o0", start):
        raise ValueError("Clean Stream token must start with o0")
    pos = start + 2
    if pos >= len(text) or text[pos] not in "123456789":
        raise ValueError("Clean Stream o0 token needs a non-zero single-digit run component")
    first = int(text[pos]); pos += 1
    # Multi-component runs always end in '+'.  Because '+' and lowercase 'o'
    # cannot occur in a hexadecimal hash, this remains unambiguous next to raw
    # hash characters.  Without '+', exactly one digit belongs to the token.
    plus = text.find("+", pos)
    if plus != -1:
        candidate = text[pos:plus]
        if candidate and all(ch in "123456789" for ch in candidate):
            return first + sum(int(ch) for ch in candidate), plus + 1
    return first, pos


def clean_hash(hash_text: str) -> str:
    h = str(hash_text).strip().lower()
    if len(h) != 64:
        raise ValueError("Clean Stream operates on 64-character hexadecimal block hashes")
    bytes.fromhex(h)
    leading = len(h) - len(h.lstrip("0"))
    trailing = len(h) - len(h.rstrip("0"))
    # Avoid double-counting the impossible-but-well-defined all-zero hash.
    if leading == 64:
        return _encode_zero_run(64)
    middle_end = 64 - trailing if trailing else 64
    middle = h[leading:middle_end]
    return (_encode_zero_run(leading) if leading else "") + middle + (_encode_zero_run(trailing) if trailing else "")


def clean_stream_from_hashes(hashes: Iterable[str]) -> str:
    return "".join(clean_hash(h) for h in hashes)


def clean_hash_stream(db, *, chains: Iterable[str] | None = None, network: str = "mainnet") -> tuple[str, int]:
    selected = [c.upper() for c in chains] if chains else list(SUPPORTED_CHAINS)
    records = list(db.iter_records(chains=selected, network=network, verified_only=False))
    order = {c: i for i, c in enumerate(selected)}
    records.sort(key=lambda r: (order.get(r.chain, 999), r.height, r.branch_index, r.block_hash))
    return clean_stream_from_hashes(r.block_hash for r in records), len(records)


def export_clean_hash_stream(db, path: str | Path, *, chains: Iterable[str] | None = None, network: str = "mainnet") -> int:
    stream, count = clean_hash_stream(db, chains=chains, network=network)
    Path(path).write_text(stream, encoding="ascii")
    return count


def decode_clean_stream(text: str, *, expected_hashes: int | None = None) -> str:
    """Expand a Clean Stream String back to the original raw hash stream.

    Tokens are recognized only by lowercase ``o0``.  The returned value is the
    original hexadecimal no-separator stream and must be a multiple of 64 chars.
    """
    text = str(text).strip()
    out = []
    pos = 0
    while pos < len(text):
        if text.startswith("o0", pos):
            length, pos = _decode_zero_token(text, pos)
            out.append("0" * length)
            continue
        ch = text[pos]
        if ch not in "0123456789abcdefABCDEF":
            raise ValueError(f"invalid Clean Stream character at offset {pos}: {ch!r}")
        out.append(ch.lower()); pos += 1
    raw = "".join(out)
    if len(raw) % 64:
        raise ValueError("decoded Clean Stream length is not a whole number of 64-character hashes")
    if expected_hashes is not None and len(raw) != int(expected_hashes) * 64:
        raise ValueError("decoded Clean Stream hash count does not match expectation")
    return raw
