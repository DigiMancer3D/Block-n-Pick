# Block-n-Pick acquisition — Alpha.4

BnP keeps acquisition separate from reconstruction. The corpus is local user-owned data under `~/.local/share/block-n-pick/corpus.sqlite3`; scans never modify a blockchain or node database.

## Source priority

1. existing BnP corpus
2. local node/daemon
3. imported exact corpus/starter snapshot
4. configured public providers

## Provider-sensitive public presets

The GUI changes Latest shortcuts according to the selected source:

- **BTC (public or local):** Latest 20 / Latest 200
- **LTC/XMR/BCH/DGB (public or local):** Latest 10 / Latest 100

These are UI convenience sizes, not claims about provider hard limits. Provider governors/request budgets still apply independently.

BTC public acquisition uses the Esplora `/blocks/<height>` batch shape and commits each successfully cross-checked batch. Litecoin's single-source BlockCypher path commits successful records in small groups (default 10) and stops cleanly on a 429/provider error.

## Partial/resumable behavior

A public-provider failure no longer discards records collected earlier in the same scan. Scan reports include:

- requested heights
- completed heights
- retained/imported records
- verified/unverified counts
- stop reason
- resume height when known

An HTTP 429 therefore becomes a **Partial** result rather than a total acquisition failure.

## Verification status

- BTC public records are verified only when mempool.space and Blockstream return the same block hash for the requested height.
- LTC BlockCypher records remain `verified=false`; they are staging data until another source corroborates them.
- USER_PICKED mode may deliberately use such unverified corpus records, but the relationship is always `UNVERIFIED-USER-PROOF`.

## Native starter JSON import

The generic Import Corpus action now recognizes the shipped `BNP-STARTER-CORPUS` JSON structure directly. `bnp/data/starter_blocks.json` therefore works through either the offline buttons or ordinary JSON import.

## Local timestamp support

Core-family scans still acquire block hashes through `getblockhash`; Alpha.3 also attempts batched verbose `getblockheader` calls so timestamped records can participate in EPOCH_LINK. Failure to retrieve a header timestamp does not discard an otherwise valid block hash.


## Manual explorer acquisition

Alpha.4 adds a separate manual/explorer importer for copied or exported block data.  This path never claims independent verification: every newly introduced row is `verified=false` until corroborated by a stronger source.  Existing verified rows cannot be downgraded by manual import.

See `docs/CORPUS_EXCHANGE.md` for accepted file/text shapes and backup/export formats.

## Alpha.5 public-provider shortcut guardrail

BTC keeps `Latest 20 / Latest 200`. LTC/XMR/BCH/DGB use `Latest 10 / Latest 30`. The built-in Litecoin BlockCypher public path caps its request budget at 30. Larger explicit ranges remain available for local-node workflows.
