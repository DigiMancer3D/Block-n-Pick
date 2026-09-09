from __future__ import annotations

import json
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from . import CANONICAL_NOTICE, __version__
from .acquisition import (
    DEFAULT_COOKIE_PATHS,
    DEFAULT_NODE_URLS,
    BlockCypherLitecoinProvider,
    make_local_scanner,
    scan_btc_public_consensus,
    scan_ltc_blockcypher,
    scan_local,
)
from .corpus import CorpusDB, load_records
from .corpus_exchange import (
    export_corpus_json, export_hash_stream, export_height_list,
    export_per_chain_hash_streams, export_clean_hash_stream, load_manual_explorer_records,
)
from .engine import build_manifest, save_outputs, next_available_output_base
from .picker import BuildCancelled
from .ingest import from_file
from .mix import equal_split, mix_string, rebalance_percentages
from .starter_corpus import import_all_starters, import_starter
from .recipe_resolver import resolve_recipe
from .mine_builder import mine_recipe, verify_recipe
from .tx_links import (
    clear_relations, list_relations, load_tx_link_evidence, save_template,
    save_tx_link_evidence, store_relations,
)

APP_DIR = Path.home() / ".local/share/block-n-pick"
CONFIG_DIR = Path.home() / ".config/block-n-pick"
DB_PATH = APP_DIR / "corpus.sqlite3"
SETTINGS = CONFIG_DIR / "settings.json"
STARTER_CHAINS = ("BTC", "LTC", "XMR", "BCH", "DGB")
COVERAGE_PRESETS = (100, 88, 76, 68, 42, 33)
CHUNK_PRESETS = (19, 17, 9, 7, 3, 1)


class HoldSpin(ttk.Frame):
    """Small numeric entry with predictable click/hold behavior.

    Click +/- changes by 1.  Holding either arrow waits 1.2 seconds, then changes
    by 10 every 1.2 seconds until released.  This is intentionally independent
    of platform-specific ttk Spinbox auto-repeat behavior.
    """

    def __init__(self, master, *, value=0, minimum=0, maximum=100, width=5, command=None):
        super().__init__(master)
        self.minimum = int(minimum)
        self.maximum = int(maximum)
        self.command = command
        self.var = tk.IntVar(value=int(value))
        self._after_id = None
        self._held_delta = 0
        self.entry = ttk.Entry(self, textvariable=self.var, width=width, justify="right")
        self.entry.grid(row=0, column=0, rowspan=2, sticky="ns")
        self.up = ttk.Button(self, text="▲", width=2)
        self.down = ttk.Button(self, text="▼", width=2)
        self.up.grid(row=0, column=1, sticky="nsew")
        self.down.grid(row=1, column=1, sticky="nsew")
        for widget, delta in ((self.up, 1), (self.down, -1)):
            widget.bind("<ButtonPress-1>", lambda _e, d=delta: self._press(d))
            widget.bind("<ButtonRelease-1>", self._release)
            widget.bind("<Leave>", self._release)
        self.entry.bind("<Return>", self._commit)
        self.entry.bind("<FocusOut>", self._commit)
        self.entry.bind("<Up>", lambda e: self._keyboard(1))
        self.entry.bind("<Down>", lambda e: self._keyboard(-1))

    def get(self) -> int:
        try:
            return self._clamp(int(self.var.get()))
        except Exception:
            return self.minimum

    def set(self, value: int, *, notify: bool = False):
        self.var.set(self._clamp(int(value)))
        if notify and self.command:
            self.command(self.get())

    def set_enabled(self, enabled: bool):
        state = "normal" if enabled else "disabled"
        self.entry.configure(state=state)
        self.up.configure(state=state)
        self.down.configure(state=state)

    def _clamp(self, value: int) -> int:
        return max(self.minimum, min(self.maximum, int(value)))

    def _change(self, delta: int):
        self.set(self.get() + delta)
        if self.command:
            self.command(self.get())

    def _press(self, delta: int):
        self._release()
        self._held_delta = delta
        self._change(delta)
        self._after_id = self.after(1200, self._hold_tick)
        return "break"

    def _hold_tick(self):
        self._after_id = None
        if self._held_delta:
            self._change(10 * self._held_delta)
            self._after_id = self.after(1200, self._hold_tick)

    def _release(self, _event=None):
        if self._after_id is not None:
            try:
                self.after_cancel(self._after_id)
            except tk.TclError:
                pass
        self._after_id = None
        self._held_delta = 0
        return "break"

    def _commit(self, _event=None):
        self.set(self.get())
        if self.command:
            self.command(self.get())

    def _keyboard(self, delta: int):
        self._change(delta)
        return "break"


class BnPApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"Block-n-Pick {__version__}")
        self.geometry("1120x760")
        self.minsize(900, 650)
        APP_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        self.db = CorpusDB(DB_PATH)

        self.target_path = tk.StringVar()
        self.output_base = tk.StringVar(value=str(Path.cwd() / "bnp_output"))
        self.rep = tk.StringVar(value="RAW")
        self.mode = tk.StringVar(value="SOLOCHAIN")
        self.user_pick = tk.StringVar(value="ALL:#0-9")
        self.epoch_seconds = 86400
        self.chains = {c: tk.BooleanVar(value=(c == "BTC")) for c in STARTER_CHAINS}
        self.chain_percent = {c: 100 if c == "BTC" else 0 for c in STARTER_CHAINS}
        self.chain_spins: dict[str, HoldSpin] = {}
        self.manual_mix_order: list[str] = []
        self.mix_preview = tk.StringVar(value="100%BTC")
        self.proof = 100
        self.max_chunk = 3
        self.status = tk.StringVar(value="Ready")

        self.scan_chain = tk.StringVar(value="BTC")
        self.scan_source = tk.StringVar(value="BTC Public Verified (2-source)")
        self.scan_start = 0
        self.scan_end = 99
        self.scan_budget = 500
        self.scan_url = tk.StringVar(value=DEFAULT_NODE_URLS["BTC"])
        self.scan_cookie = tk.StringVar(value=DEFAULT_COOKIE_PATHS.get("BTC", ""))
        self.scan_user = ""
        self.scan_password = ""
        self.scan_status = tk.StringVar(value="No scan running")
        self.scan_queue: queue.Queue = queue.Queue()
        self.scan_cancel = threading.Event()
        self.scan_thread: threading.Thread | None = None

        self.exchange_chain = tk.StringVar(value="BTC")
        self.exchange_direction = tk.StringVar(value="Ascending")
        self.exchange_scope = tk.StringVar(value="ALL")
        self.exchange_status = tk.StringVar(value="Corpus exchange ready")
        self.tx_link_selector = tk.StringVar(value="ALL")
        self.tx_link_status = tk.StringVar(value="TX-Link research layer deferred in Alpha.6")

        self.mine_recipe_path = tk.StringVar()
        self.mine_output_dir = tk.StringVar(value=str(Path.cwd()))
        self.mine_use_corpus = tk.BooleanVar(value=True)
        self.mine_status = tk.StringVar(value="Mine-Build ready")
        self.mine_queue: queue.Queue = queue.Queue()
        self.mine_cancel = threading.Event()
        self.mine_thread: threading.Thread | None = None

        self.build_status = tk.StringVar(value="No build running")
        self.build_queue: queue.Queue = queue.Queue()
        self.build_cancel = threading.Event()
        self.build_thread: threading.Thread | None = None

        self._build_ui()
        self.after(200, self._first_notice)
        self.protocol("WM_DELETE_WINDOW", self._close)

    def _build_ui(self):
        notice = ttk.Frame(self, padding=8)
        notice.pack(fill="x")
        ttk.Label(notice, text=CANONICAL_NOTICE, wraplength=950, justify="left").pack(
            side="left", fill="x", expand=True
        )
        ttk.Button(
            notice, text="About this notice",
            command=lambda: messagebox.showinfo("What Block-n-Pick means", CANONICAL_NOTICE),
        ).pack(side="right")

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=8, pady=8)
        build = ttk.Frame(self.notebook, padding=12)
        corpus = ttk.Frame(self.notebook, padding=12)
        exchange = ttk.Frame(self.notebook, padding=12)
        # Advanced relationship selectors remain in the source for backward
        # recipe compatibility, but are intentionally not exposed as active
        # prototype tabs in Alpha.6.  See Expected Upgrades in What BnP Does.
        txlinks = ttk.Frame(self.notebook, padding=12)
        mine = ttk.Frame(self.notebook, padding=12)
        help_frame = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(build, text="Build Recipe / Proof")
        self.notebook.add(corpus, text="Corpus Manager")
        self.notebook.add(exchange, text="Corpus Exchange")
        self.notebook.add(mine, text="Mine / Resolve")
        self.notebook.add(help_frame, text="What BnP Does")
        self.build_tab, self.corpus_tab, self.exchange_tab, self.mine_tab = build, corpus, exchange, mine

        row = 0
        ttk.Label(build, text="Target media/object").grid(row=row, column=0, sticky="w")
        ttk.Entry(build, textvariable=self.target_path).grid(row=row, column=1, sticky="ew", padx=6)
        ttk.Button(build, text="Browse…", command=self._browse).grid(row=row, column=2, sticky="w")
        row += 1

        ttk.Label(build, text="Representation").grid(row=row, column=0, sticky="w", pady=6)
        ttk.Combobox(build, textvariable=self.rep, values=("RAW", "B64"), state="readonly", width=12).grid(
            row=row, column=1, sticky="w"
        )
        row += 1

        ttk.Label(build, text="Mode").grid(row=row, column=0, sticky="w")
        self.mode_combo = ttk.Combobox(
            build, textvariable=self.mode, values=("SOLOCHAIN", "CROSSCHAIN"),
            state="readonly", width=20,
        )
        self.mode_combo.grid(row=row, column=1, sticky="w")
        self.mode_combo.bind("<<ComboboxSelected>>", self._mode_changed)
        ttk.Label(
            build,
            text="Prototype scope: Solochain + standard Crosschain. Advanced linked selectors are deferred under Expected Upgrades.",
            wraplength=620, justify="left",
        ).grid(row=row, column=2, sticky="w", padx=(10,0))
        row += 1

        ttk.Label(build, text="Chains / percentage").grid(row=row, column=0, sticky="nw", pady=8)
        chain_frame = ttk.Frame(build)
        chain_frame.grid(row=row, column=1, columnspan=2, sticky="w")
        for idx, chain in enumerate(STARTER_CHAINS):
            cell = ttk.Frame(chain_frame, padding=(0, 0, 10, 0))
            cell.grid(row=0, column=idx, sticky="n")
            ttk.Checkbutton(
                cell, text=chain, variable=self.chains[chain],
                command=lambda c=chain: self._on_chain_toggle(c),
            ).grid(row=0, column=0, columnspan=2, sticky="w")
            spin = HoldSpin(
                cell, value=self.chain_percent[chain], minimum=0, maximum=100, width=4,
                command=lambda v, c=chain: self._on_chain_percent(c, v),
            )
            spin.grid(row=1, column=0, sticky="w")
            ttk.Label(cell, text="%").grid(row=1, column=1, sticky="w", padx=(2, 0))
            spin.set_enabled(self.chains[chain].get())
            self.chain_spins[chain] = spin
        row += 1

        ttk.Label(build, text="Crosschain mix").grid(row=row, column=0, sticky="w")
        ttk.Label(build, textvariable=self.mix_preview).grid(row=row, column=1, columnspan=2, sticky="w")
        row += 1

        ttk.Label(build, text="Coverage / proof").grid(row=row, column=0, sticky="w", pady=6)
        coverage_line = ttk.Frame(build)
        coverage_line.grid(row=row, column=1, columnspan=2, sticky="w")
        self.proof_spin = HoldSpin(
            coverage_line, value=100, minimum=1, maximum=100, width=5,
            command=self._set_proof,
        )
        self.proof_spin.pack(side="left")
        for value in COVERAGE_PRESETS:
            ttk.Button(coverage_line, text=str(value), width=4, command=lambda v=value: self._set_proof_preset(v)).pack(
                side="left", padx=(5 if value == COVERAGE_PRESETS[0] else 2, 0)
            )
        self.proof_class_label = ttk.Label(coverage_line, text="Full → .bnp + .bnpm")
        self.proof_class_label.pack(side="left", padx=(10, 0))
        row += 1

        ttk.Label(build, text="Max chunk (alpha)").grid(row=row, column=0, sticky="w")
        chunk_line = ttk.Frame(build)
        chunk_line.grid(row=row, column=1, columnspan=2, sticky="w")
        self.chunk_spin = HoldSpin(
            chunk_line, value=3, minimum=1, maximum=19, width=5,
            command=self._set_max_chunk,
        )
        self.chunk_spin.pack(side="left")
        for value in CHUNK_PRESETS:
            ttk.Button(chunk_line, text=str(value), width=3, command=lambda v=value: self._set_chunk_preset(v)).pack(
                side="left", padx=(5 if value == CHUNK_PRESETS[0] else 2, 0)
            )
        ttk.Label(chunk_line, text="larger values search longer fragments and cost more RAM/CPU").pack(
            side="left", padx=(10, 0)
        )
        row += 1

        ttk.Label(build, text="Output base").grid(row=row, column=0, sticky="w", pady=6)
        ttk.Entry(build, textvariable=self.output_base).grid(row=row, column=1, sticky="ew")
        row += 1
        build_buttons = ttk.Frame(build)
        build_buttons.grid(row=row, column=1, columnspan=2, sticky="w", pady=(12, 4))
        self.build_button = ttk.Button(build_buttons, text="Build", command=self._build, width=18)
        self.build_button.pack(side="left")
        self.build_cancel_button = ttk.Button(
            build_buttons, text="Cancel Build", command=self._cancel_build, state="disabled", width=16
        )
        self.build_cancel_button.pack(side="left", padx=(6, 0))
        ttk.Label(build_buttons, textvariable=self.build_status).pack(side="left", padx=(12, 0))
        row += 1
        self.build_progress = ttk.Progressbar(build, mode="determinate", maximum=100)
        self.build_progress.grid(row=row, column=1, columnspan=2, sticky="ew", pady=(0, 8))
        row += 1
        self.build_resource_label = ttk.Label(
            build,
            text=(
                "Builds run in the background. Cancel Build stops safely. The source-prefix index "
                "keeps memory tied to corpus size instead of target_size × max_chunk."
            ),
            wraplength=850,
        )
        self.build_resource_label.grid(row=row, column=1, columnspan=2, sticky="w")
        build.columnconfigure(1, weight=1)
        self._mode_changed()

        # Corpus / acquisition workspace.
        ttk.Label(
            corpus,
            text=(
                "Acquire real block-hash corpus data from a local node/daemon, or use the gentle "
                "two-source BTC public cross-check. Imported CSV/JSON/JSONL remains supported."
            ),
            wraplength=1000,
        ).pack(anchor="w")

        scan = ttk.LabelFrame(corpus, text="Scan / Sync corpus", padding=8)
        scan.pack(fill="x", pady=(10, 8))
        ttk.Label(scan, text="Chain").grid(row=0, column=0, sticky="w")
        chain_combo = ttk.Combobox(scan, textvariable=self.scan_chain, values=STARTER_CHAINS, state="readonly", width=8)
        chain_combo.grid(row=0, column=1, sticky="w", padx=(4, 12))
        chain_combo.bind("<<ComboboxSelected>>", self._scan_chain_changed)
        ttk.Label(scan, text="Source").grid(row=0, column=2, sticky="w")
        self.scan_source_combo = ttk.Combobox(scan, textvariable=self.scan_source, state="readonly", width=34)
        self.scan_source_combo.grid(row=0, column=3, columnspan=3, sticky="w", padx=(4, 0))
        self.scan_source_combo.bind("<<ComboboxSelected>>", self._scan_source_changed)
        self._update_scan_sources()

        ttk.Label(scan, text="Start height").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.scan_start_spin = HoldSpin(scan, value=0, minimum=0, maximum=50_000_000, width=9, command=self._set_scan_start)
        self.scan_start_spin.grid(row=1, column=1, sticky="w", pady=(8, 0))
        ttk.Label(scan, text="End height").grid(row=1, column=2, sticky="w", pady=(8, 0))
        self.scan_end_spin = HoldSpin(scan, value=99, minimum=0, maximum=50_000_000, width=9, command=self._set_scan_end)
        self.scan_end_spin.grid(row=1, column=3, sticky="w", pady=(8, 0))
        self.latest_small_button = ttk.Button(scan, text="Latest 20", command=lambda: self._start_latest_scan(self._latest_presets()[0]))
        self.latest_small_button.grid(row=1, column=4, padx=(8, 2), pady=(8, 0))
        self.latest_large_button = ttk.Button(scan, text="Latest 200", command=lambda: self._start_latest_scan(self._latest_presets()[1]))
        self.latest_large_button.grid(row=1, column=5, padx=2, pady=(8, 0))
        self._refresh_latest_buttons()

        ttk.Label(scan, text="Node / daemon URL").grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.scan_url_entry = ttk.Entry(scan, textvariable=self.scan_url, width=42)
        self.scan_url_entry.grid(row=2, column=1, columnspan=3, sticky="ew", pady=(8, 0))
        ttk.Label(scan, text="Cookie").grid(row=3, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(scan, textvariable=self.scan_cookie, width=42).grid(row=3, column=1, columnspan=3, sticky="ew", pady=(6, 0))
        ttk.Button(scan, text="Browse…", command=self._browse_cookie).grid(row=3, column=4, sticky="w", pady=(6, 0))
        ttk.Button(scan, text="RPC login…", command=self._rpc_login).grid(row=3, column=5, sticky="w", pady=(6, 0))

        ttk.Label(scan, text="Request budget").grid(row=4, column=0, sticky="w", pady=(8, 0))
        self.budget_spin = HoldSpin(scan, value=500, minimum=2, maximum=100_000, width=8, command=self._set_scan_budget)
        self.budget_spin.grid(row=4, column=1, sticky="w", pady=(8, 0))
        ttk.Label(scan, text="public providers use a gentle 1.2s/provider minimum interval").grid(
            row=4, column=2, columnspan=4, sticky="w", pady=(8, 0)
        )

        scan_buttons = ttk.Frame(scan)
        scan_buttons.grid(row=5, column=0, columnspan=6, sticky="w", pady=(10, 0))
        self.scan_button = ttk.Button(scan_buttons, text="Scan range", command=self._start_scan)
        self.scan_button.pack(side="left")
        self.cancel_button = ttk.Button(scan_buttons, text="Cancel", command=self._cancel_scan, state="disabled")
        self.cancel_button.pack(side="left", padx=6)
        ttk.Button(scan_buttons, text="Probe source / tip", command=self._probe_source).pack(side="left")
        ttk.Button(scan_buttons, text="Test 0–9 (offline)", command=self._import_test_0_9).pack(side="left", padx=6)
        ttk.Button(scan_buttons, text="Test all 5", command=self._import_test_all).pack(side="left")
        ttk.Button(scan_buttons, text="Probe BIP158", command=self._probe_bip158).pack(side="left", padx=6)
        ttk.Label(scan_buttons, textvariable=self.scan_status).pack(side="left", padx=(12, 0))
        self.scan_progress = ttk.Progressbar(scan, mode="determinate", maximum=100)
        self.scan_progress.grid(row=6, column=0, columnspan=6, sticky="ew", pady=(8, 0))
        scan.columnconfigure(3, weight=1)

        controls = ttk.Frame(corpus)
        controls.pack(fill="x", pady=6)
        ttk.Button(controls, text="Import corpus…", command=self._import_corpus).pack(side="left")
        ttk.Button(controls, text="Refresh counts", command=self._refresh_counts).pack(side="left", padx=6)
        ttk.Button(controls, text="Prune selected chain…", command=self._prune_selected_chain).pack(side="left", padx=6)
        self.countbox = tk.Text(corpus, height=11)
        self.countbox.pack(fill="both", expand=True)
        self._refresh_counts()

        # Corpus exchange / manual explorer workspace.  Manual explorer input is
        # deliberately unverified until a node/provider later corroborates the
        # same literal block hash.
        ttk.Label(
            exchange,
            text=(
                "Bring in copied/exported block explorer results without trusting them as verified, "
                "or export the local BnP corpus for backup, migration, inspection and hash-stream experiments."
            ),
            wraplength=1000,
        ).pack(anchor="w")

        manual = ttk.LabelFrame(exchange, text="Manual / explorer block-list import", padding=8)
        manual.pack(fill="x", pady=(10, 8))
        ttk.Label(manual, text="Fallback chain").grid(row=0, column=0, sticky="w")
        ttk.Combobox(manual, textvariable=self.exchange_chain, values=STARTER_CHAINS, state="readonly", width=8).grid(
            row=0, column=1, sticky="w", padx=(4, 14)
        )
        ttk.Label(manual, text="Start height for hash-only lists").grid(row=0, column=2, sticky="w")
        self.exchange_start_spin = HoldSpin(manual, value=0, minimum=0, maximum=50_000_000, width=10)
        self.exchange_start_spin.grid(row=0, column=3, sticky="w", padx=(4, 14))
        ttk.Label(manual, text="Order").grid(row=0, column=4, sticky="w")
        ttk.Combobox(
            manual, textvariable=self.exchange_direction, values=("Ascending", "Descending"),
            state="readonly", width=11,
        ).grid(row=0, column=5, sticky="w", padx=(4, 0))
        ttk.Button(manual, text="Import explorer/manual list…", command=self._import_manual_explorer).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(10, 0)
        )
        ttk.Label(
            manual,
            text=(
                "Accepts JSON/JSONL/CSV/TSV/TXT copied from common explorers: height+hash, "
                "Esplora id+height+timestamp, BlockCypher-style hash+height+time, copied table rows, "
                "or one 64-hex hash per line. Every record imported here is UNVERIFIED."
            ),
            wraplength=780, justify="left",
        ).grid(row=1, column=2, columnspan=4, sticky="w", padx=(12, 0), pady=(10, 0))

        exporting = ttk.LabelFrame(exchange, text="Database export / portability", padding=8)
        exporting.pack(fill="x", pady=(8, 8))
        ttk.Label(exporting, text="Scope").grid(row=0, column=0, sticky="w")
        ttk.Combobox(
            exporting, textvariable=self.exchange_scope, values=("ALL",) + STARTER_CHAINS,
            state="readonly", width=8,
        ).grid(row=0, column=1, sticky="w", padx=(4, 14))
        ttk.Button(exporting, text="Export restorable corpus JSON…", command=self._export_corpus_json).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(10, 0)
        )
        ttk.Button(exporting, text="Export block-height list…", command=self._export_height_list).grid(
            row=1, column=2, sticky="w", padx=(8, 0), pady=(10, 0)
        )
        ttk.Button(exporting, text="Export raw hash stream…", command=self._export_hash_stream).grid(
            row=1, column=3, sticky="w", padx=(8, 0), pady=(10, 0)
        )
        ttk.Button(exporting, text="Clean Stream String…", command=self._export_clean_hash_stream).grid(
            row=1, column=4, sticky="w", padx=(8, 0), pady=(10, 0)
        )
        ttk.Button(exporting, text="Export all 5 per-chain streams…", command=self._export_all_hash_streams).grid(
            row=1, column=5, sticky="w", padx=(8, 0), pady=(10, 0)
        )
        ttk.Label(
            exporting,
            text=(
                "Restorable JSON preserves verification/timestamps and can be re-imported with Corpus Manager → Import corpus. "
                "Height lists use compact ranges. Raw hash streams concatenate 64-hex block hashes with NO separators in deterministic chain/height order. "
                "Clean Stream String replaces only each hash's leading/trailing zero runs using the reversible o0 additive run notation."
            ),
            wraplength=980, justify="left",
        ).grid(row=2, column=0, columnspan=6, sticky="w", pady=(10, 0))
        ttk.Label(exchange, textvariable=self.exchange_status).pack(anchor="w", pady=(4, 0))

        # TX-Link evidence workspace. Imported evidence is intentionally treated
        # as user/unverified unless a future independent verifier promotes it.
        ttk.Label(
            txlinks,
            text=(
                "Store cross-chain transaction/evidence relationships separately from block verification. "
                "TX_LINKED mapping material is serialized transaction bytes + origin block hash, side-by-side. "
                "Imports accept plain/copy-pasted hex, explorer/API JSON wrappers (hex/txhex/rawtx/result), Base64/byte-array transports, and downloaded raw sidecar files. "
                "Manual VERIFIED claims are preserved as claimed_grade but downgraded to UNVERIFIED-USER-PROOF."
            ),
            wraplength=1000, justify="left",
        ).pack(anchor="w")
        tx_controls = ttk.Frame(txlinks)
        tx_controls.pack(fill="x", pady=(10, 8))
        ttk.Button(tx_controls, text="Validate evidence…", command=self._validate_tx_links).pack(side="left")
        ttk.Button(tx_controls, text="Import TX-Link evidence…", command=self._import_tx_links).pack(side="left", padx=(6, 0))
        ttk.Button(tx_controls, text="Export TX-Link evidence…", command=self._export_tx_links).pack(side="left", padx=(6, 0))
        ttk.Button(tx_controls, text="Save JSON template…", command=self._save_tx_template).pack(side="left", padx=(6, 0))
        ttk.Button(tx_controls, text="Refresh", command=self._refresh_tx_links).pack(side="left", padx=(6, 0))
        ttk.Button(tx_controls, text="Clear TX-Link evidence…", command=self._clear_tx_links).pack(side="left", padx=(6, 0))
        self.tx_link_box = tk.Text(txlinks, height=20, wrap="none")
        self.tx_link_box.pack(fill="both", expand=True)
        ttk.Label(txlinks, textvariable=self.tx_link_status).pack(anchor="w", pady=(4, 0))
        self._refresh_tx_links()

        # Mine-Build / resolver workspace.  Unlike recipe search, Mine-Build uses
        # literal source material already embedded in .bnp/.bnpm/.bnp2, while
        # the local corpus is an optional corroboration layer.
        ttk.Label(
            mine,
            text=(
                "Resolve and construct from existing .bnp, .bnpm or .bnp2 artifacts. "
                "Machine files are detected by internal magic, not filename extension. Full recipes build the end-result file; "
                ".bnp2 proofs build a zero-filled representation plus an explicit coverage map rather than pretending missing bytes exist."
            ),
            wraplength=1000, justify="left",
        ).pack(anchor="w")
        mine_recipe_line = ttk.Frame(mine)
        mine_recipe_line.pack(fill="x", pady=(10, 6))
        ttk.Label(mine_recipe_line, text="Recipe / manifest").pack(side="left")
        ttk.Entry(mine_recipe_line, textvariable=self.mine_recipe_path).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(mine_recipe_line, text="Browse…", command=self._browse_mine_recipe).pack(side="left")

        mine_out_line = ttk.Frame(mine)
        mine_out_line.pack(fill="x", pady=6)
        ttk.Label(mine_out_line, text="Output directory").pack(side="left")
        ttk.Entry(mine_out_line, textvariable=self.mine_output_dir).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(mine_out_line, text="Browse…", command=self._browse_mine_output_dir).pack(side="left")
        ttk.Checkbutton(mine_out_line, text="Corroborate against local corpus", variable=self.mine_use_corpus).pack(side="left", padx=(12,0))

        mine_buttons = ttk.Frame(mine)
        mine_buttons.pack(fill="x", pady=(8, 6))
        ttk.Button(mine_buttons, text="Preflight / Resolve", command=lambda: self._start_mine_action("resolve")).pack(side="left")
        ttk.Button(mine_buttons, text="Verify only", command=lambda: self._start_mine_action("verify")).pack(side="left", padx=(6,0))
        ttk.Button(mine_buttons, text="Build", command=lambda: self._start_mine_action("build")).pack(side="left", padx=(6,0))
        ttk.Button(mine_buttons, text="Build + Verify", command=lambda: self._start_mine_action("build-verify")).pack(side="left", padx=(6,0))
        self.mine_cancel_button = ttk.Button(mine_buttons, text="Cancel", command=self._cancel_mine, state="disabled")
        self.mine_cancel_button.pack(side="left", padx=(6,0))
        ttk.Label(mine_buttons, textvariable=self.mine_status).pack(side="left", padx=(12,0))
        self.mine_progress = ttk.Progressbar(mine, mode="determinate", maximum=100)
        self.mine_progress.pack(fill="x", pady=(0, 6))
        self.mine_report_box = tk.Text(mine, height=24, wrap="none")
        self.mine_report_box.pack(fill="both", expand=True)

        ttk.Label(
            help_frame, text=CANONICAL_NOTICE, wraplength=950, justify="left",
            font=("TkDefaultFont", 11, "bold"),
        ).pack(anchor="w", pady=(0, 16))
        ttk.Label(
            help_frame,
            text=(
                "ACTIVE PROTOTYPE SCOPE\n"
                "• SOLOCHAIN: one chain acts as the immutable address/data pool.\n"
                "• CROSSCHAIN: two or more selected chains share reconstruction work; requested percentages are soft steering targets.\n"
                "• .bnp/.bnpm: full reconstruction recipes/manifests. .bnp2: partial coverage proofs.\n"
                "• Mine / Resolve: preflight, verify and construct end-result material directly from those recipe formats.\n\n"
                "EXPECTED UPGRADES (DEFERRED EXPERIMENTS)\n"
                "• Locked-Link Crosschain — restrict sources to shared heights: H* = H1 ∩ H2 ∩ ... ∩ Hn. Useful when multiple chains have enough overlapping history.\n"
                "• Epoch-Link Crosschain — normalize time with e = floor(timestamp / Δt), then use E* = E1 ∩ E2 ∩ ... ∩ En. Needs trustworthy timestamp coverage.\n"
                "• TX-Linked Crosschain — relationship material can pair serialized transaction bytes with origin-block hashes; real verification is best tested with exchange/multisig/customer-link data and protocol evidence.\n"
                "• User-Picked Crosschain — user-defined association sets over blocks/hashes/handles; intentionally UNVERIFIED-USER-PROOF unless independently corroborated.\n"
                "• Generic/Binary/Trinary selectors — transform or match epoch/block/hash/address/TX/UTXO identifiers under additional selector rules.\n\n"
                "These deferred modes remain readable in older recipes for compatibility, but Alpha.6 does not expose them for new builds. "
                "The prototype is intentionally narrowed so the core hypothesis can be tested through reliable Solochain/Crosschain recipe generation and Mine-Building."
            ),
            wraplength=950, justify="left",
        ).pack(anchor="w")

        ttk.Label(self, textvariable=self.status, relief="sunken", anchor="w", padding=4).pack(
            fill="x", side="bottom"
        )

    # ---------- Build workspace ----------
    def _selected_chains(self):
        return [c for c in STARTER_CHAINS if self.chains[c].get()]

    def _sync_mix_widgets(self):
        selected = self._selected_chains()
        for c in STARTER_CHAINS:
            self.chain_spins[c].set(self.chain_percent.get(c, 0))
            self.chain_spins[c].set_enabled(c in selected)
        self.mix_preview.set(mix_string(self.chain_percent, selected) if selected else "No chains selected")
        if len(selected) > 1 and self.mode.get() == "SOLOCHAIN":
            self.mode.set("CROSSCHAIN")
        elif len(selected) == 1 and self.mode.get() == "CROSSCHAIN":
            self.mode.set("SOLOCHAIN")

    def _on_chain_toggle(self, _chain):
        selected = self._selected_chains()
        self.manual_mix_order = [c for c in self.manual_mix_order if c in selected]
        if not selected:
            self.chain_percent = {c: 0 for c in STARTER_CHAINS}
        elif len(selected) == 1:
            self.chain_percent = {c: (100 if c in selected else 0) for c in STARTER_CHAINS}
            self.manual_mix_order = []
        else:
            current = dict(self.chain_percent)
            if self.manual_mix_order:
                balanced, self.manual_mix_order = rebalance_percentages(
                    current, selected, manual_order=self.manual_mix_order
                )
            else:
                balanced = equal_split(selected)
            for c in STARTER_CHAINS:
                self.chain_percent[c] = balanced.get(c, 0)
        self._sync_mix_widgets()

    def _on_chain_percent(self, chain: str, value: int):
        if not self.chains[chain].get():
            return
        self.chain_percent[chain] = value
        selected = self._selected_chains()
        if len(selected) == 1:
            self.chain_percent[chain] = 100
            self.manual_mix_order = []
        else:
            balanced, self.manual_mix_order = rebalance_percentages(
                self.chain_percent, selected, changed=chain, manual_order=self.manual_mix_order
            )
            for c in selected:
                self.chain_percent[c] = balanced[c]
        self._sync_mix_widgets()

    def _mode_changed(self, _event=None):
        mode = self.mode.get().upper()
        if mode == "CROSSCHAIN":
            self.build_resource_label.configure(text="CROSSCHAIN uses all selected chain corpora and steers toward the requested percentages without making percentage deviation a hard reconstruction failure.")
        else:
            self.build_resource_label.configure(text="SOLOCHAIN uses one selected chain. Builds run in the background and Cancel Build stops safely at indexed/output checkpoints.")

    def _set_epoch_seconds(self, value: int):
        self.epoch_seconds = max(60, int(value))

    def _set_epoch_preset(self, value: int):
        self.epoch_spin.set(value)
        self._set_epoch_seconds(value)

    def _set_proof(self, value: int):
        self.proof = max(1, min(100, int(value)))
        if self.proof >= 100:
            text = "Full → .bnp + .bnpm"
        elif self.proof >= 68:
            text = "Generic proof → .bnp2"
        elif self.proof >= 33:
            text = "Basic proof → .bnp2"
        else:
            text = "Minor proof → .bnp2"
        self.proof_class_label.configure(text=text)

    def _set_proof_preset(self, value: int):
        self.proof_spin.set(value)
        self._set_proof(value)

    def _set_max_chunk(self, value: int):
        self.max_chunk = max(1, min(19, int(value)))

    def _set_chunk_preset(self, value: int):
        self.chunk_spin.set(value)
        self._set_max_chunk(value)

    def _first_notice(self):
        try:
            settings = json.loads(SETTINGS.read_text()) if SETTINGS.exists() else {}
        except Exception:
            settings = {}
        if not settings.get("notice_acknowledged"):
            messagebox.showinfo(
                "What Block-n-Pick means",
                CANONICAL_NOTICE + "\n\nThis notice remains available in the application.",
            )
            SETTINGS.write_text(json.dumps({**settings, "notice_acknowledged": True}, indent=2), encoding="utf-8")

    def _browse(self):
        path = filedialog.askopenfilename(title="Choose any media/object")
        if path:
            self.target_path.set(path)
            self.output_base.set(str(Path(path).parent / (Path(path).stem + "_bnp")))

    def _parse_weights(self):
        chains = self._selected_chains()
        if len(chains) <= 1:
            return None
        result = {c: float(self.chain_percent[c]) for c in chains}
        if abs(sum(result.values()) - 100.0) > 1e-9:
            raise ValueError("Crosschain percentages must total 100%")
        return result

    def _build(self):
        if self.build_thread and self.build_thread.is_alive():
            return
        try:
            chains = self._selected_chains()
            if not chains:
                raise ValueError("Select at least one chain")
            mode = self.mode.get().upper()
            available = self.db.record_count(chains, verified_only=True)
            if available <= 0:
                self.notebook.select(self.corpus_tab)
                raise ValueError(
                    "No verified corpus records are available for the selected chain(s). "
                    "Use Corpus Manager → Test 0–9 / Scan range / provider presets, or import a corpus first."
                )
            target_path = self.target_path.get().strip()
            if not target_path:
                raise ValueError("Choose a target media/object first")
            target = Path(target_path)
            if not target.is_file():
                raise ValueError(f"Target file not found: {target}")
            requested_coverage = float(self.proof)
            weights = self._parse_weights()
            if len(chains) > 1 and mode == "SOLOCHAIN":
                mode = "CROSSCHAIN"
            rep = self.rep.get()
            max_chunk = self.max_chunk
            requested_output_base = self.output_base.get().strip()
            if not requested_output_base:
                raise ValueError("Choose an output base")
            output_base = str(next_available_output_base(requested_output_base))
            # Expose the serial chosen for this attempt before work begins.  A
            # failed attempt therefore cannot look successful merely because an
            # older same-name file remains on disk.
            if output_base != requested_output_base:
                self.output_base.set(output_base)
            raw_bytes = target.stat().st_size
            represented = raw_bytes if rep.upper() == "RAW" else ((raw_bytes + 2) // 3) * 4
            corpus_count = self.db.record_count(chains, verified_only=True)
        except Exception as exc:
            self.status.set(f"Build not started: {exc}")
            messagebox.showerror("Build not started", str(exc))
            return

        self.build_cancel.clear()
        self.build_progress["value"] = 0
        self.build_button.configure(state="disabled")
        self.build_cancel_button.configure(state="normal")
        self.build_status.set(f"Preparing {represented:,} {rep.upper()} bytes against {corpus_count:,} source blocks…")
        self.status.set(f"BnP build running → {output_base}")

        def progress(stage, done, total, detail):
            self.build_queue.put(("progress", stage, done, total, detail))

        def worker():
            worker_db = CorpusDB(DB_PATH)
            try:
                ingested = from_file(target_path)
                if self.build_cancel.is_set():
                    raise BuildCancelled("build cancelled before indexing")
                manifest, coverage = build_manifest(
                    raw=ingested.raw,
                    safe_name=ingested.safe_name,
                    source_kind=ingested.source_kind,
                    source_value=ingested.source_value,
                    corpus=worker_db,
                    chains=chains,
                    representation=rep,
                    mode=mode,
                    weights=weights,
                    proof_coverage=None if requested_coverage >= 100 else requested_coverage,
                    max_chunk=max_chunk,
                    progress=progress,
                    cancel=self.build_cancel,
                )
                if coverage <= 0:
                    raise RuntimeError("No target coverage found in selected corpus")
                if self.build_cancel.is_set():
                    raise BuildCancelled("build cancelled before output write")
                outputs = save_outputs(manifest, coverage, output_base, progress=progress, cancel=self.build_cancel)
                self.build_queue.put(("done", coverage, len(manifest.segments), [str(p) for p in outputs], manifest.notes))
            except BuildCancelled as exc:
                self.build_queue.put(("cancelled", str(exc)))
            except Exception as exc:
                self.build_queue.put(("error", str(exc)))
            finally:
                worker_db.close()

        self.build_thread = threading.Thread(target=worker, daemon=True)
        self.build_thread.start()
        self.after(100, self._poll_build_queue)

    def _cancel_build(self):
        if self.build_thread and self.build_thread.is_alive():
            self.build_cancel.set()
            self.build_status.set("Cancelling at the next safe search/output checkpoint…")
            self.status.set("Cancelling BnP build…")

    def _poll_build_queue(self):
        keep_polling = bool(self.build_thread and self.build_thread.is_alive())
        try:
            while True:
                msg = self.build_queue.get_nowait()
                kind = msg[0]
                if kind == "progress":
                    _, stage, done, total, detail = msg
                    fraction = done / max(1, total)
                    if stage == "index":
                        overall = 25.0 * fraction
                        label = f"Indexing corpus {done:,}/{total:,}"
                    elif stage == "search":
                        overall = 25.0 + 45.0 * fraction
                        label = f"Searching target {done:,}/{total:,}"
                    elif stage == "write_bnp":
                        overall = 70.0 + 10.0 * fraction
                        label = f"Writing .bnp recipe {done:,}/{total:,} chunks"
                    elif stage in {"pack_bnpm", "pack_bnp2"}:
                        overall = 80.0 + 15.0 * fraction
                        suffix = ".bnpm" if stage.endswith("bnpm") else ".bnp2"
                        label = f"Packing {suffix} {done:,}/{total:,} chunks"
                    elif stage in {"seal_bnpm", "seal_bnp2"}:
                        overall = 95.0 + 3.0 * fraction
                        suffix = ".bnpm" if stage.endswith("bnpm") else ".bnp2"
                        label = f"Finalizing {suffix} container"
                    elif stage == "verify_output":
                        overall = 98.0 + 2.0 * fraction
                        label = "Verifying output integrity"
                    else:
                        overall = min(69.0, 25.0 + 44.0 * fraction)
                        label = f"Building {done:,}/{total:,}"
                    self.build_progress["value"] = min(99.9, overall)
                    self.build_status.set(label + (f" — {detail}" if detail else ""))
                elif kind == "done":
                    _, coverage, chunks, outputs, notes = msg
                    self.build_progress["value"] = 100
                    picker = (notes or {}).get("picker", {})
                    idx_kind = picker.get("index_kind", "source-prefix-v1")
                    self.build_status.set(f"Done — {coverage:.4f}% coverage; {chunks:,} chunks")
                    self.status.set(f"Built {coverage:.4f}% with {chunks:,} chunks")
                    messagebox.showinfo(
                        "BnP build complete",
                        f"Coverage: {coverage:.6f}%\nChunks: {chunks:,}\nIndex: {idx_kind}\n" + "\n".join(outputs),
                    )
                elif kind == "cancelled":
                    self.build_status.set("Build cancelled safely")
                    self.status.set("BnP build cancelled")
                elif kind == "error":
                    self.build_status.set("Build not completed — no new output published")
                    self.status.set(f"Build not completed: {msg[1]}")
                    messagebox.showerror(
                        "Build not completed",
                        msg[1] + "\n\nNo new BnP output was published. Existing earlier files, if any, were left unchanged.",
                    )
        except queue.Empty:
            pass
        if keep_polling:
            self.after(100, self._poll_build_queue)
        else:
            self.build_button.configure(state="normal")
            self.build_cancel_button.configure(state="disabled")

    # ---------- Mine / Resolve workspace ----------
    def _browse_mine_recipe(self):
        path = filedialog.askopenfilename(
            title="Choose .bnp / .bnpm / .bnp2",
            filetypes=[("Block-n-Pick artifacts", "*.bnp *.bnpm *.bnp2"), ("All files", "*")],
        )
        if path:
            self.mine_recipe_path.set(path)
            self.mine_output_dir.set(str(Path(path).parent))

    def _browse_mine_output_dir(self):
        path = filedialog.askdirectory(title="Choose Mine-Build output directory")
        if path:
            self.mine_output_dir.set(path)

    def _start_mine_action(self, action: str):
        if self.mine_thread and self.mine_thread.is_alive():
            return
        recipe = self.mine_recipe_path.get().strip()
        if not recipe or not Path(recipe).is_file():
            messagebox.showerror("Mine / Resolve", "Choose an existing .bnp, .bnpm or .bnp2 artifact first.")
            return
        out_dir = self.mine_output_dir.get().strip() or str(Path(recipe).parent)
        use_corpus = bool(self.mine_use_corpus.get())
        self.mine_cancel.clear()
        self.mine_progress["value"] = 0
        self.mine_cancel_button.configure(state="normal")
        self.mine_status.set(f"Running {action}…")
        self.mine_report_box.delete("1.0", "end")

        def progress(stage, done, total, detail):
            self.mine_queue.put(("progress", stage, done, total, detail))

        def worker():
            worker_db = CorpusDB(DB_PATH) if use_corpus else None
            try:
                if action == "resolve":
                    _m, report, _payload = resolve_recipe(recipe, worker_db, include_sources=False)
                    result = report.to_dict(include_sources=False)
                    success = report.ok
                elif action == "verify":
                    r = verify_recipe(recipe, corpus=worker_db, progress=progress, cancel=self.mine_cancel)
                    result = r.to_dict(); success = r.success
                else:
                    r = mine_recipe(
                        recipe, corpus=worker_db, output_dir=out_dir,
                        verify_after_write=(action == "build-verify"),
                        progress=progress, cancel=self.mine_cancel,
                    )
                    result = r.to_dict(); success = r.success
                self.mine_queue.put(("done", action, success, result))
            except Exception as exc:
                self.mine_queue.put(("error", action, str(exc)))
            finally:
                if worker_db is not None:
                    worker_db.close()

        self.mine_thread = threading.Thread(target=worker, daemon=True)
        self.mine_thread.start()
        self.after(100, self._poll_mine_queue)

    def _cancel_mine(self):
        if self.mine_thread and self.mine_thread.is_alive():
            self.mine_cancel.set()
            self.mine_status.set("Cancelling Mine-Build at next safe checkpoint…")

    def _poll_mine_queue(self):
        keep = bool(self.mine_thread and self.mine_thread.is_alive())
        try:
            while True:
                msg = self.mine_queue.get_nowait()
                kind = msg[0]
                if kind == "progress":
                    _, stage, done, total, detail = msg
                    frac = done / max(1, total)
                    if stage == "mine_extract":
                        overall = 60 * frac
                        label = "Resolving/extracting recipe fragments"
                    elif stage == "mine_decode":
                        overall = 60 + 10 * frac
                        label = "Decoding representation"
                    elif stage == "mine_write":
                        overall = 70 + 20 * frac
                        label = "Writing .part output"
                    elif stage == "mine_verify":
                        overall = 90 + 10 * frac
                        label = "Verifying constructed output"
                    else:
                        overall = 50 * frac
                        label = stage
                    self.mine_progress["value"] = min(99.5, overall)
                    self.mine_status.set(label + (f" — {detail}" if detail else ""))
                elif kind == "done":
                    _, action, success, result = msg
                    self.mine_progress["value"] = 100 if success else 0
                    self.mine_status.set(f"{action} {'PASS' if success else 'FAILED'}")
                    self.mine_report_box.delete("1.0", "end")
                    self.mine_report_box.insert("end", json.dumps(result, indent=2))
                    if success:
                        messagebox.showinfo("Mine / Resolve", f"{action} completed successfully.")
                    else:
                        messagebox.showwarning("Mine / Resolve", f"{action} completed with a failed verification result. See the report.")
                elif kind == "error":
                    _, action, text = msg
                    self.mine_progress["value"] = 0
                    self.mine_status.set(f"{action} failed")
                    self.mine_report_box.delete("1.0", "end")
                    self.mine_report_box.insert("end", text)
                    messagebox.showerror("Mine / Resolve", text)
        except queue.Empty:
            pass
        if keep:
            self.after(100, self._poll_mine_queue)
        else:
            self.mine_cancel_button.configure(state="disabled")

    # ---------- Corpus workspace ----------
    def _set_scan_start(self, value: int):
        self.scan_start = value

    def _set_scan_end(self, value: int):
        self.scan_end = value

    def _set_scan_budget(self, value: int):
        self.scan_budget = value

    def _scan_chain_changed(self, _event=None):
        chain = self.scan_chain.get().upper()
        self.scan_url.set(DEFAULT_NODE_URLS.get(chain, ""))
        self.scan_cookie.set(DEFAULT_COOKIE_PATHS.get(chain, ""))
        self._update_scan_sources()
        self._refresh_latest_buttons()

    def _scan_source_changed(self, _event=None):
        self._refresh_latest_buttons()

    def _latest_presets(self):
        # Keep the public/local UI predictable by chain.  BTC gets the larger
        # 20/200 shortcuts; every other chain uses the gentler 10/30 pair.
        # Provider governors still enforce their own request budgets/rate rules.
        return (20, 200) if self.scan_chain.get().upper() == "BTC" else (10, 30)

    def _refresh_latest_buttons(self):
        if not hasattr(self, "latest_small_button"):
            return
        small, large = self._latest_presets()
        self.latest_small_button.configure(text=f"Latest {small}")
        self.latest_large_button.configure(text=f"Latest {large}")

    def _update_scan_sources(self):
        chain = self.scan_chain.get().upper()
        if chain == "BTC":
            values = ("BTC Public Verified (2-source)", "Local node")
        elif chain == "LTC":
            values = ("Local node", "BlockCypher seed (unverified)")
        else:
            values = ("Local node",)
        self.scan_source_combo.configure(values=values)
        if self.scan_source.get() not in values:
            self.scan_source.set(values[0])
        self._refresh_latest_buttons()

    def _browse_cookie(self):
        path = filedialog.askopenfilename(title="Choose node RPC cookie/auth file")
        if path:
            self.scan_cookie.set(path)

    def _rpc_login(self):
        user = simpledialog.askstring("RPC login", "RPC username (blank clears):", initialvalue=self.scan_user, parent=self)
        if user is None:
            return
        password = simpledialog.askstring("RPC login", "RPC password:", show="*", parent=self)
        if password is None:
            return
        self.scan_user, self.scan_password = user, password
        self.status.set("RPC login set for this session only")

    def _source_tip(self) -> int:
        chain = self.scan_chain.get().upper()
        source = self.scan_source.get()
        if source.startswith("BTC Public"):
            from .acquisition import EsploraProvider
            provider = EsploraProvider(
                "mempool.space", "https://mempool.space/api",
                min_interval=1.2, request_budget=self.scan_budget,
            )
            return provider.tip_height()
        if source.startswith("BlockCypher"):
            return BlockCypherLitecoinProvider(request_budget=min(self.scan_budget, 90)).tip_height()
        scanner = make_local_scanner(
            chain, url=self.scan_url.get(), cookie_path=self.scan_cookie.get() or None,
            username=self.scan_user or None, password=self.scan_password or None,
        )
        return scanner.tip_height()

    def _probe_source(self):
        if self.scan_thread and self.scan_thread.is_alive():
            return
        self.scan_status.set("Probing source…")
        self.status.set("Probing corpus source…")

        def worker():
            try:
                tip = self._source_tip()
                self.scan_queue.put(("probe", tip))
            except Exception as exc:
                self.scan_queue.put(("error", str(exc)))

        self.scan_thread = threading.Thread(target=worker, daemon=True)
        self.scan_thread.start()
        self.after(100, self._poll_scan_queue)

    def _probe_bip158(self):
        if self.scan_chain.get().upper() != "BTC" or self.scan_source.get() != "Local node":
            messagebox.showinfo("BIP158 probe", "BIP158 retrieval currently uses a local Bitcoin Core node with blockfilterindex enabled.")
            return
        self.scan_status.set("Probing local BIP158…")

        def worker():
            try:
                scanner = make_local_scanner(
                    "BTC", url=self.scan_url.get(), cookie_path=self.scan_cookie.get() or None,
                    username=self.scan_user or None, password=self.scan_password or None,
                )
                tip = scanner.tip_height()
                rec = scanner.block_by_height(tip)
                filt = scanner.get_block_filter(rec.block_hash)
                self.scan_queue.put(("bip158", tip, rec.block_hash, filt))
            except Exception as exc:
                self.scan_queue.put(("error", str(exc)))

        self.scan_thread = threading.Thread(target=worker, daemon=True)
        self.scan_thread.start()
        self.after(100, self._poll_scan_queue)

    def _import_test_0_9(self):
        if self.scan_thread and self.scan_thread.is_alive():
            return
        chain = self.scan_chain.get().upper()
        try:
            count = import_starter(self.db, chain)
            self.scan_start_spin.set(0); self.scan_end_spin.set(9)
            self.scan_start, self.scan_end = 0, 9
            self.scan_progress["value"] = 100
            self.scan_status.set(f"Offline test corpus ready — {chain} heights 0–9 ({count} pinned blocks)")
            self.status.set(f"Imported built-in {chain} test corpus 0–9")
            self._refresh_counts()
            messagebox.showinfo(
                "Offline test corpus ready",
                f"{chain} canonical mainnet block hashes 0–9 were imported from the pinned BnP starter corpus.\n\n"
                "This performs no API call and is intended for deterministic smoke/crosschain tests.",
            )
        except Exception as exc:
            messagebox.showerror("Starter corpus import failed", str(exc))

    def _import_test_all(self):
        if self.scan_thread and self.scan_thread.is_alive():
            return
        try:
            counts = import_all_starters(self.db)
            self.scan_progress["value"] = 100
            self.scan_status.set("Offline test corpus ready — BTC/LTC/XMR/BCH/DGB heights 0–9")
            self.status.set("Imported all five built-in test corpora")
            self._refresh_counts()
            messagebox.showinfo(
                "All starter corpora ready",
                "Imported pinned canonical mainnet heights 0–9 for BTC, LTC, XMR, BCH and DGB.\n\n"
                + "\n".join(f"{k}: {v} blocks" for k,v in counts.items()),
            )
        except Exception as exc:
            messagebox.showerror("Starter corpus import failed", str(exc))

    def _start_latest_scan(self, count: int):
        if self.scan_thread and self.scan_thread.is_alive():
            return
        self.scan_status.set(f"Resolving tip for latest {count:,}…")

        def worker():
            try:
                tip = self._source_tip()
                start = max(0, tip - count + 1)
                self.scan_queue.put(("latest", start, tip))
            except Exception as exc:
                self.scan_queue.put(("error", str(exc)))

        self.scan_thread = threading.Thread(target=worker, daemon=True)
        self.scan_thread.start()
        self.after(100, self._poll_scan_queue)

    def _start_scan(self, descending: bool = False):
        if self.scan_thread and self.scan_thread.is_alive():
            return
        start, end = self.scan_start_spin.get(), self.scan_end_spin.get()
        if end < start:
            messagebox.showerror("Invalid range", "End height must be greater than or equal to start height.")
            return
        self.scan_start, self.scan_end = start, end
        self.scan_cancel.clear()
        self.scan_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.scan_progress["value"] = 0
        self.scan_status.set(f"Scanning {end-start+1:,} block heights…")
        self.status.set("Corpus scan running…")
        chain = self.scan_chain.get().upper()
        source = self.scan_source.get()

        def progress(done, total, height):
            self.scan_queue.put(("progress", done, total, height))

        def worker():
            worker_db = CorpusDB(DB_PATH)
            try:
                if source.startswith("BTC Public"):
                    report = scan_btc_public_consensus(
                        worker_db, start, end, request_budget=self.scan_budget,
                        progress=progress, cancel=self.scan_cancel,
                    )
                elif source.startswith("BlockCypher"):
                    report = scan_ltc_blockcypher(
                        worker_db, start, end, request_budget=min(self.scan_budget, 90),
                        progress=progress, cancel=self.scan_cancel, descending=descending,
                    )
                else:
                    report = scan_local(
                        worker_db, chain, start, end, url=self.scan_url.get(),
                        cookie_path=self.scan_cookie.get() or None,
                        username=self.scan_user or None, password=self.scan_password or None,
                        progress=progress, cancel=self.scan_cancel,
                    )
                self.scan_queue.put(("done", report))
            except Exception as exc:
                self.scan_queue.put(("error", str(exc)))
            finally:
                worker_db.close()

        self.scan_thread = threading.Thread(target=worker, daemon=True)
        self.scan_thread.start()
        self.after(100, self._poll_scan_queue)

    def _cancel_scan(self):
        self.scan_cancel.set()
        self.scan_status.set("Cancelling after current request…")

    def _poll_scan_queue(self):
        keep_polling = bool(self.scan_thread and self.scan_thread.is_alive())
        try:
            while True:
                msg = self.scan_queue.get_nowait()
                kind = msg[0]
                if kind == "progress":
                    _, done, total, height = msg
                    self.scan_progress["value"] = 100.0 * done / max(1, total)
                    self.scan_status.set(f"{done:,}/{total:,} — height {height:,}")
                elif kind == "probe":
                    tip = msg[1]
                    self.scan_status.set(f"Source OK — tip height {tip:,}")
                    self.status.set("Corpus source probe PASS")
                elif kind == "latest":
                    _, start, end = msg
                    self.scan_start_spin.set(start)
                    self.scan_end_spin.set(end)
                    self.scan_start, self.scan_end = start, end
                    self.scan_thread = None
                    self._start_scan(descending=True)
                    return
                elif kind == "bip158":
                    _, tip, block_hash, filt = msg
                    self.scan_status.set(f"BIP158 PASS at height {tip:,}")
                    messagebox.showinfo(
                        "BIP158 local retrieval PASS",
                        f"Height: {tip}\nBlock: {block_hash}\nFilter bytes (hex chars): {len(str(filt.get('filter','')))}\nHeader: {filt.get('header','')}",
                    )
                elif kind == "done":
                    report = msg[1]
                    if not report.partial and not report.cancelled:
                        self.scan_progress["value"] = 100
                    state = "Cancelled" if report.cancelled else ("Partial" if report.partial else "Done")
                    completed = report.completed or report.imported + len(report.mismatches)
                    requested = report.requested or (report.end_height - report.start_height + 1)
                    self.scan_status.set(
                        f"{state} — completed {completed:,}/{requested:,}; retained {report.imported:,}; "
                        f"verified {report.verified:,}; unverified {report.unverified:,}; requests {report.requests:,}"
                    )
                    self.status.set("Corpus scan partial/resumable" if report.partial else ("Corpus scan cancelled" if report.cancelled else "Corpus scan complete"))
                    self._refresh_counts()
                    if report.partial and report.stop_reason:
                        resume = f"\nResume height: {report.resume_height}" if report.resume_height is not None else ""
                        messagebox.showwarning(
                            "Corpus scan partially completed",
                            f"BnP retained {report.imported:,} successful records before the provider stopped.\n\n"
                            f"Reason: {report.stop_reason}{resume}\n\nAlready retained records will not be discarded."
                        )
                    if report.mismatches:
                        messagebox.showwarning(
                            "Public source mismatch",
                            f"{len(report.mismatches)} heights were not admitted because the two providers did not agree or did not both return data."
                        )
                elif kind == "error":
                    detail = msg[1]
                    self.scan_status.set("Scan/probe failed")
                    self.status.set(f"Corpus acquisition failed: {detail}")
                    if "Connection refused" in detail and self.scan_source.get() == "Local node":
                        chain = self.scan_chain.get().upper()
                        detail = (
                            f"No {chain} local node/daemon appears to be listening at {self.scan_url.get()}.\n\n"
                            "This is not a BnP corpus error. Start/configure the local node, change the node URL, "
                            "or choose a public source when that chain offers one."
                        )
                    messagebox.showerror("Corpus acquisition failed", detail)
        except queue.Empty:
            pass
        if keep_polling:
            self.after(100, self._poll_scan_queue)
        else:
            self.scan_button.configure(state="normal")
            self.cancel_button.configure(state="disabled")

    def _import_corpus(self):
        path = filedialog.askopenfilename(filetypes=[("Corpus", "*.csv *.json *.jsonl *.ndjson"), ("All", "*")])
        if not path:
            return
        try:
            records = load_records(path)
            count = self.db.add_many(records)
            tx_count = 0
            try:
                data = json.loads(Path(path).read_text(encoding="utf-8"))
            except Exception:
                data = None
            if isinstance(data, dict) and data.get("format") == "BNP-CORPUS-EXPORT" and data.get("tx_links"):
                tx_count = store_relations(self.db, load_tx_link_evidence(path, trusted=False))
                self._refresh_tx_links()
            source_kind = "BnP starter corpus" if any(r.source.startswith("imported-starter-") for r in records) else "corpus"
            extra = f" + {tx_count} TX-Link group(s)" if tx_count else ""
            self.status.set(f"Imported {count} block records from {source_kind}{extra}")
            self._refresh_counts()
        except Exception as exc:
            messagebox.showerror("Import failed", str(exc))

    def _exchange_selected_chains(self):
        scope = self.exchange_scope.get().upper()
        return None if scope == "ALL" else [scope]

    def _import_manual_explorer(self):
        path = filedialog.askopenfilename(
            title="Import copied/exported explorer block list as unverified",
            filetypes=[
                ("Explorer/manual block data", "*.txt *.log *.csv *.tsv *.json *.jsonl *.ndjson"),
                ("All", "*"),
            ],
        )
        if not path:
            return
        try:
            records = load_manual_explorer_records(
                path, default_chain=self.exchange_chain.get().upper(),
                start_height=self.exchange_start_spin.get(),
                descending=self.exchange_direction.get() == "Descending",
            )
            count = self.db.add_many(records)
            by_chain = {}
            for rec in records:
                by_chain.setdefault(rec.chain, []).append(rec)
            summary = "; ".join(
                f"{chain} {min(r.height for r in rows)}..{max(r.height for r in rows)} ({len(rows)})"
                for chain, rows in sorted(by_chain.items())
            )
            self.exchange_status.set(f"Imported {count:,} unverified explorer/manual records — {summary}")
            self.status.set(f"Manual explorer import retained {count:,} unverified block records")
            self._refresh_counts()
            messagebox.showinfo(
                "Manual explorer import complete",
                f"Imported/updated {count:,} record(s) as UNVERIFIED.\n\n{summary}\n\n"
                "If a later verified node/provider scan returns the same block hash, BnP upgrades the existing record instead of downgrading or duplicating it.",
            )
        except Exception as exc:
            messagebox.showerror("Manual explorer import failed", str(exc))

    def _export_corpus_json(self):
        path = filedialog.asksaveasfilename(
            title="Export restorable BnP corpus JSON", defaultextension=".json",
            initialfile="bnp_corpus_export.json", filetypes=[("JSON", "*.json"), ("All", "*")],
        )
        if not path:
            return
        try:
            count = export_corpus_json(self.db, path, chains=self._exchange_selected_chains())
            self.exchange_status.set(f"Exported {count:,} restorable records → {path}")
            self.status.set("Corpus JSON export complete")
        except Exception as exc:
            messagebox.showerror("Corpus export failed", str(exc))

    def _export_height_list(self):
        path = filedialog.asksaveasfilename(
            title="Export known block-height list", defaultextension=".txt",
            initialfile="bnp_block_heights.txt", filetypes=[("Text", "*.txt"), ("All", "*")],
        )
        if not path:
            return
        try:
            counts = export_height_list(self.db, path, chains=self._exchange_selected_chains())
            self.exchange_status.set(
                "Exported height list — " + ", ".join(f"{k}:{v}" for k, v in counts.items())
            )
            self.status.set("Block-height list export complete")
        except Exception as exc:
            messagebox.showerror("Height-list export failed", str(exc))

    def _export_hash_stream(self):
        path = filedialog.asksaveasfilename(
            title="Export concatenated block-hash stream", defaultextension=".txt",
            initialfile=f"{self.exchange_scope.get().lower()}_block_hash_stream.txt",
            filetypes=[("Text", "*.txt"), ("All", "*")],
        )
        if not path:
            return
        try:
            count = export_hash_stream(self.db, path, chains=self._exchange_selected_chains())
            self.exchange_status.set(f"Exported {count:,} hashes with no separators → {path}")
            self.status.set("Raw block-hash stream export complete")
        except Exception as exc:
            messagebox.showerror("Hash-stream export failed", str(exc))

    def _export_clean_hash_stream(self):
        path = filedialog.asksaveasfilename(
            title="Export Clean Stream String", defaultextension=".txt",
            initialfile=f"{self.exchange_scope.get().lower()}_clean_block_hash_stream.txt",
            filetypes=[("Text", "*.txt"), ("All", "*")],
        )
        if not path:
            return
        try:
            count = export_clean_hash_stream(self.db, path, chains=self._exchange_selected_chains())
            self.exchange_status.set(f"Exported Clean Stream String for {count:,} hashes → {path}")
            self.status.set("Clean Stream String export complete")
        except Exception as exc:
            messagebox.showerror("Clean Stream export failed", str(exc))

    def _export_all_hash_streams(self):
        directory = filedialog.askdirectory(title="Choose folder for BTC/LTC/XMR/BCH/DGB hash streams")
        if not directory:
            return
        try:
            counts = export_per_chain_hash_streams(self.db, directory)
            self.exchange_status.set(
                "Exported per-chain raw streams — " + ", ".join(f"{k}:{v}" for k, v in counts.items())
            )
            self.status.set("Per-chain hash-stream export complete")
        except Exception as exc:
            messagebox.showerror("Per-chain hash-stream export failed", str(exc))

    def _validate_tx_links(self):
        path = filedialog.askopenfilename(
            title="Validate TX-Link evidence",
            filetypes=[("TX-Link JSON", "*.json"), ("All", "*")],
        )
        if not path:
            return
        try:
            relations = load_tx_link_evidence(path, trusted=False)
            lines = [f"Validated {len(relations)} relationship group(s) without importing."]
            for rel in relations[:8]:
                lines.append(f"\n{rel.relation_id} [{rel.evidence_grade}]")
                for m in rel.members[:12]:
                    raw_bytes = len(bytes.fromhex(m.tx_raw_hash)) if m.tx_raw_hash else 0
                    origin = m.block_hash or (f"height #{m.block_height}" if m.block_height is not None else "missing")
                    lines.append(
                        f"  {m.chain}: {raw_bytes} raw bytes; input={m.tx_raw_input_type or 'legacy/direct'}; origin={origin}"
                    )
            if len(relations) > 8:
                lines.append("\n…additional relationships omitted from preview")
            messagebox.showinfo("TX-Link evidence valid", "\n".join(lines))
            self.tx_link_status.set(f"Validated TX-Link evidence → {path}")
            self.status.set("TX-Link evidence validation complete; nothing imported")
        except Exception as exc:
            messagebox.showerror("TX-Link validation failed", str(exc))

    def _import_tx_links(self):
        path = filedialog.askopenfilename(
            title="Import TX-Link evidence",
            filetypes=[("TX-Link JSON", "*.json"), ("All", "*")],
        )
        if not path:
            return
        try:
            relations = load_tx_link_evidence(path, trusted=False)
            count = store_relations(self.db, relations)
            self._refresh_tx_links()
            self.tx_link_status.set(f"Imported {count} TX-Link relationship group(s)")
            self.status.set("TX-Link evidence import complete")
            messagebox.showinfo("TX-Link import complete", f"Imported {count} relationship group(s). Manual verified claims are not promoted; they remain unverified user evidence.")
        except Exception as exc:
            messagebox.showerror("TX-Link import failed", str(exc))

    def _export_tx_links(self):
        path = filedialog.asksaveasfilename(
            title="Export TX-Link evidence", defaultextension=".json",
            initialfile="bnp_tx_links.json", filetypes=[("JSON", "*.json"), ("All", "*")],
        )
        if not path:
            return
        try:
            count = save_tx_link_evidence(path, list_relations(self.db))
            self.tx_link_status.set(f"Exported {count} TX-Link relationship group(s) → {path}")
        except Exception as exc:
            messagebox.showerror("TX-Link export failed", str(exc))

    def _save_tx_template(self):
        path = filedialog.asksaveasfilename(
            title="Save TX-Link JSON template", defaultextension=".json",
            initialfile="tx_link_template.json", filetypes=[("JSON", "*.json"), ("All", "*")],
        )
        if path:
            save_template(path)
            self.tx_link_status.set(f"Saved TX-Link template → {path}")

    def _refresh_tx_links(self):
        if not hasattr(self, "tx_link_box"):
            return
        relations = list_relations(self.db)
        self.tx_link_box.delete("1.0", "end")
        if not relations:
            self.tx_link_box.insert("end", "No TX-Link relationship evidence stored.\n")
            self.tx_link_status.set("No TX-Link evidence imported")
            return
        for rel in relations:
            self.tx_link_box.insert("end", f"{rel.relation_id}  [{rel.evidence_grade}]\n")
            if rel.claimed_grade:
                self.tx_link_box.insert("end", f"  claimed_grade: {rel.claimed_grade}\n")
            for member in rel.members:
                ref = member.block_hash or (f"height #{member.block_height}" if member.block_height is not None else "unresolved origin block")
                txid = member.txid or "(no txid)"
                raw = member.tx_raw_hash or "(missing raw transaction hex)"
                if member.tx_raw_hash and len(raw) > 96:
                    raw_display = f"{raw[:32]}…{raw[-32:]} ({len(raw)//2} raw bytes)"
                else:
                    raw_display = raw
                ready = "MATERIAL" if member.tx_raw_hash and (member.block_hash or member.block_height is not None) else "WAITING"
                self.tx_link_box.insert("end", f"  {member.chain}: txid={txid}\n")
                input_type = member.tx_raw_input_type or "legacy/direct"
                witness = f" wtxid={member.tx_wtxid}" if member.tx_wtxid else ""
                self.tx_link_box.insert("end", f"    tx_raw={raw_display} + origin={ref}  [{ready}; input={input_type}]{witness}\n")
            self.tx_link_box.insert("end", "\n")
        self.tx_link_status.set(f"{len(relations)} TX-Link relationship group(s) stored")

    def _clear_tx_links(self):
        if not messagebox.askyesno(
            "Clear TX-Link evidence",
            "Delete all locally stored TX-Link relationship evidence?\n\nThis does not delete block corpus records.",
        ):
            return
        count = clear_relations(self.db)
        self._refresh_tx_links()
        self.status.set(f"Cleared {count} TX-Link relationship group(s)")

    def _refresh_counts(self):
        self.countbox.delete("1.0", "end")
        details = self.db.count_details()
        if not details:
            self.countbox.insert(
                "end",
                "No corpus records yet. Use Scan / Sync above or Import corpus.\n"
                "Tip: BTC Public Verified uses two independent public Esplora-compatible sources and stores a height only when both hashes agree.\n",
            )
        else:
            self.countbox.insert("end", "Chain        Total      Verified    Unverified    Timestamped\n")
            self.countbox.insert("end", "-----------------------------------------------------------\n")
            for chain in STARTER_CHAINS:
                d = details.get(chain, {"total": 0, "verified": 0, "unverified": 0, "timestamped": 0})
                self.countbox.insert(
                    "end", f"{chain:<6}{d['total']:>12,}{d['verified']:>13,}{d['unverified']:>14,}{d['timestamped']:>15,}\n"
                )

    def _prune_selected_chain(self):
        chain = self.scan_chain.get().upper()
        if not messagebox.askyesno("Prune chain corpus", f"Delete all locally cached BnP corpus records for {chain}?\n\nThis does not alter any blockchain or node data."):
            return
        count = self.db.prune_chain(chain)
        self._refresh_counts()
        self.status.set(f"Pruned {count:,} local {chain} corpus records")

    def _close(self):
        self.scan_cancel.set()
        self.build_cancel.set()
        self.mine_cancel.set()
        self.db.close()
        self.destroy()


def main():
    BnPApp().mainloop()


if __name__ == "__main__":
    main()
