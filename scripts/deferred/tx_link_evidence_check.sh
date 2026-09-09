#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
if [[ $# -ne 1 ]]; then
  echo "usage: bash scripts/tx_link_evidence_check.sh /path/to/evidence.json" >&2
  exit 2
fi
python - "$1" <<'PY'
from pathlib import Path
import sys
from bnp.tx_links import load_tx_link_evidence, bitcoin_family_txids_from_raw_hex

path=Path(sys.argv[1]).expanduser()
relations=load_tx_link_evidence(path,trusted=False)
print(f"TX-Link evidence valid: {len(relations)} relationship group(s)")
for rel in relations:
    print(f"\n{rel.relation_id} [{rel.evidence_grade}]")
    if rel.claimed_grade:
        print(f"  claimed_grade: {rel.claimed_grade}")
    for m in rel.members:
        raw_bytes=len(bytes.fromhex(m.tx_raw_hash)) if m.tx_raw_hash else 0
        origin=m.block_hash or (f"height #{m.block_height}" if m.block_height is not None else "missing")
        line=f"  {m.chain}: raw={raw_bytes} bytes; input={m.tx_raw_input_type or 'legacy/direct'}; origin={origin}"
        print(line)
        if m.tx_raw_hash and m.chain in {"BTC","LTC","BCH","DGB"}:
            txid,wtxid=bitcoin_family_txids_from_raw_hex(m.tx_raw_hash)
            print(f"    derived txid: {txid}")
            if wtxid != txid:
                print(f"    derived wtxid: {wtxid}")
print("\nNo database changes were made.")
PY
