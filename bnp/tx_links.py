from __future__ import annotations

from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
import base64
import binascii
import json
import re
from pathlib import Path
from typing import Any, Iterable

from .model import SUPPORTED_CHAINS, BlockRecord
from .representations import TX_LINKED_SOURCE_KIND

TX_EVIDENCE_FORMAT = "BNP-TX-LINK-EVIDENCE"
TX_EVIDENCE_VERSION = 3

VERIFIED_EXPLICIT = "VERIFIED-EXPLICIT"
VERIFIED_PROTOCOL = "VERIFIED-PROTOCOL"
UNVERIFIED_USER_PROOF = "UNVERIFIED-USER-PROOF"
UNVERIFIED_HEURISTIC = "UNVERIFIED-HEURISTIC"
EVIDENCE_GRADES = (
    VERIFIED_EXPLICIT,
    VERIFIED_PROTOCOL,
    UNVERIFIED_USER_PROOF,
    UNVERIFIED_HEURISTIC,
)

_GRADE_STRENGTH = {
    UNVERIFIED_HEURISTIC: 0,
    UNVERIFIED_USER_PROOF: 1,
    VERIFIED_PROTOCOL: 2,
    VERIFIED_EXPLICIT: 3,
}


def _clean(value: Any) -> str | None:
    if value in (None, ""):
        return None
    s = str(value).strip()
    return s or None


def _hash64(value: Any, label: str) -> str | None:
    value = _clean(value)
    if value is None:
        return None
    h = value.lower()
    if len(h) != 64:
        raise ValueError(f"TX-Link {label} must be 64 hexadecimal characters")
    try:
        bytes.fromhex(h)
    except ValueError as exc:
        raise ValueError(f"TX-Link {label} must contain only hexadecimal characters") from exc
    return h


def _raw_tx_hex(value: Any, label: str = "tx_raw_hex") -> str | None:
    """Normalize complete serialized transaction bytes into lowercase hex.

    Explorer/node transports commonly expose raw transaction bytes as a plain
    hexadecimal string.  Whitespace and an optional ``0x`` prefix are tolerated.
    Byte strings and JSON byte arrays are also accepted for imported evidence.
    """
    if value in (None, ""):
        return None
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    if isinstance(value, list) and all(isinstance(x, int) and 0 <= x <= 255 for x in value):
        return bytes(value).hex()
    text = str(value).strip()
    if text.lower().startswith("0x"):
        text = text[2:]
    h = re.sub(r"\s+", "", text).lower()
    if not h:
        return None
    if len(h) % 2:
        raise ValueError(f"TX-Link {label} must contain an even number of hexadecimal characters")
    try:
        bytes.fromhex(h)
    except ValueError as exc:
        raise ValueError(f"TX-Link {label} must contain only hexadecimal characters") from exc
    return h


def _raw_tx_base64(value: Any, label: str = "tx_raw_base64") -> str | None:
    value = _clean(value)
    if value is None:
        return None
    text = re.sub(r"\s+", "", value)
    try:
        # Standard and URL-safe alphabets are both common in JSON transports.
        pad = "=" * ((4 - len(text) % 4) % 4)
        raw = base64.b64decode(text + pad, altchars=b"-_", validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"TX-Link {label} is not valid Base64 transaction data") from exc
    if not raw:
        raise ValueError(f"TX-Link {label} cannot decode to an empty transaction")
    return raw.hex()


def _read_varint(raw: bytes, pos: int) -> tuple[int, int]:
    if pos >= len(raw):
        raise ValueError("truncated transaction while reading compact integer")
    first = raw[pos]
    if first < 0xfd:
        return first, pos + 1
    widths = {0xfd: 2, 0xfe: 4, 0xff: 8}
    width = widths[first]
    end = pos + 1 + width
    if end > len(raw):
        raise ValueError("truncated transaction while reading compact integer")
    return int.from_bytes(raw[pos + 1:end], "little"), end


def bitcoin_family_txids_from_raw_hex(raw_hex: str) -> tuple[str, str]:
    """Return (txid, wtxid/hash) for Bitcoin-family serialized transaction bytes.

    For witness transactions the display TXID hashes the stripped serialization,
    while the witness transaction hash hashes the complete serialization.  For
    non-witness transactions the two values are identical.
    """
    import hashlib
    h = _raw_tx_hex(raw_hex, "tx_raw_hex")
    raw = bytes.fromhex(h or "")
    if len(raw) < 10:
        raise ValueError("TX-Link serialized transaction is too short")

    def dh(data: bytes) -> str:
        return hashlib.sha256(hashlib.sha256(data).digest()).digest()[::-1].hex()

    wtxid = dh(raw)
    segwit = len(raw) >= 6 and raw[4] == 0 and raw[5] != 0
    if not segwit:
        return wtxid, wtxid

    # Parse just enough of the Bitcoin-family wire format to remove witness data.
    pos = 6
    vin_start = pos
    vin_count, pos = _read_varint(raw, pos)
    for _ in range(vin_count):
        if pos + 36 > len(raw):
            raise ValueError("truncated transaction input")
        pos += 36
        script_len, pos = _read_varint(raw, pos)
        if pos + script_len + 4 > len(raw):
            raise ValueError("truncated transaction input script")
        pos += script_len + 4
    vout_count, pos = _read_varint(raw, pos)
    for _ in range(vout_count):
        if pos + 8 > len(raw):
            raise ValueError("truncated transaction output")
        pos += 8
        script_len, pos = _read_varint(raw, pos)
        if pos + script_len > len(raw):
            raise ValueError("truncated transaction output script")
        pos += script_len
    base_end = pos

    # One witness stack per input.
    for _ in range(vin_count):
        items, pos = _read_varint(raw, pos)
        for _ in range(items):
            item_len, pos = _read_varint(raw, pos)
            if pos + item_len > len(raw):
                raise ValueError("truncated transaction witness")
            pos += item_len
    if pos + 4 != len(raw):
        raise ValueError("serialized transaction has unexpected trailing or missing bytes")
    stripped = raw[:4] + raw[vin_start:base_end] + raw[pos:pos + 4]
    return dh(stripped), wtxid


def bitcoin_family_txid_from_raw_hex(raw_hex: str) -> str:
    return bitcoin_family_txids_from_raw_hex(raw_hex)[0]


def _first(mapping: dict[str, Any], *keys: str):
    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            return mapping[key]
    return None


def _candidate_mappings(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Return only known envelope levels, never scripts/vin/vout nested objects."""
    out = [raw]
    for key in ("transaction", "tx", "data", "item", "result", "response", "payload", "explorer", "api_response", "rpc_response"):
        value = raw.get(key)
        if isinstance(value, dict):
            out.append(value)
    return out


def _extract_member_transport(raw: dict[str, Any], *, base_dir: Path | None = None) -> dict[str, Any]:
    """Normalize common explorer/node transaction response shapes.

    Supported transport families:
      1. plain serialized hex copied into a member field;
      2. JSON-wrapped hex (Core/Litecoin verbose ``hex``, BitRef ``txhex``,
         BlockCypher ``hex``, plus common rawtx/raw_tx aliases);
      3. raw transaction bytes carried as Base64 or JSON byte arrays.

    The function deliberately does not recursively scan arbitrary ``hex`` keys,
    avoiding accidental capture of scriptSig/scriptPubKey hex from decoded TX JSON.
    """
    maps = _candidate_mappings(raw)
    hex_keys = (
        "tx_raw_hex", "raw_tx_hex", "raw_transaction_hex", "transaction_hex",
        "serialized_tx", "serialized_transaction", "txhex", "rawtx", "raw_tx",
        "tx_raw_hash",  # historical BnP field name
    )
    b64_keys = (
        "tx_raw_base64", "raw_tx_base64", "raw_transaction_base64",
        "transaction_base64", "raw_base64", "serialized_tx_base64",
    )
    bytes_keys = ("tx_raw_bytes", "raw_tx_bytes", "raw_bytes")

    tx_hex = None
    input_type = None
    for m in maps:
        value = _first(m, *hex_keys)
        if value is not None:
            tx_hex = _raw_tx_hex(value, "serialized transaction hex")
            input_type = "HEX"
            break
        # Root-level/known-envelope ``hex`` is a standard verbose RPC/API field.
        if m.get("hex") not in (None, ""):
            tx_hex = _raw_tx_hex(m.get("hex"), "hex")
            input_type = "JSON-HEX"
            break
        # ``raw`` is accepted only when it is scalar/bytes, never a decoded object.
        if m.get("raw") not in (None, "") and not isinstance(m.get("raw"), dict):
            raw_value = m.get("raw")
            enc = str(m.get("encoding") or m.get("raw_encoding") or "hex").lower()
            if "base64" in enc or enc == "b64":
                tx_hex = _raw_tx_base64(raw_value, "raw")
                input_type = "JSON-BASE64"
            else:
                tx_hex = _raw_tx_hex(raw_value, "raw")
                input_type = "JSON-HEX"
            break
    if tx_hex is None:
        for m in maps:
            value = _first(m, *b64_keys)
            if value is not None:
                tx_hex = _raw_tx_base64(value)
                input_type = "BASE64"
                break
    if tx_hex is None:
        for m in maps:
            value = _first(m, *bytes_keys)
            if value is not None:
                tx_hex = _raw_tx_hex(value, "raw transaction byte array")
                input_type = "BYTE-ARRAY"
                break
    # JSON-RPC verbosity=0 commonly wraps the plain transaction hex in result.
    if tx_hex is None and isinstance(raw.get("result"), str):
        tx_hex = _raw_tx_hex(raw["result"], "JSON-RPC result")
        input_type = "JSON-RPC-HEX"

    # Esplora-style /raw endpoints return binary bytes.  A downloaded sidecar can
    # be referenced without forcing it through JSON/Base64 first.
    if tx_hex is None:
        raw_file = _first(raw, "tx_raw_file", "raw_tx_file", "raw_transaction_file", "transaction_file")
        if raw_file is not None:
            rp = Path(str(raw_file)).expanduser()
            if not rp.is_absolute() and base_dir is not None:
                rp = base_dir / rp
            payload = rp.read_bytes()
            # If a downloaded response is plain-text hex, keep the wire bytes it
            # describes; otherwise use the binary response bytes directly.
            try:
                text = payload.decode("ascii").strip()
            except UnicodeDecodeError:
                text = ""
            compact = re.sub(r"\s+", "", text)
            if compact and len(compact) % 2 == 0 and re.fullmatch(r"(?:0x)?[0-9A-Fa-f]+", compact):
                tx_hex = _raw_tx_hex(compact, "raw transaction file")
                input_type = "FILE-HEX"
            else:
                tx_hex = payload.hex()
                input_type = "FILE-BINARY"

    txid = None
    generic_hash = None
    wtxid = None
    block_hash = None
    block_height = None
    for m in maps:
        txid = txid or _first(m, "txid", "transaction_id", "transactionId", "transaction_id_hex")
        wtxid = wtxid or _first(m, "wtxid", "witness_txid", "witness_hash")
        generic_hash = generic_hash or _first(m, "hash", "transaction_hash", "tx_hash")
        block_hash = block_hash or _first(
            m, "origin_block_hash", "tx_origin_block_hash", "block_hash", "blockhash",
            "minedInBlockHash", "mined_in_block_hash"
        )
        block_height = block_height if block_height not in (None, "") else _first(
            m, "origin_block_height", "tx_origin_block_height", "block_height", "blockheight",
            "height", "blockHeight", "minedInBlockHeight", "mined_in_block_height"
        )
        status = m.get("status")
        if isinstance(status, dict):
            block_hash = block_hash or _first(status, "block_hash", "blockhash", "hash")
            if block_height in (None, ""):
                block_height = _first(status, "block_height", "height")
        block = m.get("block")
        if isinstance(block, dict):
            block_hash = block_hash or _first(block, "hash", "block_hash", "blockhash")
            if block_height in (None, ""):
                block_height = _first(block, "height", "block_height")
        elif isinstance(block, str) and len(block.strip()) == 64:
            block_hash = block_hash or block

    # APIs differ on the generic `hash` field.  With an explicit TXID (Bitcoin
    # Core verbose) it is the witness hash; without TXID (e.g. BlockCypher) it
    # commonly identifies the transaction itself.
    if txid in (None, "") and generic_hash not in (None, ""):
        txid = generic_hash
    elif wtxid in (None, "") and generic_hash not in (None, "") and str(generic_hash).lower() != str(txid).lower():
        wtxid = generic_hash

    return {
        "tx_raw_hex": tx_hex,
        "tx_raw_input_type": input_type,
        "txid": txid,
        "wtxid": wtxid,
        "block_hash": block_hash,
        "block_height": block_height,
    }


@dataclass(frozen=True)
class TxLinkMember:
    chain: str
    txid: str | None = None
    tx_raw_hash: str | None = None
    tx_wtxid: str | None = None
    tx_raw_input_type: str | None = None
    block_hash: str | None = None  # canonical internal name: origin block hash
    block_height: int | None = None  # canonical internal name: origin block height
    tx_raw_hash_algorithm: str | None = None
    address: str | None = None
    utxo: str | None = None
    pointer_data: str | None = None
    notes: str | None = None

    def normalized(self) -> "TxLinkMember":
        chain = self.chain.upper().strip()
        if chain not in SUPPORTED_CHAINS:
            raise ValueError(f"unsupported TX-Link chain: {chain}")
        block_hash = _hash64(self.block_hash, "origin_block_hash")
        tx_raw_hash = _raw_tx_hex(self.tx_raw_hash, "serialized transaction hex")
        tx_wtxid = _hash64(self.tx_wtxid, "wtxid") if self.tx_wtxid else None
        block_height = None if self.block_height in (None, "") else int(self.block_height)
        if block_height is not None and block_height < 0:
            raise ValueError("TX-Link origin_block_height cannot be negative")
        txid = _clean(self.txid)
        # BTC/LTC/BCH/DGB use Bitcoin-family display TXIDs.  If a 64-hex TXID
        # and raw transaction serialization are both supplied, this is an
        # inexpensive local consistency check; it does not verify the cross-chain
        # relationship itself.
        if tx_raw_hash and chain in {"BTC", "LTC", "BCH", "DGB"}:
            derived_txid, derived_wtxid = bitcoin_family_txids_from_raw_hex(tx_raw_hash)
            if txid:
                tid = txid.lower()
                if len(tid) == 64:
                    try:
                        bytes.fromhex(tid)
                    except ValueError:
                        pass
                    else:
                        if derived_txid != tid:
                            raise ValueError(
                                f"TX-Link {chain} txid does not match the supplied serialized transaction "
                                f"(derived {derived_txid})"
                            )
            if tx_wtxid and derived_wtxid != tx_wtxid:
                raise ValueError(
                    f"TX-Link {chain} wtxid/hash does not match the supplied serialized transaction "
                    f"(derived {derived_wtxid})"
                )
        return TxLinkMember(
            chain=chain,
            txid=txid,
            tx_raw_hash=tx_raw_hash,
            tx_wtxid=tx_wtxid,
            tx_raw_input_type=_clean(self.tx_raw_input_type),
            block_hash=block_hash,
            block_height=block_height,
            tx_raw_hash_algorithm=_clean(self.tx_raw_hash_algorithm),
            address=_clean(self.address),
            utxo=_clean(self.utxo),
            pointer_data=_clean(self.pointer_data),
            notes=_clean(self.notes),
        )

    @property
    def origin_block_hash(self) -> str | None:
        return self.block_hash

    @property
    def origin_block_height(self) -> int | None:
        return self.block_height

    def to_dict(self) -> dict[str, Any]:
        # Version 2 names the two source-material halves explicitly.  Import still
        # accepts the v1 block_hash/block_height names for backwards compatibility.
        out: dict[str, Any] = {"chain": self.chain}
        if self.txid is not None: out["txid"] = self.txid
        if self.tx_raw_hash is not None: out["tx_raw_hex"] = self.tx_raw_hash
        if self.tx_wtxid is not None: out["wtxid"] = self.tx_wtxid
        if self.tx_raw_input_type is not None: out["tx_raw_input_type"] = self.tx_raw_input_type
        if self.tx_raw_hash_algorithm is not None: out["tx_raw_hash_algorithm"] = self.tx_raw_hash_algorithm
        if self.block_hash is not None: out["origin_block_hash"] = self.block_hash
        if self.block_height is not None: out["origin_block_height"] = self.block_height
        for key in ("address", "utxo", "pointer_data", "notes"):
            value = getattr(self, key)
            if value is not None: out[key] = value
        return out


@dataclass
class TxLinkRelation:
    relation_id: str
    evidence_grade: str = UNVERIFIED_USER_PROOF
    members: list[TxLinkMember] = field(default_factory=list)
    claimed_grade: str | None = None
    source: str = "manual"
    protocol: str | None = None
    proof_digest: str | None = None
    notes: str | None = None
    created_utc: str = ""

    def normalized(self, *, trusted: bool = False, source: str | None = None) -> "TxLinkRelation":
        relation_id = str(self.relation_id).strip()
        if not relation_id:
            raise ValueError("TX-Link relation_id cannot be empty")
        requested = str(self.evidence_grade or UNVERIFIED_USER_PROOF).strip().upper()
        if requested not in EVIDENCE_GRADES:
            raise ValueError(f"unknown TX-Link evidence grade: {requested}")
        claimed = _clean(self.claimed_grade)
        actual = requested
        if not trusted and requested in {VERIFIED_EXPLICIT, VERIFIED_PROTOCOL}:
            claimed = requested
            actual = UNVERIFIED_USER_PROOF
        members = [m.normalized() for m in self.members]
        if len(members) < 2:
            raise ValueError("TX-Link relation needs at least two transaction/evidence members")
        return TxLinkRelation(
            relation_id=relation_id,
            evidence_grade=actual,
            members=members,
            claimed_grade=claimed,
            source=source or self.source or "manual",
            protocol=_clean(self.protocol),
            proof_digest=_clean(self.proof_digest),
            notes=_clean(self.notes),
            created_utc=self.created_utc or datetime.now(timezone.utc).isoformat(),
        )

    def to_dict(self) -> dict[str, Any]:
        out = {
            "relation_id": self.relation_id,
            "evidence_grade": self.evidence_grade,
            "members": [m.to_dict() for m in self.members],
            "source": self.source,
            "created_utc": self.created_utc,
        }
        for key in ("claimed_grade", "protocol", "proof_digest", "notes"):
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        return out


@dataclass(frozen=True)
class TxLinkedSourceRecord:
    """Picker-compatible source whose bytes are serialized transaction || origin block hash."""
    chain: str
    network: str
    height: int
    block_hash: str
    branch_index: int = 0
    status: str = "canonical"
    source: str = "tx-link"
    verified: bool = False
    timestamp: int | None = None
    tx_raw_hash: str = ""
    relation_id: str = ""
    relation_grade: str = UNVERIFIED_USER_PROOF
    source_kind: str = TX_LINKED_SOURCE_KIND


def aggregate_evidence_grade(grades: Iterable[str]) -> str:
    items = [str(g).upper() for g in grades if g]
    if not items:
        return UNVERIFIED_USER_PROOF
    return min(items, key=lambda g: _GRADE_STRENGTH.get(g, -1))


def relation_from_mapping(data: dict[str, Any], *, base_dir: Path | None = None) -> TxLinkRelation:
    members = []
    for raw in data.get("members") or data.get("transactions") or data.get("legs") or []:
        if not isinstance(raw, dict):
            continue
        extracted = _extract_member_transport(raw, base_dir=base_dir)
        members.append(TxLinkMember(
            chain=raw.get("chain") or raw.get("ticker") or raw.get("coin") or "",
            txid=extracted.get("txid"),
            tx_raw_hash=extracted.get("tx_raw_hex"),
            tx_wtxid=extracted.get("wtxid"),
            tx_raw_input_type=extracted.get("tx_raw_input_type"),
            block_hash=extracted.get("block_hash"),
            block_height=extracted.get("block_height"),
            tx_raw_hash_algorithm=raw.get("tx_raw_hash_algorithm") or raw.get("raw_hash_algorithm"),
            address=raw.get("address"),
            utxo=raw.get("utxo") or raw.get("outpoint"),
            pointer_data=raw.get("pointer_data") or raw.get("op_return") or raw.get("memo"),
            notes=raw.get("notes"),
        ))
    return TxLinkRelation(
        relation_id=data.get("relation_id") or data.get("id") or data.get("name") or "",
        evidence_grade=data.get("evidence_grade") or data.get("grade") or UNVERIFIED_USER_PROOF,
        claimed_grade=data.get("claimed_grade"),
        members=members,
        source=data.get("source") or "manual",
        protocol=data.get("protocol"),
        proof_digest=data.get("proof_digest") or data.get("proof_hash"),
        notes=data.get("notes"),
        created_utc=data.get("created_utc") or "",
    )


def load_tx_link_evidence(path: str | Path, *, trusted: bool = False) -> list[TxLinkRelation]:
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and data.get("format") == TX_EVIDENCE_FORMAT:
        rows = data.get("relations") or []
    elif isinstance(data, dict) and data.get("format") == "BNP-CORPUS-EXPORT":
        rows = data.get("tx_links") or []
    elif isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        rows = data.get("relations") or [data]
    else:
        raise ValueError("TX-Link evidence must be a JSON object or list")
    source = f"{'trusted' if trusted else 'manual'}-tx-link:{path.name}"
    return [relation_from_mapping(row, base_dir=path.parent).normalized(trusted=trusted, source=source) for row in rows]


def save_tx_link_evidence(path: str | Path, relations: Iterable[TxLinkRelation]) -> int:
    relations = list(relations)
    payload = {
        "format": TX_EVIDENCE_FORMAT,
        "version": TX_EVIDENCE_VERSION,
        "mapping_material": "serialized_tx_bytes||origin_block_hash",
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "relation_count": len(relations),
        "relations": [r.to_dict() for r in relations],
    }
    Path(path).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return len(relations)


def template_payload() -> dict[str, Any]:
    return {
        "format": TX_EVIDENCE_FORMAT,
        "version": TX_EVIDENCE_VERSION,
        "mapping_material": "serialized_tx_bytes||origin_block_hash",
        "accepted_raw_transaction_inputs": [
            "tx_raw_hex / legacy tx_raw_hash / raw_tx_hex / transaction_hex",
            "explorer/API JSON: hex, txhex, rawtx, raw_tx, JSON-RPC result",
            "tx_raw_base64 / raw byte array / tx_raw_file sidecar (hex text or binary)",
        ],
        "relations": [
            {
                "relation_id": "example-crosschain-tx-001",
                "evidence_grade": UNVERIFIED_USER_PROOF,
                "protocol": "optional-protocol-name",
                "notes": "User-supplied relationship; BnP does not independently certify it.",
                "members": [
                    {
                        "chain": "BTC",
                        "txid": "optional-display-transaction-id",
                        "tx_raw_hex": "replace-with-full-serialized-transaction-hex",
                        "origin_block_hash": "replace-with-64-hex-origin-block-hash",
                        "origin_block_height": 0,
                        "pointer_data": "optional OP_RETURN/commitment/reference",
                    },
                    {
                        "chain": "LTC",
                        "txid": "optional-display-transaction-id",
                        "tx_raw_hex": "replace-with-full-serialized-transaction-hex",
                        "origin_block_hash": "replace-with-64-hex-origin-block-hash",
                        "origin_block_height": 0,
                    },
                ],
            }
        ],
    }


def save_template(path: str | Path) -> None:
    Path(path).write_text(json.dumps(template_payload(), indent=2) + "\n", encoding="utf-8")


def store_relations(db, relations: Iterable[TxLinkRelation]) -> int:
    relations = list(relations)
    with db.conn:
        for rel in relations:
            rel = rel.normalized(trusted=True)  # already sanitized at import boundary
            db.conn.execute(
                """INSERT INTO tx_links(relation_id,evidence_grade,claimed_grade,source,protocol,proof_digest,notes,created_utc)
                VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(relation_id) DO UPDATE SET
                evidence_grade=excluded.evidence_grade,
                claimed_grade=excluded.claimed_grade,
                source=excluded.source,
                protocol=excluded.protocol,
                proof_digest=excluded.proof_digest,
                notes=excluded.notes,
                created_utc=excluded.created_utc""",
                (
                    rel.relation_id, rel.evidence_grade, rel.claimed_grade, rel.source,
                    rel.protocol, rel.proof_digest, rel.notes, rel.created_utc,
                ),
            )
            db.conn.execute("DELETE FROM tx_link_members WHERE relation_id=?", (rel.relation_id,))
            for ordinal, member in enumerate(rel.members):
                db.conn.execute(
                    """INSERT INTO tx_link_members(
                    relation_id,ordinal,chain,txid,tx_raw_hash,tx_wtxid,tx_raw_input_type,tx_raw_hash_algorithm,
                    block_hash,block_height,address,utxo,pointer_data,notes
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        rel.relation_id, ordinal, member.chain, member.txid, member.tx_raw_hash,
                        member.tx_wtxid, member.tx_raw_input_type, member.tx_raw_hash_algorithm, member.block_hash, member.block_height,
                        member.address, member.utxo, member.pointer_data, member.notes,
                    ),
                )
    return len(relations)


def list_relations(db, relation_ids: Iterable[str] | None = None) -> list[TxLinkRelation]:
    ids = [str(x) for x in relation_ids or []]
    params: list[Any] = []
    where = ""
    if ids:
        where = " WHERE relation_id IN (%s)" % ",".join("?" for _ in ids)
        params.extend(ids)
    rows = db.conn.execute(
        "SELECT relation_id,evidence_grade,claimed_grade,source,protocol,proof_digest,notes,created_utc FROM tx_links"
        + where + " ORDER BY relation_id",
        params,
    ).fetchall()
    out = []
    for row in rows:
        member_rows = db.conn.execute(
            """SELECT chain,txid,tx_raw_hash,tx_wtxid,tx_raw_input_type,block_hash,block_height,tx_raw_hash_algorithm,
            address,utxo,pointer_data,notes
            FROM tx_link_members WHERE relation_id=? ORDER BY ordinal,id""",
            (row[0],),
        ).fetchall()
        members = [TxLinkMember(
            chain=m[0], txid=m[1], tx_raw_hash=m[2], tx_wtxid=m[3], tx_raw_input_type=m[4],
            block_hash=m[5], block_height=m[6], tx_raw_hash_algorithm=m[7],
            address=m[8], utxo=m[9], pointer_data=m[10], notes=m[11],
        ).normalized() for m in member_rows]
        out.append(TxLinkRelation(
            relation_id=row[0], evidence_grade=row[1], claimed_grade=row[2], source=row[3],
            protocol=row[4], proof_digest=row[5], notes=row[6], created_utc=row[7], members=members,
        ))
    return out


def clear_relations(db) -> int:
    with db.conn:
        count = int(db.conn.execute("SELECT COUNT(*) FROM tx_links").fetchone()[0])
        db.conn.execute("DELETE FROM tx_link_members")
        db.conn.execute("DELETE FROM tx_links")
    return count


def relation_ids_from_selector(selector: str | None) -> list[str] | None:
    text = (selector or "ALL").strip()
    if not text or text.upper() == "ALL":
        return None
    return [x.strip() for x in text.split(",") if x.strip()]


def _resolve_origin_block(db, member: TxLinkMember):
    """Resolve origin block metadata, allowing literal user-supplied hashes.

    If the literal origin hash exists in the corpus, its local verification state
    is retained.  If it is not present, the literal hash is still valid mapping
    material for UNVERIFIED user evidence and is represented as an unverified
    synthetic record.  A height-only member still requires local corpus lookup.
    """
    if member.block_hash:
        known = db.resolve(member.chain, "mainnet", member.block_hash)
        if known is not None:
            return known, False
        return BlockRecord(
            member.chain, "mainnet",
            int(member.block_height) if member.block_height is not None else -1,
            member.block_hash, 0, "literal", "tx-link-literal-origin", False, None,
        ), True
    if member.block_height is None:
        return None, False
    row = db.conn.execute(
        """SELECT chain,network,height,block_hash,branch_index,status,source,verified,timestamp
        FROM blocks WHERE chain=? AND network='mainnet' AND height=?
        ORDER BY CASE WHEN status='canonical' THEN 0 ELSE 1 END, branch_index, block_hash LIMIT 1""",
        (member.chain.upper(), int(member.block_height)),
    ).fetchone()
    if not row:
        return None, False
    return BlockRecord(row[0],row[1],row[2],row[3],row[4],row[5],row[6],bool(row[7]),row[8]), False


def resolve_tx_link_sources(db, *, chains: Iterable[str], selector: str | None = None):
    """Resolve relationship members into picker sources.

    Each eligible member contributes the concatenated source material:
        serialized transaction bytes || origin_block_hash
    A relationship is eligible when at least two selected chains have both
    serialized transaction hex and an origin block hash.  Literal origin hashes
    may be used as unverified user evidence even before they exist in the corpus.
    """
    selected_chains = {c.upper() for c in chains}
    relations = list_relations(db, relation_ids_from_selector(selector))
    if not relations:
        raise ValueError("TX_LINKED found no stored relationship groups for the requested selector")

    accepted_relations: list[TxLinkRelation] = []
    source_records: list[TxLinkedSourceRecord] = []
    missing_tx_raw_hash = 0
    missing_origin_block = 0
    literal_origin_used = 0
    skipped_single_chain_relations = 0

    for rel in relations:
        members = [m for m in rel.members if m.chain in selected_chains]
        if len({m.chain for m in members}) < 2:
            skipped_single_chain_relations += 1
            continue
        pending: list[TxLinkedSourceRecord] = []
        ready_chains: set[str] = set()
        for member in members:
            has_raw = bool(member.tx_raw_hash)
            if not has_raw:
                missing_tx_raw_hash += 1
            origin, used_literal = _resolve_origin_block(db, member)
            if origin is None:
                missing_origin_block += 1
            if used_literal:
                literal_origin_used += 1
            if not has_raw or origin is None:
                continue
            pending.append(TxLinkedSourceRecord(
                chain=origin.chain,
                network=origin.network,
                height=origin.height,
                block_hash=origin.block_hash,
                branch_index=origin.branch_index,
                status=origin.status,
                source=f"tx-link:{rel.relation_id}",
                verified=origin.verified,
                timestamp=origin.timestamp,
                tx_raw_hash=member.tx_raw_hash,
                relation_id=rel.relation_id,
                relation_grade=rel.evidence_grade,
            ))
            ready_chains.add(origin.chain.upper())
        if len(ready_chains) >= 2:
            accepted_relations.append(rel)
            source_records.extend(pending)

    if not accepted_relations:
        raise ValueError(
            "TX_LINKED stored relationship groups did not produce a two-chain raw+origin material pool: "
            f"missing tx_raw_hash={missing_tx_raw_hash}; missing origin block in local corpus={missing_origin_block}; "
            f"single-chain/selector groups skipped={skipped_single_chain_relations}. "
            "Each usable member needs serialized transaction hex plus an origin_block_hash, or an origin_block_height that resolves in the local corpus."
        )

    grade = aggregate_evidence_grade(r.evidence_grade for r in accepted_relations)
    return {
        "relations": accepted_relations,
        "relation_grade": grade,
        "records": source_records,
        "missing_tx_raw_hash": missing_tx_raw_hash,
        "missing_origin_block": missing_origin_block,
        "literal_origin_used": literal_origin_used,
        "unresolved_members": missing_tx_raw_hash + missing_origin_block,
        "material_kind": "serialized_tx_bytes||origin_block_hash",
    }


# Backward-compatible function name for external callers.  Alpha.5 used this to
# return a block-only pool; Alpha.5-r3 returns serialized-transaction+origin material.
def resolve_tx_link_pool(db, *, chains: Iterable[str], selector: str | None = None):
    return resolve_tx_link_sources(db, chains=chains, selector=selector)


def filter_records_for_tx_links(records, pool):
    # Kept for import compatibility.  The corrected engine indexes pool['records']
    # directly because TX-linked material is not just a block hash anymore.
    return list(pool.get("records") or [])
