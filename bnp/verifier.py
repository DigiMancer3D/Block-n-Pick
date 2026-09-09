from __future__ import annotations
import base64
from dataclasses import dataclass,field
from pathlib import Path
from .corpus import CorpusDB
from .hashing import sha256_hex
from .model import RecipeManifest
from .representations import represent_segment_source
@dataclass
class VerifyReport:
    container_kind:str;integrity_ok:bool;source_records_ok:bool;reconstructed_bytes:int;expected_representation_bytes:int
    sha256_raw_ok:bool|None=None;sha256_b64_ok:bool|None=None;target_identity_ok:bool|None=None;coverage_percent:float=0.0;warnings:list[str]=field(default_factory=list)
def reconstruct_representation(manifest,corpus=None,require_verified_sources=True,partial=False):
    segs=sorted(manifest.segments,key=lambda s:s.target_offset);out=bytearray(manifest.target.representation_size if partial else 0);source_ok=True;expected=0
    for s in segs:
        if corpus is not None:
            r=corpus.resolve(s.chain,s.network,s.block_hash)
            if r is None or (require_verified_sources and not r.verified):source_ok=False
        data=represent_segment_source(s);frag=data[s.source_offset:s.source_offset+s.length]
        if len(frag)!=s.length:raise ValueError("segment range exceeds represented block hash")
        if partial:out[s.target_offset:s.target_offset+s.length]=frag
        else:
            if s.target_offset!=expected:raise ValueError("full recipe has non-contiguous target offsets")
            out.extend(frag);expected+=s.length
    return out,source_ok
def verify_full(manifest,corpus=None):
    mode = manifest.mode.upper()
    user_unverified = mode in {"USER_PICKED", "TX_LINKED"}
    rep_bytes,source_ok=reconstruct_representation(
        manifest, corpus, require_verified_sources=not user_unverified, partial=False
    );rep=manifest.target.representation.upper()
    raw=bytes(rep_bytes) if rep=="RAW" else base64.b64decode(bytes(rep_bytes),validate=True)
    ro=sha256_hex(raw)==manifest.target.sha256_raw if manifest.target.sha256_raw else None;bo=sha256_hex(base64.b64encode(raw))==manifest.target.sha256_b64 if manifest.target.sha256_b64 else None
    report=VerifyReport(manifest.format,True,source_ok,len(rep_bytes),manifest.target.representation_size,ro,bo,(ro is not False and bo is not False),100.0 if len(rep_bytes)==manifest.target.representation_size else 0.0)
    if corpus is None:report.warnings.append("Source hashes were used literally; no local corpus was supplied for independent chain/source confirmation.")
    if mode == "USER_PICKED":
        report.warnings.append("USER_PICKED association is UNVERIFIED-USER-PROOF; source presence may be checked but BnP does not certify the user-supplied relationship.")
    if mode == "TX_LINKED":
        grade = (manifest.notes.get("alignment") or {}).get("relation_grade", "UNVERIFIED-USER-PROOF")
        report.warnings.append(f"TX_LINKED relationship grade is {grade}; reconstruction verification does not independently promote relationship evidence.")
    return report,raw
def verify_proof(manifest,claimed_coverage,target_file=None,corpus=None):
    mode = manifest.mode.upper()
    user_unverified = mode in {"USER_PICKED", "TX_LINKED"}
    _,source_ok=reconstruct_representation(
        manifest, corpus, require_verified_sources=not user_unverified, partial=True
    );covered=sum(s.length for s in manifest.segments);coverage=100.0*covered/manifest.target.representation_size if manifest.target.representation_size else 100.0
    report=VerifyReport("BNP2",True,source_ok,covered,manifest.target.representation_size,coverage_percent=coverage)
    if abs(coverage-claimed_coverage)>1e-6:report.warnings.append("Claimed coverage does not equal segment coverage.")
    if target_file is not None:
        raw=Path(target_file).read_bytes();tr=raw if manifest.target.representation=="RAW" else base64.b64encode(raw);ok=True
        for s in manifest.segments:
            src=represent_segment_source(s)[s.source_offset:s.source_offset+s.length]
            if src!=tr[s.target_offset:s.target_offset+s.length]:ok=False;break
        report.sha256_raw_ok=sha256_hex(raw)==manifest.target.sha256_raw;report.sha256_b64_ok=sha256_hex(base64.b64encode(raw))==manifest.target.sha256_b64;report.target_identity_ok=ok and report.sha256_raw_ok and report.sha256_b64_ok
    else:report.warnings.append("No target object supplied: full-file association cannot be independently confirmed.")
    if corpus is None:report.warnings.append("No local corpus supplied for independent source membership confirmation.")
    if mode == "USER_PICKED":
        report.warnings.append("USER_PICKED association is UNVERIFIED-USER-PROOF and is not promoted to verified by this check.")
    if mode == "TX_LINKED":
        grade = (manifest.notes.get("alignment") or {}).get("relation_grade", "UNVERIFIED-USER-PROOF")
        report.warnings.append(f"TX_LINKED relationship grade is {grade} and is not promoted by this proof verification.")
    return report
