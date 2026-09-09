#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
printf 'ABxyCDuv' > "$TMP/target.bin"
python - "$TMP" <<'PY'
import json,sys
from pathlib import Path
root=Path(sys.argv[1])
btc_block=(b'xy'+b'X'*30).hex(); ltc_block=(b'uv'+b'Y'*30).hex()
btc_raw=(b'AB'+b'R'*70).hex(); ltc_raw=(b'CD'+b'S'*90).hex()
(root/'corpus.json').write_text(json.dumps([
 {'chain':'BTC','network':'mainnet','height':100,'block_hash':btc_block,'verified':True},
]))
(root/'tx_links.json').write_text(json.dumps({
 'format':'BNP-TX-LINK-EVIDENCE','version':2,'mapping_material':'tx_raw_hash||origin_block_hash','relations':[{
  'relation_id':'smoke-link','evidence_grade':'VERIFIED-PROTOCOL',
  'members':[
   {'chain':'BTC','txid':'btc-smoke','tx_raw_hash':btc_raw,'origin_block_hash':btc_block},
   {'chain':'LTC','txid':'ltc-smoke','tx_raw_hash':ltc_raw,'origin_block_hash':ltc_block},
  ]
 }]
}))
PY
python run_bnp.py --db "$TMP/db.sqlite3" import-corpus "$TMP/corpus.json" >/dev/null
python run_bnp.py --db "$TMP/db.sqlite3" import-tx-links "$TMP/tx_links.json" >/dev/null
python run_bnp.py --db "$TMP/db.sqlite3" build "$TMP/target.bin" \
  --chains BTC!LTC --mode TX_LINKED --tx-links smoke-link \
  --weights '50%BTC!50%LTC' --max-chunk 2 --out "$TMP/tx_recipe" > "$TMP/build.json"
python run_bnp.py --db "$TMP/db.sqlite3" verify "$TMP/tx_recipe.bnpm" \
  --write-output "$TMP/rebuilt.bin" > "$TMP/verify.json"
cmp "$TMP/target.bin" "$TMP/rebuilt.bin"
python - "$TMP/build.json" "$TMP/verify.json" <<'PY'
import json,sys
b=json.load(open(sys.argv[1])); v=json.load(open(sys.argv[2]))
assert b['coverage_percent']==100.0
assert b['notes']['alignment']['relation_grade']=='UNVERIFIED-USER-PROOF'
assert b['notes']['chain_bytes']=={'BTC':4,'LTC':4}
assert b['notes']['alignment']['mapping_material']=='tx_raw_hash||origin_block_hash'
assert b['notes']['alignment']['literal_origin_used'] >= 1
assert (not v['source_records_ok']) and v['sha256_raw_ok'] and v['sha256_b64_ok']
print('BnP TX_LINKED standalone smoke PASS')
print('Coverage: 100% | BTC: 4 bytes | LTC: 4 bytes')
print('Mapping material: serialized transaction bytes || origin block hash bytes')
print('Manual VERIFIED-PROTOCOL claim correctly stored as UNVERIFIED-USER-PROOF')
PY
