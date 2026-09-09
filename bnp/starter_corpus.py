from __future__ import annotations

import json
from importlib import resources

from .model import BlockRecord, SUPPORTED_CHAINS


def load_starter_data() -> dict:
    text = resources.files("bnp.data").joinpath("starter_blocks.json").read_text(encoding="utf-8")
    return json.loads(text)


def starter_records(chain: str) -> list[BlockRecord]:
    chain = chain.upper().strip()
    if chain not in SUPPORTED_CHAINS:
        raise ValueError(f"unsupported starter chain {chain}")
    data = load_starter_data()
    entry = data["chains"][chain]
    hashes = entry["hashes"]
    if len(hashes) != 10:
        raise ValueError(f"starter corpus for {chain} must contain exactly heights 0-9")
    return [
        BlockRecord(
            chain=chain,
            network="mainnet",
            height=height,
            block_hash=block_hash,
            branch_index=0,
            status="canonical",
            source=f"builtin-starter-v1:{chain}",
            verified=True,
        ).normalized()
        for height, block_hash in enumerate(hashes)
    ]


def import_starter(db, chain: str) -> int:
    records = starter_records(chain)
    # Preserve a richer existing source/timestamp if this exact block hash was
    # already acquired from a node or independently cross-checked provider.
    with db.conn:
        for rec in records:
            if db.resolve(rec.chain, rec.network, rec.block_hash) is None:
                db.add(rec)
    return len(records)


def import_all_starters(db) -> dict[str, int]:
    return {chain: import_starter(db, chain) for chain in SUPPORTED_CHAINS}
