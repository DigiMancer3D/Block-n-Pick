# Block-n-Pick Corpus Exchange — Alpha.4

Alpha.4 adds a portability and manual-acquisition layer without changing the meaning of the BnP corpus.  Data copied or exported by a block explorer is useful as a candidate address/data pool, but BnP must not silently treat a single manually supplied result as verified.

## Corpus Exchange tab

The GUI now has a **Corpus Exchange** tab with two groups.

### Manual / explorer block-list import

The importer accepts:

- JSON arrays or nested JSON containing block objects;
- JSONL / NDJSON;
- CSV / TSV with named block fields;
- copied text/table rows containing a block height and 64-hex block hash;
- `height hash`, `hash height`, or `#height: hash` text;
- one 64-hex block hash per line when a Start height and ascending/descending order are supplied.

Common field aliases include:

- height: `height`, `block_height`, `blockheight`, `block_number`, `number`;
- hash: `block_hash`, `blockhash`, `hash`, or Esplora-style `id` when it is 64 hex characters;
- time: `timestamp`, `block_time`, `blocktime`, `time`, `date`, `datetime`;
- chain: `chain`, `ticker`, `coin`, `currency`, `symbol`.

This covers common explorer/API shapes such as Esplora block objects (`id`, `height`, `timestamp`), BlockCypher-style objects (`hash`, `height`, `time`), and simple `getblockhash` results saved as a hash-only list.

**Every record imported through Manual / explorer import is stored as unverified.**

If the same literal block hash is already verified in the local corpus, manual import cannot downgrade it.  If a later verified node/provider scan returns a hash that was previously imported manually, BnP upgrades the existing record rather than creating a second record.

## Restorable corpus JSON

**Export restorable corpus JSON** writes `BNP-CORPUS-EXPORT` version 1:

```json
{
  "format": "BNP-CORPUS-EXPORT",
  "version": 1,
  "blocks": [
    {
      "chain": "LTC",
      "network": "mainnet",
      "height": 19,
      "branch_index": 0,
      "block_hash": "...",
      "status": "canonical",
      "source": "manual-explorer:example.json",
      "verified": false,
      "timestamp": null
    }
  ]
}
```

The existing **Corpus Manager → Import corpus…** action accepts this export directly and preserves verification/timestamp metadata.

## Block-height list

**Export block-height list** writes a compact human-readable inventory, for example:

```text
# BNP-BLOCK-HEIGHT-LIST v1
# network=mainnet
BTC:0-1200,965563-965612
LTC:0-19,3099440-3099449
XMR:0-9
BCH:0-9
DGB:0-9
```

This is an inventory/export format, not a verification proof.

## Raw hash streams

**Export raw hash stream** writes the selected scope as one continuous hexadecimal string with **no separators and no newline** between block hashes.  Records are ordered deterministically by selected chain order, height, branch index, then literal block hash.

The scope can be one chain or `ALL`.  **Export all 5 per-chain streams** writes five separate files:

```text
btc_block_hash_stream.txt
ltc_block_hash_stream.txt
xmr_block_hash_stream.txt
bch_block_hash_stream.txt
dgb_block_hash_stream.txt
```

These streams are intended for external hashing/encoding/research workflows.  Because a no-separator stream does not retain record boundaries/metadata by itself, use the restorable corpus JSON when portability/recovery is the goal.

## CLI

```bash
# Explorer/manual list: always unverified
python run_bnp.py import-manual explorer.txt --chain LTC --start-height 20

# Hash-only latest list in descending height order
python run_bnp.py import-manual latest.txt --chain LTC --start-height 3099449 --descending

# Full restorable backup
python run_bnp.py export-corpus bnp_backup.json

# One-chain backup
python run_bnp.py export-corpus ltc_backup.json --chains LTC

# Height inventory
python run_bnp.py export-heights heights.txt

# One continuous all-chain stream
python run_bnp.py export-hash-stream all_hashes.txt

# One continuous LTC-only stream
python run_bnp.py export-hash-stream ltc_hashes.txt --chains LTC

# Five separate streams
python run_bnp.py export-hash-streams ./hash_streams
```

## Alpha.5 Clean Stream String

In addition to the raw no-separator hash stream, Alpha.5 can export a reversible **Clean Stream String**. Only leading/trailing sequential zeroes inside each individual 64-hex block hash are reduced. Interior zeroes remain unchanged.

Examples: `000000000000000000` -> `o099+` and `0000000000` -> `o091+`. See `CLEAN_STREAM.md` for the exact additive-run grammar.
