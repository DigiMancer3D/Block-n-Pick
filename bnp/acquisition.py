from __future__ import annotations

import base64
import json
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from .model import BlockRecord
from .provider_governor import ProviderGovernor, ProviderPolicy

DEFAULT_NODE_URLS = {
    "BTC": "http://127.0.0.1:8332",
    "LTC": "http://127.0.0.1:9332",
    "XMR": "http://127.0.0.1:18081",
    "BCH": "http://127.0.0.1:8332",
    "DGB": "http://127.0.0.1:14022",
}

DEFAULT_COOKIE_PATHS = {
    "BTC": "~/.bitcoin/.cookie",
    "LTC": "~/.litecoin/.cookie",
    "BCH": "~/.bitcoin-cash-node/.cookie",
    "DGB": "~/.digibyte/.cookie",
}


class AcquisitionError(RuntimeError):
    pass


@dataclass
class ScanReport:
    chain: str
    source: str
    start_height: int
    end_height: int
    imported: int = 0
    verified: int = 0
    unverified: int = 0
    mismatches: list[int] = field(default_factory=list)
    requests: int = 0
    cancelled: bool = False
    completed: int = 0
    requested: int = 0
    partial: bool = False
    stop_reason: str | None = None
    resume_height: int | None = None


def _request(req: urllib.request.Request, timeout: float = 12.0, opener=None):
    fn = opener.open if opener is not None else urllib.request.urlopen
    try:
        return fn(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        retry_after = exc.headers.get("Retry-After") if exc.headers else None
        suffix = f" (Retry-After: {retry_after})" if retry_after else ""
        raise AcquisitionError(f"HTTP {exc.code} from {req.full_url}{suffix}") from exc
    except urllib.error.URLError as exc:
        raise AcquisitionError(f"network error contacting {req.full_url}: {exc.reason}") from exc


def _read_json(req: urllib.request.Request, timeout: float = 12.0, opener=None):
    with _request(req, timeout=timeout, opener=opener) as response:
        return json.loads(response.read().decode("utf-8"))


def _read_text(req: urllib.request.Request, timeout: float = 12.0, opener=None) -> str:
    with _request(req, timeout=timeout, opener=opener) as response:
        return response.read().decode("utf-8").strip()


class JsonRpcClient:
    def __init__(
        self,
        url: str,
        *,
        username: str | None = None,
        password: str | None = None,
        cookie_path: str | Path | None = None,
        digest_auth: bool = False,
        timeout: float = 8.0,
        opener=None,
    ):
        self.url = url.rstrip("/")
        self.timeout = float(timeout)
        self._id = 0
        if cookie_path:
            p = Path(cookie_path).expanduser()
            if p.exists():
                cookie = p.read_text(encoding="utf-8").strip()
                if ":" in cookie:
                    username, password = cookie.split(":", 1)
        self.username = username or ""
        self.password = password or ""
        self.digest_auth = bool(digest_auth)
        if opener is not None:
            self.opener = opener
        elif digest_auth and self.username:
            mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
            mgr.add_password(None, self.url, self.username, self.password)
            self.opener = urllib.request.build_opener(urllib.request.HTTPDigestAuthHandler(mgr))
        else:
            self.opener = urllib.request.build_opener()

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "User-Agent": "Block-n-Pick/0.1"}
        if self.username and not self.digest_auth:
            token = base64.b64encode(f"{self.username}:{self.password}".encode()).decode()
            headers["Authorization"] = "Basic " + token
        return headers

    def call(self, method: str, params=None):
        self._id += 1
        payload = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or []}
        req = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        obj = _read_json(req, timeout=self.timeout, opener=self.opener)
        if obj.get("error"):
            raise AcquisitionError(f"RPC {method} failed: {obj['error']}")
        return obj.get("result")

    def batch(self, calls: list[tuple[str, list]]):
        payload = []
        ids = []
        for method, params in calls:
            self._id += 1
            ids.append(self._id)
            payload.append({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params})
        req = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        data = _read_json(req, timeout=self.timeout, opener=self.opener)
        by_id = {row.get("id"): row for row in data}
        out = []
        for ident in ids:
            row = by_id.get(ident)
            if row is None:
                raise AcquisitionError(f"RPC batch response missing id {ident}")
            if row.get("error"):
                raise AcquisitionError(f"RPC batch call failed: {row['error']}")
            out.append(row.get("result"))
        return out


class BitcoinFamilyRPC:
    def __init__(self, chain: str, client: JsonRpcClient):
        self.chain = chain.upper()
        self.client = client

    def tip_height(self) -> int:
        return int(self.client.call("getblockcount"))

    def block_by_height(self, height: int) -> BlockRecord:
        h = str(self.client.call("getblockhash", [int(height)]))
        return BlockRecord(self.chain, "mainnet", int(height), h, source=f"local-rpc:{self.chain}", verified=True)

    def scan_range(self, start: int, end: int, progress=None, cancel=None) -> Iterable[BlockRecord]:
        heights = list(range(int(start), int(end) + 1))
        done = 0
        for base in range(0, len(heights), 200):
            if cancel and cancel.is_set():
                break
            chunk = heights[base : base + 200]
            hashes = self.client.batch([("getblockhash", [h]) for h in chunk])
            # Header timestamps make EPOCH_LINK usable with local Core-family
            # nodes.  Keep hash acquisition authoritative even when an older or
            # unusual RPC endpoint cannot provide batched verbose headers.
            try:
                headers = self.client.batch([("getblockheader", [str(h), True]) for h in hashes])
            except Exception:
                headers = [None] * len(hashes)
            for height, block_hash, header in zip(chunk, hashes, headers):
                done += 1
                if progress:
                    progress(done, len(heights), height)
                timestamp = int(header.get("time")) if isinstance(header, dict) and header.get("time") is not None else None
                yield BlockRecord(
                    self.chain, "mainnet", height, str(block_hash),
                    source=f"local-rpc:{self.chain}", verified=True, timestamp=timestamp,
                )

    def get_block_filter(self, block_hash: str, filter_type: str = "basic") -> dict:
        return dict(self.client.call("getblockfilter", [block_hash, filter_type]))


class MoneroRPC:
    def __init__(self, client: JsonRpcClient):
        self.client = client

    def tip_height(self) -> int:
        result = self.client.call("get_block_count", {})
        return int(result["count"]) - 1

    def scan_range(self, start: int, end: int, progress=None, cancel=None) -> Iterable[BlockRecord]:
        total = int(end) - int(start) + 1
        done = 0
        for lo in range(int(start), int(end) + 1, 1000):
            if cancel and cancel.is_set():
                break
            hi = min(int(end), lo + 999)
            result = self.client.call(
                "get_block_headers_range",
                {"start_height": lo, "end_height": hi, "fill_pow_hash": False},
            )
            if result.get("status") not in (None, "OK"):
                raise AcquisitionError(f"monerod returned status {result.get('status')}")
            for header in result.get("headers", []):
                height = int(header["height"])
                done += 1
                if progress:
                    progress(done, total, height)
                yield BlockRecord(
                    "XMR", "mainnet", height, str(header["hash"]),
                    status="stale" if header.get("orphan_status") else "canonical",
                    source="monerod", verified=not bool(result.get("untrusted")),
                    timestamp=int(header["timestamp"]) if header.get("timestamp") is not None else None,
                )


class EsploraProvider:
    def __init__(
        self,
        name: str,
        base_url: str,
        *,
        min_interval: float = 1.2,
        request_budget: int | None = 500,
        opener=None,
    ):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.governor = ProviderGovernor(ProviderPolicy(min_interval=min_interval, daily_budget=request_budget))
        self.requests = 0
        self.opener = opener

    def _get_json(self, path: str):
        self.governor.acquire()
        self.requests += 1
        req = urllib.request.Request(self.base_url + path, headers={"User-Agent": "Block-n-Pick/0.1"})
        return _read_json(req, opener=self.opener)

    def _get_text(self, path: str):
        self.governor.acquire()
        self.requests += 1
        req = urllib.request.Request(self.base_url + path, headers={"User-Agent": "Block-n-Pick/0.1"})
        return _read_text(req, opener=self.opener)

    def tip_height(self) -> int:
        return int(self._get_text("/blocks/tip/height"))

    def batch_descending(self, start_height: int) -> dict[int, tuple[str, int | None]]:
        rows = self._get_json(f"/blocks/{int(start_height)}")
        out = {}
        for row in rows:
            height = int(row["height"])
            block_hash = str(row.get("id") or row.get("hash"))
            timestamp = row.get("timestamp")
            out[height] = (block_hash, None if timestamp is None else int(timestamp))
        return out


class BlockCypherLitecoinProvider:
    """Low-volume Litecoin seed source.  Records are intentionally unverified
    until another source agrees; it is useful for corpus staging, not full recipes.
    """
    def __init__(self, min_interval: float = 1.2, request_budget: int | None = 30, opener=None):
        self.governor = ProviderGovernor(ProviderPolicy(min_interval=min_interval, daily_budget=request_budget))
        self.requests = 0
        self.opener = opener
        self.base_url = "https://api.blockcypher.com/v1/ltc/main"

    def tip_height(self) -> int:
        self.governor.acquire(); self.requests += 1
        req = urllib.request.Request(self.base_url, headers={"User-Agent": "Block-n-Pick/0.1"})
        return int(_read_json(req, opener=self.opener)["height"])

    def block_by_height(self, height: int) -> BlockRecord:
        self.governor.acquire(); self.requests += 1
        req = urllib.request.Request(
            f"{self.base_url}/blocks/{int(height)}?txstart=1&limit=1",
            headers={"User-Agent": "Block-n-Pick/0.1"},
        )
        row = _read_json(req, opener=self.opener)
        timestamp = None
        if row.get("time"):
            try:
                timestamp = int(datetime.fromisoformat(str(row["time"]).replace("Z", "+00:00")).timestamp())
            except Exception:
                timestamp = None
        return BlockRecord(
            "LTC", "mainnet", int(height), str(row["hash"]),
            source="blockcypher-seed", verified=False, timestamp=timestamp,
        )


def scan_btc_public_consensus(
    db,
    start: int,
    end: int,
    *,
    min_interval: float = 1.2,
    request_budget: int = 500,
    progress: Callable[[int, int, int], None] | None = None,
    cancel: threading.Event | None = None,
    provider_a: EsploraProvider | None = None,
    provider_b: EsploraProvider | None = None,
) -> ScanReport:
    if start < 0 or end < start:
        raise ValueError("scan range must satisfy 0 <= start <= end")
    a = provider_a or EsploraProvider("mempool.space", "https://mempool.space/api", min_interval=min_interval, request_budget=request_budget)
    b = provider_b or EsploraProvider("Blockstream", "https://blockstream.info/api", min_interval=min_interval, request_budget=request_budget)
    report = ScanReport("BTC", "public-consensus:mempool.space+blockstream", start, end)
    total = end - start + 1
    report.requested = total
    done = 0
    current = end
    while current >= start:
        if cancel and cancel.is_set():
            report.cancelled = True
            report.partial = done < total
            report.stop_reason = "cancelled"
            break
        try:
            rows_a = a.batch_descending(current)
            rows_b = b.batch_descending(current)
        except AcquisitionError as exc:
            report.partial = done < total
            report.stop_reason = str(exc)
            report.resume_height = current
            break
        floor = max(start, current - 9)
        # Commit every provider batch.  A later 429/network failure therefore
        # never discards already cross-checked BTC history.
        with db.conn:
            for height in range(current, floor - 1, -1):
                if cancel and cancel.is_set():
                    report.cancelled = True
                    report.stop_reason = "cancelled"
                    break
                va = rows_a.get(height)
                vb = rows_b.get(height)
                if not va or not vb:
                    report.mismatches.append(height)
                elif va[0].lower() != vb[0].lower():
                    report.mismatches.append(height)
                else:
                    db.add(BlockRecord(
                        "BTC", "mainnet", height, va[0], source=report.source,
                        verified=True, timestamp=va[1] if va[1] is not None else vb[1],
                    ))
                    report.imported += 1
                    report.verified += 1
                done += 1
                report.completed = done
                if progress:
                    progress(done, total, height)
        if report.cancelled:
            report.partial = done < total
            report.resume_height = floor - 1 if floor > start else None
            break
        current = floor - 1
    report.requests = a.requests + b.requests
    if done < total and not report.partial:
        report.partial = True
    if report.partial and report.resume_height is None and current >= start:
        report.resume_height = current
    return report


def scan_ltc_blockcypher(
    db,
    start: int,
    end: int,
    *,
    request_budget: int = 30,
    progress: Callable[[int, int, int], None] | None = None,
    cancel: threading.Event | None = None,
    provider: BlockCypherLitecoinProvider | None = None,
    commit_batch: int = 10,
    descending: bool = False,
) -> ScanReport:
    """Stage Litecoin public records while preserving partial progress.

    BlockCypher is intentionally a single-source, unverified seed.  Successful
    records are committed in small batches.  HTTP 429 or another provider stop
    produces a resumable partial report instead of discarding prior work.
    """
    if start < 0 or end < start:
        raise ValueError("scan range must satisfy 0 <= start <= end")
    provider = provider or BlockCypherLitecoinProvider(request_budget=min(int(request_budget), 30))
    report = ScanReport("LTC", "blockcypher-seed", start, end)
    total = end - start + 1
    report.requested = total
    pending: list[BlockRecord] = []
    done = 0

    def flush():
        nonlocal pending
        if not pending:
            return
        with db.conn:
            for rec in pending:
                db.add(rec)
        pending = []

    heights = range(end, start - 1, -1) if descending else range(start, end + 1)
    for height in heights:
        if cancel and cancel.is_set():
            report.cancelled = True
            report.partial = done < total
            report.stop_reason = "cancelled"
            report.resume_height = height - 1 if descending else height
            break
        try:
            rec = provider.block_by_height(height)
        except AcquisitionError as exc:
            flush()
            report.partial = True
            report.stop_reason = str(exc)
            report.resume_height = height - 1 if descending else height
            break
        pending.append(rec)
        report.imported += 1
        report.unverified += 1
        done += 1
        report.completed = done
        if len(pending) >= max(1, int(commit_batch)):
            flush()
        if progress:
            progress(done, total, height)
    flush()
    report.requests = provider.requests
    if done < total and not report.partial and not report.cancelled:
        report.partial = True
    return report


def make_local_scanner(
    chain: str,
    *,
    url: str | None = None,
    cookie_path: str | Path | None = None,
    username: str | None = None,
    password: str | None = None,
    timeout: float = 8.0,
    opener=None,
):
    chain = chain.upper()
    base = (url or DEFAULT_NODE_URLS[chain]).rstrip("/")
    if chain == "XMR":
        rpc_url = base if base.endswith("/json_rpc") else base + "/json_rpc"
        client = JsonRpcClient(
            rpc_url, username=username, password=password,
            digest_auth=bool(username), timeout=timeout, opener=opener,
        )
        return MoneroRPC(client)
    if cookie_path is None:
        default_cookie = DEFAULT_COOKIE_PATHS.get(chain)
        if default_cookie and Path(default_cookie).expanduser().exists():
            cookie_path = default_cookie
    client = JsonRpcClient(
        base, username=username, password=password,
        cookie_path=cookie_path, timeout=timeout, opener=opener,
    )
    return BitcoinFamilyRPC(chain, client)


def scan_local(
    db,
    chain: str,
    start: int,
    end: int,
    *,
    url: str | None = None,
    cookie_path: str | Path | None = None,
    username: str | None = None,
    password: str | None = None,
    progress=None,
    cancel: threading.Event | None = None,
    scanner=None,
) -> ScanReport:
    if start < 0 or end < start:
        raise ValueError("scan range must satisfy 0 <= start <= end")
    scanner = scanner or make_local_scanner(
        chain, url=url, cookie_path=cookie_path, username=username, password=password,
    )
    report = ScanReport(chain.upper(), "local-node", start, end)
    report.requested = end - start + 1
    with db.conn:
        for record in scanner.scan_range(start, end, progress=progress, cancel=cancel):
            db.add(record)
            report.imported += 1
            report.completed += 1
            if record.verified:
                report.verified += 1
            else:
                report.unverified += 1
    report.cancelled = bool(cancel and cancel.is_set())
    report.partial = report.completed < report.requested
    if report.cancelled:
        report.stop_reason = "cancelled"
        report.resume_height = start + report.completed
    return report
