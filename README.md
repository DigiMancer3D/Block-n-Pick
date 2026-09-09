# Block-n-Pick v0.1.0-alpha.7

Block-n-Pick (BnP) references coincidentally matching fragments in independently verifiable blockchain hash corpora and uses those references to reconstruct or partially cover external data.

> **BnP does not claim that the reconstructed object is stored in a blockchain. A blockchain is an immutable, independently verifiable address/data pool whose coincidentally matching fragments can be referenced by a reconstruction recipe.**

## Public test-build status

Alpha.7 is the first BnP snapshot intended to be suitable for a public GitHub **working test build**. The packaged release gate passes 106/106 tests. A real 580,263-byte PNG has also been Mine-Built from an existing recipe and independently compared byte-for-byte with its original; both SHA-256 to `acfda1fcca08db06ecc466abee60d499cce9de151cf32181678ecfa19288c0fc`.

Live public-provider calls are intentionally not part of the deterministic release verifier, so provider uptime/rate limits remain external runtime variables. See `docs/PUBLIC_RELEASE.md` for the accepted scope and public-alpha limitations.

Alpha.7 keeps the active prototype focused on the two modes that have passed real-media acceptance:

- **Solochain**
- **Crosschain**

The major change is **streaming Mine-Building for large recipes**. Recipe Resolution and Mine-Build still support `.bnp`, `.bnpm`, and `.bnp2`, but full construction no longer creates a complete target bytearray before writing.

## Alpha.7 highlights

### Mine / Resolve

```text
Build Recipe / Proof
Corpus Manager
Corpus Exchange
Mine / Resolve
What BnP Does
```

Mine / Resolve provides:

- Preflight / Resolve
- Verify only
- Build
- Build + Verify
- content-based `.bnp` / BNPM / BNP2 detection
- machine-container integrity checks
- literal/offline source resolution
- optional local-corpus corroboration
- exact RAW/B64 identity verification

### Streaming construction backend

Full recipes use:

```text
recipe segments
   ↓
unique-source representation cache
   ↓
fragment stream
   ↓
.part output
   ├─ incremental SHA256-RAW
   └─ incremental canonical SHA256-B64
   ↓
streaming post-write verify
   ↓
atomic publish
```

Mine results identify the backend as:

```text
segment-stream-v2
```

and report source-cache size plus peak streaming-buffer size.

For B64 recipes, the represented Base64 stream is decoded incrementally; the full Base64 representation is not materialized in RAM.

### `.bnp2` proofs

Proof construction uses a zero-filled/sparse represented address-space file and writes only the covered ranges. A version-2 coverage JSON records the exact covered ranges/holes and makes clear that uncovered `0x00` bytes are placeholders, not claimed target bytes.

### Active build scope

New recipes are generated as:

```text
SOLOCHAIN
CROSSCHAIN
```

Locked-Link, Epoch-Link, TX-Linked, User-Picked, Generic, Binary and Trinary selector foundations remain documented under `docs/EXPECTED_UPGRADES.md` and readable for compatibility, but are not active prototype build choices.

## CachyOS / Arch-family use

Each release is standalone and can be kept beside older versions:

```bash
unzip Block-n-Pick_v0.1.0-alpha.7.zip
cd Block-n-Pick_v0.1.0-alpha.7

chmod +x scripts/*.sh
bash scripts/setup_cachyos.sh
bash scripts/verify_release.sh
bash scripts/run_gui.sh
```

Persistent GUI corpus:

```text
~/.local/share/block-n-pick/corpus.sqlite3
```

## Mine-Build CLI

```bash
python run_bnp.py resolve recipe.bnpm
python run_bnp.py resolve recipe.bnpm --no-corpus

python run_bnp.py mine recipe.bnpm --action verify
python run_bnp.py mine recipe.bnpm --action build --out-dir ./mined
python run_bnp.py mine recipe.bnpm --action build-verify --out-dir ./mined

python run_bnp.py mine proof.bnp2 --action build-verify --out-dir ./proof-material
```

A standalone isolated construction smoke is included:

```bash
bash scripts/mine_build_smoke.sh
```

## Main documentation

- `docs/MINE_BUILDING.md`
- `docs/EXPECTED_UPGRADES.md`
- `docs/FORMAT_SPEC.md`
- `docs/ACQUISITION.md`
- `docs/CORPUS_EXCHANGE.md`
- `docs/CLEAN_STREAM.md`
- `docs/ROADMAP_TO_MINE_BUILD.md`
- `docs/BHB_CHKR_BRIDGE.md`
