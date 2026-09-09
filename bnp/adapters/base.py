from __future__ import annotations
from dataclasses import dataclass
from typing import Protocol
from ..model import BlockRecord
@dataclass(frozen=True)
class ChainCapabilities:
    block_by_height:bool=False;block_by_hash:bool=False;header_range:bool=False;transaction:bool=False;utxo:bool=False;address:bool=False;timestamp:bool=False;alternate_blocks:bool=False;batch_requests:bool=False;local_node:bool=False
class ChainAdapter(Protocol):
    ticker:str;network:str;capabilities:ChainCapabilities
    def block_by_height(self,height:int)->BlockRecord:...
