# Mine-Building — Alpha.7

Mine-Build consumes an existing `.bnp`, `.bnpm`, or `.bnp2` artifact and reconstructs the addressed target material.

## Full recipes

Alpha.7 uses `segment-stream-v2`:

1. detect and resolve recipe content;
2. verify machine-container integrity where applicable;
3. sort/validate target offsets during resolution;
4. represent each unique literal source once and cache that small source value;
5. stream every referenced fragment directly to `output.part`;
6. compute SHA256-RAW and canonical SHA256-B64 incrementally while constructing;
7. reject before publication if the identity does not match;
8. optionally stream-read `output.part` for a second independent post-write SHA-256 check;
9. atomically rename to the final output.

RAW reconstruction does not allocate a complete target bytearray. B64 reconstruction validates and decodes the Base64 representation incrementally in 4-character groups.

The result report includes:

```text
construction_backend
source_cache_entries
source_cache_bytes
stream_peak_buffer_bytes
```

The source cache is job-local and normally scales with the number of unique block/source values rather than the number of recipe chunks.

## Proof recipes (`.bnp2`)

A proof cannot reconstruct unknown target bytes. Alpha.7 therefore creates the represented address-space file with its full declared size, leaves uncovered ranges as zero placeholders, seeks directly to every covered target range, and writes only proven fragments.

It also emits `BNP-MINE-COVERAGE-MAP` version 2 containing covered/hole ranges, target identity and streaming/cache metadata.

`0x00` in uncovered ranges is explicitly **not claimed target data**.

## Safety

- Existing target files are not overwritten.
- Construction uses `.part` and atomic publish.
- Cancellation removes unfinished `.part` files.
- Full targets require target-hash identity before publication.
- Machine container integrity is checked before construction.
