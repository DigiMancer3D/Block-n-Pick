#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
DB="$TMP/corpus.sqlite3"
TARGET="$TMP/mine_target.bin"
printf 'MineBuild!' > "$TARGET"
python - "$DB" <<'PY'
from pathlib import Path
import sys
from bnp.corpus import CorpusDB
from bnp.model import BlockRecord
raw=b'MineBuild!'+b'Z'*(32-len(b'MineBuild!'))
db=CorpusDB(Path(sys.argv[1])); db.add_many([BlockRecord('BTC','mainnet',77,raw.hex(),verified=True,source='mine-smoke')]); db.close()
PY
python run_bnp.py --db "$DB" build "$TARGET" --chains BTC --mode SOLOCHAIN --rep RAW --max-chunk 3 --out "$TMP/mine_recipe" >/dev/null
python run_bnp.py --db "$DB" resolve "$TMP/mine_recipe.bnpm" >/dev/null
python run_bnp.py --db "$DB" mine "$TMP/mine_recipe.bnpm" --action verify >/dev/null
mkdir -p "$TMP/out"
python run_bnp.py --db "$DB" mine "$TMP/mine_recipe.bnpm" --action build-verify --out-dir "$TMP/out" >/dev/null
cmp "$TARGET" "$TMP/out/mine_target.bin"
printf '%s\n' 'BnP Mine-Build standalone smoke PASS'
printf '%s\n' 'recipe -> resolve -> verify -> build+verify -> exact cmp'
