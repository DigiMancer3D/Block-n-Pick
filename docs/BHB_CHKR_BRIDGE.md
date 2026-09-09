# BHB_CHKR → Block-n-Pick bridge (foundation)

The current BHB_CHKR implementation stores its Bloom state in `bloom_state.json` with Base64-encoded `bloom_utxo` and `bloom_tx` byte arrays plus SHA-256 model hashes. Its current constants are 2,097,152 Bloom bits and three seeded SHA-256 probes.

BnP alpha.1 includes `bnp.bhb_bridge.load_bhb_state()` as a read-only compatibility layer. It can:

- validate both saved Bloom arrays against their BHB model hashes;
- reproduce BHB TXID membership probes;
- reproduce BHB `txid:vout` UTXO membership probes;
- retain the BHB `synced_blocks` coverage set.

It intentionally cannot reverse a Bloom filter into TXIDs, UTXOs, block hashes, or other original values. A positive result means *possibly present*; a negative result means *definitely absent for that filter state*. Exact resolution must still come from an exact corpus, local node, or provider.

Planned use in BnP is candidate routing and cheap elimination before exact lookups, not source reconstruction.
