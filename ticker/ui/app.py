"""
Small cross-platform desktop app: live heart rate display + persistent logging.

Reads heart rate from whichever source config.HR_SOURCE selects -- a BLE
strap or broadcasting watch, or a watch pushing over the network (see
hr_source) -- shows live bpm plus a scrolling graph, and logs to the v2 observations
database (ticker.db.store) whenever a session is running, so it can be
analyzed later.

Each reading becomes several rows: heart rate, one per RR interval in the
packet, and HRV derived from those by the normalizer -- the strap never
sends HRV itself. A v1 database is migrated in place the first time this
runs; see ticker/db/migrate.py.

Sessions are a *logical*, user-controlled concept -- Start/Stop -- separate
from the connection, which comes and goes on its own (auto-reconnect) in
the background. A session survives brief connection drops; only the user
ends it.

Nothing here is hardcoded to a specific device, transport or machine -- see
config.py to point this at a different source, device, database location.

Run:
    python -m ticker.ui.app
"""

from __future__ import annotations

import itertools
import queue
import sys
import time
import traceback
import tkinter as tk
from collections import deque

import config
import hr_source
from ticker.ingest.session_logger import SessionLogger


def _setup_logging():
    """When packaged with PyInstaller's --windowed flag there is no console,
    so sys.stdout/sys.stderr can be None -- printing (e.g. traceback.print_exc()
    in our own exception handlers) would then raise instead of just logging.
    Redirect both to a log file next to the database so diagnostics are
    always captured somewhere findable, and printing never crashes the app.
    """
    if getattr(sys, "frozen", False):
        log_path = config.DB_PATH.parent / "ticker.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = open(log_path, "a", buffering=1, encoding="utf-8")
        sys.stdout = log_file
        sys.stderr = log_file


class HRApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Ticker")
        self.root.configure(bg=config.BG)
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.out_queue: "queue.Queue" = queue.Queue()
        try:
            self.source = hr_source.create_source(self.out_queue)
        except ValueError as exc:
            # A misspelled HRM_SOURCE shouldn't be an invisible crash in a
            # windowed build. Come up with no source, and let the error ride
            # the normal queue so it lands in the status line like any other.
            self.source = None
            self.out_queue.put({"type": "error", "message": str(exc)})

        # All persistence lives behind this: opening and migrating the
        # database, sessions, and turning messages into observations. It
        # reports its own failures onto out_queue and stays usable-but-inert
        # if the database can't be opened, so the window still shows live bpm.
        self.logger = (SessionLogger(config.HR_SOURCE, error_queue=self.out_queue)
                       if self.source is not None else None)

        # Connection state (independent of session state)
        self.connected = False
        self.connected_device_name = None
        self.connected_device_address = None

        # Logical session state
        self._token_counter = itertools.count(1)
        self.session_active = False
        self.session_token = None
        self.session_start_ts = None
        self.sample_count = 0
        self.hr_sum = 0
        self.hr_min = None
        self.hr_max = None
        self.graph_points = deque()  # (epoch_seconds, hr)

        self._build_ui()
        if self.source is not None:
            self.source.start()
        self.root.after(config.POLL_INTERVAL_MS, self._tick)

    # UI construction

    def _build_ui(self):
        pad = {"padx": 12, "pady": 6}

        self.status_var = tk.StringVar(value="Starting…")
        self.status_label = tk.Label(
            self.root, textvariable=self.status_var, bg=config.BG, fg=config.WARN,
            font=(config.FONT_FAMILY, 11, "bold"), anchor="w",
        )
        self.status_label.grid(row=0, column=0, sticky="w", **pad)

        self.bpm_var = tk.StringVar(value="--")
        tk.Label(
            self.root, textvariable=self.bpm_var, bg=config.BG, fg=config.ACCENT,
            font=(config.FONT_FAMILY, 64, "bold"),
        ).grid(row=1, column=0, sticky="n", padx=12, pady=(0, 0))
        tk.Label(self.root, text="bpm", bg=config.BG, fg=config.SUBTEXT,
                  font=(config.FONT_FAMILY, 12)).grid(row=2, column=0, sticky="n")

        tiles = tk.Frame(self.root, bg=config.BG)
        tiles.grid(row=3, column=0, sticky="ew", padx=12, pady=10)
        for i in range(4):
            tiles.columnconfigure(i, weight=1)

        self.avg_var = tk.StringVar(value="--")
        self.min_var = tk.StringVar(value="--")
        self.max_var = tk.StringVar(value="--")
        self.elapsed_var = tk.StringVar(value="00:00")

        self._make_tile(tiles, 0, "AVG", self.avg_var)
        self._make_tile(tiles, 1, "MIN", self.min_var)
        self._make_tile(tiles, 2, "MAX", self.max_var)
        self._make_tile(tiles, 3, "TIME", self.elapsed_var)

        controls = tk.Frame(self.root, bg=config.BG)
        controls.grid(row=4, column=0, sticky="ew", padx=12, pady=(0, 8))
        controls.columnconfigure(1, weight=1)

        tk.Label(controls, text="Session:", bg=config.BG, fg=config.SUBTEXT,
                  font=(config.FONT_FAMILY, 9)).grid(row=0, column=0, sticky="w")
        self.label_var = tk.StringVar(value="")
        self.label_entry = tk.Entry(
            controls, textvariable=self.label_var, bg=config.PANEL_BG, fg=config.TEXT,
            insertbackground=config.TEXT, relief="flat",
        )
        self.label_entry.grid(row=0, column=1, sticky="ew", padx=8)

        self.start_button = tk.Button(
            controls, text="Start Session", command=self._on_start_stop_clicked,
            bg=config.GOOD, fg="#0a0a0a", activebackground=config.GOOD,
            relief="flat", font=(config.FONT_FAMILY, 9, "bold"), padx=10, state="disabled",
        )
        self.start_button.grid(row=0, column=2, sticky="e")

        self.canvas = tk.Canvas(
            self.root, width=config.GRAPH_WIDTH, height=config.GRAPH_HEIGHT,
            bg=config.PANEL_BG, highlightthickness=0,
        )
        self.canvas.grid(row=5, column=0, padx=12, pady=(0, 8))

        self.footer_var = tk.StringVar(value=f"Data: {config.DB_PATH}")
        tk.Label(
            self.root, textvariable=self.footer_var, bg=config.BG, fg=config.SUBTEXT,
            font=(config.FONT_FAMILY, 8), anchor="w",
        ).grid(row=6, column=0, sticky="w", padx=12, pady=(0, 10))

    def _make_tile(self, parent, col, title, var):
        frame = tk.Frame(parent, bg=config.PANEL_BG)
        frame.grid(row=0, column=col, sticky="ew", padx=4)
        tk.Label(frame, text=title, bg=config.PANEL_BG, fg=config.SUBTEXT,
                  font=(config.FONT_FAMILY, 9, "bold")).pack(pady=(8, 0))
        tk.Label(frame, textvariable=var, bg=config.PANEL_BG, fg=config.TEXT,
                  font=(config.FONT_FAMILY, 18, "bold")).pack(pady=(0, 8))

    # queue draining / periodic tick

    def _tick(self):
        # However this call goes, it must always reschedule itself -- an
        # unhandled exception here would silently and permanently stop all
        # future UI updates without crashing the app (it would just look
        # frozen). Everything risky is therefore wrapped, and the
        # `finally` guarantees the next tick is always scheduled.
        try:
            while True:
                try:
                    msg = self.out_queue.get_nowait()
                except queue.Empty:
                    break
                self._handle_message(msg)

            if self.session_active and self.session_start_ts is not None:
                now = time.time()
                elapsed = int(now - self.session_start_ts)
                self.elapsed_var.set(f"{elapsed // 60:02d}:{elapsed % 60:02d}")
                # Driven from the tick rather than from incoming samples, so
                # the trace keeps scrolling (and eventually empties) while a
                # connection is dropped, instead of freezing mid-graph.
                cutoff = now - config.GRAPH_WINDOW_SEC
                while self.graph_points and self.graph_points[0][0] < cutoff:
                    self.graph_points.popleft()
                self._redraw_graph(now)
        except Exception:
            traceback.print_exc()
        finally:
            self.root.after(config.POLL_INTERVAL_MS, self._tick)

    def _handle_message(self, msg: dict):
        try:
            kind = msg["type"]
            if kind == "status":
                self._handle_status(msg)
            elif kind == "sample":
                self._handle_sample(msg)
            elif kind == "error":
                self.status_var.set(f"⚠ {msg['message']}")
                self.status_label.configure(fg=config.WARN)
        except Exception:
            traceback.print_exc()

    def _handle_status(self, msg: dict):
        status = msg["status"]
        # A source may supply its own wording -- the HTTP one uses it to show
        # the URL to point the watch at, which the generic text can't. Where
        # it doesn't, fall back to the transport-neutral phrasing below.
        detail = msg.get("message")
        if status == "searching":
            self.connected = False
            self.bpm_var.set("--")
            self.status_var.set(detail or "Searching for heart rate monitor…")
            self.status_label.configure(fg=config.WARN)
        elif status == "connected":
            self.connected = True
            self.connected_device_name = msg.get("device_name")
            self.connected_device_address = msg.get("device_address")
            name = self.connected_device_name or "device"
            self.status_var.set(detail or f"Connected to {name}")
            self.status_label.configure(fg=config.GOOD)
        elif status == "reconnecting":
            self.connected = False
            self.bpm_var.set("--")
            self.status_var.set(detail or "Connection lost — reconnecting…")
            self.status_label.configure(fg=config.WARN)
        elif status == "stopped":
            self.connected = False
            self.bpm_var.set("--")
            self.status_var.set(detail or "Stopped")
            self.status_label.configure(fg=config.SUBTEXT)
        self._update_start_button_state()

    def _handle_sample(self, msg: dict):
        hr = msg["hr"]
        now = time.time()

        # The live number is always shown once connected, session or not --
        # it's useful feedback that the strap is actually being read.
        self.bpm_var.set(str(hr))

        if not self.session_active:
            return

        if self.logger is not None:
            self.logger.log_sample(msg)

        self.sample_count += 1
        self.hr_sum += hr
        self.hr_min = hr if self.hr_min is None else min(self.hr_min, hr)
        self.hr_max = hr if self.hr_max is None else max(self.hr_max, hr)

        self.avg_var.set(f"{self.hr_sum / self.sample_count:.0f}")
        self.min_var.set(str(self.hr_min))
        self.max_var.set(str(self.hr_max))

        # Pruning and redrawing are the tick's job -- see _tick().
        self.graph_points.append((now, hr))

    # session control

    def _update_start_button_state(self):
        if self.session_active:
            return  # Stop must stay clickable even if the connection drops
        self.start_button.configure(state="normal" if self.connected else "disabled")

    def _on_start_stop_clicked(self):
        if self.session_active:
            self._stop_session()
        else:
            self._start_session()

    def _start_session(self):
        if not self.connected:
            return
        self.session_token = next(self._token_counter)
        label = self.label_var.get().strip() or None
        if self.logger is not None:
            self.logger.start_session(
                label=label, device_name=self.connected_device_name,
                device_address=self.connected_device_address)

        self.session_active = True
        self.session_start_ts = time.time()
        self.sample_count = 0
        self.hr_sum = 0
        self.hr_min = None
        self.hr_max = None
        self.graph_points.clear()

        self.avg_var.set("--")
        self.min_var.set("--")
        self.max_var.set("--")
        self.elapsed_var.set("00:00")
        self.canvas.delete("all")
        self.footer_var.set(f"Data: {config.DB_PATH}  (session #{self.session_token})")

        self.start_button.configure(text="Stop Session", bg=config.BAD)
        self.label_entry.configure(state="disabled")

    def _stop_session(self):
        if self.session_start_ts is not None:
            elapsed = int(time.time() - self.session_start_ts)
            self.elapsed_var.set(f"{elapsed // 60:02d}:{elapsed % 60:02d}")

        if self.logger is not None:
            self.logger.end_session()

        self.session_active = False
        self.session_start_ts = None
        self.footer_var.set(f"Data: {config.DB_PATH}")

        self.start_button.configure(text="Start Session", bg=config.GOOD)
        self.label_entry.configure(state="normal")
        self.label_var.set("")  # don't silently carry the old label into the next session
        self._update_start_button_state()

    # graph

    def _redraw_graph(self, now):
        self.canvas.delete("all")
        if len(self.graph_points) < 2:
            return

        hrs = [hr for _, hr in self.graph_points]
        hr_lo, hr_hi = min(hrs), max(hrs)
        if hr_hi == hr_lo:
            hr_hi += 1
        pad = max((hr_hi - hr_lo) * 0.15, 1)
        lo, hi = hr_lo - pad, hr_hi + pad

        def to_xy(t, hr):
            x = config.GRAPH_WIDTH * (1 - (now - t) / config.GRAPH_WINDOW_SEC)
            y = config.GRAPH_HEIGHT - (hr - lo) / (hi - lo) * config.GRAPH_HEIGHT
            return x, y

        coords = []
        for t, hr in self.graph_points:
            coords.extend(to_xy(t, hr))
        self.canvas.create_line(*coords, fill=config.GRAPH_LINE, width=2, smooth=True)
        self.canvas.create_text(6, 8, anchor="nw", text=f"{hr_hi}",
                                  fill=config.SUBTEXT, font=(config.FONT_FAMILY, 8))
        self.canvas.create_text(6, config.GRAPH_HEIGHT - 8, anchor="sw", text=f"{hr_lo}",
                                  fill=config.SUBTEXT, font=(config.FONT_FAMILY, 8))

    # shutdown

    def on_close(self):
        if self.session_active:
            self._stop_session()
        if self.source is not None:
            self.source.stop()
        if self.logger is not None:
            self.logger.close()     # commits whatever is still coalescing
        self.root.destroy()


def main():
    _setup_logging()
    root = tk.Tk()
    HRApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
