from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import CANONICAL_NOTICE, __version__
from .acquisition import (
    DEFAULT_NODE_URLS,
    BlockCypherLitecoinProvider,
    make_local_scanner,
    scan_btc_public_consensus,
    scan_ltc_blockcypher,
    scan_local,
)
from .container_format import inspect_container
from .corpus import CorpusDB, load_records
from .corpus_exchange import (
    export_corpus_json, export_hash_stream, export_height_list,
    export_per_chain_hash_streams, export_clean_hash_stream, load_manual_explorer_records,
)
from .engine import build_manifest, save_outputs
from .ingest import from_base64, from_fetch, from_file, from_url_literal
from .recipe_io import parse_bnp, read_machine
from .recipe_resolver import resolve_recipe, load_recipe_artifact
from .mine_builder import mine_recipe, verify_recipe
from .starter_corpus import import_all_starters, import_starter
from .verifier import verify_full, verify_proof
from .tx_links import (
    clear_relations, list_relations, load_tx_link_evidence, save_template,
    save_tx_link_evidence, store_relations,
)

DEFAULT_DB = Path.home() / ".local/share/block-n-pick/corpus.sqlite3"


def parse_weights(value):
    if not value:
        return {}
    out = {}
    for item in value.split("!"):
        pct, chain = item.split("%", 1)
        out[chain.upper()] = float(pct)
    if abs(sum(out.values()) - 100) > 1e-9:
        raise ValueError("cross-chain percentages must total 100")
    return out


def _add_node_args(parser):
    parser.add_argument("--rpc-url")
    parser.add_argument("--cookie")
    parser.add_argument("--rpc-user")
    parser.add_argument("--rpc-password")


def main(argv=None):
    p = argparse.ArgumentParser(prog="block-n-pick")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--db", default=str(DEFAULT_DB))
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("notice")
    imp = sub.add_parser("import-corpus")
    imp.add_argument("file")
    imp.add_argument("--chain")
    imp.add_argument("--network", default="mainnet")
    sub.add_parser("corpus-counts")

    manual = sub.add_parser("import-manual", help="import copied/explorer block data as unverified")
    manual.add_argument("file")
    manual.add_argument("--chain", required=True, choices=["BTC", "LTC", "XMR", "BCH", "DGB"])
    manual.add_argument("--network", default="mainnet")
    manual.add_argument("--start-height", type=int)
    manual.add_argument("--descending", action="store_true")

    exp = sub.add_parser("export-corpus")
    exp.add_argument("file")
    exp.add_argument("--chains", help="! separated chain list; default ALL")
    heights = sub.add_parser("export-heights")
    heights.add_argument("file")
    heights.add_argument("--chains", help="! separated chain list; default ALL")
    stream = sub.add_parser("export-hash-stream")
    stream.add_argument("file")
    stream.add_argument("--chains", help="! separated chain list; default ALL")
    streams = sub.add_parser("export-hash-streams")
    streams.add_argument("directory")
    clean_stream = sub.add_parser("export-clean-hash-stream")
    clean_stream.add_argument("file")
    clean_stream.add_argument("--chains", help="! separated chain list; default ALL")

    tximp = sub.add_parser("import-tx-links", help="import TX-Link relationship evidence as user/unverified evidence")
    tximp.add_argument("file")
    txexp = sub.add_parser("export-tx-links")
    txexp.add_argument("file")
    txlist = sub.add_parser("list-tx-links")
    txtemplate = sub.add_parser("tx-link-template")
    txtemplate.add_argument("file")
    sub.add_parser("clear-tx-links")

    starter = sub.add_parser("import-starter")
    starter.add_argument("--chain", choices=["BTC", "LTC", "XMR", "BCH", "DGB", "ALL"], default="ALL")

    prune = sub.add_parser("prune-chain")
    prune.add_argument("chain")

    tip = sub.add_parser("tip")
    tip.add_argument("--chain", required=True, choices=["BTC", "LTC", "XMR", "BCH", "DGB"])
    tip.add_argument("--source", choices=["local", "btc-public", "ltc-blockcypher"], default="local")
    tip.add_argument("--budget", type=int, default=500)
    _add_node_args(tip)

    scan = sub.add_parser("scan")
    scan.add_argument("--chain", required=True, choices=["BTC", "LTC", "XMR", "BCH", "DGB"])
    scan.add_argument("--source", choices=["local", "btc-public", "ltc-blockcypher"], default="local")
    scan.add_argument("--start", type=int, required=True)
    scan.add_argument("--end", type=int, required=True)
    scan.add_argument("--budget", type=int, default=500)
    _add_node_args(scan)

    latest = sub.add_parser("scan-latest")
    latest.add_argument("--chain", required=True, choices=["BTC", "LTC", "XMR", "BCH", "DGB"])
    latest.add_argument("--source", choices=["local", "btc-public", "ltc-blockcypher"], default="local")
    latest.add_argument("--count", type=int, default=500)
    latest.add_argument("--budget", type=int, default=500)
    _add_node_args(latest)

    filt = sub.add_parser("bip158")
    filt.add_argument("block_hash")
    filt.add_argument("--rpc-url", default=DEFAULT_NODE_URLS["BTC"])
    filt.add_argument("--cookie")
    filt.add_argument("--rpc-user")
    filt.add_argument("--rpc-password")

    b = sub.add_parser("build")
    b.add_argument("target")
    b.add_argument("--input-kind", choices=["FILE", "B64", "URL", "FETCH"], default="FILE")
    b.add_argument("--name", help="safe BnP name for B64/URL input")
    b.add_argument("--chains", required=True)
    b.add_argument("--rep", choices=["RAW", "B64"], default="RAW")
    b.add_argument("--mode", choices=["SOLOCHAIN","CROSSCHAIN"], default="SOLOCHAIN")
    b.add_argument("--weights")
    b.add_argument("--proof", type=float)
    b.add_argument("--max-chunk", type=int, default=3)
    b.add_argument("--out", required=True)

    i = sub.add_parser("inspect")
    i.add_argument("recipe")
    v = sub.add_parser("verify")
    v.add_argument("recipe")
    v.add_argument("--target")
    v.add_argument("--no-corpus", action="store_true")
    v.add_argument("--write-output")

    rr = sub.add_parser("resolve", help="preflight/resolve .bnp/.bnpm/.bnp2 by content")
    rr.add_argument("recipe")
    rr.add_argument("--no-corpus", action="store_true", help="do not corroborate against the local corpus")
    rr.add_argument("--sources", action="store_true", help="include per-source resolution rows")

    mine = sub.add_parser("mine", help="Mine-Build a BnP recipe/proof into output material")
    mine.add_argument("recipe")
    mine.add_argument("--action", choices=["verify","build","build-verify"], default="build-verify")
    mine.add_argument("--out-dir")
    mine.add_argument("--output")
    mine.add_argument("--no-corpus", action="store_true")

    a = p.parse_args(argv)
    if a.cmd == "notice":
        print(CANONICAL_NOTICE)
        return 0

    db = CorpusDB(a.db)
    try:
        if a.cmd == "import-corpus":
            imported = db.add_many(load_records(a.file, a.chain, a.network))
            tx_imported = 0
            try:
                data = json.loads(Path(a.file).read_text(encoding="utf-8"))
            except Exception:
                data = None
            if isinstance(data, dict) and data.get("format") == "BNP-CORPUS-EXPORT" and data.get("tx_links"):
                relations = load_tx_link_evidence(a.file, trusted=False)
                tx_imported = store_relations(db, relations)
            print(json.dumps({"imported": imported, "tx_links_imported": tx_imported, "counts": db.count_details()}, indent=2))
            return 0
        if a.cmd == "corpus-counts":
            print(json.dumps(db.count_details(), indent=2))
            return 0
        if a.cmd == "import-manual":
            records = load_manual_explorer_records(
                a.file, default_chain=a.chain, default_network=a.network,
                start_height=a.start_height, descending=a.descending,
            )
            print(json.dumps({
                "imported": db.add_many(records), "verified": 0,
                "source": "manual/explorer", "counts": db.count_details(),
            }, indent=2))
            return 0
        if a.cmd in {"export-corpus", "export-heights", "export-hash-stream", "export-clean-hash-stream"}:
            chains = [c.upper() for c in a.chains.split("!")] if a.chains else None
            if a.cmd == "export-corpus":
                result = {"records": export_corpus_json(db, a.file, chains=chains), "file": a.file}
            elif a.cmd == "export-heights":
                result = {"heights": export_height_list(db, a.file, chains=chains), "file": a.file}
            elif a.cmd == "export-hash-stream":
                result = {"hashes": export_hash_stream(db, a.file, chains=chains), "file": a.file}
            else:
                result = {"hashes": export_clean_hash_stream(db, a.file, chains=chains), "file": a.file, "encoding": "BNP-CLEAN-STREAM-o0-v1"}
            print(json.dumps(result, indent=2))
            return 0
        if a.cmd == "export-hash-streams":
            print(json.dumps({"streams": export_per_chain_hash_streams(db, a.directory), "directory": a.directory}, indent=2))
            return 0
        if a.cmd == "import-tx-links":
            relations = load_tx_link_evidence(a.file, trusted=False)
            print(json.dumps({"relations_imported": store_relations(db, relations), "relationship_grade_policy": "manual verified claims downgrade to UNVERIFIED-USER-PROOF"}, indent=2))
            return 0
        if a.cmd == "export-tx-links":
            relations = list_relations(db)
            print(json.dumps({"relations": save_tx_link_evidence(a.file, relations), "file": a.file}, indent=2))
            return 0
        if a.cmd == "list-tx-links":
            print(json.dumps({"relations": [r.to_dict() for r in list_relations(db)]}, indent=2))
            return 0
        if a.cmd == "tx-link-template":
            save_template(a.file)
            print(json.dumps({"file": a.file, "format": "BNP-TX-LINK-EVIDENCE"}, indent=2))
            return 0
        if a.cmd == "clear-tx-links":
            print(json.dumps({"deleted": clear_relations(db)}, indent=2))
            return 0
        if a.cmd == "import-starter":
            if a.chain == "ALL":
                imported = import_all_starters(db)
            else:
                imported = {a.chain: import_starter(db, a.chain)}
            print(json.dumps({"starter": "builtin-starter-v1", "imported": imported, "counts": db.count_details()}, indent=2))
            return 0
        if a.cmd == "prune-chain":
            print(json.dumps({"chain": a.chain.upper(), "deleted": db.prune_chain(a.chain)}, indent=2))
            return 0

        if a.cmd in {"tip", "scan", "scan-latest"}:
            chain = a.chain.upper()
            source = a.source

            def get_tip():
                if source == "btc-public":
                    if chain != "BTC":
                        raise SystemExit("btc-public source requires --chain BTC")
                    from .acquisition import EsploraProvider
                    return EsploraProvider("mempool.space", "https://mempool.space/api", request_budget=a.budget).tip_height()
                if source == "ltc-blockcypher":
                    if chain != "LTC":
                        raise SystemExit("ltc-blockcypher source requires --chain LTC")
                    return BlockCypherLitecoinProvider(request_budget=min(a.budget, 30)).tip_height()
                scanner = make_local_scanner(
                    chain, url=a.rpc_url, cookie_path=a.cookie,
                    username=a.rpc_user, password=a.rpc_password,
                )
                return scanner.tip_height()

            if a.cmd == "tip":
                print(json.dumps({"chain": chain, "source": source, "tip_height": get_tip()}, indent=2))
                return 0

            if a.cmd == "scan-latest":
                if a.count <= 0:
                    raise SystemExit("--count must be positive")
                end = get_tip()
                start = max(0, end - a.count + 1)
            else:
                start, end = a.start, a.end

            if source == "btc-public":
                if chain != "BTC":
                    raise SystemExit("btc-public source requires --chain BTC")
                report = scan_btc_public_consensus(db, start, end, request_budget=a.budget)
            elif source == "ltc-blockcypher":
                if chain != "LTC":
                    raise SystemExit("ltc-blockcypher source requires --chain LTC")
                report = scan_ltc_blockcypher(
                    db, start, end, request_budget=min(a.budget, 30),
                    descending=(a.cmd == "scan-latest"),
                )
            else:
                report = scan_local(
                    db, chain, start, end, url=a.rpc_url, cookie_path=a.cookie,
                    username=a.rpc_user, password=a.rpc_password,
                )
            payload = report if isinstance(report, dict) else report.__dict__
            payload["counts"] = db.count_details()
            print(json.dumps(payload, indent=2))
            return 0

        if a.cmd == "bip158":
            scanner = make_local_scanner(
                "BTC", url=a.rpc_url, cookie_path=a.cookie,
                username=a.rpc_user, password=a.rpc_password,
            )
            print(json.dumps(scanner.get_block_filter(a.block_hash), indent=2))
            return 0

        if a.cmd == "build":
            if not 1 <= a.max_chunk <= 19:
                raise SystemExit("--max-chunk must be between 1 and 19")
            if a.input_kind == "FILE":
                ing = from_file(a.target)
            elif a.input_kind == "B64":
                text = Path(a.target[1:]).read_text(encoding="utf-8") if a.target.startswith("@") else a.target
                ing = from_base64(text, a.name or "payload!bin")
            elif a.input_kind == "URL":
                ing = from_url_literal(a.target, a.name or "url!txt")
            else:
                ing = from_fetch(a.target, a.name)
            chains = [c.upper() for c in a.chains.split("!")]
            weights = parse_weights(a.weights)
            mode = a.mode.upper()
            mode = "CROSSCHAIN" if len(chains) > 1 and mode == "SOLOCHAIN" else mode
            if len(chains) > 9:
                raise SystemExit("BnP supports up to 9 chains")
            available = db.record_count(chains, verified_only=True)
            if available <= 0:
                raise SystemExit("no verified corpus records for selected chain(s); scan or import corpus first")
            m, c = build_manifest(
                raw=ing.raw, safe_name=ing.safe_name, source_kind=ing.source_kind,
                source_value=ing.source_value, corpus=db, chains=chains,
                representation=a.rep, mode=mode, weights=weights or None,
                proof_coverage=a.proof, max_chunk=a.max_chunk,
            )
            outs = save_outputs(m, c, a.out)
            print(json.dumps({
                "coverage_percent": c, "segments": len(m.segments),
                "outputs": [str(x) for x in outs], "sha256_raw": m.target.sha256_raw,
                "sha256_b64": m.target.sha256_b64, "notes": m.notes,
            }, indent=2))
            return 0

        if a.cmd == "resolve":
            corpus = None if a.no_corpus else db
            _manifest, report, _payload = resolve_recipe(a.recipe, corpus, include_sources=a.sources)
            print(json.dumps(report.to_dict(include_sources=a.sources), indent=2))
            return 0 if report.ok else 2

        if a.cmd == "mine":
            corpus = None if a.no_corpus else db
            if a.action == "verify":
                result = verify_recipe(a.recipe, corpus=corpus)
            else:
                result = mine_recipe(
                    a.recipe, corpus=corpus, output_dir=a.out_dir, output_path=a.output,
                    verify_after_write=(a.action == "build-verify"),
                )
            print(json.dumps(result.to_dict(), indent=2))
            return 0 if result.success else 2

        if a.cmd == "inspect":
            _m, report, _payload = resolve_recipe(a.recipe, db, include_sources=False)
            print(json.dumps(report.to_dict(include_sources=False), indent=2))
            return 0 if report.ok else 2

        if a.cmd == "verify":
            corpus = None if a.no_corpus else db
            kind, m, payload = load_recipe_artifact(a.recipe)
            if kind in {"BNP", "BNPM"}:
                r, raw = verify_full(m, corpus)
            else:
                r = verify_proof(m, float(payload["coverage_percent"]), a.target, corpus); raw = None
            if a.write_output and raw is not None:
                Path(a.write_output).write_bytes(raw)
            print(json.dumps(r.__dict__, indent=2))
            return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
