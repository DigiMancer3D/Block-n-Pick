from __future__ import annotations
import csv, json, sqlite3
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any
from .model import BlockRecord

SCHEMA="""
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS blocks (
 chain TEXT NOT NULL, network TEXT NOT NULL, height INTEGER NOT NULL,
 branch_index INTEGER NOT NULL DEFAULT 0, block_hash TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'canonical', source TEXT NOT NULL DEFAULT 'import',
 verified INTEGER NOT NULL DEFAULT 1, timestamp INTEGER,
 PRIMARY KEY(chain, network, block_hash));
CREATE INDEX IF NOT EXISTS idx_blocks_height ON blocks(chain, network, height, branch_index);
CREATE INDEX IF NOT EXISTS idx_blocks_status ON blocks(chain, network, status);
CREATE TABLE IF NOT EXISTS tx_links (
 relation_id TEXT PRIMARY KEY, evidence_grade TEXT NOT NULL, claimed_grade TEXT,
 source TEXT NOT NULL DEFAULT 'manual', protocol TEXT, proof_digest TEXT, notes TEXT, created_utc TEXT
);
CREATE TABLE IF NOT EXISTS tx_link_members (
 id INTEGER PRIMARY KEY AUTOINCREMENT, relation_id TEXT NOT NULL, ordinal INTEGER NOT NULL DEFAULT 0,
 chain TEXT NOT NULL, txid TEXT, tx_raw_hash TEXT, tx_wtxid TEXT, tx_raw_input_type TEXT, tx_raw_hash_algorithm TEXT,
 block_hash TEXT, block_height INTEGER, address TEXT, utxo TEXT,
 pointer_data TEXT, notes TEXT, FOREIGN KEY(relation_id) REFERENCES tx_links(relation_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_tx_link_members_relation ON tx_link_members(relation_id, ordinal);
CREATE INDEX IF NOT EXISTS idx_tx_link_members_block ON tx_link_members(chain, block_height, block_hash);
"""
class CorpusDB:
    def __init__(self,path:str|Path):
        self.path=Path(path).expanduser(); self.path.parent.mkdir(parents=True,exist_ok=True)
        self.conn=sqlite3.connect(self.path); self.conn.executescript(SCHEMA)
        self._migrate_schema()
    def _migrate_schema(self):
        cols={row[1] for row in self.conn.execute("PRAGMA table_info(tx_link_members)")}
        with self.conn:
            if "tx_raw_hash" not in cols:
                self.conn.execute("ALTER TABLE tx_link_members ADD COLUMN tx_raw_hash TEXT")
            if "tx_raw_hash_algorithm" not in cols:
                self.conn.execute("ALTER TABLE tx_link_members ADD COLUMN tx_raw_hash_algorithm TEXT")
            if "tx_wtxid" not in cols:
                self.conn.execute("ALTER TABLE tx_link_members ADD COLUMN tx_wtxid TEXT")
            if "tx_raw_input_type" not in cols:
                self.conn.execute("ALTER TABLE tx_link_members ADD COLUMN tx_raw_input_type TEXT")
    def close(self): self.conn.close()
    def add(self,record:BlockRecord):
        r=record.normalized(); self.conn.execute("""INSERT INTO blocks(chain,network,height,branch_index,block_hash,status,source,verified,timestamp)
        VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(chain,network,block_hash) DO UPDATE SET
        height=excluded.height,
        branch_index=excluded.branch_index,
        status=CASE WHEN excluded.verified >= blocks.verified THEN excluded.status ELSE blocks.status END,
        source=CASE WHEN excluded.verified >= blocks.verified THEN excluded.source ELSE blocks.source END,
        verified=MAX(blocks.verified, excluded.verified),
        timestamp=COALESCE(excluded.timestamp, blocks.timestamp)""",
        (r.chain,r.network,r.height,r.branch_index,r.block_hash,r.status,r.source,1 if r.verified else 0,r.timestamp))
    def add_many(self,records:Iterable[BlockRecord])->int:
        n=0
        with self.conn:
            for r in records: self.add(r); n+=1
        return n
    def iter_records(self,chains:Iterable[str]|None=None,network:str="mainnet",verified_only:bool=True)->Iterator[BlockRecord]:
        params:[Any]=[network.lower()]; where=["network=?"]
        if verified_only: where.append("verified=1")
        cs=[c.upper() for c in chains] if chains else []
        if cs: where.append("chain IN (%s)" % ",".join("?" for _ in cs)); params.extend(cs)
        sql="SELECT chain,network,height,block_hash,branch_index,status,source,verified,timestamp FROM blocks WHERE "+" AND ".join(where)+" ORDER BY chain,height,branch_index"
        for row in self.conn.execute(sql,params): yield BlockRecord(row[0],row[1],row[2],row[3],row[4],row[5],row[6],bool(row[7]),row[8])
    def resolve(self,chain:str,network:str,block_hash:str)->BlockRecord|None:
        row=self.conn.execute("SELECT chain,network,height,block_hash,branch_index,status,source,verified,timestamp FROM blocks WHERE chain=? AND network=? AND block_hash=?",(chain.upper(),network.lower(),block_hash.lower())).fetchone()
        return None if not row else BlockRecord(row[0],row[1],row[2],row[3],row[4],row[5],row[6],bool(row[7]),row[8])
    def counts(self)->dict[str,int]: return {r[0]:r[1] for r in self.conn.execute("SELECT chain,COUNT(*) FROM blocks GROUP BY chain ORDER BY chain")}
    def count_details(self)->dict[str,dict[str,int]]:
        out={}
        sql=(
            "SELECT chain,COUNT(*),"
            "SUM(CASE WHEN verified=1 THEN 1 ELSE 0 END),"
            "SUM(CASE WHEN timestamp IS NOT NULL THEN 1 ELSE 0 END) "
            "FROM blocks GROUP BY chain ORDER BY chain"
        )
        for chain,total,verified,timestamped in self.conn.execute(sql):
            out[chain]={
                "total":int(total),
                "verified":int(verified or 0),
                "unverified":int(total)-int(verified or 0),
                "timestamped":int(timestamped or 0),
            }
        return out
    def record_count(self,chains:Iterable[str]|None=None,network:str="mainnet",verified_only:bool=False)->int:
        params:[Any]=[network.lower()];where=["network=?"]
        if verified_only: where.append("verified=1")
        cs=[c.upper() for c in chains] if chains else []
        if cs: where.append("chain IN (%s)" % ",".join("?" for _ in cs));params.extend(cs)
        return int(self.conn.execute("SELECT COUNT(*) FROM blocks WHERE "+" AND ".join(where),params).fetchone()[0])
    def verified_count(self,chains:Iterable[str]|None=None,network:str="mainnet")->int:
        return self.record_count(chains,network,verified_only=True)
    def prune_chain(self,chain:str)->int:
        with self.conn: cur=self.conn.execute("DELETE FROM blocks WHERE chain=?",(chain.upper(),))
        return cur.rowcount

def _boolish(v:Any,default:bool=True)->bool:
    if v is None:return default
    if isinstance(v,bool):return v
    return str(v).strip().lower() not in {"0","false","no","unverified"}
def _record_from_mapping(d:dict[str,Any],default_chain:str|None,default_network:str)->BlockRecord:
    chain=d.get("chain") or d.get("ticker") or default_chain
    if not chain: raise ValueError("chain missing (use a chain column or --chain)")
    h=d.get("block_hash") or d.get("hash")
    if not h: raise ValueError("block_hash/hash missing")
    return BlockRecord(str(chain),str(d.get("network") or default_network),int(d["height"]),str(h),int(d.get("branch_index",d.get("branch",0)) or 0),str(d.get("status") or "canonical"),str(d.get("source") or "import"),_boolish(d.get("verified"),True),None if d.get("timestamp") in (None,"") else int(d["timestamp"]))
def load_records(path:str|Path,default_chain:str|None=None,default_network:str="mainnet")->list[BlockRecord]:
    path=Path(path); out=[]
    if path.suffix.lower()==".csv":
        with path.open(newline="",encoding="utf-8") as fh:
            for row in csv.DictReader(fh): out.append(_record_from_mapping(row,default_chain,default_network))
        return out
    text=path.read_text(encoding="utf-8")
    if path.suffix.lower() in {".jsonl",".ndjson"}:
        for line in text.splitlines():
            if line.strip(): out.append(_record_from_mapping(json.loads(line),default_chain,default_network))
        return out
    data=json.loads(text)
    # Native BnP starter-corpus files are nested by chain rather than being a
    # flat list of block rows.  Detect them before the generic mapping path so
    # users can import the shipped starter_blocks.json directly.
    if isinstance(data,dict) and data.get("format") == "BNP-CORPUS-EXPORT":
        rows=data.get("blocks") or []
        for row in rows:
            out.append(_record_from_mapping(row,default_chain,default_network))
        return out
    if isinstance(data,dict) and data.get("format") == "BNP-STARTER-CORPUS":
        chains=data.get("chains") or {}
        for chain,entry in chains.items():
            hashes=(entry or {}).get("hashes") or []
            for height,block_hash in enumerate(hashes):
                out.append(BlockRecord(
                    str(chain), default_network, height, str(block_hash),
                    branch_index=0, status="canonical",
                    source=f"imported-starter-v{data.get('version',1)}:{str(chain).upper()}",
                    verified=True,
                ).normalized())
        return out
    data=data.get("blocks",[data]) if isinstance(data,dict) else data
    for row in data: out.append(_record_from_mapping(row,default_chain,default_network))
    return out
