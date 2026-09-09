from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

SUPPORTED_CHAINS = ("BTC", "LTC", "XMR", "BCH", "DGB")
SUPPORTED_REPRESENTATIONS = ("RAW", "B64")
# Alpha.6 intentionally narrows the active prototype surface to the two modes
# that have passed real-media acceptance.  Deferred selectors remain readable
# for backward compatibility with older recipes and are documented under
# "Expected Upgrades" rather than exposed for new builds.
ACTIVE_BUILD_MODES = ("SOLOCHAIN", "CROSSCHAIN")
DEFERRED_EXPERIMENTAL_MODES = (
    "LOCKED_LINK", "TX_LINKED", "EPOCH_LINK", "USER_PICKED",
    "BINARY_CROSSCHAIN", "TRINARY_CROSSCHAIN", "GENERIC_CROSSCHAIN",
)
SUPPORTED_MODES = ACTIVE_BUILD_MODES + DEFERRED_EXPERIMENTAL_MODES

@dataclass(frozen=True)
class BlockRecord:
    chain: str
    network: str
    height: int
    block_hash: str
    branch_index: int = 0
    status: str = "canonical"
    source: str = "import"
    verified: bool = True
    timestamp: int | None = None

    def normalized(self) -> "BlockRecord":
        h = self.block_hash.lower().strip()
        if len(h) != 64:
            raise ValueError(f"block hash must be 64 hex characters, got {len(h)}")
        bytes.fromhex(h)
        return BlockRecord(
            chain=self.chain.upper().strip(), network=self.network.lower().strip(),
            height=int(self.height), block_hash=h, branch_index=int(self.branch_index),
            status=self.status.lower().strip(), source=self.source, verified=bool(self.verified),
            timestamp=None if self.timestamp is None else int(self.timestamp),
        )

@dataclass(frozen=True)
class Segment:
    chain: str
    network: str
    height: int
    branch_index: int
    block_hash: str
    representation: str
    source_offset: int
    length: int
    target_offset: int
    verified_source: bool = True
    relation_grade: str = "VERIFIED-EXPLICIT"
    source_kind: str = "BLOCK_HASH"
    tx_raw_hash: str | None = None
    relation_id: str | None = None

    def to_dict(self) -> dict[str, Any]: return asdict(self)
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Segment": return cls(**data)

@dataclass
class TargetIdentity:
    safe_name: str
    raw_size: int
    representation_size: int
    sha256_raw: str
    sha256_b64: str
    representation: str
    source_kind: str = "FILE"
    source_value: str | None = None
    def to_dict(self) -> dict[str, Any]: return asdict(self)

@dataclass
class RecipeManifest:
    format: str
    version: int
    mode: str
    target: TargetIdentity
    segments: list[Segment] = field(default_factory=list)
    chain_weights: dict[str, float] = field(default_factory=dict)
    strategy: str = "FEWEST_CHUNKS"
    created_utc: str = ""
    notes: dict[str, Any] = field(default_factory=dict)
    def to_dict(self) -> dict[str, Any]: return asdict(self)
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RecipeManifest":
        data = dict(data)
        data["target"] = TargetIdentity(**data["target"])
        data["segments"] = [Segment.from_dict(x) for x in data.get("segments", [])]
        return cls(**data)

def proof_class(coverage_percent: float) -> str:
    if coverage_percent <= 0: return "NONE"
    if coverage_percent <= 32: return "MINOR"
    if coverage_percent <= 67: return "BASIC"
    if coverage_percent < 100: return "GENERIC"
    return "FULL"
