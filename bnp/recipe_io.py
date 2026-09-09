from __future__ import annotations
import json, os, re
from pathlib import Path
from typing import Any, Iterable
from .container_format import pack_container_stream,unpack_container,verify_container_stream
from .model import RecipeManifest,Segment,TargetIdentity,proof_class
from .picker import BuildCancelled

CHAIN_RE=re.compile(r"^\|([A-Z0-9]+):([A-Z0-9]+)\|==>\{$")
SELECTOR_RE=re.compile(r"^#(?:(\d+)(?:\.(\d+))?@([0-9a-fA-F]{64})|hash:([0-9a-fA-F]{64}))\[(\d+),(\d+)\];?$")
TX_SELECTOR_RE=re.compile(r"^#txraw:([0-9a-fA-F]+)\+(?:#(-?\d+)(?:\.(\d+))?@|#hash:)([0-9a-fA-F]{64})\[(\d+),(\d+)\];?$")
TARGET_RE=re.compile(r"^<==\|(.+)\|$")


def _cancelled(cancel) -> bool:
    return bool(cancel is not None and cancel.is_set())


def _emit(progress, stage, done, total, detail=""):
    if progress:
        progress(stage,int(done),int(total),detail)


def _j(value: Any) -> bytes:
    return json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode("utf-8")


def write_bnp(manifest,path,*,progress=None,cancel=None):
    """Stream a human recipe line-by-line and atomically publish it."""
    out=Path(path); out.parent.mkdir(parents=True,exist_ok=True); part=out.with_name(out.name+".part")
    segments=manifest.segments; total=len(segments); cur=None
    _emit(progress,"write_bnp",0,max(1,total),"opening human recipe output")
    try:
        with part.open("w",encoding="utf-8",newline="\n") as f:
            for i,s in enumerate(segments,1):
                if _cancelled(cancel): raise BuildCancelled("build cancelled while writing human recipe")
                key=(s.chain,s.representation)
                if key!=cur:
                    if cur is not None:f.write("}\n")
                    f.write(f"|{s.chain}:{s.representation}|==>{{\n");cur=key
                b="" if s.branch_index==0 else f".{s.branch_index}";end=s.source_offset+s.length-1
                if getattr(s,"source_kind","BLOCK_HASH") == "TX_RAW_PLUS_ORIGIN_BLOCK":
                    origin_ref = f"#{s.height}{b}@{s.block_hash}" if s.height >= 0 else f"#hash:{s.block_hash}"
                    f.write(f"#txraw:{s.tx_raw_hash}+{origin_ref}[{s.source_offset},{end}];\n")
                else:
                    f.write(f"#{s.height}{b}@{s.block_hash}[{s.source_offset},{end}];\n")
                if i==total or i % 25000 == 0:
                    _emit(progress,"write_bnp",i,total,"writing human recipe")
            if cur is not None:f.write("}\n")
            f.write(f"<==|{manifest.target.safe_name}|\n@MODE:{manifest.mode}\n@REP:{manifest.target.representation}\n")
            if manifest.chain_weights:f.write("@CHAIN-MIX:"+"!".join(f"{v:g}%{k}" for k,v in manifest.chain_weights.items())+"\n")
            f.write(f"@SHA256:RAW:{manifest.target.sha256_raw}\n@SHA256:B64:{manifest.target.sha256_b64}\n")
            f.flush();os.fsync(f.fileno())
        os.replace(part,out)
        _emit(progress,"write_bnp",max(1,total),max(1,total),"human recipe complete")
    finally:
        if part.exists():
            try:part.unlink()
            except OSError:pass


def parse_bnp(path):
    lines=[x.strip() for x in Path(path).read_text(encoding="utf-8").splitlines() if x.strip()];chain=rep=None;segs=[];to=0;name=None;mode="SOLOCHAIN";sr=sb="";weights={}
    for line in lines:
        if (m:=CHAIN_RE.match(line)):chain,rep=m.group(1),m.group(2);continue
        if line=="}":chain=rep=None;continue
        if (m:=TX_SELECTOR_RE.match(line)):
            if not chain or not rep:raise ValueError("selector outside chain section")
            txh=m.group(1).lower();h=int(m.group(2)) if m.group(2) is not None else -1;b=int(m.group(3) or 0);bh=m.group(4).lower();st,en=int(m.group(5)),int(m.group(6));l=en-st+1
            if len(txh)%2: raise ValueError("TX raw transaction hex must have even length")
            bytes.fromhex(txh)
            if l<=0:raise ValueError("invalid selector range")
            segs.append(Segment(chain,"mainnet",h,b,bh,rep,st,l,to,True,"UNVERIFIED-USER-PROOF","TX_RAW_PLUS_ORIGIN_BLOCK",txh,None));to+=l;continue
        if (m:=SELECTOR_RE.match(line)):
            if not chain or not rep:raise ValueError("selector outside chain section")
            h=int(m.group(1)) if m.group(1) else -1;b=int(m.group(2) or 0);bh=(m.group(3) or m.group(4)).lower();st,en=int(m.group(5)),int(m.group(6));l=en-st+1
            if l<=0:raise ValueError("invalid selector range")
            segs.append(Segment(chain,"mainnet",h,b,bh,rep,st,l,to));to+=l;continue
        if (m:=TARGET_RE.match(line)):name=m.group(1);continue
        if line.startswith("@MODE:"):mode=line.split(":",1)[1]
        elif line.startswith("@SHA256:RAW:"):sr=line.split(":",2)[2]
        elif line.startswith("@SHA256:B64:"):sb=line.split(":",2)[2]
        elif line.startswith("@CHAIN-MIX:"):
            for p in line.split(":",1)[1].split("!"): pct,t=p.split("%",1);weights[t]=float(pct)
    if name is None:raise ValueError("missing target")
    representation=segs[0].representation if segs else "RAW"
    target=TargetIdentity(name,to if representation=="RAW" else 0,to,sr,sb,representation)
    return RecipeManifest("BNP",1,mode,target,segs,weights)


def _segment_block_key(s: Segment):
    return (s.chain,s.network,s.height,s.branch_index,s.block_hash,s.representation,bool(s.verified_source),s.relation_grade,getattr(s,"source_kind","BLOCK_HASH"),getattr(s,"tx_raw_hash",None),getattr(s,"relation_id",None))


def _compact_block_table(manifest: RecipeManifest, *, kind="BNPM", progress=None, cancel=None):
    index={}; table=[]; total=max(1,len(manifest.segments))
    stage="pack_"+kind.lower()
    _emit(progress,stage,0,total,"building compact source block table")
    for i,s in enumerate(manifest.segments,1):
        if _cancelled(cancel): raise BuildCancelled("build cancelled while building compact block table")
        key=_segment_block_key(s)
        if key not in index:
            index[key]=len(table)
            table.append({
                "block_hash":s.block_hash,
                "branch_index":s.branch_index,
                "chain":s.chain,
                "height":s.height,
                "network":s.network,
                "relation_grade":s.relation_grade,
                "representation":s.representation,
                "verified_source":bool(s.verified_source),
                "source_kind":getattr(s,"source_kind","BLOCK_HASH"),
                "tx_raw_hash":getattr(s,"tx_raw_hash",None),
                "relation_id":getattr(s,"relation_id",None),
            })
        if i==total or i%100000==0:
            # First pass occupies the first 10% of the machine-pack stage.
            _emit(progress,stage,max(1,int(i*0.10)),total,"building compact source block table")
    return index,table


def _machine_json_chunks(manifest: RecipeManifest, kind: str, extras: dict[str,Any], *, progress=None, cancel=None) -> Iterable[bytes]:
    """Yield compact canonical JSON without expanding millions of segment dicts.

    ``block-table-v1`` interns immutable block identity/provenance once and stores
    each segment as ``[block_index, source_offset, length, target_offset]``.
    The reader expands this back to the same RecipeManifest API, and legacy
    Alpha.1/Alpha.2 machine manifests containing full ``segments`` dictionaries
    remain readable.
    """
    block_index,block_table=_compact_block_table(manifest,kind=kind,progress=progress,cancel=cancel)
    simple={
        "block_table":block_table,
        "chain_weights":manifest.chain_weights,
        "created_utc":manifest.created_utc,
        "format":kind,
        "mode":manifest.mode,
        "notes":manifest.notes,
        "segment_encoding":"block-table-v1",
        "strategy":manifest.strategy,
        "target":manifest.target.to_dict(),
        "version":manifest.version,
        **extras,
    }
    keys=sorted([*simple.keys(),"segments_compact"])
    yield b"{"
    first_key=True
    total=len(manifest.segments)
    for key in keys:
        if not first_key:yield b"," 
        first_key=False
        yield _j(key);yield b":"
        if key!="segments_compact":
            yield _j(simple[key]);continue
        yield b"["
        for i,s in enumerate(manifest.segments,1):
            if _cancelled(cancel):raise BuildCancelled("build cancelled while packing machine manifest")
            if i>1:yield b"," 
            bi=block_index[_segment_block_key(s)]
            # Integers need no JSON escaping; avoiding millions of temporary dicts
            # and json.dumps calls is a major large-media finalization win.
            yield f"[{bi},{int(s.source_offset)},{int(s.length)},{int(s.target_offset)}]".encode("ascii")
            if i==total or i%25000==0:
                # Serialization/compression occupies the remaining 90% of this stage.
                packed_progress=min(total,max(1,int(total*0.10 + i*0.90)))
                _emit(progress,"pack_"+kind.lower(),packed_progress,total,"serializing/compressing compact machine manifest")
        yield b"]"
    yield b"}"

def _write_machine(manifest,path,kind,extras,*,progress=None,cancel=None):
    total=max(1,len(manifest.segments))
    chunks=_machine_json_chunks(manifest,kind,extras,progress=progress,cancel=cancel)
    def final_progress(done,packed_total):
        # Separate stage so the GUI does not show 100% until integrity seals are on disk.
        _emit(progress,"seal_"+kind.lower(),done,packed_total,"writing packed payload and integrity seals")
    pack_container_stream(kind,chunks,path,cancel=cancel,final_progress=final_progress)
    _emit(progress,"verify_output",0,1,f"checking {kind} integrity seals")
    verify_container_stream(path)
    _emit(progress,"verify_output",1,1,f"{kind} integrity verified")


def write_bnpm(manifest,path,*,progress=None,cancel=None):
    _write_machine(manifest,path,"BNPM",{},progress=progress,cancel=cancel)


def write_bnp2(manifest,coverage_percent,path,*,progress=None,cancel=None):
    extras={
        "coverage_percent":round(float(coverage_percent),8),
        "coverage_tag":"coverage@"+(f"{coverage_percent:.8f}".rstrip("0").rstrip("."))+"%",
        "proof_class":proof_class(coverage_percent),
    }
    _write_machine(manifest,path,"BNP2",extras,progress=progress,cancel=cancel)


def read_machine(path)->tuple[str,RecipeManifest,dict[str,Any]]:
    kind,p=unpack_container(path)
    if p.get("segment_encoding") == "block-table-v1":
        blocks=p.get("block_table",[]); segs=[]
        for row in p.get("segments_compact",[]):
            if len(row)!=4: raise ValueError("invalid compact segment row")
            bi,source_offset,length,target_offset=map(int,row)
            try:b=blocks[bi]
            except Exception as exc: raise ValueError("compact segment references invalid block-table index") from exc
            segs.append(Segment(
                chain=b["chain"],network=b["network"],height=int(b["height"]),branch_index=int(b.get("branch_index",0)),
                block_hash=b["block_hash"],representation=b["representation"],source_offset=source_offset,length=length,
                target_offset=target_offset,verified_source=bool(b.get("verified_source",True)),relation_grade=b.get("relation_grade","VERIFIED-EXPLICIT"),
                source_kind=b.get("source_kind","BLOCK_HASH"),tx_raw_hash=b.get("tx_raw_hash"),relation_id=b.get("relation_id"),
            ))
        t=TargetIdentity(**p["target"])
        m=RecipeManifest(
            format=p.get("format",kind),version=int(p.get("version",1)),mode=p.get("mode","SOLOCHAIN"),target=t,
            segments=segs,chain_weights={k:float(v) for k,v in p.get("chain_weights",{}).items()},
            strategy=p.get("strategy","FEWEST_CHUNKS"),created_utc=p.get("created_utc",""),notes=p.get("notes",{}),
        )
    else:
        m=RecipeManifest.from_dict({k:v for k,v in p.items() if k not in {"coverage_percent","coverage_tag","proof_class"}})
    return kind,m,p
