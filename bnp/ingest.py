from __future__ import annotations
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from .hashing import decode_base64_flexible, hash_bundle
from .safe_notation import decode_bnp_safe, encode_bnp_safe
@dataclass
class IngestedTarget:
    raw:bytes; safe_name:str; source_kind:str; source_value:str|None
    @property
    def hashes(self): return hash_bundle(self.raw)
def from_file(path:str|Path)->IngestedTarget:
    p=Path(path); return IngestedTarget(p.read_bytes(),encode_bnp_safe(p.name),"FILE",str(p))
def from_base64(text:str|bytes,name:str="payload!bin")->IngestedTarget: return IngestedTarget(decode_base64_flexible(text),name,"B64",None)
def from_url_literal(safe_url:str,name:str="url!txt")->IngestedTarget: return IngestedTarget(decode_bnp_safe(safe_url).encode(),name,"URL",safe_url)
def from_fetch(safe_url:str,name:str|None=None,timeout:float=20.0)->IngestedTarget:
    url=decode_bnp_safe(safe_url); req=urllib.request.Request(url,headers={"User-Agent":"Block-n-Pick/0.1"})
    with urllib.request.urlopen(req,timeout=timeout) as resp: data=resp.read(); final_url=resp.geturl()
    if name is None: name=encode_bnp_safe(final_url.rsplit("/",1)[-1] or "fetched.bin")
    return IngestedTarget(data,name,"FETCH",safe_url)
