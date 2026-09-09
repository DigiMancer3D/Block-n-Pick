#!/usr/bin/env bash
set -euo pipefail
export TERM="${TERM:-xterm}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

printf '%s\n' '============================================================'
printf '%s\n' 'Block-n-Pick v0.1.0-alpha.7 streaming Mine-Build verification'
printf '%s\n' '============================================================'

if command -v sha256sum >/dev/null 2>&1 && [[ -f SOURCE_SHA256SUMS.txt ]]; then
  sha256sum -c SOURCE_SHA256SUMS.txt >/dev/null
  printf '%s\n' 'BnP source integrity PASS'
fi

python -m compileall -q bnp tests run_bnp.py
python -m unittest discover -s tests -v

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/home"

if command -v xvfb-run >/dev/null 2>&1; then
  HOME="$TMP/home" xvfb-run -a python - <<'PY'
from bnp.gui import BnPApp
app=BnPApp(); app.update_idletasks()
texts=[app.notebook.tab(i,'text') for i in range(app.notebook.index('end'))]
assert texts == ['Build Recipe / Proof','Corpus Manager','Corpus Exchange','Mine / Resolve','What BnP Does'], texts
assert 'TX Links' not in texts
assert tuple(app.mode_combo.cget('values')) == ('SOLOCHAIN','CROSSCHAIN')
app.scan_chain.set('BTC'); app._refresh_latest_buttons(); assert app._latest_presets()==(20,200)
for chain in ('LTC','XMR','BCH','DGB'):
    app.scan_chain.set(chain); app._refresh_latest_buttons(); assert app._latest_presets()==(10,30)
app.destroy()
print('BnP Alpha.7 GUI reduced-scope/streaming-Mine/provider smoke PASS')
PY
else
  printf '%s\n' 'BnP GUI smoke SKIP (xvfb-run not installed)'
fi

python - "$TMP" <<'PY'
from pathlib import Path
import base64, hashlib, json, sys
from bnp.corpus import CorpusDB
from bnp.engine import build_manifest, save_outputs
from bnp.model import BlockRecord
from bnp.recipe_resolver import resolve_recipe
from bnp.mine_builder import mine_recipe, verify_recipe

root=Path(sys.argv[1])
db=CorpusDB(root/'mine.sqlite3')
try:
    target=b'BlockNPick!'
    source=target+bytes([0xA5])*(32-len(target))
    db.add_many([BlockRecord('BTC','mainnet',123,source.hex(),verified=True,source='release-smoke')])
    m,c=build_manifest(raw=target,safe_name='exact!bin',source_kind='FILE',source_value='exact.bin',corpus=db,chains=['BTC'],representation='RAW',mode='SOLOCHAIN',max_chunk=3)
    outs=save_outputs(m,c,root/'exact_recipe')
    bnp=[p for p in outs if str(p).endswith('.bnp')][0]
    bnpm=[p for p in outs if str(p).endswith('.bnpm')][0]

    # Human and machine resolution.
    for recipe in (bnp,bnpm):
        _m,report,_=resolve_recipe(recipe,db,include_sources=False)
        assert report.ok and report.coverage_percent==100.0 and report.corpus_sources_verified>=1
        vr=verify_recipe(recipe,corpus=db)
        assert vr.success and vr.sha256_raw_ok and vr.sha256_b64_ok

    # Content detection defeats extension renaming.
    renamed=root/'renamed-as-human.bnp'; renamed.write_bytes(bnpm.read_bytes())
    _m,report,_=resolve_recipe(renamed,db,include_sources=False)
    assert report.detected_kind=='BNPM' and report.container_integrity_ok
    assert any('does not match' in w for w in report.warnings)

    # Mine exact end-result and post-write verification.
    result=mine_recipe(bnpm,corpus=db,output_dir=root/'mined',verify_after_write=True)
    assert result.success and Path(result.full_output).read_bytes()==target and result.post_write_sha256_ok
    assert result.construction_backend=='segment-stream-v2' and result.source_cache_entries>=1

    # Existing target is never overwritten.
    second=mine_recipe(bnpm,corpus=db,output_dir=root/'mined',verify_after_write=True)
    assert Path(second.full_output)!=Path(result.full_output)
    assert Path(second.full_output).read_bytes()==target
finally:
    db.close()
print('BnP Mine-Build full .bnp/.bnpm exact construction smoke PASS')
print('BnP content-based machine detection/no-overwrite smoke PASS')
print('SHA256:',hashlib.sha256(b'BlockNPick!').hexdigest())

# BNP2 proof materialization with explicit holes.
proofdb=CorpusDB(root/'proof.sqlite3')
try:
    proofdb.add_many([BlockRecord('BTC','mainnet',1,(b'AB'+b'X'*30).hex(),verified=True)])
    target=b'ABCDABCD'
    m,c=build_manifest(raw=target,safe_name='proof!bin',source_kind='FILE',source_value='proof.bin',corpus=proofdb,chains=['BTC'],representation='RAW',mode='SOLOCHAIN',proof_coverage=50,max_chunk=2)
    [proof]=save_outputs(m,c,root/'proof_recipe')
    _m,report,payload=resolve_recipe(proof,proofdb,include_sources=False)
    assert report.ok and report.detected_kind=='BNP2' and report.hole_ranges
    result=mine_recipe(proof,corpus=proofdb,output_dir=root/'proof-out',verify_after_write=True)
    doc=json.loads(Path(result.coverage_map_output).read_text())
    assert doc['covered_ranges'] and doc['hole_ranges'] and 'NOT claimed' in doc['zero_fill_semantics']
    assert doc['construction_backend']=='segment-stream-v2'
finally:
    proofdb.close()
print('BnP BNP2 partial material + coverage-map smoke PASS')
PY

bash scripts/mine_build_smoke.sh
printf '%s\n' 'NOTE: live provider requests are intentionally excluded from release verification.'
printf '%s\n' 'Block-n-Pick Alpha.7 streaming Mine-Build verification PASS'
