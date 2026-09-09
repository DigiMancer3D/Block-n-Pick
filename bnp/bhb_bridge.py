from __future__ import annotations
import base64,hashlib,json,struct
from dataclasses import dataclass
from pathlib import Path
BHB_BLOOM_SIZE=2_097_152;BHB_NUM_HASHES=3
@dataclass
class BHBState:
    bloom_utxo:bytes;bloom_tx:bytes;model_hash_utxo:str;model_hash_tx:str;synced_blocks:set[int]
    @staticmethod
    def _index(data,seed,size=BHB_BLOOM_SIZE):return int.from_bytes(hashlib.sha256(data+struct.pack(">I",seed)).digest()[:4],"big")%size
    @staticmethod
    def _contains(bits,key):
        for seed in range(BHB_NUM_HASHES):
            i=BHBState._index(key,seed)
            if (bits[i//8]&(1<<(i%8)))==0:return False
        return True
    def tx_maybe_seen(self,txid_hex):return self._contains(self.bloom_tx,bytes.fromhex(txid_hex))
    def utxo_maybe_seen(self,txid_hex,vout):return self._contains(self.bloom_utxo,bytes.fromhex(txid_hex)+struct.pack(">I",int(vout)))
def load_bhb_state(path):
    d=json.loads(Path(path).read_text(encoding="utf-8"));u=base64.b64decode(d["bloom_utxo"]);t=base64.b64decode(d["bloom_tx"])
    if hashlib.sha256(u).hexdigest()!=d["model_hash_utxo"]:raise ValueError("BHB UTXO Bloom model hash mismatch")
    if hashlib.sha256(t).hexdigest()!=d["model_hash_tx"]:raise ValueError("BHB TX Bloom model hash mismatch")
    return BHBState(u,t,d["model_hash_utxo"],d["model_hash_tx"],set(map(int,d.get("synced_blocks",[]))))
