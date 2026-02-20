#!/usr/bin/env python3
"""
mep_gui.py

Tkinter GUI for MEP RFSoC sweep/record control via X11 forwarding.

Wraps MEPController from start_mep_rx_v2.py.

Usage:
    ssh -X mep@<jetson> python3 ~/mep-examples/scripts/mep_gui.py

Author: john.marino@colorado.edu
"""

import sys
import os
import math
import threading
import logging
import tkinter as tk
from tkinter import ttk, scrolledtext

# Allow importing start_mep_rx_v2 from the same directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from start_mep_rx_v2 import (
    MEPController,
    get_frequency_list,
    TUNER_INJECTION_SIDE,
    RFSOC_CMD_TOPIC,
)


# ===== TEXT LOGGING HANDLER ===== #

class _TextHandler(logging.Handler):
    """Logging handler that appends records to a ScrolledText widget."""

    def __init__(self, widget: scrolledtext.ScrolledText):
        super().__init__()
        self.widget = widget

    def emit(self, record: logging.LogRecord):
        msg = self.format(record) + "\n"

        def _append():
            self.widget.configure(state="normal")
            self.widget.insert(tk.END, msg)
            self.widget.see(tk.END)
            self.widget.configure(state="disabled")

        # Schedule on main thread
        self.widget.after(0, _append)


# ===== MAIN GUI CLASS ===== #

class MEPGui:
    CHANNEL_OPTIONS = ["A", "B", "C", "D"]
    TUNER_OPTIONS   = ["None"] + list(TUNER_INJECTION_SIDE.keys()) + ["auto"]

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("MEP RFSoC Controller")
        self.root.resizable(True, True)

        self.mep: MEPController = None
        self._sweep_thread: threading.Thread = None

        self._build_ui()
        self._setup_logging()
        self._schedule_poll()

    # ------------------------------------------------------------------ #
    #  UI Construction                                                     #
    # ------------------------------------------------------------------ #

    def _build_ui(self):
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(3, weight=1)

        # ---- Parameters ---- #
        params = ttk.LabelFrame(self.root, text="Parameters")
        params.grid(row=0, column=0, padx=10, pady=6, sticky="ew")
        params.columnconfigure(1, weight=1)
        params.columnconfigure(3, weight=1)

        field_defs = [
            # (label, key, default, column-pair)
            ("Freq Start (MHz)", "freq_start", "7000",  0),
            ("Freq End (MHz)",   "freq_end",   "",      0),
            ("Step (MHz)",       "step",       "10",    0),
            ("Dwell (s)",        "dwell",      "60",    0),
            ("ADC IF (MHz)",     "adc_if",     "",      2),
            ("Capture Name",     "capture_name", "",    2),
            ("Restart Interval (s)", "restart_interval", "", 2),
        ]

        rows_left  = 0
        rows_right = 0
        self._vars = {}
        for label, key, default, col in field_defs:
            var = tk.StringVar(value=default)
            self._vars[key] = var
            row = rows_left if col == 0 else rows_right
            ttk.Label(params, text=label).grid(
                row=row, column=col,     sticky="w", padx=5, pady=2)
            ttk.Entry(params, textvariable=var, width=18).grid(
                row=row, column=col + 1, sticky="ew", padx=5, pady=2)
            if col == 0:
                rows_left += 1
            else:
                rows_right += 1

        # Channel dropdown (right column, after entries)
        ttk.Label(params, text="Channel").grid(
            row=rows_right, column=2, sticky="w", padx=5, pady=2)
        self._vars["channel"] = tk.StringVar(value="A")
        ttk.Combobox(
            params, textvariable=self._vars["channel"],
            values=self.CHANNEL_OPTIONS, width=16, state="readonly",
        ).grid(row=rows_right, column=3, sticky="ew", padx=5, pady=2)
        rows_right += 1

        # Tuner dropdown (right column)
        ttk.Label(params, text="Tuner").grid(
            row=rows_right, column=2, sticky="w", padx=5, pady=2)
        self._vars["tuner"] = tk.StringVar(value="None")
        ttk.Combobox(
            params, textvariable=self._vars["tuner"],
            values=self.TUNER_OPTIONS, width=16, state="readonly",
        ).grid(row=rows_right, column=3, sticky="ew", padx=5, pady=2)

        # ---- Buttons ---- #
        btn_frame = ttk.Frame(self.root)
        btn_frame.grid(row=1, column=0, padx=10, pady=4, sticky="ew")
        for i in range(4):
            btn_frame.columnconfigure(i, weight=1)

        ttk.Button(btn_frame, text="Start Sweep",
                   command=self._start_sweep).grid(row=0, column=0, padx=4, pady=3, sticky="ew")
        ttk.Button(btn_frame, text="Start Single",
                   command=self._start_single).grid(row=0, column=1, padx=4, pady=3, sticky="ew")
        ttk.Button(btn_frame, text="Stop RFSoC",
                   command=self._stop_rfsoc).grid(row=0, column=2, padx=4, pady=3, sticky="ew")
        ttk.Button(btn_frame, text="Stop All",
                   command=self._stop_all).grid(row=0, column=3, padx=4, pady=3, sticky="ew")

        # ---- Status bar ---- #
        status_frame = ttk.LabelFrame(self.root, text="Status")
        status_frame.grid(row=2, column=0, padx=10, pady=4, sticky="ew")
        status_frame.columnconfigure(0, weight=1)
        self._status_var = tk.StringVar(value="Idle — no controller connected")
        ttk.Label(status_frame, textvariable=self._status_var,
                  anchor="w").grid(row=0, column=0, padx=6, pady=3, sticky="ew")

        # ---- Log box ---- #
        log_frame = ttk.LabelFrame(self.root, text="Log")
        log_frame.grid(row=3, column=0, padx=10, pady=6, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)

        self._log_text = scrolledtext.ScrolledText(
            log_frame, height=16, width=80, state="disabled",
            font=("Courier", 9),
        )
        self._log_text.grid(row=0, column=0, padx=5, pady=5, sticky="nsew")

    # ------------------------------------------------------------------ #
    #  Logging                                                             #
    # ------------------------------------------------------------------ #

    def _setup_logging(self):
        handler = _TextHandler(self._log_text)
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(message)s",
                              datefmt="%H:%M:%S")
        )
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.INFO)
        root_logger.addHandler(handler)

    # ------------------------------------------------------------------ #
    #  Parameter parsing                                                   #
    # ------------------------------------------------------------------ #

    def _parse_params(self) -> dict:
        """Parse and validate UI fields. Raises ValueError on bad input."""
        freq_start = float(self._vars["freq_start"].get())

        freq_end_s = self._vars["freq_end"].get().strip()
        freq_end   = float(freq_end_s) if freq_end_s else float("nan")

        step  = float(self._vars["step"].get())
        dwell = float(self._vars["dwell"].get())

        channel    = self._vars["channel"].get()
        tuner_str  = self._vars["tuner"].get()
        tuner      = None if tuner_str == "None" else tuner_str

        adc_if_s = self._vars["adc_if"].get().strip()
        adc_if   = float(adc_if_s) if adc_if_s else None

        capture_name_s = self._vars["capture_name"].get().strip()
        capture_name   = capture_name_s if capture_name_s else None

        restart_s = self._vars["restart_interval"].get().strip()
        restart_interval = int(restart_s) if restart_s else None

        sample_rate = int(step)

        return {
            "freq_start":       freq_start,
            "freq_end":         freq_end,
            "step":             step,
            "dwell":            dwell,
            "channel":          channel,
            "tuner":            tuner,
            "adc_if":           adc_if,
            "capture_name":     capture_name,
            "restart_interval": restart_interval,
            "sample_rate":      sample_rate,
        }

    # ------------------------------------------------------------------ #
    #  Controller management                                               #
    # ------------------------------------------------------------------ #

    def _get_or_create_mep(self, params: dict) -> MEPController:
        """
        Return the existing MEPController when config is unchanged.
        Otherwise disconnect the old one and create a fresh connection.
        """
        needs_new = (
            self.mep is None
            or self.mep.channel       != params["channel"]
            or self.mep.sample_rate   != params["sample_rate"]
            or self.mep.tuner         != params["tuner"]
            or self.mep.adc_if        != params["adc_if"]
            or self.mep.capture_name  != params["capture_name"]
        )

        if needs_new:
            if self.mep is not None:
                logging.info("Configuration changed — reconnecting")
                try:
                    self.mep.stop_recorder()
                except Exception:
                    pass
                try:
                    self.mep.disconnect()
                except Exception:
                    pass

            logging.info("Connecting to MQTT broker")
            self.mep = MEPController(
                channel      = params["channel"],
                sample_rate  = params["sample_rate"],
                tuner        = params["tuner"],
                adc_if       = params["adc_if"],
                capture_name = params["capture_name"],
            )

        return self.mep

    # ------------------------------------------------------------------ #
    #  Button handlers                                                     #
    # ------------------------------------------------------------------ #

    def _start_sweep(self):
        if self._sweep_thread and self._sweep_thread.is_alive():
            logging.warning("Sweep already running — use Stop All first")
            return

        try:
            params = self._parse_params()
        except ValueError as e:
            logging.error(f"Parameter error: {e}")
            return

        def _worker():
            try:
                mep = self._get_or_create_mep(params)
                mep._stop_flag.clear()
                freqs_hz = get_frequency_list(
                    params["freq_start"], params["freq_end"], params["step"]
                )
                n = len(freqs_hz) if hasattr(freqs_hz, "__len__") else "?"
                logging.info(f"Starting sweep: {n} steps, dwell={params['dwell']}s")
                mep.run_sweep(freqs_hz, params["dwell"], params["restart_interval"])
            except Exception as e:
                logging.error(f"Sweep error: {e}", exc_info=True)
            finally:
                self._status_var.set("Idle")

        self._sweep_thread = threading.Thread(target=_worker, daemon=True, name="sweep")
        self._status_var.set("Sweeping...")
        self._sweep_thread.start()

    def _start_single(self):
        if self._sweep_thread and self._sweep_thread.is_alive():
            logging.warning("Sweep already running — use Stop All first")
            return

        try:
            params = self._parse_params()
        except ValueError as e:
            logging.error(f"Parameter error: {e}")
            return

        def _worker():
            try:
                mep = self._get_or_create_mep(params)
                mep._stop_flag.clear()
                f_hz = int(params["freq_start"] * 1e6)
                logging.info(f"Starting single capture at {params['freq_start']} MHz")
                mep.run_single(f_hz)
                self._status_var.set("Single capture running")
            except Exception as e:
                logging.error(f"Single capture error: {e}", exc_info=True)
                self._status_var.set("Error")

        self._sweep_thread = threading.Thread(target=_worker, daemon=True, name="single")
        self._status_var.set("Starting single capture...")
        self._sweep_thread.start()

    def _stop_rfsoc(self):
        """Publish RFSoC reset only. Recorder keeps running."""
        if self.mep is None:
            logging.warning("No active controller — nothing to reset")
            return
        logging.info("Stop RFSoC — sending reset")
        self.mep._publish(RFSOC_CMD_TOPIC, {"task_name": "reset"})
        self._status_var.set("RFSoC reset sent")

    def _stop_all(self):
        """Interrupt sweep, stop recorder, reset RFSoC, disconnect."""
        def _worker():
            if self.mep is not None:
                logging.info("Stop All — requesting sweep stop")
                self.mep.request_stop()

            if self._sweep_thread and self._sweep_thread.is_alive():
                logging.info("Waiting for sweep thread to exit...")
                self._sweep_thread.join(timeout=5.0)
                if self._sweep_thread.is_alive():
                    logging.warning("Sweep thread did not exit within 5s")

            if self.mep is not None:
                try:
                    self.mep.stop_recorder()
                    self.mep._publish(RFSOC_CMD_TOPIC, {"task_name": "reset"})
                    self.mep.disconnect()
                except Exception as e:
                    logging.error(f"Stop All cleanup error: {e}", exc_info=True)
                finally:
                    self.mep = None

            self._status_var.set("Idle — no controller connected")
            logging.info("Stop All complete")

        threading.Thread(target=_worker, daemon=True, name="stop_all").start()

    # ------------------------------------------------------------------ #
    #  Status polling                                                      #
    # ------------------------------------------------------------------ #

    def _schedule_poll(self):
        self.root.after(1000, self._poll_status)

    def _poll_status(self):
        """Read the cached RFSoC telemetry and update the status bar."""
        if self.mep is not None:
            with self.mep._tlm_lock:
                tlm = self.mep._tlm
            if tlm:
                state = tlm.get("state", "?")
                f_c   = float(tlm.get("f_c_hz", 0)) / 1e6
                pps   = tlm.get("pps_count", "?")
                sweep_active = self._sweep_thread and self._sweep_thread.is_alive()
                label = "Sweeping" if sweep_active else "Connected"
                self._status_var.set(
                    f"{label} — state={state}  f_c={f_c:.2f} MHz  pps={pps}"
                )
        self.root.after(1000, self._poll_status)


# ===== ENTRY POINT ===== #

def main():
    root = tk.Tk()
    app  = MEPGui(root)

    def _on_close():
        # Clean up controller on window close
        if app.mep is not None:
            logging.info("Window closed — cleaning up")
            try:
                app.mep.stop_recorder()
                app.mep.disconnect()
            except Exception:
                pass
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", _on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
