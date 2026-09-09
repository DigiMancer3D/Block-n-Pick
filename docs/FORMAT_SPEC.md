# Block-n-Pick Format Contract — v1 / Alpha.3 semantics

## Canonical meaning notice

**BnP does not claim that the reconstructed object is stored in a blockchain. A blockchain is an immutable, independently verifiable address/data pool whose coincidentally matching fragments can be referenced by a reconstruction recipe.**

## Files

- `.bnp` — human-readable full reconstruction recipe; no machine/coverage header.
- `.bnpm` — packed machine-readable full manifest; binary magic `BNPM\0\1`, compressed canonical payload, integrity hashes.
- `.bnp2` — packed BnP Proof; binary magic `BNP2\0\1`, protected coverage metadata and proof class.

Renaming an extension does not change internal format detection.

## Proof grades

- 1–32% MINOR
- 33–67% BASIC
- 68–99% GENERIC
- 100% FULL

Coverage is byte coverage of the selected target representation, not perceptual similarity.

For a requested non-full proof percentage, Alpha.3 accepts actual coverage from **1 point under through 2 points over**, preferring on-target/over-target output before the under-target fallback.

## Ranges

Human `.bnp` ranges are zero-based and inclusive. Machine containers store offset + length.

## Representations and hashes

`RAW` is the 32 bytes obtained by hex-decoding canonical hash text left-to-right. `B64` is RFC 4648 Base64 of RAW. Targets record SHA256-RAW and SHA256-B64.

## Safe notation

`.` maps to `!`; literal `!` maps to `!!`, making the transform reversible.

## Generated selector

```text
#HEIGHT[.BRANCH]@LITERAL_HASH[start,end]
```

Literal hashes remain authoritative even when human height/branch aliases are present.

## Crosschain mix

```text
10%BTC!50%LTC!40%BCH
```

The requested distribution applies to covered recipe material. Full and partial Crosschain output targets exact byte quotas first; if exact quotas obstruct otherwise available reconstruction/coverage, the mix becomes a soft target and BnP records the nearest feasible achieved share instead of failing on percentage deviation alone. Proof coverage tolerance remains a separate rule.

## Alignment modes

- `SOLOCHAIN`
- `CROSSCHAIN`
- `LOCKED_LINK` — only heights present on every selected chain
- `USER_PICKED` — explicit user-supplied block/hash/short-handle selectors
- `EPOCH_LINK` — only normalized timestamp epochs represented by every selected chain

See `docs/ALIGNMENT.md`.

## Evidence grades

- `VERIFIED-EXPLICIT`
- `VERIFIED-PROTOCOL`
- `VERIFIED-HEIGHT-ALIGNMENT`
- `VERIFIED-EPOCH-ALIGNMENT`
- `UNVERIFIED-USER-PROOF`
- `UNVERIFIED-HEURISTIC`

USER_PICKED associations are never automatically promoted to verified. They may deliberately reference unverified corpus records; `verified_source` and `relation_grade` are separate segment properties.

## Machine segment storage

Compact `block-table-v1` machine payloads intern immutable block/provenance metadata once and store segment references by block-table index. Legacy full-segment v1 machine files remain readable.

## Starter chains

BTC, LTC, XMR, BCH, DGB. The core remains adapter-oriented rather than hard-coding reconstruction logic per chain.

## Alpha.6 Mine-Build interpretation

Alpha.6 actively generates only `SOLOCHAIN` and `CROSSCHAIN` recipes, but readers retain compatibility with earlier manifests whose `@MODE`/machine `mode` names a deferred selector.

Mine-Build resolves machine type by binary magic rather than file extension. Full `.bnp`/BNPM artifacts must cover the complete representation byte range contiguously. BNP2 may contain holes; Mine-Build exposes those holes in a `BNP-MINE-COVERAGE-MAP` and does not claim zero-filled placeholders are target bytes.

The literal hash/material carried in a recipe is sufficient to reconstruct its selected byte fragments. Local corpus membership is reported separately as corroboration (`CORPUS-VERIFIED`, `CORPUS-UNVERIFIED`, or `LITERAL-ONLY`).
