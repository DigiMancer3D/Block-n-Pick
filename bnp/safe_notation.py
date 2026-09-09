from __future__ import annotations

def encode_bnp_safe(value: str) -> str:
    out=[]
    for ch in value:
        out.append("!!" if ch == "!" else "!" if ch == "." else ch)
    return "".join(out)

def decode_bnp_safe(value: str) -> str:
    out=[]; i=0
    while i < len(value):
        if value[i] != "!": out.append(value[i]); i += 1; continue
        if i + 1 < len(value) and value[i+1] == "!": out.append("!"); i += 2
        else: out.append("."); i += 1
    return "".join(out)
