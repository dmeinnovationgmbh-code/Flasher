"""Tkinter desktop front-end for the MED17.7.5 flasher.

The GUI ties the whole toolkit together in five tabs:

* **Connection** - choose a CAN backend (or the built-in simulator), pick an ECU
  profile, connect and read identification.
* **Flash**      - load a firmware file (or fetch one from the file server),
  watch a live progress bar/log while it flashes, and abort if needed.
* **Seed/Key**   - compute keys interactively and run the seed/key server.
* **File Server**- browse, download and upload firmware in the repository.
* **Log**        - the full application log.

Flashing runs on a worker thread; progress and log lines are marshalled back to
the Tk main loop through a thread-safe queue (Tk is single-threaded).
"""

from __future__ import annotations

import logging
import os
import queue
import threading
from typing import Optional

from ..core import (
    IsoTpConfig,
    IsoTpLayer,
    UdsClient,
    UdsTiming,
    create_bus,
    default_profile,
    load_firmware,
    load_profile,
)
from ..core.ecu_profile import EcuProfile
from ..core.flash_sequence import Flasher, FlashProgress, ProfileSeedKey, Stage
from ..exceptions import Med17FlasherError
from ..logging_setup import configure_logging, get_logger
from ..seedkey import SeedKeyStore, compute_key, list_algorithms

log = get_logger("gui")


def _require_tk():
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk

        return tk, ttk, filedialog, messagebox
    except Exception as exc:  # pragma: no cover - environment dependent
        raise Med17FlasherError(
            "Tkinter is not available. Install it (e.g. 'apt install python3-tk')."
        ) from exc


class _QueueLogHandler(logging.Handler):
    """A logging handler that pushes formatted records into a queue."""

    def __init__(self, q: "queue.Queue[str]") -> None:
        super().__init__()
        self.q = q

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.q.put_nowait(self.format(record))
        except queue.Full:  # pragma: no cover
            pass


class FlasherApp:
    """The main application window."""

    def __init__(self) -> None:
        tk, ttk, filedialog, messagebox = _require_tk()
        self.tk = tk
        self.ttk = ttk
        self.filedialog = filedialog
        self.messagebox = messagebox

        self.root = tk.Tk()
        self.root.title("DME Innovation MED17 Flasher")
        self.root.geometry("880x620")
        self.root.minsize(760, 520)

        # thread -> UI communication
        self._events: "queue.Queue" = queue.Queue()
        self._log_queue: "queue.Queue[str]" = queue.Queue()

        # runtime state
        self.profile: EcuProfile = default_profile()
        self.bus = None
        self.uds: Optional[UdsClient] = None
        self.simulator = None
        self.flasher: Optional[Flasher] = None
        self._abort = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._server_handles = {}

        self._build_ui()
        self._attach_log_handler()
        self.root.after(80, self._pump)

    # ------------------------------------------------------------------ #
    # UI construction
    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        tk, ttk = self.tk, self.ttk
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True, padx=8, pady=8)

        self.tab_conn = ttk.Frame(notebook)
        self.tab_flash = ttk.Frame(notebook)
        self.tab_seed = ttk.Frame(notebook)
        self.tab_files = ttk.Frame(notebook)
        self.tab_log = ttk.Frame(notebook)
        notebook.add(self.tab_conn, text="Connection")
        notebook.add(self.tab_flash, text="Flash")
        notebook.add(self.tab_seed, text="Seed/Key")
        notebook.add(self.tab_files, text="File Server")
        notebook.add(self.tab_log, text="Log")

        self._build_connection_tab()
        self._build_flash_tab()
        self._build_seedkey_tab()
        self._build_fileserver_tab()
        self._build_log_tab()

        self.status = tk.StringVar(value="Ready")
        ttk.Label(self.root, textvariable=self.status, relief="sunken", anchor="w").pack(
            fill="x", side="bottom"
        )

    def _build_connection_tab(self) -> None:
        tk, ttk = self.tk, self.ttk
        frm = self.tab_conn
        row = 0

        ttk.Label(frm, text="CAN backend:").grid(row=row, column=0, sticky="w", padx=6, pady=6)
        self.var_backend = tk.StringVar(value="virtual")
        ttk.Entry(frm, textvariable=self.var_backend, width=28).grid(row=row, column=1, sticky="w")
        self.var_sim = tk.BooleanVar(value=True)
        ttk.Checkbutton(frm, text="Use built-in simulator", variable=self.var_sim).grid(
            row=row, column=2, sticky="w", padx=6
        )
        row += 1

        ttk.Label(frm, text="Profile:").grid(row=row, column=0, sticky="w", padx=6, pady=6)
        self.var_profile = tk.StringVar(value="(built-in MED17.7.5)")
        ttk.Entry(frm, textvariable=self.var_profile, width=48).grid(row=row, column=1, columnspan=2, sticky="w")
        ttk.Button(frm, text="Browse...", command=self._pick_profile).grid(row=row, column=3, padx=6)
        row += 1

        ttk.Label(frm, text="TX id:").grid(row=row, column=0, sticky="w", padx=6)
        self.var_tx = tk.StringVar(value=f"0x{self.profile.can.tx_id:03X}")
        ttk.Entry(frm, textvariable=self.var_tx, width=10).grid(row=row, column=1, sticky="w")
        ttk.Label(frm, text="RX id:").grid(row=row, column=1, sticky="e")
        self.var_rx = tk.StringVar(value=f"0x{self.profile.can.rx_id:03X}")
        ttk.Entry(frm, textvariable=self.var_rx, width=10).grid(row=row, column=2, sticky="w")
        row += 1

        btns = ttk.Frame(frm)
        btns.grid(row=row, column=0, columnspan=4, sticky="w", padx=6, pady=8)
        ttk.Button(btns, text="Connect", command=self._connect).pack(side="left")
        ttk.Button(btns, text="Disconnect", command=self._disconnect).pack(side="left", padx=6)
        ttk.Button(btns, text="Read identification", command=self._identify).pack(side="left")
        row += 1

        ttk.Label(frm, text="Identification:").grid(row=row, column=0, sticky="nw", padx=6)
        self.txt_ident = tk.Text(frm, height=12, width=80, state="disabled")
        self.txt_ident.grid(row=row, column=1, columnspan=3, sticky="nsew", padx=6, pady=6)
        frm.rowconfigure(row, weight=1)
        frm.columnconfigure(1, weight=1)

    def _build_flash_tab(self) -> None:
        tk, ttk = self.tk, self.ttk
        frm = self.tab_flash

        top = ttk.Frame(frm)
        top.pack(fill="x", padx=6, pady=6)
        ttk.Label(top, text="Firmware:").pack(side="left")
        self.var_firmware = tk.StringVar()
        ttk.Entry(top, textvariable=self.var_firmware, width=54).pack(side="left", padx=6)
        ttk.Button(top, text="Browse...", command=self._pick_firmware).pack(side="left")

        opts = ttk.Frame(frm)
        opts.pack(fill="x", padx=6)
        ttk.Label(opts, text="Base addr (raw .bin):").pack(side="left")
        self.var_base = tk.StringVar(value="0x80040000")
        ttk.Entry(opts, textvariable=self.var_base, width=14).pack(side="left", padx=6)
        self.var_seedstore = tk.StringVar()
        ttk.Label(opts, text="Seed/key store:").pack(side="left", padx=(12, 0))
        ttk.Entry(opts, textvariable=self.var_seedstore, width=24).pack(side="left", padx=6)
        ttk.Button(opts, text="...", width=3, command=self._pick_seedstore).pack(side="left")

        actions = ttk.Frame(frm)
        actions.pack(fill="x", padx=6, pady=8)
        self.btn_flash = ttk.Button(actions, text="Flash", command=self._start_flash)
        self.btn_flash.pack(side="left")
        self.btn_abort = ttk.Button(actions, text="Abort", command=self._abort_flash, state="disabled")
        self.btn_abort.pack(side="left", padx=6)
        ttk.Button(actions, text="Dry run", command=lambda: self._start_flash(dry_run=True)).pack(side="left")

        self.progress = ttk.Progressbar(frm, mode="determinate", maximum=100)
        self.progress.pack(fill="x", padx=6, pady=(4, 2))
        self.var_stage = tk.StringVar(value="idle")
        ttk.Label(frm, textvariable=self.var_stage).pack(anchor="w", padx=6)

        self.txt_flashlog = tk.Text(frm, height=18, state="disabled")
        self.txt_flashlog.pack(fill="both", expand=True, padx=6, pady=6)

    def _build_seedkey_tab(self) -> None:
        tk, ttk = self.tk, self.ttk
        frm = self.tab_seed

        box = ttk.LabelFrame(frm, text="Compute key")
        box.pack(fill="x", padx=6, pady=6)
        ttk.Label(box, text="Algorithm:").grid(row=0, column=0, sticky="w", padx=6, pady=4)
        self.var_algo = tk.StringVar(value="med17")
        ttk.Combobox(box, textvariable=self.var_algo, values=list_algorithms(), width=14).grid(
            row=0, column=1, sticky="w"
        )
        ttk.Label(box, text="Level:").grid(row=0, column=2, sticky="e")
        self.var_level = tk.StringVar(value="0x11")
        ttk.Entry(box, textvariable=self.var_level, width=8).grid(row=0, column=3, sticky="w")

        ttk.Label(box, text="Seed (hex):").grid(row=1, column=0, sticky="w", padx=6)
        self.var_seed = tk.StringVar(value="11223344")
        ttk.Entry(box, textvariable=self.var_seed, width=24).grid(row=1, column=1, sticky="w")
        ttk.Label(box, text="Params (k=..,rounds=..):").grid(row=1, column=2, sticky="e")
        self.var_params = tk.StringVar(value="k=0x1C5A36B7,rounds=5,shift=5")
        ttk.Entry(box, textvariable=self.var_params, width=28).grid(row=1, column=3, sticky="w")

        ttk.Button(box, text="Compute", command=self._compute_key).grid(row=2, column=0, padx=6, pady=6)
        self.var_key = tk.StringVar(value="")
        ttk.Label(box, text="Key:").grid(row=2, column=1, sticky="e")
        ttk.Entry(box, textvariable=self.var_key, width=24, state="readonly").grid(row=2, column=2, sticky="w")

        srv = ttk.LabelFrame(frm, text="Seed/key server")
        srv.pack(fill="x", padx=6, pady=6)
        ttk.Label(srv, text="HTTP port:").grid(row=0, column=0, padx=6, pady=4)
        self.var_sk_port = tk.StringVar(value="8377")
        ttk.Entry(srv, textvariable=self.var_sk_port, width=8).grid(row=0, column=1, sticky="w")
        ttk.Label(srv, text="TCP port:").grid(row=0, column=2)
        self.var_sk_tcp = tk.StringVar(value="8378")
        ttk.Entry(srv, textvariable=self.var_sk_tcp, width=8).grid(row=0, column=3, sticky="w")
        ttk.Button(srv, text="Start", command=self._start_seedkey_server).grid(row=0, column=4, padx=6)
        ttk.Button(srv, text="Stop", command=self._stop_seedkey_server).grid(row=0, column=5)

    def _build_fileserver_tab(self) -> None:
        tk, ttk = self.tk, self.ttk
        frm = self.tab_files

        conn = ttk.Frame(frm)
        conn.pack(fill="x", padx=6, pady=6)
        ttk.Label(conn, text="Server URL:").pack(side="left")
        self.var_fs_url = tk.StringVar(value="http://127.0.0.1:8080")
        ttk.Entry(conn, textvariable=self.var_fs_url, width=30).pack(side="left", padx=6)
        ttk.Label(conn, text="Token:").pack(side="left")
        self.var_fs_token = tk.StringVar()
        ttk.Entry(conn, textvariable=self.var_fs_token, width=16, show="*").pack(side="left", padx=6)
        ttk.Button(conn, text="Refresh", command=self._refresh_firmwares).pack(side="left")

        cols = ("id", "filename", "ecu", "sw", "size")
        self.tree = ttk.Treeview(frm, columns=cols, show="headings", height=12)
        for col, width in zip(cols, (140, 200, 110, 140, 80)):
            self.tree.heading(col, text=col)
            self.tree.column(col, width=width)
        self.tree.pack(fill="both", expand=True, padx=6, pady=6)

        actions = ttk.Frame(frm)
        actions.pack(fill="x", padx=6, pady=6)
        ttk.Button(actions, text="Use selected for flash", command=self._use_selected_firmware).pack(side="left")
        ttk.Button(actions, text="Upload...", command=self._upload_firmware).pack(side="left", padx=6)
        ttk.Button(actions, text="Delete selected", command=self._delete_firmware).pack(side="left")

    def _build_log_tab(self) -> None:
        tk = self.tk
        self.txt_log = tk.Text(self.tab_log, state="disabled")
        self.txt_log.pack(fill="both", expand=True, padx=6, pady=6)

    # ------------------------------------------------------------------ #
    # Logging plumbing
    # ------------------------------------------------------------------ #
    def _attach_log_handler(self) -> None:
        handler = _QueueLogHandler(self._log_queue)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S"))
        logging.getLogger("med17flasher").addHandler(handler)
        logging.getLogger("med17flasher").setLevel(logging.INFO)

    def _append(self, widget, text: str) -> None:
        widget.configure(state="normal")
        widget.insert("end", text + "\n")
        widget.see("end")
        widget.configure(state="disabled")

    def _pump(self) -> None:
        # Drain both queues; a failure handling any single item must never stop
        # the pump (the reschedule below always runs).
        try:
            try:
                while True:
                    line = self._log_queue.get_nowait()
                    self._append(self.txt_log, line)
            except queue.Empty:
                pass
            try:
                while True:
                    kind, payload = self._events.get_nowait()
                    try:
                        self._handle_event(kind, payload)
                    except Exception:  # noqa: BLE001 - one bad event won't kill the UI
                        log.exception("error handling UI event %r", kind)
            except queue.Empty:
                pass
        finally:
            self.root.after(80, self._pump)

    def _handle_event(self, kind: str, payload) -> None:
        if kind == "progress":
            p: FlashProgress = payload
            self.progress["value"] = p.percent
            self.var_stage.set(f"{p.stage.value}: {p.message}")
            self._append(self.txt_flashlog, f"[{p.stage.value}] {p.message or p.block_name}")
        elif kind == "done":
            self._flash_finished(success=True, message=payload)
        elif kind == "error":
            self._flash_finished(success=False, message=payload)
        elif kind == "status":
            self.status.set(payload)
        elif kind == "ident":
            self._show_ident(payload)

    # ------------------------------------------------------------------ #
    # Connection actions
    # ------------------------------------------------------------------ #
    def _pick_profile(self) -> None:
        path = self.filedialog.askopenfilename(
            title="Select ECU profile", filetypes=[("Profiles", "*.yaml *.yml *.json"), ("All", "*.*")]
        )
        if path:
            self.var_profile.set(path)

    def _load_selected_profile(self) -> EcuProfile:
        path = self.var_profile.get()
        if path and not path.startswith("("):
            return load_profile(path)
        return default_profile()

    def _connect(self) -> None:
        try:
            self._disconnect()
            self.profile = self._load_selected_profile()
            try:
                self.profile.can.tx_id = int(self.var_tx.get(), 0)
                self.profile.can.rx_id = int(self.var_rx.get(), 0)
            except ValueError:
                pass
            if self.var_sim.get():
                from ..core import VirtualCanNetwork
                from ..simulator import VirtualEcu, VirtualEcuConfig

                # Fresh isolated network per connect so reconnecting never
                # leaves stale simulators sharing the bus.
                net = VirtualCanNetwork()
                ecu_bus = net.new_endpoint("ecu")
                self.bus = net.new_endpoint("tester")
                self.simulator = VirtualEcu(
                    ecu_bus,
                    self.profile,
                    VirtualEcuConfig(
                        security_algorithm=self.profile.security.algorithm,
                        security_params=self.profile.security.params,
                    ),
                )
                self.simulator.start()
            else:
                self.bus = create_bus(self.var_backend.get())
            self.uds = self._make_uds(self.bus, self.profile)
            self.status.set(f"Connected ({'simulator' if self.var_sim.get() else self.var_backend.get()})")
            log.info("connected using %s", "simulator" if self.var_sim.get() else self.var_backend.get())
        except Exception as exc:  # noqa: BLE001
            self.messagebox.showerror("Connect failed", str(exc))
            self.status.set("Connect failed")

    def _make_uds(self, bus, profile: EcuProfile) -> UdsClient:
        tp = IsoTpLayer(
            bus,
            IsoTpConfig(
                tx_id=profile.can.tx_id,
                rx_id=profile.can.rx_id,
                is_extended_id=profile.can.is_extended_id,
                padding_byte=profile.can.padding_byte,
            ),
        )
        return UdsClient(tp, UdsTiming(p2=profile.timing.p2, p2_star=profile.timing.p2_star))

    def _disconnect(self) -> None:
        if self.simulator:
            self.simulator.stop()
            self.simulator = None
        if self.bus:
            try:
                self.bus.close()
            except Exception:  # noqa: BLE001
                pass
            self.bus = None
        self.uds = None
        self.status.set("Disconnected")

    def _identify(self) -> None:
        if not self.uds:
            self.messagebox.showwarning("Not connected", "Connect first.")
            return

        def worker():
            try:
                flasher = Flasher(self.uds, self.profile, ProfileSeedKey(self.profile))
                ident = flasher.identify()
                self._events.put(("ident", ident))
            except Exception as exc:  # noqa: BLE001
                self._events.put(("status", f"identify failed: {exc}"))

        threading.Thread(target=worker, daemon=True).start()

    def _show_ident(self, ident) -> None:
        self.txt_ident.configure(state="normal")
        self.txt_ident.delete("1.0", "end")
        for did, value in ident:
            text = value.decode("latin-1", "replace").strip("\x00 ")
            self.txt_ident.insert("end", f"DID 0x{did:04X}: {text!r}  ({value.hex()})\n")
        self.txt_ident.configure(state="disabled")

    # ------------------------------------------------------------------ #
    # Flash actions
    # ------------------------------------------------------------------ #
    def _pick_firmware(self) -> None:
        path = self.filedialog.askopenfilename(
            title="Select firmware",
            filetypes=[("Firmware", "*.bin *.hex *.s19 *.srec"), ("All", "*.*")],
        )
        if path:
            self.var_firmware.set(path)

    def _pick_seedstore(self) -> None:
        path = self.filedialog.askopenfilename(title="Seed/key store", filetypes=[("JSON", "*.json")])
        if path:
            self.var_seedstore.set(path)

    def _start_flash(self, dry_run: bool = False) -> None:
        if not self.uds:
            self.messagebox.showwarning("Not connected", "Connect first.")
            return
        firmware = self.var_firmware.get()
        if not firmware or not os.path.isfile(firmware):
            self.messagebox.showwarning("No firmware", "Pick a firmware file first.")
            return
        if self._worker and self._worker.is_alive():
            return
        self._abort = threading.Event()
        self.btn_flash.configure(state="disabled")
        self.btn_abort.configure(state="normal")
        self.progress["value"] = 0

        def worker():
            try:
                base = int(self.var_base.get(), 0) if self.var_base.get() else 0
                image = load_firmware(firmware, base_address=base)
                resolver = ProfileSeedKey(self.profile)
                store_path = self.var_seedstore.get()
                if store_path and os.path.isfile(store_path):
                    resolver = SeedKeyStore.load(store_path)
                flasher = Flasher(
                    self.uds,
                    self.profile,
                    resolver,
                    progress=lambda p: self._events.put(("progress", p)),
                    abort_event=self._abort,
                )
                if dry_run:
                    for block in image.blocks_for(self.profile.memory_map):
                        self._events.put(
                            ("progress", FlashProgress(Stage.IDLE,
                             message=f"[dry] {block.name} 0x{block.address:08X} {block.size} bytes"))
                        )
                    self._events.put(("done", "dry run complete"))
                    return
                result = flasher.flash(image)
                self._events.put(("done", f"flashed {', '.join(result.blocks)} in {result.duration:.1f}s"))
            except Exception as exc:  # noqa: BLE001
                self._events.put(("error", str(exc)))

        self._worker = threading.Thread(target=worker, daemon=True)
        self._worker.start()

    def _abort_flash(self) -> None:
        self._abort.set()
        self.status.set("Aborting...")

    def _flash_finished(self, success: bool, message: str) -> None:
        self.btn_flash.configure(state="normal")
        self.btn_abort.configure(state="disabled")
        if success:
            self.progress["value"] = 100
            self.status.set(message)
        else:
            self.status.set(f"FAILED: {message}")
            self.messagebox.showerror("Flash failed", message)

    # ------------------------------------------------------------------ #
    # Seed/key actions
    # ------------------------------------------------------------------ #
    def _compute_key(self) -> None:
        try:
            seed = bytes.fromhex(self.var_seed.get().replace(" ", ""))
            params = {}
            for item in self.var_params.get().split(","):
                if "=" in item:
                    k, v = item.split("=", 1)
                    params[k.strip()] = v.strip()
            level = int(self.var_level.get(), 0)
            key = compute_key(self.var_algo.get(), seed, level=level, params=params)
            self.var_key.set(key.hex())
        except Exception as exc:  # noqa: BLE001
            self.messagebox.showerror("Compute failed", str(exc))

    def _start_seedkey_server(self) -> None:
        from ..seedkey.server import SeedKeyHttpServer, SeedKeyService, SeedKeyTcpServer

        if "seedkey" in self._server_handles:
            return
        try:
            service = SeedKeyService(SeedKeyStore.default())
            http = SeedKeyHttpServer(service, port=int(self.var_sk_port.get()))
            tcp = SeedKeyTcpServer(service, port=int(self.var_sk_tcp.get()))
            http.start()
            tcp.start()
            self._server_handles["seedkey"] = (http, tcp)
            self.status.set(f"seed/key server on {self.var_sk_port.get()}/{self.var_sk_tcp.get()}")
        except Exception as exc:  # noqa: BLE001
            self.messagebox.showerror("Server failed", str(exc))

    def _stop_seedkey_server(self) -> None:
        handles = self._server_handles.pop("seedkey", None)
        if handles:
            for h in handles:
                h.stop()
            self.status.set("seed/key server stopped")

    # ------------------------------------------------------------------ #
    # File server actions
    # ------------------------------------------------------------------ #
    def _fs_client(self):
        from ..server import FileServerClient

        return FileServerClient(self.var_fs_url.get(), token=self.var_fs_token.get() or None)

    def _refresh_firmwares(self) -> None:
        try:
            client = self._fs_client()
            items = client.list()
            for row in self.tree.get_children():
                self.tree.delete(row)
            for m in items:
                self.tree.insert(
                    "", "end",
                    values=(m["id"], m["filename"], m["ecu"], m["sw_version"], m["size"]),
                )
            self.status.set(f"{len(items)} firmware(s) listed")
        except Exception as exc:  # noqa: BLE001
            self.messagebox.showerror("File server", str(exc))

    def _selected_fw_id(self) -> Optional[str]:
        sel = self.tree.selection()
        if not sel:
            return None
        return self.tree.item(sel[0])["values"][0]

    def _use_selected_firmware(self) -> None:
        fw_id = self._selected_fw_id()
        if not fw_id:
            return
        try:
            client = self._fs_client()
            data = client.download_bytes(str(fw_id))
            meta = client.get_meta(str(fw_id))
            import tempfile

            suffix = os.path.splitext(meta.get("filename", "fw.bin"))[1] or ".bin"
            fd, path = tempfile.mkstemp(suffix=suffix)
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            self.var_firmware.set(path)
            self.var_base.set(hex(int(meta.get("base_address", 0))))
            self.status.set(f"downloaded {meta.get('filename')} -> {path}")
        except Exception as exc:  # noqa: BLE001
            self.messagebox.showerror("Download failed", str(exc))

    def _upload_firmware(self) -> None:
        path = self.filedialog.askopenfilename(title="Upload firmware")
        if not path:
            return
        try:
            self._fs_client().upload(path)
            self._refresh_firmwares()
        except Exception as exc:  # noqa: BLE001
            self.messagebox.showerror("Upload failed", str(exc))

    def _delete_firmware(self) -> None:
        fw_id = self._selected_fw_id()
        if not fw_id:
            return
        if not self.messagebox.askyesno("Delete", f"Delete firmware {fw_id}?"):
            return
        try:
            self._fs_client().delete(str(fw_id))
            self._refresh_firmwares()
        except Exception as exc:  # noqa: BLE001
            self.messagebox.showerror("Delete failed", str(exc))

    # ------------------------------------------------------------------ #
    def run(self) -> None:
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.mainloop()

    def _on_close(self) -> None:
        try:
            self._stop_seedkey_server()
            self._disconnect()
        finally:
            self.root.destroy()


def main(args=None) -> int:
    configure_logging(logging.INFO)
    try:
        app = FlasherApp()
    except Med17FlasherError as exc:
        print(str(exc))
        return 1
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
