# BnP Built-in Starter Corpus v1

Purpose: provide a tiny deterministic, network-free corpus for smoke tests. It is **not** intended as a meaningful reconstruction corpus.

The bundled data lives in `bnp/data/starter_blocks.json` and contains canonical mainnet block hashes at heights 0 through 9 for BTC, LTC, XMR, BCH and DGB. `Test all 5` therefore imports 50 records.

## Behavior

- `Test 0–9 (offline)` imports the selected chain only.
- `Test all 5` imports every starter chain.
- Starter records are marked verified/canonical because the exact pinned chain history is part of the shipped test fixture.
- If the same chain/hash is already present from a richer node/provider acquisition, the existing record is preserved rather than replaced by `builtin-starter-v1` metadata.
- No network call is made.

## BCH note

Bitcoin Cash shares Bitcoin's chain history prior to the 2017 fork. Consequently its heights 0–9 use the same block hashes as Bitcoin heights 0–9. This is intentional and is useful for Locked-Link/Crosschain tests because it demonstrates that chain identity remains separate even when historical hash values are equal.

## Data integrity

Release tests require every starter chain to contain exactly heights 0–9, every hash to be a 64-character normalized value, and every starter record to import as verified. Additional anchor-value regressions pin BTC genesis, LTC height 9, XMR height 3, DGB genesis, and BCH/BTC early-history equality.

This dataset is intentionally small enough to inspect and replace in a future data-versioned release if an upstream historical source discrepancy is ever discovered.
