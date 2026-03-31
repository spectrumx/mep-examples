#!/usr/bin/env python3
"""
mep_gui.py

Tkinter GUI for MEP RFSoC sweep/record control via X11 forwarding.

All hardware communication goes through MEPBus + CaptureController (start_mep_rx.py).
This file is purely presentation: widgets, layout, and callbacks.

Usage:
    ssh -X mep@<jetson> python3 ~/mep-examples/scripts/mep_gui.py

Author: john.marino@colorado.edu
"""

import sys
import os
import shutil
import re
import math
import json
import base64
import struct
import time
import socket
import subprocess
import queue
import threading
import logging
from collections import deque
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from start_mep_rx import (
    MEPBus,
    CaptureController,
    DockerManager,
    get_frequency_list,
    resolve_injection,
    sync_ntp_on_rfsoc,
    get_primary_network_info,
    get_primary_network_info_detailed,
    get_thermal_info_detailed,
    get_jetson_power_mode,
    set_jetson_power_mode_detailed,
    TUNER_INJECTION_SIDE,
    CHANNEL_OPTIONS,
    TUNER_OPTIONS,
    SAMPLE_RATE_OPTIONS,
    RFSOC_CMD_TOPIC,
    RECORDER_CMD_TOPIC,
    RECORDER_STATUS_TOPIC,
    TUNER_CMD_TOPIC,
    TUNER_STATUS_TOPIC,
    RFSOC_STATUS_TOPIC,
    AFE_CMD_TOPIC,
    AFE_STATUS_TOPIC,
    AFE_ANNOUNCE_TOPIC,
    AFE_GNSS_TOPIC,
    AFE_IMU_TOPIC,
    AFE_MAG_TOPIC,
    AFE_HK_TOPIC,
    AFE_REGISTERS_TOPIC,
    MQTT_BROKER,
    MQTT_PORT,
    RECORDER_CONFIG_DIR,
    DOCKER_COMPOSE_DIR,
)

# ===== LAYOUT CONSTANTS ===== #
LEFT_PANEL_WIDTH = 450
ADV_PANEL_WIDTH = 475
DEFAULT_WIN_HEIGHT = 700


# ===== TEXT LOGGING HANDLER ===== #

class _TextHandler(logging.Handler):
    """Logging handler that appends records to a ScrolledText widget."""

    def __init__(self, widget: scrolledtext.ScrolledText):
        super().__init__()
        self.widget = widget
        self._pending = queue.SimpleQueue()
        self._closed = False

    def emit(self, record: logging.LogRecord):
        if self._closed:
            return
        try:
            msg = self.format(record) + "\n"
        except Exception:
            self.handleError(record)
            return
        self._pending.put(msg)

    def flush_pending(self):
        if self._closed:
            return

        messages = []
        while True:
            try:
                messages.append(self._pending.get_nowait())
            except queue.Empty:
                break

        if not messages:
            return

        try:
            self.widget.configure(state="normal")
            for msg in messages:
                self.widget.insert(tk.END, msg)
            self.widget.see(tk.END)
            self.widget.configure(state="disabled")
        except tk.TclError:
            self._closed = True

    def close(self):
        self._closed = True
        super().close()


# ===== MAIN GUI CLASS ===== #

class MEPGui:

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("MEP Control App")
        self.root.resizable(True, True)

        self._sweep_thread: threading.Thread = None
        self._afe_updating = False
        
        # Jetson Health state
        self._jetson_nvpmodel_modes, self._jetson_nvpmodel_default_id = self._read_nvpmodel_config()
        self._jetson_health_busy = False
        self._jetson_nvpmodel_busy = False
        self._jetson_cpu_prev = None
        self._jetson_health_state = {}
        
        # AFE register cache
        self._afe_reg_cache = {}
        
        # Recorder status cache
        self._rec_status_cache = {"state": "—", "file": "—"}
        
        # MQTT streaming
        self._mqtt_messages = deque(maxlen=2000)
        self._mqtt_lock = threading.Lock()
        self._mqtt_rendered_count = 0
        self._mqtt_paused = False
        self._gui_queue = queue.SimpleQueue()
        self._gui_queue_closed = False
        
        # Docker manager (system-level compose orchestration)
        self.docker = DockerManager(DOCKER_COMPOSE_DIR)
        self._docker_suppress_tree_stream = False
        
        # RFSoC monitoring
        self._monitor_rfsoc_tlm = None
        self._monitor_rfsoc_log_key = None
        self._monitor_rfsoc_tlm_lock = threading.Lock()
        self._monitor_rfsoc_tlm_event = threading.Event()
        
        # SPEC tab state
        self._spec_lock = threading.Lock()
        self._spec_topic = ""  # populated from controller after lazy init
        self._spec_stream_enabled = False
        self._spec_latest = None
        self._spec_rows = deque(maxlen=180)
        self._spec_render_pending = False
        self._spec_bins = 256
        self._spec_render_interval_ms = 80
        self._spec_min_ingest_interval_s = 0.03
        self._spec_last_ingest_mono = 0.0
        self._spec_frame_seq = 0
        self._spec_last_rendered_seq = -1
        self._spec_wf_row_counter = 0
        self._spec_wf_row_tags = deque()
        self._spec_color_lut = self._spec_build_color_lut()

        self._vars = {}
        self._build_ui()
        self._setup_logging()

        # ---- MQTT bus (always-on) ----
        self.bus = MEPBus()
        self.capture = None  # created on Start click via _get_or_create_capture

        # ---- Register listeners on bus (always active) ----
        self.bus.on_message(self._on_mqtt_message)
        self.bus.on_connection_state(self._on_mqtt_connection_state)
        self.bus.on_status(RECORDER_STATUS_TOPIC, self._on_recorder_status)
        self.bus.on_status(RFSOC_STATUS_TOPIC, self._on_rfsoc_status)
        self.bus.on_status(TUNER_STATUS_TOPIC, self._on_tuner_status)
        self.bus.on_status(AFE_STATUS_TOPIC, self._on_afe_status)
        self.bus.on_status(AFE_GNSS_TOPIC, self._on_gnss)
        self.bus.on_status(AFE_IMU_TOPIC, self._on_imu)
        self.bus.on_status(AFE_MAG_TOPIC, self._on_mag)
        self.bus.on_status(AFE_HK_TOPIC, self._on_hk)
        self.bus.on_status(AFE_REGISTERS_TOPIC, self._on_afe_registers)
        self.bus.on_status(AFE_ANNOUNCE_TOPIC, self._on_afe_announce)
        self.bus.on_status_pattern(self.bus.spec_topic, self._on_spec_data)

        # Seed UI from cached retained payloads that may have arrived before listeners were registered.
        self._seed_ui_from_cache()
        
        # ---- SPEC topic (pattern from bus) ----
        self._spec_topic = self.bus.spec_topic
        if "spec_topic" in self._vars:
            self._vars["spec_topic"].set(self._spec_topic)

        # ---- Startup sequence with intentional delays ----
        self.root.after(50, self._schedule_housekeeping)
        self.root.after(3000, self._afe_refresh)
        self.root.after(20, self._pump_gui_queue)
        self.root.after(50, self._pump_text_log)

    # ------------------------------------------------------------------ #
    #  MQTT listener callbacks (called from MQTT thread → schedule to GUI)
    # ------------------------------------------------------------------ #

    def _on_mqtt_message(self, topic: str, payload: bytes):
        """Global listener: log every MQTT message in the MQTT tab."""
        self._gui_call(self._mqtt_log_message, topic, payload)

    def _on_mqtt_connection_state(self, status: dict):
        self._gui_call(self._refresh_status_grid)

    def _on_recorder_status(self, data: dict):
        state = data.get("state", "—")
        logging.info(f"Recorder: {state}")
        self._gui_call(self._rec_status_update, data)
        self._gui_call(self._refresh_status_grid)

    def _on_rfsoc_status(self, data: dict):
        f_c = float(data.get("f_c_hz", 0)) / 1e6
        pps = data.get("pps_count", "?")
        state = data.get("state", "?")
        logging.info(f"RFSoC: {state}  f_c={f_c:.2f} MHz  pps={pps}")
        self._gui_call(self._soc_apply, data)
        self._gui_call(self._refresh_status_grid)

    def _on_tuner_status(self, data: dict):
        if "task_name" in data and "value" in data and "state" not in data:
            self._gui_call(self._tun_handle_response, data)
        else:
            state = data.get("state", "?")
            logging.info(f"Tuner: {state}")
        self._gui_call(self._tun_refresh)
        self._gui_call(self._refresh_status_grid)

    def _on_afe_status(self, data: dict):
        state = data.get("state", "?")
        logging.info(f"AFE: {state}")
        self._gui_call(self._refresh_status_grid)

    def _on_gnss(self, data: dict):
        self._gui_call(self._tlm_gps_update, data)

    def _on_imu(self, data: dict):
        self._gui_call(self._tlm_imu_update, data)

    def _on_mag(self, data: dict):
        self._gui_call(self._tlm_mag_update, data)

    def _on_hk(self, data: dict):
        self._gui_call(self._tlm_hk_update, data)

    def _on_afe_registers(self, data: dict):
        self._gui_call(self._afe_apply_state, data)
        self._gui_call(self._refresh_status_grid)

    def _on_afe_announce(self, data: dict):
        """Handle afe/announce retained message — populate dynamic widgets."""
        self._gui_call(self._apply_afe_announce, data)

    def _seed_ui_from_cache(self):
        """Apply already-cached MQTT state to UI once after listener registration."""
        self._refresh_status_grid()

        tlm = self.bus.get_cached_status(RFSOC_STATUS_TOPIC)
        if isinstance(tlm, dict):
            self._soc_apply(tlm)

        rec = self.bus.get_cached_status(RECORDER_STATUS_TOPIC)
        if isinstance(rec, dict):
            self._rec_status_update(rec)

        tun = self.bus.get_cached_status(TUNER_STATUS_TOPIC)
        if isinstance(tun, dict):
            self._tun_refresh()

    def _apply_afe_announce(self, data: dict):
        """Populate all announce-driven widgets on the GUI thread."""
        describe = data.get("describe", {})

        # ---- AFE tab (registers, devices) ---- #
        self._afe_populate_from_announce(data)

        # ---- Polling interval default/range (fallback from hk.rate if polling missing) ---- #
        hk_rate_ref = describe.get("hk", {}).get("reference", {}).get("rate", {})
        if hk_rate_ref and hasattr(self, "_poll_interval_spin"):
            r = hk_rate_ref["range"]
            self._poll_interval_spin.configure(from_=r[0], to=r[1])
            self._vars["poll_interval_s"].set(hk_rate_ref["default"])

        # ---- Time source radio buttons (dynamic from announce) ---- #
        time_ref = describe.get("time", {}).get("reference", {})
        source_opts = time_ref.get("time_source_options", {})
        if source_opts and hasattr(self, "_time_source_frame"):
            for w in self._time_source_frame.winfo_children():
                w.destroy()
            col = 0
            first_val = None
            for _code, label in sorted(source_opts.items()):
                val = label.lower()
                if val == "notset":
                    continue
                if first_val is None:
                    first_val = val
                ttk.Radiobutton(self._time_source_frame, text=label,
                                variable=self._vars["time_source"],
                                value=val,
                                command=self._tlm_apply_time_config).grid(row=0, column=col, sticky="w", padx=2)
                col += 1
            if first_val:
                self._vars["time_source"].set(first_val)

        # ---- Epoch combobox (from announce dict) ---- #
        epoch_opts = time_ref.get("time_epoch_options", {})
        if epoch_opts and hasattr(self, "_epoch_combo"):
            labels = [v.lower() for _k, v in sorted(epoch_opts.items())
                      if v.lower() != "notset"]
            self._epoch_combo["values"] = labels
            if labels:
                self._vars["epoch_mode"].set(labels[0])

        # ---- Logging (rate range) ---- #
        log_ref = describe.get("logging", {}).get("reference", {})
        log_rate_range = log_ref.get("log_rate_range", [])
        if log_rate_range and hasattr(self, "_tlm_log_rate_spin"):
            self._tlm_log_rate_spin.configure(from_=log_rate_range[0], to=log_rate_range[1])
            self._vars["log_rate"].set(log_rate_range[0])

        logging.info("GUI: announce data applied to all dynamic widgets")

    def _on_spec_data(self, topic: str, data: dict):
        """Listener for SPEC messages (radiohound/clients/data/+ pattern)."""
        self._gui_call(self._spec_handle_stream_message, topic, data)

    def _gui_call(self, func, *args, **kwargs):
        if self._gui_queue_closed:
            return
        if threading.current_thread() is threading.main_thread():
            func(*args, **kwargs)
            return
        self._gui_queue.put((func, args, kwargs))

    def _pump_gui_queue(self):
        if self._gui_queue_closed:
            return
        while True:
            try:
                func, args, kwargs = self._gui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                func(*args, **kwargs)
            except tk.TclError:
                self._gui_queue_closed = True
                return
            except Exception:
                logging.exception("GUI dispatch callback failed")
        try:
            self.root.after(20, self._pump_gui_queue)
        except tk.TclError:
            self._gui_queue_closed = True

    # ------------------------------------------------------------------ #
    #  Capture controller management (lazy initialization)                #
    # ------------------------------------------------------------------ #

    def _get_or_create_capture(self, params: dict) -> CaptureController:
        """
        Return a stable CaptureController and update runtime fields in place.

        Recreate only when tuner identity/fixed IF changes. Channel, sample rate,
        injection mode, and capture name are runtime parameters and do not require
        recreating the capture controller.
        """
        needs_new = (
            self.capture is None
            or self.capture.tuner      != params["tuner"]
            or self.capture.adc_if_mhz != params["adc_if_mhz"]
        )

        if needs_new:
            if self.capture is not None:
                logging.info("Configuration changed — recreating capture controller")
                try:
                    self.capture.close()
                except Exception as e:
                    logging.warning(f"Failed to close capture controller: {e}")

            if self._vars.get("sync_ntp") and self._vars["sync_ntp"].get():
                logging.info("Syncing NTP on RFSoC...")
                sync_ntp_on_rfsoc(os.path.dirname(os.path.abspath(__file__)))

            logging.info("Creating capture controller")
            self.capture = CaptureController(self.bus)

            self.capture.configure_sweep(
                channel=params["channel"],
                sample_rate_mhz=params["sample_rate_mhz"],
                tuner=params["tuner"],
                adc_if_mhz=params["adc_if_mhz"],
                injection=params["injection"],
                capture_name=params["capture_name"],
            )
        else:
            self.capture.channel = params["channel"]
            self.capture.sample_rate_mhz = params["sample_rate_mhz"]
            self.capture.capture_name = params["capture_name"]
            self.capture.injection = params["injection"]

        # Keep active config display in sync with current sample-rate selection
        config_name = f"sr{params['sample_rate_mhz']}MHz"
        if "rec_active_config" in self._vars:
            self._vars["rec_active_config"].set(config_name)

        return self.capture

    # ------------------------------------------------------------------ #
    #  TLM tab updaters (run on GUI thread)
    # ------------------------------------------------------------------ #

    def _set_var(self, key: str, val):
        if key in self._vars:
            self._vars[key].set(str(val) if val is not None else "—")



    def _tlm_gps_update(self, data: dict):
        lat = data.get("lat", data.get("latitude", "—"))
        lon = data.get("lon", data.get("longitude", "—"))
        if lat is None:
            lat = "—"
        if lon is None:
            lon = "—"
        self._set_var("tlm_gps_time", data.get("utc_time", data.get("timestamp", "—")))
        self._set_var("tlm_gps_fix", data.get("fix_valid", data.get("fix", "—")))
        self._set_var("tlm_gps_latlon", f"({lat}, {lon})")
        self._set_var("tlm_gps_speed", data.get("speed_knots", data.get("speed", "—")))

    def _tlm_imu_update(self, data: dict):
        acc_x = data.get("acc_x", "—")
        acc_y = data.get("acc_y", "—")
        acc_z = data.get("acc_z", "—")
        gyr_x = data.get("gyr_x", "—")
        gyr_y = data.get("gyr_y", "—")
        gyr_z = data.get("gyr_z", "—")
        self._set_var("tlm_acc_x", acc_x)
        self._set_var("tlm_acc_y", acc_y)
        self._set_var("tlm_acc_z", acc_z)
        self._set_var("tlm_gyr_x", gyr_x)
        self._set_var("tlm_gyr_y", gyr_y)
        self._set_var("tlm_gyr_z", gyr_z)

    def _tlm_mag_update(self, data: dict):
        mag_x = data.get("mag_x", "—")
        mag_y = data.get("mag_y", "—")
        mag_z = data.get("mag_z", "—")
        self._set_var("tlm_mag_x", mag_x)
        self._set_var("tlm_mag_y", mag_y)
        self._set_var("tlm_mag_z", mag_z)

    def _tlm_hk_update(self, data: dict):
        keys = (
            "ocxo_locked", "spi_ok", "mag_ok", "imu_ok", "sw_temp_c", "mag_temp_c",
            "imu_temp_c", "imu_active", "imu_tilt",
        )
        for key in keys:
            self._set_var(f"tlm_hk_{key}", data.get(key, "—"))

    # ------------------------------------------------------------------ #
    #  UI Construction
    # ------------------------------------------------------------------ #

    def _build_ui(self):
        self.root.columnconfigure(0, weight=0)
        self.root.columnconfigure(1, weight=0)
        self.root.rowconfigure(0, weight=1)
        self.root.minsize(LEFT_PANEL_WIDTH, 600)

        # ---- Left pane (fixed width, full height) ---- #
        left = ttk.Frame(self.root, width=LEFT_PANEL_WIDTH, height=DEFAULT_WIN_HEIGHT)
        left.grid(row=0, column=0, sticky="nsew")
        left.grid_propagate(False)
        left.columnconfigure(0, weight=1)
        left.rowconfigure(5, weight=1)

        self._build_tune_section(left, row=0)
        self._build_record_section(left, row=1)
        self._build_updown_section(left, row=2)
        self._build_control_section(left, row=3)

        # ---- Status bar ---- #
        status_frame = ttk.LabelFrame(left, text="Status")
        status_frame.grid(row=4, column=0, padx=10, pady=4, sticky="ew")
        status_frame.columnconfigure(0, weight=1, uniform="status")
        status_frame.columnconfigure(1, weight=1, uniform="status")
        status_frame.columnconfigure(2, weight=1, uniform="status")
        self._status_var = tk.StringVar(value="Idle")
        self._status_cells = {}
        self._status_tooltip = None
        self._status_tooltip_label = None
        self._build_status_grid(status_frame)

        # ---- Log box ---- #
        log_frame = ttk.LabelFrame(left, text="Log")
        log_frame.grid(row=5, column=0, padx=10, pady=6, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)

        self._log_text = scrolledtext.ScrolledText(
            log_frame, height=15, width=80, state="disabled",
            font=("Courier", 9),
        )
        self._log_text.grid(row=0, column=0, padx=5, pady=5, sticky="nsew")
        self._bind_copy_menu(self._log_text)

        # ---- Right pane: Advanced Options ---- #
        self._build_advanced_section(self.root)

    # ---- Section builders ---- #

    def _build_status_grid(self, parent: ttk.Frame):
        specs = [
            ("mqtt", "MQTT", 0, 0),
            ("rfsoc", "RFSoC", 0, 1),
            ("afe", "AFE", 0, 2),
            ("tuner", "Tuner", 1, 0),
            ("recorder", "Recorder", 1, 1),
        ]

        for key, label, row, col in specs:
            cell = ttk.Frame(parent)
            cell.grid(row=row, column=col, padx=2, pady=2, sticky="ew")
            cell.columnconfigure(1, weight=1)

            led = tk.Canvas(cell, width=12, height=12, highlightthickness=0, bd=0)
            led.grid(row=0, column=0, padx=(0, 3), sticky="w")
            oval = led.create_oval(2, 2, 10, 10, fill="#777777", outline="#555555")

            text_var = tk.StringVar(value="—")
            ttk.Label(cell, textvariable=text_var, anchor="w", font=("TkDefaultFont", 8)).grid(
                row=0, column=1, sticky="ew"
            )

            cell.bind("<Enter>", lambda e, k=key: self._status_tooltip_show(k, e))
            cell.bind("<Leave>", lambda e: self._status_tooltip_hide())
            led.bind("<Enter>", lambda e, k=key: self._status_tooltip_show(k, e))
            led.bind("<Leave>", lambda e: self._status_tooltip_hide())

            self._status_cells[key] = {
                "label": label,
                "canvas": led,
                "oval": oval,
                "text_var": text_var,
                "detail": "",
            }

        # Keep 2x3 geometry; leave final slot intentionally empty.
        spacer = ttk.Frame(parent)
        spacer.grid(row=1, column=2, padx=2, pady=2, sticky="ew")

        self._set_status_cell("mqtt", "gray", "unknown")
        self._set_status_cell("rfsoc", "gray", "unknown")
        self._set_status_cell("afe", "gray", "unknown")
        self._set_status_cell("tuner", "gray", "unknown")
        self._set_status_cell("recorder", "gray", "unknown")

    def _status_led_color(self, level: str) -> str:
        return {
            "green": "#26a269",
            "yellow": "#e5a50a",
            "red": "#c01c28",
            "gray": "#777777",
        }.get(level, "#777777")

    def _set_status_cell(self, key: str, level: str, text: str, detail: str = None):
        cell = self._status_cells.get(key)
        if not cell:
            return
        color = self._status_led_color(level)
        cell["canvas"].itemconfigure(cell["oval"], fill=color)
        text = self._compact_status_text(text)
        cell["text_var"].set(f"{cell['label']}: {text}")
        cell["detail"] = str(detail if detail is not None else text)

    def _status_tooltip_show(self, key: str, event):
        cell = self._status_cells.get(key)
        if not cell:
            return
        detail = (cell.get("detail") or "").strip()
        if not detail:
            return
        if self._status_tooltip is None:
            self._status_tooltip = tk.Toplevel(self.root)
            self._status_tooltip.wm_overrideredirect(True)
            self._status_tooltip.attributes("-topmost", True)
            self._status_tooltip_label = ttk.Label(
                self._status_tooltip,
                text="",
                justify="left",
                relief="solid",
                borderwidth=1,
                padding=(4, 2),
                background="#ffffe0",
            )
            self._status_tooltip_label.pack()
        self._status_tooltip_label.configure(text=detail)
        x = event.x_root + 10
        y = event.y_root + 10
        self._status_tooltip.geometry(f"+{x}+{y}")
        self._status_tooltip.deiconify()

    def _status_tooltip_hide(self):
        if self._status_tooltip is not None:
            self._status_tooltip.withdraw()

    def _compact_status_text(self, text: str, max_len: int = 18) -> str:
        s = str(text or "—").strip().replace("\n", " ")
        if len(s) <= max_len:
            return s
        return s[: max_len - 1] + "…"

    def _safe_float(self, value, default=None):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _refresh_status_grid(self):
        conn = self.bus.get_connection_status()
        mqtt_ok = bool(conn.get("connected"))
        if mqtt_ok:
            self._set_status_cell(
                "mqtt",
                "green",
                "connected",
                detail=f"Connected to {conn.get('broker')}:{conn.get('port')}",
            )
        else:
            err = conn.get("last_error") or "offline"
            self._set_status_cell(
                "mqtt",
                "red",
                "offline",
                detail=f"Disconnected from {conn.get('broker')}:{conn.get('port')} ({err})",
            )

        tlm = self.bus.get_cached_status(RFSOC_STATUS_TOPIC)
        if isinstance(tlm, dict):
            state = str(tlm.get("state", "?")).lower()
            f_c_hz = self._safe_float(tlm.get("f_c_hz"), 0.0)
            f_c_mhz = f_c_hz / 1e6
            bad_states = {"error", "offline", "disconnected", "fault"}
            level = "red" if state in bad_states else "green"
            pps = tlm.get("pps_count", "?")
            self._set_status_cell(
                "rfsoc",
                level,
                f"{state} {f_c_mhz:.1f}MHz",
                detail=f"state={state}, f_c={f_c_mhz:.3f} MHz, pps={pps}",
            )
        else:
            level = "yellow" if mqtt_ok else "red"
            self._set_status_cell("rfsoc", level, "no tlm", detail="No RFSoC telemetry in cache")

        afe_status = self.bus.get_cached_status(AFE_STATUS_TOPIC)
        afe_regs = self.bus.get_cached_status(AFE_REGISTERS_TOPIC)
        afe_any = afe_status if isinstance(afe_status, dict) else afe_regs
        if isinstance(afe_any, dict):
            afe_state = str(afe_any.get("state", "online")).lower()
            level = "red" if afe_state in {"error", "offline", "disconnected"} else "green"
            self._set_status_cell("afe", level, afe_state, detail=f"AFE status: {afe_state}")
        else:
            level = "yellow" if mqtt_ok else "red"
            self._set_status_cell("afe", level, "no data", detail="No AFE status/register messages in cache")

        tuner_norm = self.bus.get_tuner_status_normalized()
        selected_tuner = self._vars.get("tuner", tk.StringVar(value="None")).get()
        if isinstance(tuner_norm, dict):
            active_tuner = tuner_norm.get("name") or selected_tuner
            lo_val = self._safe_float(tuner_norm.get("lo_mhz"))
            lo_txt = f"LO={lo_val:.1f}" if lo_val is not None else "LO=—"
            t_state = str(tuner_norm.get("state", "unknown")).lower()
            level = "red" if t_state in {"error", "offline", "disconnected"} else "green"
            self._set_status_cell(
                "tuner",
                level,
                f"{active_tuner} {lo_txt}",
                detail=f"state={t_state}, active={active_tuner}, {lo_txt} MHz",
            )
        elif str(selected_tuner).lower() == "none":
            self._set_status_cell("tuner", "gray", "disabled", detail="Tuner selection is None")
        else:
            level = "yellow" if mqtt_ok else "red"
            self._set_status_cell("tuner", level, "no data", detail="No tuner status in cache")

        rec_status = self.bus.get_cached_status(RECORDER_STATUS_TOPIC)
        if isinstance(rec_status, dict):
            rec_state = str(rec_status.get("state", "unknown")).lower()
            if rec_state in {"error", "offline", "failed"}:
                level = "red"
            elif rec_state in {"starting", "configuring", "unknown"}:
                level = "yellow"
            else:
                level = "green"
            rec_file = rec_status.get("file") or rec_status.get("filename") or rec_status.get("path") or "—"
            self._set_status_cell(
                "recorder",
                level,
                rec_state,
                detail=f"state={rec_state}, file={rec_file}",
            )
        else:
            sweep_active = self._sweep_thread and self._sweep_thread.is_alive()
            if sweep_active:
                self._set_status_cell("recorder", "yellow", "starting", detail="Sweep active, waiting for recorder status")
            else:
                level = "yellow" if mqtt_ok else "red"
                self._set_status_cell("recorder", level, "no data", detail="No recorder status in cache")

    def _build_tune_section(self, parent: ttk.Frame, row: int):
        frame = ttk.LabelFrame(parent, text="Tune")
        frame.grid(row=row, column=0, padx=10, pady=6, sticky="ew")
        frame.columnconfigure(0, weight=1)

        self._tune_notebook = ttk.Notebook(frame)
        self._tune_notebook.grid(row=0, column=0, padx=5, pady=5, sticky="ew")

        single_f = ttk.Frame(self._tune_notebook, padding=8)
        sweep_f  = ttk.Frame(self._tune_notebook, padding=8)
        self._tune_notebook.add(single_f, text="Single")
        self._tune_notebook.add(sweep_f,  text="Sweep")

        # Shared vars used by both tabs.
        self._vars["freq_start"] = tk.StringVar(value="7000")
        if "dwell" not in self._vars:
            self._vars["dwell"] = tk.StringVar(value="5")
        self._vars["single_dwell_enabled"] = tk.BooleanVar(value=False)

        # Single tab
        single_f.columnconfigure(1, weight=1)
        single_f.columnconfigure(2, weight=0)
        ttk.Label(single_f, text="Freq (MHz)").grid(
            row=0, column=0, sticky="w", padx=5, pady=4)
        ttk.Entry(single_f, textvariable=self._vars["freq_start"], width=20).grid(
            row=0, column=1, columnspan=2, sticky="ew", padx=5, pady=4)

        ttk.Label(single_f, text="Dwell (s)").grid(
            row=1, column=0, sticky="w", padx=5, pady=4)
        self._single_dwell_entry = ttk.Entry(
            single_f,
            textvariable=self._vars["dwell"],
            width=14,
            state="disabled",
        )
        self._single_dwell_entry.grid(row=1, column=1, sticky="ew", padx=5, pady=4)
        ttk.Checkbutton(
            single_f,
            text="Enable",
            variable=self._vars["single_dwell_enabled"],
            command=self._toggle_single_dwell,
        ).grid(row=1, column=2, sticky="w", padx=5, pady=4)

        # Sweep tab
        sweep_f.columnconfigure(1, weight=1)
        sweep_fields = [
            ("Start (MHz)", "freq_start", "7000"),
            ("End (MHz)",   "freq_end",   "8000"),
            ("Step (MHz)",  "step",       "10"),
            ("Dwell (s)",   "dwell",      "5"),
        ]
        for r, (label, key, default) in enumerate(sweep_fields):
            if key not in self._vars:
                self._vars[key] = tk.StringVar(value=default)
            ttk.Label(sweep_f, text=label).grid(
                row=r, column=0, sticky="w", padx=5, pady=4)
            ttk.Entry(sweep_f, textvariable=self._vars[key], width=20).grid(
                row=r, column=1, sticky="ew", padx=5, pady=4)

    def _build_record_section(self, parent: ttk.Frame, row: int):
        frame = ttk.LabelFrame(parent, text="Record")
        frame.grid(row=row, column=0, padx=10, pady=6, sticky="ew")
        frame.columnconfigure(1, weight=1)
        frame.columnconfigure(3, weight=1)

        ttk.Label(frame, text="Capture Name").grid(
            row=0, column=0, sticky="w", padx=5, pady=4)
        self._vars["capture_name"] = tk.StringVar(value="")
        ttk.Entry(frame, textvariable=self._vars["capture_name"], width=20).grid(
            row=0, column=1, columnspan=3, sticky="ew", padx=5, pady=4)

        ttk.Label(frame, text="Channel").grid(
            row=1, column=0, sticky="w", padx=5, pady=4)
        self._vars["channel"] = tk.StringVar(value="A")
        ttk.Combobox(
            frame, textvariable=self._vars["channel"],
            values=CHANNEL_OPTIONS, width=16, state="readonly",
        ).grid(row=1, column=1, sticky="ew", padx=5, pady=4)

        ttk.Label(frame, text="Sample Rate (MHz)").grid(
            row=1, column=2, sticky="w", padx=5, pady=4)
        self._vars["sample_rate_mhz"] = tk.StringVar(value="10")
        ttk.Combobox(
            frame, textvariable=self._vars["sample_rate_mhz"],
            values=SAMPLE_RATE_OPTIONS, width=16, state="readonly",
        ).grid(row=1, column=3, sticky="ew", padx=5, pady=4)

    def _build_updown_section(self, parent: ttk.Frame, row: int):
        frame = ttk.LabelFrame(parent, text="Up/Down Convert")
        frame.grid(row=row, column=0, padx=10, pady=6, sticky="ew")
        frame.columnconfigure(1, weight=1)
        frame.columnconfigure(3, weight=1)

        # Row 0: Tuner | RFSoC IF
        ttk.Label(frame, text="Tuner").grid(
            row=0, column=0, sticky="w", padx=5, pady=4)
        self._vars["tuner"] = tk.StringVar(value="None")
        ttk.Combobox(
            frame, textvariable=self._vars["tuner"],
            values=TUNER_OPTIONS, width=16, state="readonly",
        ).grid(row=0, column=1, sticky="ew", padx=5, pady=4)

        ttk.Label(frame, text="RFSoC IF (MHz)").grid(
            row=0, column=2, sticky="w", padx=5, pady=4)
        self._vars["adc_if_mhz"] = tk.StringVar(value="1090")
        self._if_entry = ttk.Entry(
            frame, textvariable=self._vars["adc_if_mhz"], width=16, state="disabled")
        self._if_entry.grid(row=0, column=3, sticky="ew", padx=5, pady=4)

        # Row 1: Injection Mode | Synth LO
        ttk.Label(frame, text="Injection Mode").grid(
            row=1, column=0, sticky="w", padx=5, pady=4)
        self._vars["injection_mode"] = tk.StringVar(value="High")
        ttk.Combobox(
            frame, textvariable=self._vars["injection_mode"],
            values=["High", "Low"], width=16, state="readonly",
        ).grid(row=1, column=1, sticky="ew", padx=5, pady=4)

        ttk.Label(frame, text="Synth LO (MHz)").grid(
            row=1, column=2, sticky="w", padx=5, pady=4)
        self._vars["synth_lo"] = tk.StringVar(value="—")
        ttk.Entry(
            frame, textvariable=self._vars["synth_lo"], width=16, state="disabled",
        ).grid(row=1, column=3, sticky="ew", padx=5, pady=4)

        self._vars["tuner"].trace_add("write", self._on_tuner_change)
        self._vars["freq_start"].trace_add("write", self._update_synth_lo)
        self._vars["adc_if_mhz"].trace_add("write", self._update_synth_lo)
        self._vars["injection_mode"].trace_add("write", self._update_synth_lo)
        self._update_synth_lo()

    def _build_control_section(self, parent: ttk.Frame, row: int):
        frame = ttk.LabelFrame(parent, text="Control")
        frame.grid(row=row, column=0, padx=10, pady=6, sticky="ew")
        frame.columnconfigure(0, weight=1)
        frame.columnconfigure(1, weight=1)
        frame.columnconfigure(2, weight=1)

        ttk.Button(frame, text="Start",
                   command=self._start).grid(row=0, column=0, padx=4, pady=3, sticky="ew")
        ttk.Button(frame, text="Stop",
                   command=self._stop_all).grid(row=0, column=1, padx=4, pady=3, sticky="ew")

        self._adv_btn_text = tk.StringVar(value="\u25b6 Advanced")
        ttk.Button(frame, textvariable=self._adv_btn_text,
                   command=self._toggle_advanced).grid(
            row=0, column=2, padx=4, pady=3, sticky="ew")

    def _build_advanced_section(self, parent: ttk.Frame):
        frame = ttk.LabelFrame(parent, text="Advanced Options", width=ADV_PANEL_WIDTH)
        frame.grid(row=0, column=1, padx=(0, 10), pady=6, sticky="nsew")
        frame.grid_propagate(False)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        self._adv_frame = frame
        frame.grid_remove()

        nb = ttk.Notebook(frame)
        nb.grid(row=0, column=0, padx=5, pady=5, sticky="nsew")
        self._adv_nb = nb

        afe_f  = ttk.Frame(nb, padding=8)
        rec_f  = ttk.Frame(nb, padding=8)
        docker_f = ttk.Frame(nb, padding=8)
        tlm_f  = ttk.Frame(nb, padding=8)
        jh_f = ttk.Frame(nb, padding=8)
        soc_f  = ttk.Frame(nb, padding=8)
        spec_f = ttk.Frame(nb, padding=8)
        tun_f  = ttk.Frame(nb, padding=8)
        mqtt_f = ttk.Frame(nb, padding=8)
        nb.add(afe_f,  text="AFE")
        nb.add(rec_f,  text="REC")
        nb.add(docker_f, text="DOC")
        nb.add(tlm_f,  text="TLM")
        nb.add(jh_f, text="JET")
        nb.add(soc_f,  text="SOC")
        nb.add(spec_f, text="SPEC")
        nb.add(tun_f,  text="TUN")
        nb.add(mqtt_f, text="MQTT")
        nb.bind("<<NotebookTabChanged>>", self._on_adv_tab_changed)

        self._build_afe_tab(afe_f)
        self._build_rec_tab(rec_f)
        self._build_docker_tab(docker_f)
        self._build_tlm_tab(tlm_f)
        self._build_jetson_health_tab(jh_f)
        self._build_soc_tab(soc_f)
        self._build_spec_tab(spec_f)
        self._build_tun_tab(tun_f)
        self._build_mqtt_tab(mqtt_f)

    def _is_adv_tab_selected(self, tab_text: str) -> bool:
        """Return True only when Advanced is visible and the named tab is active."""
        if not hasattr(self, "_adv_frame") or not hasattr(self, "_adv_nb"):
            return False
        if self._adv_nb is None:
            return False
        if not self._adv_frame.winfo_viewable():
            return False
        try:
            current = self._adv_nb.index("current")
            return self._adv_nb.tab(current, "text") == tab_text
        except Exception:
            return False

    def _on_adv_tab_changed(self, event=None):
        if self._is_adv_tab_selected("MQTT"):
            self._mqtt_render_from_buffer()

    # ---- MQTT tab ---- #

    # ---- DOCKER tab ---- #
    # (built by _build_docker_tab below the DOCKER helpers section)

    def _build_mqtt_tab(self, frame: ttk.Frame):
        """MQTT tab: live log of all incoming MQTT messages + manual publish."""
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)   # log row expands

        # Initialize MQTT stream state
        self._mqtt_paused = False
        self._vars["mqtt_stream_state"] = tk.StringVar(value="live")

        # ---- Logging frame (Stream/Pause controls) ---- #
        log_ctl_f = ttk.LabelFrame(frame, text="Logging")
        log_ctl_f.grid(row=0, column=0, padx=4, pady=(4, 2), sticky="ew")
        log_ctl_f.columnconfigure(0, weight=1)
        log_ctl_f.columnconfigure(1, weight=1)
        log_ctl_f.columnconfigure(2, weight=1)

        # Status label for live/paused state
        state_lbl = ttk.Label(
            log_ctl_f,
            textvariable=self._vars["mqtt_stream_state"],
            foreground="grey",
            font=("TkFixedFont", 8),
        )
        state_lbl.grid(row=0, column=0, columnspan=3, sticky="w", padx=5, pady=(4, 1))

        ttk.Button(log_ctl_f, text="Stream",
                   command=self._mqtt_stream_resume).grid(
            row=1, column=0, padx=5, pady=(0, 2), sticky="ew")
        ttk.Button(log_ctl_f, text="Pause",
                   command=self._mqtt_stream_pause).grid(
            row=1, column=1, padx=5, pady=(0, 2), sticky="ew")
        ttk.Button(log_ctl_f, text="Clear",
                   command=self._mqtt_clear_buffer_and_widget).grid(
            row=1, column=2, padx=5, pady=(0, 2), sticky="ew")

        # ---- Logging options ---- #
        self._vars["mqtt_suppress_announce"] = tk.BooleanVar(value=True)
        ttk.Checkbutton(log_ctl_f, text="Suppress announce topics",
                        variable=self._vars["mqtt_suppress_announce"]).grid(
            row=2, column=0, columnspan=3, sticky="w", padx=5, pady=(0, 4))
        self._vars["mqtt_suppress_announce"].trace_add(
            "write", lambda *_: self._mqtt_render_from_buffer())

        # ---- Message log (shorter to leave room for publish panel) ---- #
        self._mqtt_text = scrolledtext.ScrolledText(
            frame, height=12, wrap="word", font=("TkFixedFont", 9),
            background="#f5f5f5", exportselection=False)
        self._mqtt_text.grid(row=1, column=0, sticky="nsew", padx=4, pady=(0, 2))
        self._mqtt_text.bind("<Key>",
            lambda e: None if (e.state & 0x4 and e.keysym in ("c", "C", "a", "A"))
                      else "break")
        self._bind_copy_menu(self._mqtt_text, allow_paste=False)

        # ---- Manual publish ---- #
        pub_f = ttk.LabelFrame(frame, text="Publish Message")
        pub_f.grid(row=2, column=0, sticky="ew", padx=4, pady=(2, 6))
        pub_f.columnconfigure(1, weight=1)

        ttk.Label(pub_f, text="Topic").grid(
            row=0, column=0, sticky="w", padx=5, pady=3)
        self._vars["mqtt_pub_topic"] = tk.StringVar(value="tuner_control/command")
        self._mqtt_pub_topic_entry = ttk.Entry(pub_f, textvariable=self._vars["mqtt_pub_topic"])
        self._mqtt_pub_topic_entry.grid(row=0, column=1, sticky="ew", padx=5, pady=3)
        self._bind_copy_menu(self._mqtt_pub_topic_entry, strvar=self._vars["mqtt_pub_topic"], allow_paste=True)

        ttk.Label(pub_f, text="Payload").grid(
            row=1, column=0, sticky="nw", padx=5, pady=3)
        self._mqtt_pub_payload = tk.Text(pub_f, height=4, wrap="word",
                                         font=("TkFixedFont", 9))
        self._mqtt_pub_payload.grid(row=1, column=1, sticky="ew", padx=5, pady=3)
        self._bind_copy_menu(self._mqtt_pub_payload, allow_paste=True)
        self._mqtt_pub_payload.insert(
            "1.0",
            json.dumps({"arguments": {}, "task_name": "status"}, indent=2),
        )

        ttk.Button(pub_f, text="Publish",
                   command=self._mqtt_publish_manual).grid(
            row=2, column=0, columnspan=2, padx=5, pady=(0, 5), sticky="ew")

        self._add_copyable_note(
            frame,
            "Source: MQTT broker stream via wildcard subscription (#) on localhost:1883",
            row=3,
            wraplength=420,
        )
        self._mqtt_render_from_buffer()

    def _build_spec_tab(self, frame: ttk.Frame):
        """SPEC tab: live FFT line plot and rolling waterfall from MQTT spectrum frames."""
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(2, weight=1)
        frame.rowconfigure(3, weight=1)

        cfg_f = ttk.LabelFrame(frame, text="Stream")
        cfg_f.grid(row=0, column=0, padx=4, pady=(4, 2), sticky="ew")
        cfg_f.columnconfigure(1, weight=1)
        ttk.Label(cfg_f, text="Topic").grid(row=0, column=0, sticky="w", padx=5, pady=3)
        self._vars["spec_topic"] = tk.StringVar(value=self._spec_topic)
        self._spec_topic_entry = ttk.Entry(cfg_f, textvariable=self._vars["spec_topic"], exportselection=False)
        self._spec_topic_entry.grid(
            row=0, column=1, sticky="ew", padx=5, pady=3
        )
        self._bind_copy_menu(self._spec_topic_entry)
        ttk.Button(cfg_f, text="Stream", command=self._spec_stream_on).grid(
            row=0, column=2, padx=(2, 2), pady=3
        )
        ttk.Button(cfg_f, text="Pause", command=self._spec_stream_off).grid(
            row=0, column=3, padx=(2, 5), pady=3
        )
        self._vars["spec_stream_state"] = tk.StringVar(value="paused")
        ttk.Label(cfg_f, textvariable=self._vars["spec_stream_state"], foreground="grey").grid(
            row=1, column=0, columnspan=4, sticky="w", padx=5, pady=(0, 2)
        )

        ctl_f = ttk.LabelFrame(frame, text="Display")
        ctl_f.grid(row=1, column=0, padx=4, pady=(2, 2), sticky="ew")
        ctl_f.columnconfigure(1, weight=1)
        self._vars["spec_autoscale"] = tk.BooleanVar(value=True)
        ttk.Checkbutton(ctl_f, text="Auto color scale", variable=self._vars["spec_autoscale"],
                        command=self._spec_request_render).grid(row=0, column=0, sticky="w", padx=5, pady=2)
        ttk.Label(ctl_f, text="Min").grid(row=0, column=1, sticky="e", padx=(5, 2), pady=2)
        self._vars["spec_vmin"] = tk.DoubleVar(value=-120.0)
        self._spec_min_scale = ttk.Scale(ctl_f, from_=-200.0, to=50.0, variable=self._vars["spec_vmin"],
                                         command=lambda _v: self._spec_request_render())
        self._spec_min_scale.grid(row=0, column=2, sticky="ew", padx=2, pady=2)
        ttk.Label(ctl_f, text="Max").grid(row=0, column=3, sticky="e", padx=(8, 2), pady=2)
        self._vars["spec_vmax"] = tk.DoubleVar(value=0.0)
        self._spec_max_scale = ttk.Scale(ctl_f, from_=-200.0, to=50.0, variable=self._vars["spec_vmax"],
                                         command=lambda _v: self._spec_request_render())
        self._spec_max_scale.grid(row=0, column=4, sticky="ew", padx=2, pady=2)
        ttk.Label(ctl_f, text="FFT Bins").grid(row=1, column=0, sticky="w", padx=5, pady=(0, 2))
        self._vars["spec_bins"] = tk.IntVar(value=self._spec_bins)
        bin_vals = (64, 128, 256, 512, 1024, 2048)
        bin_f = ttk.Frame(ctl_f)
        bin_f.grid(row=1, column=1, columnspan=4, sticky="w", padx=(2, 2), pady=(0, 2))
        self._spec_bin_buttons = {}
        for i, n in enumerate(bin_vals):
            btn = tk.Button(
                bin_f,
                text=str(n),
                width=4,
                relief="raised",
                padx=2,
                pady=1,
                command=lambda v=n: self._spec_apply_bins(v),
            )
            btn.grid(row=0, column=i, padx=(0 if i == 0 else 2, 0), pady=0, sticky="w")
            self._spec_bin_buttons[n] = btn
        ctl_f.columnconfigure(2, weight=1)
        ctl_f.columnconfigure(4, weight=1)
        self._spec_update_bin_button_states()

        self._spec_line_canvas = tk.Canvas(frame, height=170, background="#111111", highlightthickness=1,
                                           highlightbackground="#333333")
        self._spec_line_canvas.grid(row=2, column=0, padx=4, pady=(2, 2), sticky="nsew")
        self._spec_line_canvas.bind("<Motion>", lambda e: self._spec_cursor_update(e, from_waterfall=False))
        self._spec_line_canvas.bind("<Button-1>", lambda e: self._spec_cursor_update(e, from_waterfall=False))

        self._spec_wf_canvas = tk.Canvas(frame, height=220, background="#000000", highlightthickness=1,
                                         highlightbackground="#333333")
        self._spec_wf_canvas.grid(row=3, column=0, padx=4, pady=(2, 2), sticky="nsew")
        self._spec_wf_canvas.bind("<Motion>", lambda e: self._spec_cursor_update(e, from_waterfall=True))
        self._spec_wf_canvas.bind("<Button-1>", lambda e: self._spec_cursor_update(e, from_waterfall=True))

        self._vars["spec_summary"] = tk.StringVar(value="Paused. Press Stream to start SPEC updates")
        ttk.Label(frame, textvariable=self._vars["spec_summary"], foreground="grey",
                  font=("TkDefaultFont", 8)).grid(row=4, column=0, sticky="w", padx=4, pady=(0, 2))
        self._vars["spec_cursor"] = tk.StringVar(value="Cursor: —")
        ttk.Label(frame, textvariable=self._vars["spec_cursor"], foreground="grey",
                  font=("TkDefaultFont", 8)).grid(row=5, column=0, sticky="w", padx=4, pady=(0, 2))

        self._add_copyable_note(
            frame,
            "Source: MQTT topic radiohound/clients/data/<device-id> (base64 float32 bins)",
            row=6,
            wraplength=420,
        )

    def _spec_reset_display(self):
        """Clear canvases and reset display state for a fresh SPEC stream."""
        with self._spec_lock:
            self._spec_rows.clear()
            self._spec_latest = None
            self._spec_last_rendered_seq = -1
            self._spec_wf_row_counter = 0
            self._spec_wf_row_tags.clear()
        if hasattr(self, "_spec_wf_canvas"):
            self._spec_wf_canvas.delete("all")
        if hasattr(self, "_spec_line_canvas"):
            self._spec_line_canvas.delete("all")

    def _spec_stream_on(self):
        self._spec_reset_display()
        self._spec_stream_enabled = True
        if "spec_stream_state" in self._vars:
            self._vars["spec_stream_state"].set("streaming")
        if "spec_summary" in self._vars:
            self._vars["spec_summary"].set(f"Listening on {self._spec_topic}")
        self._spec_request_render()

    def _spec_stream_off(self):
        self._spec_stream_enabled = False
        if "spec_stream_state" in self._vars:
            self._vars["spec_stream_state"].set("paused")
        if "spec_summary" in self._vars:
            self._vars["spec_summary"].set(f"Paused on {self._spec_topic}")

    def _spec_update_bin_button_states(self):
        if not hasattr(self, "_spec_bin_buttons"):
            return
        for n, btn in self._spec_bin_buttons.items():
            if n == self._spec_bins:
                btn.configure(relief="sunken", bd=3)
            else:
                btn.configure(relief="raised", bd=2)

    def _spec_apply_bins(self, n=None):
        allowed = {64, 128, 256, 512, 1024, 2048}
        try:
            if n is None:
                n = int(self._vars.get("spec_bins", tk.IntVar(value=self._spec_bins)).get())
            else:
                n = int(n)
        except Exception:
            logging.error("SPEC: bins must be an integer")
            self._vars["spec_bins"].set(self._spec_bins)
            self._spec_update_bin_button_states()
            return
        if n not in allowed:
            logging.error("SPEC: bins must be one of 64, 128, 256, 512, 1024, 2048")
            self._vars["spec_bins"].set(self._spec_bins)
            self._spec_update_bin_button_states()
            return
        self._vars["spec_bins"].set(n)
        if n == self._spec_bins:
            self._spec_update_bin_button_states()
            return
        self._spec_bins = n
        self._spec_update_bin_button_states()
        self._spec_reset_display()
        if self._spec_stream_enabled and "spec_summary" in self._vars:
            self._vars["spec_summary"].set(f"Listening on {self._spec_topic} (bins={self._spec_bins})")

    def _spec_decode_bins(self, data_b64: str):
        raw = base64.b64decode(data_b64)
        if len(raw) < 4:
            return []
        n = len(raw) // 4
        raw = raw[: n * 4]
        return [x[0] for x in struct.iter_unpack("<f", raw)]

    def _spec_resample(self, values, n_out: int):
        if not values:
            return []
        if len(values) == n_out:
            return list(values)
        if len(values) < 2:
            return [float(values[0])] * n_out
        step = (len(values) - 1) / max(1, n_out - 1)
        out = []
        for i in range(n_out):
            src = i * step
            j = int(src)
            frac = src - j
            if j >= len(values) - 1:
                out.append(float(values[-1]))
            else:
                a = float(values[j])
                b = float(values[j + 1])
                out.append(a + (b - a) * frac)
        return out

    def _spec_build_color_lut(self):
        lut = []
        for idx in range(256):
            t = idx / 255.0
            if t < 0.33:
                u = t / 0.33
                r, g, b = 0, int(255 * u), int(128 + 127 * u)
            elif t < 0.66:
                u = (t - 0.33) / 0.33
                r, g, b = int(255 * u), 255, int(255 * (1.0 - u))
            else:
                u = (t - 0.66) / 0.34
                r, g, b = 255, int(255 * (1.0 - u)), 0
            lut.append((r, g, b))
        return lut

    def _spec_render_waterfall_row(self, row, vmin: float, vmax: float):
        if not row:
            return
        ww = max(10, self._spec_wf_canvas.winfo_width())
        wh = max(10, self._spec_wf_canvas.winfo_height())

        # Shift existing history down by one pixel, then draw newest row at y=0.
        self._spec_wf_canvas.move("specwf", 0, 1)
        row_tag = f"specwf_row_{self._spec_wf_row_counter}"
        self._spec_wf_row_counter += 1

        scale = 255.0 / (vmax - vmin) if vmax > vmin else 0.0
        n = len(row)
        for i, v in enumerate(row):
            x0 = int(i * ww / n)
            x1 = int((i + 1) * ww / n)
            if x1 <= x0:
                x1 = x0 + 1
            if scale <= 0.0:
                idx = 0
            else:
                idx = int((float(v) - vmin) * scale)
                if idx < 0:
                    idx = 0
                elif idx > 255:
                    idx = 255
            r, g, b = self._spec_color_lut[idx]
            self._spec_wf_canvas.create_rectangle(
                x0, 0, x1, 1,
                outline="",
                fill=f"#{r:02x}{g:02x}{b:02x}",
                tags=("specwf", row_tag),
            )

        self._spec_wf_row_tags.append(row_tag)
        while len(self._spec_wf_row_tags) > wh:
            stale = self._spec_wf_row_tags.popleft()
            self._spec_wf_canvas.delete(stale)

    def _spec_get_abs_freq_limits(self, latest: dict, n_bins: int):
        cf = latest.get("center_frequency")
        fmin_off = latest.get("fmin")
        fmax_off = latest.get("fmax")
        if isinstance(cf, (int, float)) and isinstance(fmin_off, (int, float)) and isinstance(fmax_off, (int, float)):
            return float(cf + fmin_off), float(cf + fmax_off)
        return 0.0, float(max(0, n_bins - 1))

    def _spec_fmt_freq(self, hz: float):
        return f"{hz/1e6:.3f} MHz"

    def _spec_amp_to_db(self, amp: float):
        # Convert linear amplitude to dBFS-like scale with floor protection.
        a = abs(float(amp))
        if a < 1e-12:
            a = 1e-12
        return 20.0 * math.log10(a)

    def _spec_fmt_amp(self, amp: float):
        return f"{float(amp):.2f} dB"

    def _spec_bin_to_freq(self, idx: int, n_bins: int, latest: dict):
        f0, f1 = self._spec_get_abs_freq_limits(latest, n_bins)
        if n_bins <= 1:
            return f0
        return f0 + (f1 - f0) * (idx / (n_bins - 1))

    def _spec_cursor_update(self, event, from_waterfall: bool):
        with self._spec_lock:
            latest = dict(self._spec_latest) if isinstance(self._spec_latest, dict) else None
            rows = list(self._spec_rows)
        if not latest or not rows:
            return

        if from_waterfall:
            canvas = self._spec_wf_canvas
        else:
            canvas = self._spec_line_canvas
        w = max(10, canvas.winfo_width())

        row_latest = latest.get("row", [])
        n = len(row_latest)
        if n <= 0:
            return

        x = 0 if event.x < 0 else (w - 1 if event.x >= w else event.x)
        idx = int(round(x * (n - 1) / max(1, w - 1)))
        idx = 0 if idx < 0 else (n - 1 if idx >= n else idx)
        freq_hz = self._spec_bin_to_freq(idx, n, latest)

        if from_waterfall:
            h = max(1, self._spec_wf_canvas.winfo_height())
            y = 0 if event.y < 0 else (h - 1 if event.y >= h else event.y)
            row_offset = int(y)
            if row_offset >= len(rows):
                row_offset = len(rows) - 1
            entry = rows[-1 - row_offset]
            amp = float(entry["row"][idx])
            ts = entry.get("ts", "?")
            label = f"Cursor: f={self._spec_fmt_freq(freq_hz)}  t={ts}  amp={self._spec_fmt_amp(amp)}"
        else:
            amp = float(row_latest[idx])
            ts = latest.get("ts", "?")
            label = f"Cursor: f={self._spec_fmt_freq(freq_hz)}  t={ts}  amp={self._spec_fmt_amp(amp)}"

        if "spec_cursor" in self._vars:
            self._vars["spec_cursor"].set(label)

    def _spec_handle_stream_message(self, topic: str, data: dict):
        if not self._spec_stream_enabled:
            return
        now = time.monotonic()
        if (now - self._spec_last_ingest_mono) < self._spec_min_ingest_interval_s:
            return
        self._spec_last_ingest_mono = now
        try:
            bins = self._spec_decode_bins(data.get("data", ""))
        except Exception as e:
            logging.debug(f"SPEC: decode failed: {e}")
            return
        if not bins:
            return
        row_lin = self._spec_resample(bins, self._spec_bins)
        row = [self._spec_amp_to_db(v) for v in row_lin]
        metadata = data.get("metadata", {}) if isinstance(data.get("metadata"), dict) else {}
        with self._spec_lock:
            self._spec_rows.append({"row": row, "ts": data.get("timestamp")})
            self._spec_frame_seq += 1
            self._spec_latest = {
                "ts": data.get("timestamp"),
                "center_frequency": data.get("center_frequency"),
                "sample_rate": data.get("sample_rate"),
                "n": len(bins),
                "row": row,
                "fmin": metadata.get("fmin"),
                "fmax": metadata.get("fmax"),
                "scan_time": metadata.get("scan_time"),
            }
        if self._is_adv_tab_selected("SPEC"):
            self._gui_call(self._spec_request_render)

    def _spec_request_render(self):
        if not self._spec_stream_enabled:
            return
        if self._spec_render_pending:
            return
        self._spec_render_pending = True
        self.root.after(self._spec_render_interval_ms, self._spec_render)

    def _spec_render(self):
        self._spec_render_pending = False
        if not hasattr(self, "_spec_line_canvas") or not hasattr(self, "_spec_wf_canvas"):
            return
        with self._spec_lock:
            latest = dict(self._spec_latest) if isinstance(self._spec_latest, dict) else None
            rows = list(self._spec_rows)
            seq = self._spec_frame_seq
        if not latest or not rows:
            return
        if seq == self._spec_last_rendered_seq:
            return
        self._spec_last_rendered_seq = seq

        vals = latest.get("row", [])
        if not vals:
            return

        auto = bool(self._vars.get("spec_autoscale", tk.BooleanVar(value=True)).get())
        if auto:
            vmin = min(min(r["row"]) for r in rows)
            vmax = max(max(r["row"]) for r in rows)
        else:
            vmin = float(self._vars.get("spec_vmin", tk.DoubleVar(value=-120.0)).get())
            vmax = float(self._vars.get("spec_vmax", tk.DoubleVar(value=0.0)).get())

        self._spec_line_canvas.delete("all")
        w = max(10, self._spec_line_canvas.winfo_width())
        h = max(10, self._spec_line_canvas.winfo_height())
        n = len(vals)
        points = []
        for i, v in enumerate(vals):
            x = int(i * (w - 1) / max(1, n - 1))
            y_norm = 0.0 if vmax <= vmin else (float(v) - vmin) / (vmax - vmin)
            y_norm = 0.0 if y_norm < 0.0 else (1.0 if y_norm > 1.0 else y_norm)
            y = int((1.0 - y_norm) * (h - 1))
            points.extend((x, y))
        if len(points) >= 4:
            self._spec_line_canvas.create_line(*points, fill="#6ad7ff", width=1)
        self._spec_line_canvas.create_text(6, 6, anchor="nw", fill="#cccccc", text="Live FFT")
        self._spec_line_canvas.create_text(6, h // 2, anchor="w", fill="#aaaaaa", text="Amplitude (dB)")
        self._spec_line_canvas.create_text(6, 20, anchor="nw", fill="#888888", text=f"max {self._spec_fmt_amp(vmax)}")
        self._spec_line_canvas.create_text(6, h - 20, anchor="sw", fill="#888888", text=f"min {self._spec_fmt_amp(vmin)}")
        f0, f1 = self._spec_get_abs_freq_limits(latest, n)
        self._spec_line_canvas.create_text(6, h - 4, anchor="sw", fill="#aaaaaa",
                                           text=self._spec_fmt_freq(f0))
        self._spec_line_canvas.create_text(w - 6, h - 4, anchor="se", fill="#aaaaaa",
                                           text=self._spec_fmt_freq(f1))
        self._spec_line_canvas.create_text(w // 2, h - 4, anchor="s", fill="#aaaaaa",
                                           text="Frequency")

        self._spec_render_waterfall_row(vals, vmin, vmax)
        self._spec_wf_canvas.delete("specwf_label")
        wf_w = max(10, self._spec_wf_canvas.winfo_width())
        wf_h = max(10, self._spec_wf_canvas.winfo_height())
        self._spec_wf_canvas.create_text(6, 6, anchor="nw", fill="#cccccc",
                                         text="Waterfall", tags=("specwf_label",))
        scan_t = latest.get("scan_time")
        if isinstance(scan_t, (int, float)) and scan_t > 0:
            visible_rows = max(1, len(self._spec_wf_row_tags))
            span = scan_t * visible_rows
            self._spec_wf_canvas.create_text(wf_w // 2, 6,
                                             anchor="n", fill="#aaaaaa", text="now",
                                             tags=("specwf_label",))
            self._spec_wf_canvas.create_text(wf_w // 2, wf_h - 4,
                                             anchor="s", fill="#aaaaaa",
                                             text=f"-{span:.1f} s", tags=("specwf_label",))
        self._spec_wf_canvas.create_text(wf_w // 2,
                                         wf_h - 20,
                                         anchor="s", fill="#aaaaaa", text="Time",
                                         tags=("specwf_label",))
        self._spec_wf_canvas.create_text(6, self._spec_wf_canvas.winfo_height() - 18,
                                         anchor="sw", fill="#aaaaaa", text=self._spec_fmt_freq(f0),
                                         tags=("specwf_label",))
        self._spec_wf_canvas.create_text(self._spec_wf_canvas.winfo_width() - 6,
                                         self._spec_wf_canvas.winfo_height() - 18,
                                         anchor="se", fill="#aaaaaa", text=self._spec_fmt_freq(f1),
                                         tags=("specwf_label",))

        if "spec_summary" in self._vars:
            ts = latest.get("ts", "?")
            cf = latest.get("center_frequency", "?")
            sr = latest.get("sample_rate", "?")
            n_in = latest.get("n", "?")
            self._vars["spec_summary"].set(
                f"ts={ts}   cf={cf} Hz   sr={sr} Hz   bins={n_in}"
            )

    def _mqtt_publish_manual(self):
        """Publish an arbitrary MQTT message from the manual publish panel."""
        topic = self._vars["mqtt_pub_topic"].get().strip()
        payload = self._mqtt_pub_payload.get("1.0", "end-1c").strip()
        if not topic:
            logging.error("MQTT publish: topic is empty")
            return
        try:
            self.bus.publish(topic, payload)
            logging.info(f"MQTT published → {topic}")
        except Exception as e:
            logging.error(f"MQTT publish failed: {e}")

    def _mqtt_stream_pause(self):
        """Pause the MQTT stream log."""
        self._mqtt_paused = True
        self._vars["mqtt_stream_state"].set("paused")

    def _mqtt_stream_resume(self):
        """Resume the MQTT stream log."""
        self._mqtt_paused = False
        self._vars["mqtt_stream_state"].set("live")

    def _mqtt_format_entry(self, ts: str, topic: str, payload: bytes) -> str:
        try:
            decoded = payload.decode("utf-8")
            stripped = decoded.strip()
            if stripped:
                try:
                    parsed = json.loads(stripped)
                    pretty = json.dumps(parsed, indent=2, sort_keys=True)
                    body = "\n".join(f"  {ln}" for ln in pretty.splitlines())
                except Exception:
                    body = "\n".join(f"  {ln}" for ln in decoded.rstrip().splitlines())
            else:
                body = "  <empty>"
        except Exception:
            body = f"  {repr(payload)}"
        return f"{ts}  {topic}\n{body}\n"

    def _mqtt_capture_message(self, topic: str, payload: bytes):
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        with self._mqtt_lock:
            self._mqtt_messages.append((ts, topic, payload))

    def _mqtt_flush_buffer_to_widget(self):
        if not hasattr(self, "_mqtt_text"):
            return
        if not self._is_adv_tab_selected("MQTT"):
            return
        # Do not log if stream is paused
        if self._mqtt_paused:
            return

        suppress = bool(self._vars.get("mqtt_suppress_announce", tk.BooleanVar()).get())
        with self._mqtt_lock:
            entries = list(self._mqtt_messages)
            start = min(self._mqtt_rendered_count, len(entries))
            tail = entries[start:]

        if not tail:
            return

        lines = []
        for ts, topic, payload in tail:
            if suppress and "announce" in topic.lower():
                continue
            lines.append(self._mqtt_format_entry(ts, topic, payload))

        if lines:
            self._mqtt_text.insert("end", "".join(lines))
            self._mqtt_text.see("end")

        self._mqtt_rendered_count = len(entries)
        self._mqtt_trim_widget_lines()

    def _mqtt_trim_widget_lines(self, max_lines: int = 500):
        if not hasattr(self, "_mqtt_text"):
            return
        lines = int(self._mqtt_text.index("end-1c").split(".")[0])
        if lines > max_lines:
            self._mqtt_text.delete("1.0", f"{lines - max_lines}.0")

    def _mqtt_render_from_buffer(self):
        if not hasattr(self, "_mqtt_text"):
            return
        self._mqtt_text.delete("1.0", "end")
        self._mqtt_rendered_count = 0
        self._mqtt_flush_buffer_to_widget()

    def _mqtt_clear_buffer_and_widget(self):
        with self._mqtt_lock:
            self._mqtt_messages.clear()
        self._mqtt_rendered_count = 0
        if hasattr(self, "_mqtt_text"):
            self._mqtt_text.delete("1.0", "end")

    def _rec_status_apply_to_ui(self):
        if "rec_status" not in self._vars or "rec_status_file" not in self._vars:
            return
        self._vars["rec_status"].set(self._rec_status_cache.get("state", "—"))
        self._vars["rec_status_file"].set(self._rec_status_cache.get("file", "—"))

    def _mqtt_log_message(self, topic: str, payload: bytes):
        """Compatibility shim for legacy call sites; routes through buffered model."""
        self._mqtt_capture_message(topic, payload)
        self._mqtt_flush_buffer_to_widget()

    def _bind_copy_menu(self, widget, strvar=None, allow_paste=True):
        """Attach right-click Copy (and optionally Paste) + Ctrl+C/V for Entry/Text widgets."""
        menu = tk.Menu(widget, tearoff=0)

        def _popup_menu(e):
            menu.tk_popup(e.x_root, e.y_root)
            return "break"

        def _copy():
            try:
                if isinstance(widget, (tk.Text, scrolledtext.ScrolledText)):
                    ranges = widget.tag_ranges("sel")
                    if len(ranges) < 2:
                        return
                    sel = widget.get(ranges[0], ranges[1])
                elif isinstance(widget, (tk.Entry, ttk.Entry, ttk.Spinbox, tk.Spinbox)):
                    if not widget.selection_present():
                        return
                    sel = widget.selection_get()
                else:
                    sel = widget.selection_get()
            except Exception:
                return
            self.root.clipboard_clear()
            self.root.clipboard_append(sel)

        def _paste():
            try:
                data = self.root.clipboard_get()
            except Exception:
                return
            try:
                if isinstance(widget, (tk.Entry, ttk.Entry, ttk.Spinbox, tk.Spinbox)):
                    widget.insert("insert", data)
                    if strvar is not None:
                        strvar.set(widget.get())
                elif isinstance(widget, (tk.Text, scrolledtext.ScrolledText)):
                    widget.insert("insert", data)
            except Exception:
                return

        menu.add_command(label="Copy", command=_copy)
        if allow_paste:
            menu.add_command(label="Paste", command=_paste)
        widget.bind("<Button-3>", _popup_menu)
        widget.bind("<Control-c>", lambda e: (_copy(), "break")[1])
        if allow_paste:
            widget.bind("<Control-v>", lambda e: (_paste(), "break")[1])

    def _add_copyable_note(self, parent, text: str, row: int, wraplength: int = 420):
        """Render subtle gray footer text that still allows selection/copy."""
        wraplength = min(int(wraplength), 320)
        est_lines = max(1, min(3, (len(text) // max(40, wraplength // 7)) + 1))
        note = tk.Text(
            parent,
            height=est_lines,
            wrap="word",
            font=("TkDefaultFont", 8),
            foreground="grey",
            borderwidth=0,
            highlightthickness=0,
            relief="flat",
            padx=0,
            pady=0,
            background=self.root.cget("bg"),
        )
        note.grid(row=row, column=0, padx=4, pady=(0, 2), sticky="ew")
        note.insert("1.0", text)
        note.configure(state="disabled")
        note.bind(
            "<Key>",
            lambda e: None if (e.state & 0x4 and e.keysym in ("c", "C", "a", "A"))
                      else "break",
        )
        self._bind_copy_menu(note)
        return note

    def _build_jetson_health_tab(self, frame: ttk.Frame):
        """Jetson Health tab: low-cost host readouts (temp/power/cpu/mem)."""
        frame.columnconfigure(0, weight=1)

        def _ro_row(parent, row, label, key, default="-"):
            sv = tk.StringVar(value=default)
            self._vars[key] = sv
            ttk.Label(parent, text=label).grid(
                row=row, column=0, sticky="w", padx=5, pady=2)
            e = ttk.Entry(parent, textvariable=sv, state="readonly", width=26)
            e.grid(row=row, column=1, sticky="ew", padx=5, pady=2)
            self._bind_copy_menu(e, sv)

        top_f = ttk.Frame(frame)
        top_f.grid(row=0, column=0, padx=4, pady=(4, 2), sticky="ew")
        top_f.columnconfigure(0, weight=1)
        top_f.columnconfigure(1, weight=1)

        # ---- System ---- #
        sys_f = ttk.LabelFrame(top_f, text="System")
        sys_f.grid(row=0, column=0, padx=(0, 2), pady=0, sticky="nsew")
        sys_f.columnconfigure(1, weight=1)
        _ro_row(sys_f, 0, "CPU Usage", "jh_cpu_usage")
        _ro_row(sys_f, 1, "Memory", "jh_ram")
        _ro_row(sys_f, 2, "Disk Avail", "jh_disk")

        poll_f = ttk.LabelFrame(top_f, text="Polling")
        poll_f.grid(row=0, column=1, padx=(2, 0), pady=0, sticky="nsew")
        poll_f.columnconfigure(0, weight=1)
        self._vars["jh_auto_refresh"] = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            poll_f,
            text="Auto-refresh (1s, active tab only)",
            variable=self._vars["jh_auto_refresh"],
        ).grid(row=0, column=0, sticky="w", padx=6, pady=(8, 4))
        ttk.Button(poll_f, text="Refresh now", command=self._jetson_health_refresh_now).grid(
            row=1, column=0, sticky="ew", padx=6, pady=(4, 8)
        )

        # ---- Network ---- #
        net_f = ttk.LabelFrame(frame, text="Network")
        net_f.grid(row=1, column=0, padx=4, pady=(2, 2), sticky="ew")
        net_f.columnconfigure(1, weight=1)
        _ro_row(net_f, 0, "Status", "jh_net_status")
        _ro_row(net_f, 1, "MAC", "jh_net_mac")
        _ro_row(net_f, 2, "IP", "jh_net_ip")
        self._vars["jh_net_reason"] = tk.StringVar(value="")
        ttk.Label(
            net_f,
            textvariable=self._vars["jh_net_reason"],
            foreground="grey",
            font=("TkDefaultFont", 8),
            wraplength=380,
            justify="left",
        ).grid(row=3, column=0, columnspan=2, sticky="w", padx=5, pady=(0, 2))

        # ---- Power Mode ---- #
        pm_f = ttk.LabelFrame(frame, text="Power Mode")
        pm_f.grid(row=2, column=0, padx=4, pady=(2, 2), sticky="ew")
        pm_f.columnconfigure(1, weight=1)
        _ro_row(pm_f, 0, "Current", "jh_nvpmodel")

        self._vars["jh_nvpmodel_conf_path"] = tk.StringVar(value="/etc/nvpmodel.conf")
        ttk.Label(pm_f, text="Config File").grid(
            row=1, column=0, sticky="w", padx=5, pady=2)
        conf_e = ttk.Entry(
            pm_f,
            textvariable=self._vars["jh_nvpmodel_conf_path"],
            width=26,
            state="readonly",
        )
        conf_e.grid(row=1, column=1, sticky="ew", padx=5, pady=2)
        self._bind_copy_menu(conf_e, self._vars["jh_nvpmodel_conf_path"])

        mode_f = ttk.Frame(pm_f)
        mode_f.grid(row=2, column=0, columnspan=2, sticky="ew", padx=5, pady=(2, 4))
        mode_f.columnconfigure(1, weight=1)
        ttk.Label(mode_f, text="Select").grid(
            row=0, column=0, sticky="w", pady=2)
        mode_choices = self._jetson_nvpmodel_choice_values()
        initial_choice = self._jetson_nvpmodel_choice_for_id(self._jetson_nvpmodel_default_id)
        if not initial_choice and mode_choices:
            initial_choice = mode_choices[0]
        self._vars["jh_nvpmodel_select"] = tk.StringVar(value=initial_choice or "")
        mode_combo = ttk.Combobox(
            mode_f,
            textvariable=self._vars["jh_nvpmodel_select"],
            values=mode_choices,
            state="readonly",
            width=18,
        )
        mode_combo.grid(row=0, column=1, sticky="ew", padx=5, pady=2)
        self._bind_copy_menu(mode_combo)
        ttk.Button(mode_f, text="Set (required reboot!)",
                   command=self._jetson_health_apply_nvpmodel).grid(
            row=0, column=2, padx=(0, 2), pady=2)

        # ---- Thermal ---- #
        th_f = ttk.LabelFrame(frame, text="Thermal")
        th_f.grid(row=3, column=0, padx=4, pady=(2, 2), sticky="ew")
        th_f.columnconfigure(0, weight=1)
        th_f.columnconfigure(1, weight=1)
        for i in range(1, 7):
            name_key = f"jh_temp_name_{i}"
            val_key = f"jh_temp_val_{i}"
            name_sv = tk.StringVar(value=f"Temp {i}")
            self._vars[name_key] = name_sv
            self._vars[val_key] = tk.StringVar(value="-")
            cell = ttk.Frame(th_f)
            r = (i - 1) // 2
            c = (i - 1) % 2
            cell.grid(row=r, column=c, sticky="ew", padx=5, pady=2)
            cell.columnconfigure(1, weight=1)
            ttk.Label(cell, textvariable=name_sv).grid(row=0, column=0, sticky="w", padx=(0, 4))
            e = ttk.Entry(cell, textvariable=self._vars[val_key], state="readonly", width=16)
            e.grid(row=0, column=1, sticky="ew")
            self._bind_copy_menu(e, self._vars[val_key])
        self._vars["jh_thermal_reason"] = tk.StringVar(value="")
        ttk.Label(
            th_f,
            textvariable=self._vars["jh_thermal_reason"],
            foreground="grey",
            font=("TkDefaultFont", 8),
            wraplength=380,
            justify="left",
        ).grid(row=3, column=0, columnspan=2, sticky="w", padx=5, pady=(0, 2))

        # ---- Power ---- #
        pw_f = ttk.LabelFrame(frame, text="Power")
        pw_f.grid(row=4, column=0, padx=4, pady=(2, 2), sticky="ew")
        pw_f.columnconfigure(1, weight=1)
        for i in range(1, 4):
            name_key = f"jh_pwr_name_{i}"
            val_key = f"jh_pwr_val_{i}"
            name_sv = tk.StringVar(value=self._jetson_health_state.get(name_key, f"Rail {i}"))
            self._vars[name_key] = name_sv
            self._vars[val_key] = tk.StringVar(value=self._jetson_health_state.get(val_key, "-"))
            ttk.Label(pw_f, textvariable=name_sv).grid(
                row=i - 1, column=0, sticky="w", padx=5, pady=2)
            e = ttk.Entry(pw_f, textvariable=self._vars[val_key], state="readonly", width=24)
            e.grid(row=i - 1, column=1, sticky="ew", padx=5, pady=2)
            self._bind_copy_menu(e, self._vars[val_key])

        self._vars["jh_tegrastats_last"] = tk.StringVar(
            value="Last queried: never"
        )
        ttk.Label(
            pw_f,
            textvariable=self._vars["jh_tegrastats_last"],
            foreground="grey",
            font=("TkDefaultFont", 8),
        ).grid(row=3, column=0, columnspan=2, sticky="w", padx=5, pady=(4, 0))

        ttk.Button(pw_f, text="Query tegrastats",
                   command=self._jetson_health_poll_tegrastats).grid(
            row=4, column=0, columnspan=2, padx=5, pady=(4, 2), sticky="ew")

        self._add_copyable_note(
            frame,
            "System rows auto-update from /proc, /sys/class/thermal, nvpmodel, and /etc/nvpmodel.conf. Applying a new mode may require root or passwordless sudo. Power rows update only when Query tegrastats is pressed.",
            row=6,
            wraplength=420,
        )
        self._vars["jh_host_metrics_status"] = tk.StringVar(value="")
        ttk.Label(
            frame,
            textvariable=self._vars["jh_host_metrics_status"],
            foreground="grey",
            font=("TkDefaultFont", 8),
        ).grid(row=5, column=0, sticky="w", padx=4, pady=(0, 2))
        self.root.after(250, self._jetson_health_sync_nvpmodel_choice)

    def _jetson_health_set(self, key: str, value):
        val = "-" if value is None else str(value)
        if key in self._vars:
            self._vars[key].set(val)

    def _jetson_health_apply(self, data: dict):
        if not isinstance(data, dict):
            return
        for key, value in data.items():
            self._jetson_health_set(key, value)

    def _read_nvpmodel_config(self, path: str = "/etc/nvpmodel.conf"):
        modes = []
        default_id = None
        mode_re = re.compile(r"<\s*POWER_MODEL\s+ID\s*=\s*(\d+)\s+NAME\s*=\s*([^>]+?)\s*>", re.IGNORECASE)
        default_re = re.compile(r"<\s*PM_CONFIG\s+DEFAULT\s*=\s*(\d+)\s*>", re.IGNORECASE)

        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    s = line.strip()
                    if not s or s.startswith("#"):
                        continue
                    m = mode_re.search(s)
                    if m:
                        modes.append((m.group(1), m.group(2).strip()))
                        continue
                    m = default_re.search(s)
                    if m:
                        default_id = m.group(1)
        except Exception as e:
            logging.debug(f"Failed to read nvpmodel modes from {path}: {e}")
            return [], None

        return modes, default_id

    def _jetson_nvpmodel_choice_values(self) -> list[str]:
        return [f"{mode_id}: {name}" for mode_id, name in self._jetson_nvpmodel_modes]

    def _jetson_nvpmodel_name_for_id(self, mode_id):
        if mode_id is None:
            return None
        mode_id = str(mode_id)
        for candidate_id, name in self._jetson_nvpmodel_modes:
            if candidate_id == mode_id:
                return name
        return None

    def _jetson_nvpmodel_choice_for_id(self, mode_id):
        name = self._jetson_nvpmodel_name_for_id(mode_id)
        if name is None or mode_id is None:
            return None
        return f"{mode_id}: {name}"

    def _jetson_nvpmodel_id_from_choice(self, choice: str):
        m = re.match(r"\s*(\d+)\s*:", choice or "")
        return m.group(1) if m else None

    def _format_nvpmodel_display(self, mode_id, mode_name):
        if mode_name and mode_id:
            return f"{mode_name} (ID {mode_id})"
        if mode_name:
            return mode_name
        if mode_id:
            known_name = self._jetson_nvpmodel_name_for_id(mode_id)
            if known_name:
                return f"{known_name} (ID {mode_id})"
            return f"ID {mode_id}"
        return None

    def _read_thermal_sysfs(self, limit: int = 6) -> list[tuple[str, float]]:
        out = []
        base = "/sys/class/thermal"
        try:
            entries = sorted(name for name in os.listdir(base) if name.startswith("thermal_zone"))
        except Exception as e:
            logging.debug(f"Failed to list thermal zones from {base}: {e}")
            return out

        for name in entries:
            t_path = os.path.join(base, name, "temp")
            ty_path = os.path.join(base, name, "type")
            try:
                with open(t_path, "r", encoding="utf-8") as f:
                    raw = f.read().strip()
                with open(ty_path, "r", encoding="utf-8") as f:
                    tname = f.read().strip()
                temp_c = float(raw) / 1000.0
                if -100.0 <= temp_c <= 250.0:
                    out.append((tname or name, temp_c))
            except Exception as e:
                logging.debug(f"Failed to read thermal zone {name}: {e}")
                continue

            if len(out) >= limit:
                break

        return out

    def _read_meminfo(self):
        total_kb = None
        avail_kb = None
        try:
            with open("/proc/meminfo", "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        total_kb = int(line.split()[1])
                    elif line.startswith("MemAvailable:"):
                        avail_kb = int(line.split()[1])
        except Exception as e:
            logging.warning(f"Failed to read memory info from /proc/meminfo: {e}")
            return None

        if total_kb is None or avail_kb is None or total_kb <= 0:
            return None
        used_kb = max(total_kb - avail_kb, 0)
        return used_kb, total_kb

    def _read_disk_usage(self, path: str = "/"):
        try:
            usage = shutil.disk_usage(path)
        except Exception as e:
            logging.warning(f"Failed to read disk usage for '{path}': {e}")
            return None
        return usage.free, usage.total

    def _read_cpu_usage(self):
        try:
            with open("/proc/stat", "r", encoding="utf-8") as f:
                first = f.readline().strip()
        except Exception as e:
            logging.warning(f"Failed to read CPU usage from /proc/stat: {e}")
            return None

        parts = first.split()
        if len(parts) < 5 or parts[0] != "cpu":
            return None

        try:
            vals = [int(x) for x in parts[1:]]
        except Exception as e:
            logging.warning(f"Failed to parse CPU stats: {e}")
            return None

        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
        total = sum(vals)

        prev = self._jetson_cpu_prev
        self._jetson_cpu_prev = (total, idle)
        if prev is None:
            return None

        dt = total - prev[0]
        di = idle - prev[1]
        if dt <= 0:
            return None
        return (dt - di) * 100.0 / dt

    def _read_nvpmodel(self):
        result = get_jetson_power_mode()
        if result:
            mode_id, mode_name = result
        else:
            mode_id, mode_name = None, None
        return self._format_nvpmodel_display(mode_id, mode_name)

    def _read_tegrastats_snapshot(self):
        """Return one tegrastats line parsed into rails and temp tokens."""
        try:
            out = subprocess.check_output(
                ["tegrastats", "--interval", "1000"],
                stderr=subprocess.DEVNULL,
                timeout=3.0,
                text=True,
            )
        except subprocess.TimeoutExpired as e:
            out = e.output or ""
        except Exception as e:
            logging.debug(f"Failed to read tegrastats snapshot: {e}")
            return {}, []

        if isinstance(out, bytes):
            out = out.decode(errors="ignore")

        blob = " ".join(s.strip() for s in out.splitlines() if s.strip())
        if not blob:
            return {}, []

        rails = {}
        for m in re.finditer(r"([A-Z0-9_]+)\s+(\d+)mW/(\d+)mW", blob):
            name = m.group(1)
            inst = m.group(2)
            avg = m.group(3)
            rails[name] = f"{inst}/{avg} mW"

        temp_map = {}
        for m in re.finditer(r"([A-Za-z0-9_]+)@(-?\d+(?:\.\d+)?)C", blob):
            temp_map[m.group(1)] = f"{float(m.group(2)):.1f} C"

        temps = list(temp_map.items())

        return rails, temps

    def _jetson_health_collect(self, include_tegrastats: bool = False) -> dict:
        data = {}

        missing_host_paths = []
        if not os.path.exists("/proc/stat"):
            missing_host_paths.append("/proc/stat")
        if not os.path.exists("/proc/meminfo"):
            missing_host_paths.append("/proc/meminfo")
        if missing_host_paths:
            data["jh_host_metrics_status"] = (
                "Host metrics unavailable on this OS: " + ", ".join(missing_host_paths)
            )
        else:
            data["jh_host_metrics_status"] = ""

        mode = self._read_nvpmodel()
        if mode:
            data["jh_nvpmodel"] = mode

        default_choice = self._jetson_nvpmodel_choice_for_id(self._jetson_nvpmodel_default_id)
        if default_choice:
            data["jh_nvpmodel_default"] = default_choice

        cpu = self._read_cpu_usage()
        if cpu is not None:
            data["jh_cpu_usage"] = f"{cpu:.1f}%"

        mem = self._read_meminfo()
        if mem is not None:
            used_kb, total_kb = mem
            used_mb = used_kb / 1024.0
            total_mb = total_kb / 1024.0
            pct = (used_kb * 100.0 / total_kb) if total_kb > 0 else 0.0
            data["jh_ram"] = f"{used_mb:.0f}/{total_mb:.0f} MB ({pct:.1f}%)"

        disk = self._read_disk_usage("/")
        if disk is not None:
            free_b, total_b = disk
            free_gb = free_b / (1024.0 ** 3)
            total_gb = total_b / (1024.0 ** 3)
            used_pct = ((total_b - free_b) * 100.0 / total_b) if total_b > 0 else 0.0
            data["jh_disk"] = f"{free_gb:.1f}/{total_gb:.1f} GiB free ({used_pct:.1f}% used)"

        net_details = get_primary_network_info_detailed()
        data["jh_net_status"] = net_details.get("status", "Offline")
        data["jh_net_mac"] = net_details.get("mac", "-")
        data["jh_net_ip"] = net_details.get("ipv4", "-")
        if net_details.get("error_code"):
            data["jh_net_reason"] = (
                f"Reason: {net_details.get('error_code')}"
                + (f" ({net_details.get('detail')})" if net_details.get("detail") else "")
            )
        else:
            data["jh_net_reason"] = ""

        thermal_details = get_thermal_info_detailed(limit=6)
        temps = [(name, f"{temp:.1f} C") for name, temp in thermal_details.get("temps", [])]
        rails = {}

        if thermal_details.get("error_code") and thermal_details.get("error_code") != "thermal_partial":
            data["jh_thermal_reason"] = (
                f"Reason: {thermal_details.get('error_code')}"
                + (f" ({thermal_details.get('detail')})" if thermal_details.get("detail") else "")
            )
        else:
            data["jh_thermal_reason"] = ""

        if include_tegrastats:
            ts_rails, ts_temps = self._read_tegrastats_snapshot()
            rails = ts_rails
            if not temps and ts_temps:
                temps = ts_temps[:6]

        for i in range(1, 7):
            if i <= len(temps):
                tname, tval = temps[i - 1]
                data[f"jh_temp_name_{i}"] = tname
                data[f"jh_temp_val_{i}"] = tval
            else:
                data[f"jh_temp_name_{i}"] = f"Temp {i}"
                data[f"jh_temp_val_{i}"] = "-"

        if include_tegrastats:
            data["jh_pwr_name_1"] = "VDD_IN"
            data["jh_pwr_val_1"] = rails.get("VDD_IN", "-")
            other = [name for name in sorted(rails.keys()) if name != "VDD_IN"]

            if other:
                data["jh_pwr_name_2"] = other[0]
                data["jh_pwr_val_2"] = rails.get(other[0], "-")
            else:
                data["jh_pwr_name_2"] = "Rail 1"
                data["jh_pwr_val_2"] = "-"

            if len(other) > 1:
                data["jh_pwr_name_3"] = other[1]
                data["jh_pwr_val_3"] = rails.get(other[1], "-")
            else:
                data["jh_pwr_name_3"] = "Rail 2"
                data["jh_pwr_val_3"] = "-"

        return data

    def _jetson_health_poll(self):
        # Keep background load minimal unless the user is actively viewing JET.
        if not self._vars.get("jh_auto_refresh", tk.BooleanVar(value=True)).get():
            return
        if not self._adv_frame.winfo_viewable():
            return
        try:
            current = self._adv_nb.index("current")
            if self._adv_nb.tab(current, "text") != "JET":
                return
        except Exception:
            return

        if self._jetson_health_busy:
            return

        self._jetson_health_busy = True

        def _worker():
            try:
                data = self._jetson_health_collect(False)
                self._gui_call(self._jetson_health_apply, data)
            finally:
                self._gui_call(setattr, self, "_jetson_health_busy", False)

        threading.Thread(target=_worker, daemon=True).start()

    def _jetson_health_refresh_now(self):
        if self._jetson_health_busy:
            return

        def _worker():
            try:
                data = self._jetson_health_collect(False)
                self._gui_call(self._jetson_health_apply, data)
            finally:
                self._gui_call(setattr, self, "_jetson_health_busy", False)

        self._jetson_health_busy = True
        threading.Thread(target=_worker, daemon=True).start()

    def _jetson_health_sync_nvpmodel_choice(self):
        def _worker():
            result = get_jetson_power_mode()
            if result:
                mode_id, mode_name = result
            else:
                mode_id, mode_name = None, None
            display = self._format_nvpmodel_display(mode_id, mode_name)
            choice = self._jetson_nvpmodel_choice_for_id(mode_id)

            def _apply_current():
                if display:
                    self._jetson_health_set("jh_nvpmodel", display)
                if choice and "jh_nvpmodel_select" in self._vars:
                    self._vars["jh_nvpmodel_select"].set(choice)

            self._gui_call(_apply_current)

        threading.Thread(target=_worker, daemon=True).start()

    def _confirm_nvpmodel_reboot(self, mode_id: str, choice: str) -> bool:
        target = choice or f"ID {mode_id}"
        return messagebox.askokcancel(
            title="Apply Power Mode",
            message=(
                f"Applying {target} will immediately reboot this Jetson.\n\n"
                "Press OK to apply the mode and reboot now.\n"
                "Press Cancel to leave the current mode unchanged."
            ),
            parent=self.root,
        )

    def _jetson_health_apply_nvpmodel(self):
        if self._jetson_nvpmodel_busy:
            logging.warning("JET: nvpmodel mode change already in progress")
            return

        choice_var = self._vars.get("jh_nvpmodel_select")
        choice = choice_var.get().strip() if choice_var is not None else ""
        mode_id = self._jetson_nvpmodel_id_from_choice(choice)
        if mode_id is None:
            logging.error("JET: select a valid nvpmodel mode before applying")
            return

        if not self._confirm_nvpmodel_reboot(mode_id, choice):
            logging.info("JET: nvpmodel mode change cancelled")
            return

        self._jetson_nvpmodel_busy = True
        logging.warning("JET: applying nvpmodel mode %s and rebooting now", choice or mode_id)

        def _worker():
            display = self._jetson_nvpmodel_choice_for_id(mode_id) or f"ID {mode_id}"
            try:
                result = set_jetson_power_mode_detailed(mode_id)
                if not result.get("ok"):
                    logging.error(
                        "JET: failed to set nvpmodel mode %s (%s): %s",
                        mode_id,
                        result.get("error_code") or "unknown",
                        result.get("detail") or "no detail",
                    )
                    return

                def _apply_result():
                    if display:
                        self._jetson_health_set("jh_nvpmodel", display)
                    if "jh_nvpmodel_select" in self._vars:
                        self._vars["jh_nvpmodel_select"].set(display)

                self._gui_call(_apply_result)
                logging.info(
                    "JET: nvpmodel accepted %s; reboot in progress. %s",
                    display,
                    result.get("detail") or "",
                )
            except Exception as e:
                logging.error(f"JET: failed to set nvpmodel mode {mode_id}: {e}")
                return
            finally:
                self._gui_call(setattr, self, "_jetson_nvpmodel_busy", False)

        threading.Thread(target=_worker, daemon=True).start()

    def _jetson_health_poll_tegrastats(self):
        """Update power rows on demand using a one-shot tegrastats snapshot."""
        def _worker():
            import datetime

            rails, _temps = self._read_tegrastats_snapshot()
            queried = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            data = {
                "jh_pwr_name_1": "VDD_IN",
                "jh_pwr_val_1": rails.get("VDD_IN", "-"),
                "jh_tegrastats_last": f"Last queried: {queried}",
            }
            other = [name for name in sorted(rails.keys()) if name != "VDD_IN"]

            if other:
                data["jh_pwr_name_2"] = other[0]
                data["jh_pwr_val_2"] = rails.get(other[0], "-")
            else:
                data["jh_pwr_name_2"] = "Rail 1"
                data["jh_pwr_val_2"] = "-"

            if len(other) > 1:
                data["jh_pwr_name_3"] = other[1]
                data["jh_pwr_val_3"] = rails.get(other[1], "-")
            else:
                data["jh_pwr_name_3"] = "Rail 2"
                data["jh_pwr_val_3"] = "-"

            self._gui_call(self._jetson_health_apply, data)

        threading.Thread(target=_worker, daemon=True).start()

    def _build_soc_tab(self, frame: ttk.Frame):
        frame.columnconfigure(0, weight=1)

        def _ro_row(parent, row, label, key, unit=""):
            sv = tk.StringVar(value="—")
            self._vars[key] = sv
            ttk.Label(parent, text=label).grid(
                row=row, column=0, sticky="w", padx=5, pady=2)
            e = ttk.Entry(parent, textvariable=sv, state="readonly", width=18)
            e.grid(row=row, column=1, sticky="ew", padx=5, pady=2)
            self._bind_copy_menu(e, sv)
            if unit:
                ttk.Label(parent, text=unit, foreground="grey").grid(
                    row=row, column=2, sticky="w")

        st_f = ttk.LabelFrame(frame, text="RFSoC Status")
        st_f.grid(row=0, column=0, padx=4, pady=(4, 2), sticky="ew")
        st_f.columnconfigure(1, weight=1)
        _ro_row(st_f, 0, "State",       "soc_state")
        _ro_row(st_f, 1, "Center Frequency Metadata", "soc_fc",  "MHz")
        _ro_row(st_f, 2, "RFSoC IF Tuned Frequency",  "soc_fif", "MHz")
        _ro_row(st_f, 3, "Sample Rate", "soc_fs",       "MHz")
        _ro_row(st_f, 4, "PPS Count",   "soc_pps")
        _ro_row(st_f, 5, "Channels",    "soc_channels")

        cfg_f = ttk.LabelFrame(frame, text="Settings")
        cfg_f.grid(row=1, column=0, padx=4, pady=(2, 4), sticky="ew")
        cfg_f.columnconfigure(0, weight=1)
        self._vars["sync_ntp"] = tk.BooleanVar(value=False)
        ttk.Checkbutton(cfg_f, text="Sync NTP on connect",
                        variable=self._vars["sync_ntp"]).grid(
            row=0, column=0, sticky="w", padx=6, pady=4)

        tst_f = ttk.LabelFrame(frame, text="Manual Control")
        tst_f.grid(row=2, column=0, padx=4, pady=(0, 4), sticky="ew")
        tst_f.columnconfigure(1, weight=1)
        ttk.Label(tst_f, text="IF (MHz)").grid(
            row=0, column=0, sticky="w", padx=5, pady=3)
        self._vars["soc_if_test"] = tk.StringVar(value="1090")
        ttk.Entry(tst_f, textvariable=self._vars["soc_if_test"], width=12).grid(
            row=0, column=1, sticky="ew", padx=5, pady=3)
        ttk.Button(tst_f, text="Set IF", command=self._soc_set_if_test).grid(
            row=0, column=2, padx=5, pady=3, sticky="ew")
        ttk.Label(
            tst_f,
            text="Warning: do not change IF during an active capture/sweep.",
            foreground="grey",
            font=("TkDefaultFont", 8),
        ).grid(row=1, column=0, columnspan=3, sticky="w", padx=5, pady=(0, 3))

        btn_f = ttk.Frame(frame)
        btn_f.grid(row=3, column=0, padx=4, pady=(0, 6), sticky="ew")
        btn_f.columnconfigure(0, weight=1)
        btn_f.columnconfigure(1, weight=1)
        btn_f.columnconfigure(2, weight=1)
        ttk.Button(btn_f, text="Reset RFSoC",
                   command=self._rfsoc_reset).grid(
            row=0, column=0, padx=(0, 2), sticky="ew")
        ttk.Button(btn_f, text="Refresh TLM",
                   command=self._soc_refresh).grid(
            row=0, column=1, padx=(2, 0), sticky="ew")
        ttk.Button(btn_f, text="Arm Next PPS",
                   command=self._soc_arm_next_pps).grid(
            row=0, column=2, padx=(2, 0), sticky="ew")

    # ---- TUN tab ---- #

    def _build_tun_tab(self, frame: ttk.Frame):
        frame.columnconfigure(0, weight=1)

        # Summary
        sum_f = ttk.LabelFrame(frame, text="Tuner Summary")
        sum_f.grid(row=0, column=0, padx=4, pady=(4, 2), sticky="ew")
        sum_f.columnconfigure(1, weight=1)

        ttk.Label(sum_f, text="State").grid(row=0, column=0, sticky="w", padx=5, pady=2)
        self._vars["tun_state"] = tk.StringVar(value="—")
        _s = ttk.Entry(sum_f, textvariable=self._vars["tun_state"],
                       state="readonly", width=18)
        _s.grid(row=0, column=1, sticky="ew", padx=5, pady=2)
        self._bind_copy_menu(_s, self._vars["tun_state"])

        ttk.Label(sum_f, text="Name").grid(row=1, column=0, sticky="w", padx=5, pady=2)
        self._vars["tun_name"] = tk.StringVar(value="—")
        _n = ttk.Entry(sum_f, textvariable=self._vars["tun_name"],
                       state="readonly", width=18)
        _n.grid(row=1, column=1, sticky="ew", padx=5, pady=2)
        self._bind_copy_menu(_n, self._vars["tun_name"])

        ttk.Label(sum_f, text="Lock Status (Valon only)").grid(
            row=2, column=0, sticky="w", padx=5, pady=2)
        self._vars["tun_lock_status"] = tk.StringVar(value="N/A")
        self._tun_lock_entry = ttk.Entry(
            sum_f,
            textvariable=self._vars["tun_lock_status"],
            state="readonly",
            width=18,
        )
        self._tun_lock_entry.grid(row=2, column=1, sticky="ew", padx=5, pady=2)
        self._bind_copy_menu(self._tun_lock_entry, self._vars["tun_lock_status"])
        ttk.Button(sum_f, text="Get", command=self._tun_check_lock).grid(
            row=2, column=2, padx=(2, 5), pady=2, sticky="e")

        # Status dump
        st_f = ttk.LabelFrame(frame, text="Tuner Status (full)")
        st_f.grid(row=1, column=0, padx=4, pady=(2, 2), sticky="ew")
        st_f.columnconfigure(0, weight=1)
        self._tun_status_text = scrolledtext.ScrolledText(
            st_f, height=14, wrap="word", font=("TkFixedFont", 9),
            background="#f5f5f5")
        self._tun_status_text.insert("end", "no status received")
        self._tun_status_text.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)
        self._tun_status_text.bind("<Key>",
            lambda e: None if (e.state & 0x4 and e.keysym in ("c", "C", "a", "A"))
                      else "break")
        self._bind_copy_menu(self._tun_status_text)

        # Controls
        ctrl_f = ttk.LabelFrame(frame, text="Manual Control")
        ctrl_f.grid(row=2, column=0, padx=4, pady=(2, 4), sticky="ew")
        ctrl_f.columnconfigure(1, weight=1)

        ttk.Label(ctrl_f, text="Freq (MHz)").grid(
            row=0, column=0, sticky="w", padx=5, pady=3)
        self._vars["tun_set_freq"] = tk.StringVar(value="")
        ttk.Entry(ctrl_f, textvariable=self._vars["tun_set_freq"],
                  width=10).grid(row=0, column=1, sticky="ew", padx=5, pady=3)
        ttk.Button(ctrl_f, text="Set",
                   command=self._tun_set_freq).grid(row=0, column=2, padx=2, pady=3)
        ttk.Button(ctrl_f, text="Get",
                   command=self._tun_get_freq).grid(row=0, column=3, padx=2, pady=3)

        ttk.Label(ctrl_f, text="Power (dBm)").grid(
            row=1, column=0, sticky="w", padx=5, pady=3)
        self._vars["tun_set_power"] = tk.StringVar(value="")
        _pw_entry = ttk.Entry(ctrl_f, textvariable=self._vars["tun_set_power"], width=10)
        _pw_entry.grid(row=1, column=1, sticky="ew", padx=5, pady=3)
        _pw_set_btn = ttk.Button(ctrl_f, text="Set", command=self._tun_set_power)
        _pw_set_btn.grid(row=1, column=2, padx=2, pady=3)
        _pw_get_btn = ttk.Button(ctrl_f, text="Get", command=self._tun_get_power)
        _pw_get_btn.grid(row=1, column=3, padx=2, pady=3)
        ttk.Label(ctrl_f, text="(Valon only)",
                  foreground="grey", font=("TkDefaultFont", 8)).grid(
            row=2, column=0, columnspan=4, sticky="w", padx=5, pady=(0, 4))

        ttk.Separator(ctrl_f, orient="horizontal").grid(
            row=3, column=0, columnspan=4, sticky="ew", padx=4, pady=2)

        ttk.Button(ctrl_f, text="Init Tuner",
                   command=self._tun_init).grid(
            row=4, column=0, columnspan=4, padx=4, pady=3, sticky="ew")
        ttk.Button(ctrl_f, text="Restart Tuner",
                   command=self._tun_restart).grid(
            row=5, column=0, columnspan=4, padx=4, pady=3, sticky="ew")
        ttk.Button(ctrl_f, text="Publish: Get Status",
                   command=self._tun_send_status).grid(
            row=6, column=0, columnspan=4, padx=4, pady=3, sticky="ew")

        self._valon_only_widgets = [_pw_entry, _pw_set_btn, _pw_get_btn]
        self._vars["tun_name"].trace_add(
            "write", lambda *_: self._tun_update_capability_buttons())
        self._tun_update_capability_buttons()

    # ---- TLM tab ---- #

    def _build_tlm_tab(self, frame: ttk.Frame):
        frame.columnconfigure(0, weight=1)

        def _ro_value(parent, row, col, key, width=24):
            sv = tk.StringVar(value="—")
            self._vars[key] = sv
            e = ttk.Entry(parent, textvariable=sv, state="readonly", width=width)
            e.grid(row=row, column=col, sticky="ew", padx=2, pady=1)
            self._bind_copy_menu(e, sv, allow_paste=False)
            return e

        # ---- GPS ---- #
        gps_f = ttk.LabelFrame(frame, text="GPS")
        gps_f.grid(row=0, column=0, padx=4, pady=(4, 2), sticky="ew")
        gps_f.columnconfigure(1, weight=1)
        
        # Left: Display
        left_f = ttk.Frame(gps_f)
        left_f.grid(row=0, column=0, sticky="n", padx=(5, 10), pady=4)
        left_f.columnconfigure(1, weight=1)
        ttk.Label(left_f, text="UTC Time").grid(row=0, column=0, sticky="w", padx=2, pady=1)
        _ro_value(left_f, 0, 1, "tlm_gps_time", width=18)
        ttk.Label(left_f, text="Fix").grid(row=1, column=0, sticky="w", padx=2, pady=1)
        _ro_value(left_f, 1, 1, "tlm_gps_fix", width=18)
        ttk.Label(left_f, text="Lat/Lon").grid(row=2, column=0, sticky="w", padx=2, pady=1)
        _ro_value(left_f, 2, 1, "tlm_gps_latlon", width=18)
        ttk.Label(left_f, text="Speed (kt)").grid(row=3, column=0, sticky="w", padx=2, pady=1)
        _ro_value(left_f, 3, 1, "tlm_gps_speed", width=18)
        
        # Right: Time controls (GPS has no rate command — display only)
        right_f = ttk.Frame(gps_f)
        right_f.grid(row=0, column=1, sticky="n", padx=(10, 5), pady=4)
        right_f.columnconfigure(1, weight=1)
        
        ttk.Label(right_f, text="Time Source", font=("TkDefaultFont", 9, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 2))
        self._vars["time_source"] = tk.StringVar(value="")
        self._time_source_frame = ttk.Frame(right_f)
        self._time_source_frame.grid(row=1, column=0, columnspan=2, sticky="w")
        # Radio buttons created dynamically by _apply_afe_announce
        
        ttk.Label(right_f, text="Epoch", font=("TkDefaultFont", 9, "bold")).grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(4, 2))
        self._vars["epoch_mode"] = tk.StringVar(value="")
        self._epoch_combo = ttk.Combobox(right_f, textvariable=self._vars["epoch_mode"],
                     values=[], state="readonly",
                     width=10)
        self._epoch_combo.grid(row=3, column=0, columnspan=2, sticky="w")
        self._epoch_combo.bind("<<ComboboxSelected>>", lambda _e: self._tlm_apply_time_config())

        # ---- IMU ---- #
        imu_f = ttk.LabelFrame(frame, text="IMU")
        imu_f.grid(row=1, column=0, padx=4, pady=(2, 2), sticky="ew")
        imu_f.columnconfigure(1, weight=1)
        imu_f.columnconfigure(3, weight=1)

        ttk.Label(imu_f, text="Accelerometer [g]", font=("TkDefaultFont", 9, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", padx=(7, 2), pady=(4, 2))
        ttk.Label(imu_f, text="Gyroscope [deg/sec]", font=("TkDefaultFont", 9, "bold")).grid(
            row=0, column=2, columnspan=2, sticky="w", padx=(16, 2), pady=(4, 2))

        ttk.Label(imu_f, text="X").grid(row=1, column=0, sticky="w", padx=(7, 2), pady=1)
        _ro_value(imu_f, 1, 1, "tlm_acc_x", width=12)
        ttk.Label(imu_f, text="g").grid(row=1, column=2, sticky="w", padx=(2, 10), pady=1)
        ttk.Label(imu_f, text="X").grid(row=1, column=3, sticky="w", padx=(12, 2), pady=1)
        _ro_value(imu_f, 1, 4, "tlm_gyr_x", width=12)
        ttk.Label(imu_f, text="deg/s").grid(row=1, column=5, sticky="w", padx=(2, 6), pady=1)

        ttk.Label(imu_f, text="Y").grid(row=2, column=0, sticky="w", padx=(7, 2), pady=1)
        _ro_value(imu_f, 2, 1, "tlm_acc_y", width=12)
        ttk.Label(imu_f, text="g").grid(row=2, column=2, sticky="w", padx=(2, 10), pady=1)
        ttk.Label(imu_f, text="Y").grid(row=2, column=3, sticky="w", padx=(12, 2), pady=1)
        _ro_value(imu_f, 2, 4, "tlm_gyr_y", width=12)
        ttk.Label(imu_f, text="deg/s").grid(row=2, column=5, sticky="w", padx=(2, 6), pady=1)

        ttk.Label(imu_f, text="Z").grid(row=3, column=0, sticky="w", padx=(7, 2), pady=1)
        _ro_value(imu_f, 3, 1, "tlm_acc_z", width=12)
        ttk.Label(imu_f, text="g").grid(row=3, column=2, sticky="w", padx=(2, 10), pady=1)
        ttk.Label(imu_f, text="Z").grid(row=3, column=3, sticky="w", padx=(12, 2), pady=1)
        _ro_value(imu_f, 3, 4, "tlm_gyr_z", width=12)
        ttk.Label(imu_f, text="deg/s").grid(row=3, column=5, sticky="w", padx=(2, 6), pady=1)

        ttk.Label(imu_f, text="ACC ODR").grid(row=4, column=0, sticky="w", padx=(7, 2), pady=(6, 1))
        _ro_value(imu_f, 4, 1, "tlm_imu_acc_odr", width=12)
        ttk.Label(imu_f, text="GYR ODR").grid(row=4, column=3, sticky="w", padx=(12, 2), pady=(6, 1))
        _ro_value(imu_f, 4, 4, "tlm_imu_gyr_odr", width=12)

        # ---- Magnetometer ---- #
        mag_f = ttk.LabelFrame(frame, text="Magnetometer")
        mag_f.grid(row=2, column=0, padx=4, pady=(2, 2), sticky="ew")
        mag_f.columnconfigure(1, weight=1)

        ttk.Label(mag_f, text="Magnetometer", font=("TkDefaultFont", 9, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", padx=(7, 2), pady=(4, 2))

        ttk.Label(mag_f, text="X").grid(row=1, column=0, sticky="w", padx=(7, 2), pady=1)
        _ro_value(mag_f, 1, 1, "tlm_mag_x", width=14)
        ttk.Label(mag_f, text="Y").grid(row=2, column=0, sticky="w", padx=(7, 2), pady=1)
        _ro_value(mag_f, 2, 1, "tlm_mag_y", width=14)
        ttk.Label(mag_f, text="Z").grid(row=3, column=0, sticky="w", padx=(7, 2), pady=1)
        _ro_value(mag_f, 3, 1, "tlm_mag_z", width=14)

        ttk.Label(mag_f, text="CCR").grid(row=4, column=0, sticky="w", padx=(7, 2), pady=(6, 1))
        _ro_value(mag_f, 4, 1, "tlm_mag_ccr", width=18)
        ttk.Label(mag_f, text="UPDR").grid(row=5, column=0, sticky="w", padx=(7, 2), pady=1)
        _ro_value(mag_f, 5, 1, "tlm_mag_updr", width=18)

        # ---- Housekeeping ---- #
        hk_f = ttk.LabelFrame(frame, text="Housekeeping")
        hk_f.grid(row=3, column=0, padx=4, pady=(2, 2), sticky="ew")
        hk_f.columnconfigure(0, weight=1)
        hk_f.columnconfigure(1, weight=1)
        hk_f.columnconfigure(2, weight=1)

        hk_items = [
            ("ocxo_locked", "ocxo_locked"),
            ("spi_ok", "spi_ok"),
            ("mag_ok", "mag_ok"),
            ("imu_ok", "imu_ok"),
            ("sw_temp_c", "sw_temp_c"),
            ("mag_temp_c", "mag_temp_c"),
            ("imu_temp_c", "imu_temp_c"),
            ("imu_active", "imu_active"),
            ("imu_tilt", "imu_tilt"),
        ]
        for idx, (label, key) in enumerate(hk_items):
            r = idx // 3
            c = idx % 3
            cell = ttk.Frame(hk_f)
            cell.grid(row=r, column=c, sticky="ew", padx=2, pady=1)
            cell.columnconfigure(1, weight=1)
            ttk.Label(cell, text=label).grid(row=0, column=0, sticky="w", padx=1)
            _ro_value(cell, 0, 1, f"tlm_hk_{key}", width=12)

        # ---- Polling ---- #
        poll_f = ttk.LabelFrame(frame, text="Polling")
        poll_f.grid(row=4, column=0, padx=4, pady=(2, 2), sticky="ew")
        poll_f.columnconfigure(1, weight=1)
        ttk.Label(poll_f, text="Interval (s)").grid(row=0, column=0, sticky="w", padx=5, pady=4)
        self._vars["poll_interval_s"] = tk.IntVar(value=1)
        self._poll_interval_spin = ttk.Spinbox(
            poll_f, from_=1, to=3600, increment=1, textvariable=self._vars["poll_interval_s"], width=8
        )
        self._poll_interval_spin.grid(row=0, column=1, sticky="w", padx=5, pady=4)
        ttk.Button(poll_f, text="Get", width=8,
                   command=self._tlm_get_polling_interval).grid(row=0, column=2, padx=2, pady=4)
        ttk.Button(poll_f, text="Set", width=8,
                   command=self._tlm_set_polling_interval).grid(row=0, column=3, padx=(2, 5), pady=4)
        ttk.Button(poll_f, text="Refresh", width=8,
               command=self._tlm_refresh_telemetry).grid(row=0, column=4, padx=(2, 5), pady=4)

        # ---- Logging ---- #
        log_f = ttk.LabelFrame(frame, text="Logging")
        log_f.grid(row=5, column=0, padx=4, pady=(2, 4), sticky="ew")
        log_f.columnconfigure(1, weight=1)

        self._vars["log_enabled"] = tk.StringVar(value="enabled")
        rb_f = ttk.Frame(log_f)
        rb_f.grid(row=0, column=0, columnspan=2, sticky="w", padx=5, pady=(4, 2))
        ttk.Radiobutton(rb_f, text="Enable", variable=self._vars["log_enabled"], value="enabled").pack(side="left", padx=(0, 8))
        ttk.Radiobutton(rb_f, text="Disable", variable=self._vars["log_enabled"], value="disabled").pack(side="left")

        ttk.Label(log_f, text="Log Path").grid(row=1, column=0, sticky="w", padx=5, pady=2)
        self._vars["log_path"] = tk.StringVar(value="/var/log/mep/")
        ttk.Entry(log_f, textvariable=self._vars["log_path"]).grid(row=1, column=1, sticky="ew", padx=5, pady=2)

        ttk.Label(log_f, text="Log Interval (s)").grid(row=2, column=0, sticky="w", padx=5, pady=2)
        self._vars["log_rate"] = tk.IntVar(value=1)
        self._tlm_log_rate_spin = ttk.Spinbox(
            log_f, from_=1, to=3600, increment=1, textvariable=self._vars["log_rate"], width=10
        )
        self._tlm_log_rate_spin.grid(row=2, column=1, sticky="w", padx=5, pady=2)
        ttk.Button(log_f, text="Get", width=8,
                   command=self._tlm_get_logging).grid(row=2, column=2, padx=2, pady=2)
        ttk.Button(log_f, text="Set", width=8,
                   command=self._tlm_set_logging).grid(row=2, column=3, padx=(2, 5), pady=2)

    # ---- CONFIG tab (Logging) ---- #

    def _build_config_tab(self, frame: ttk.Frame):
        frame.columnconfigure(0, weight=1)

        logging_f = ttk.LabelFrame(frame, text="Logging")
        logging_f.grid(row=0, column=0, padx=4, pady=(4, 2), sticky="ew")
        logging_f.columnconfigure(1, weight=1)

        # Enable/Disable
        self._vars["log_enabled"] = tk.BooleanVar(value=False)
        ttk.Checkbutton(logging_f, text="Enable Logging",
                        variable=self._vars["log_enabled"]).grid(
            row=0, column=0, columnspan=2, sticky="w", padx=5, pady=5)

        # Log path
        ttk.Label(logging_f, text="Log Path").grid(
            row=1, column=0, sticky="w", padx=5, pady=3)
        self._vars["log_path"] = tk.StringVar(value="/var/log/mep/")
        ttk.Entry(logging_f, textvariable=self._vars["log_path"]).grid(
            row=1, column=1, sticky="ew", padx=5, pady=3)

        # Log rate (interval in seconds)
        ttk.Label(logging_f, text="Log Rate (s)").grid(
            row=2, column=0, sticky="w", padx=5, pady=3)
        self._vars["log_rate"] = tk.IntVar(value=0)
        self._log_rate_spin = ttk.Spinbox(logging_f, from_=0, to=0, increment=1,
                    textvariable=self._vars["log_rate"],
                    width=12)
        self._log_rate_spin.grid(row=2, column=1, sticky="w", padx=5, pady=3)

        # Service log mode (populated dynamically from announce)
        ttk.Label(logging_f, text="Service Log Mode").grid(
            row=3, column=0, sticky="w", padx=5, pady=3)
        self._vars["service_log_mode"] = tk.StringVar(value="")
        self._log_mode_combo = ttk.Combobox(logging_f, textvariable=self._vars["service_log_mode"],
                     values=[],
                     state="readonly", width=12)
        self._log_mode_combo.grid(row=3, column=1, sticky="w", padx=5, pady=3)

        # Buttons
        btn_f = ttk.Frame(logging_f)
        btn_f.grid(row=4, column=0, columnspan=2, sticky="ew", padx=5, pady=5)
        btn_f.columnconfigure(0, weight=1)
        btn_f.columnconfigure(1, weight=1)
        btn_f.columnconfigure(2, weight=1)
        ttk.Button(btn_f, text="Enable",
                   command=self._log_enable).pack(side="left", padx=2)
        ttk.Button(btn_f, text="Disable",
                   command=self._log_disable).pack(side="left", padx=2)
        ttk.Button(btn_f, text="Apply Settings",
                   command=self._log_apply_settings).pack(side="left", padx=2)

        # Status display
        status_f = ttk.LabelFrame(frame, text="Status")
        status_f.grid(row=1, column=0, padx=4, pady=(2, 4), sticky="ew")
        status_f.columnconfigure(0, weight=1)
        
        self._vars["log_status"] = tk.StringVar(value="—")
        ttk.Entry(status_f, textvariable=self._vars["log_status"],
                  state="readonly").grid(row=0, column=0, sticky="ew", padx=5, pady=5)

    # ---- AFE tab ---- #

    def _build_afe_tab(self, frame: ttk.Frame):
        frame.columnconfigure(0, weight=1)
        self._afe_frame = frame
        self._afe_placeholder = ttk.Label(frame, text="Waiting for AFE service announce…",
                                          foreground="grey")
        self._afe_placeholder.grid(row=0, column=0, padx=20, pady=20)

        # Bottom buttons (always present)
        btn_f = ttk.Frame(frame)
        btn_f.grid(row=99, column=0, padx=4, pady=(0, 6), sticky="ew")
        btn_f.columnconfigure(0, weight=1)
        btn_f.columnconfigure(1, weight=1)
        ttk.Button(btn_f, text="Refresh State",
                   command=self._afe_refresh).grid(
            row=0, column=0, padx=(0, 2), sticky="ew")
        ttk.Button(btn_f, text="Reset to Defaults",
                   command=self._afe_reset_defaults).grid(
            row=0, column=1, padx=(2, 0), sticky="ew")

    def _afe_populate_from_announce(self, announce: dict):
        """Build AFE register widgets from afe/announce describe data."""
        describe = announce.get("describe", {})
        reg_ref = describe.get("registers", {}).get("reference", {})
        reg_pins = reg_ref.get("register_pins", {})
        devices = reg_ref.get("devices", [])
        rx_devices = reg_ref.get("rx_devices", [])
        atten_range = reg_ref.get("attenuation_db_range", [0, 31])
        tx_devices = [d for d in devices if d.startswith("tx")]

        # Remove placeholder
        if hasattr(self, "_afe_placeholder") and self._afe_placeholder.winfo_exists():
            self._afe_placeholder.destroy()

        # Destroy old dynamic content if repopulating
        for attr in ("_afe_main_f", "_afe_rx_outer", "_afe_tx_outer"):
            w = getattr(self, attr, None)
            if w is not None and w.winfo_exists():
                w.destroy()

        frame = self._afe_frame

        # Cache the register_pins for apply_state and reset_defaults
        self._afe_reg_pins = reg_pins
        self._afe_atten_range = atten_range

        # ---- Main Block (misc) ---- #
        misc_pins = reg_pins.get("misc", [])
        if misc_pins:
            self._afe_main_f = ttk.LabelFrame(frame, text="Main Block (misc)")
            self._afe_main_f.grid(row=0, column=0, padx=4, pady=(4, 2), sticky="ew")
            self._afe_main_f.columnconfigure(0, weight=1)

            row_i = 0
            for reg in misc_pins:
                name = reg["name"]
                if name.startswith("NOT_USED"):
                    continue

                label = reg.get("label", name.replace("_", " ").title())

                if name == "GNSS_ANT_SEL":
                    ant_f = ttk.Frame(self._afe_main_f)
                    ant_f.grid(row=row_i, column=0, sticky="w", padx=6, pady=(4, 2))
                    ttk.Label(ant_f, text=f"{label}:").grid(row=0, column=0, sticky="w")
                    self._vars["afe_misc_GNSS_ANT_SEL"] = tk.StringVar(
                        value="external" if reg["default"] == 0 else "internal")

                    def _ant_cb(*_):
                        if self._afe_updating:
                            return
                        v = self._vars["afe_misc_GNSS_ANT_SEL"].get()
                        self.bus.afe_set_register("misc", "GNSS_ANT_SEL", 1 if v == "internal" else 0)

                    self._vars["afe_misc_GNSS_ANT_SEL"].trace_add("write", _ant_cb)
                    ttk.Radiobutton(ant_f, text=reg.get("1", "Internal"),
                                    variable=self._vars["afe_misc_GNSS_ANT_SEL"],
                                    value="internal").grid(row=0, column=1, padx=6)
                    ttk.Radiobutton(ant_f, text=reg.get("0", "External"),
                                    variable=self._vars["afe_misc_GNSS_ANT_SEL"],
                                    value="external").grid(row=0, column=2, padx=6)
                    row_i += 1
                    continue

                key = f"afe_misc_{name}"
                self._vars[key] = tk.BooleanVar(value=bool(reg["default"]))

                def _main_cb(name=name, key=key):
                    if self._afe_updating:
                        return
                    v = self._vars[key].get()
                    self.bus.afe_set_register("misc", name, int(v))

                self._vars[key].trace_add("write", lambda *_, cb=_main_cb: cb())
                ttk.Checkbutton(self._afe_main_f, text=label,
                                variable=self._vars[key]).grid(
                    row=row_i, column=0, sticky="w", padx=6, pady=1)
                row_i += 1

        # ---- RX Channels ---- #
        if rx_devices:
            self._afe_rx_outer = ttk.LabelFrame(frame, text="RX Channels")
            self._afe_rx_outer.grid(row=1, column=0, padx=4, pady=(2, 4), sticky="ew")
            self._afe_rx_outer.columnconfigure(0, weight=1)

            rx_nb = ttk.Notebook(self._afe_rx_outer)
            rx_nb.grid(row=0, column=0, padx=4, pady=4, sticky="ew")

            for device in rx_devices:
                pins = reg_pins.get(device, [])
                ch_label = device.upper()
                ch_f = ttk.Frame(rx_nb, padding=6)
                ch_f.columnconfigure(1, weight=1)
                rx_nb.add(ch_f, text=ch_label)

                row_i = 0
                for reg in pins:
                    name = reg["name"]
                    if name.startswith("NOT_USED"):
                        continue
                    if name.startswith("ATTEN_"):
                        continue

                    key = f"afe_{device}_{name}"
                    label = reg.get("label", name.replace("_", " ").title())
                    self._vars[key] = tk.BooleanVar(value=bool(reg["default"]))

                    def _rx_cb(device=device, name=name, key=key):
                        if self._afe_updating:
                            return
                        v = self._vars[key].get()
                        self.bus.afe_set_register(device, name, int(v))

                    self._vars[key].trace_add("write", lambda *_, cb=_rx_cb: cb())
                    ttk.Checkbutton(ch_f, text=label,
                                    variable=self._vars[key]).grid(
                        row=row_i, column=0, columnspan=2, sticky="w", pady=1)
                    row_i += 1

                # Attenuation spinbox
                ttk.Separator(ch_f, orient="horizontal").grid(
                    row=row_i, column=0, columnspan=2, sticky="ew", pady=4)
                row_i += 1
                ttk.Label(ch_f, text="Attenuation (dB)").grid(
                    row=row_i, column=0, sticky="w")
                atten_key = f"afe_{device}_atten"
                self._vars[atten_key] = tk.IntVar(value=0)

                def _atten_cb(device=device, key=atten_key):
                    if self._afe_updating:
                        return
                    db = int(self._vars[key].get())
                    self.bus.afe_set_attenuation(device, db)

                self._vars[atten_key].trace_add("write", lambda *_, cb=_atten_cb: cb())
                ttk.Spinbox(ch_f, from_=atten_range[0], to=atten_range[1], increment=1,
                            textvariable=self._vars[atten_key],
                            width=6, state="readonly").grid(
                    row=row_i, column=1, sticky="w", padx=5)
                ttk.Label(ch_f, text="dB", foreground="grey").grid(
                    row=row_i, column=2, sticky="w")

        # ---- TX Channels ---- #
        if tx_devices:
            self._afe_tx_outer = ttk.LabelFrame(frame, text="TX Channels")
            self._afe_tx_outer.grid(row=2, column=0, padx=4, pady=(2, 4), sticky="ew")
            self._afe_tx_outer.columnconfigure(0, weight=1)

            tx_nb = ttk.Notebook(self._afe_tx_outer)
            tx_nb.grid(row=0, column=0, padx=4, pady=4, sticky="ew")

            for device in tx_devices:
                pins = reg_pins.get(device, [])
                ch_label = device.upper()
                ch_f = ttk.Frame(tx_nb, padding=6)
                ch_f.columnconfigure(0, weight=1)
                tx_nb.add(ch_f, text=ch_label)

                row_i = 0
                for reg in pins:
                    name = reg["name"]
                    if name.startswith("NOT_USED"):
                        continue

                    key = f"afe_{device}_{name}"
                    label = reg.get("label", name.replace("_", " ").title())
                    self._vars[key] = tk.BooleanVar(value=bool(reg["default"]))

                    def _tx_cb(device=device, name=name, key=key):
                        if self._afe_updating:
                            return
                        v = self._vars[key].get()
                        self.bus.afe_set_register(device, name, int(v))

                    self._vars[key].trace_add("write", lambda *_, cb=_tx_cb: cb())
                    ttk.Checkbutton(ch_f, text=label,
                                    variable=self._vars[key]).grid(
                        row=row_i, column=0, sticky="w", pady=1)
                    row_i += 1

        logging.info("AFE tab populated from announce data")

    # ---- REC tab ---- #

    def _build_rec_tab(self, frame: ttk.Frame):
        frame.columnconfigure(0, weight=1)

        # Status
        status_frame = ttk.LabelFrame(frame, text="Recorder Status")
        status_frame.grid(row=0, column=0, padx=4, pady=(4, 2), sticky="ew")
        status_frame.columnconfigure(1, weight=1)

        ttk.Label(status_frame, text="State").grid(
            row=0, column=0, sticky="w", padx=5, pady=2)
        self._vars["rec_status"] = tk.StringVar(value="—")
        _se = ttk.Entry(status_frame, textvariable=self._vars["rec_status"],
                        state="readonly", width=18)
        _se.grid(row=0, column=1, sticky="ew", padx=5, pady=2)
        self._bind_copy_menu(_se, self._vars["rec_status"])

        ttk.Label(status_frame, text="File").grid(
            row=1, column=0, sticky="w", padx=5, pady=2)
        self._vars["rec_status_file"] = tk.StringVar(value="—")
        _fe = ttk.Entry(status_frame, textvariable=self._vars["rec_status_file"],
                        state="readonly", width=18)
        _fe.grid(row=1, column=1, sticky="ew", padx=5, pady=2)
        self._bind_copy_menu(_fe, self._vars["rec_status_file"])

        # Spectrograms
        sg_frame = ttk.LabelFrame(frame, text="Spectrograms")
        sg_frame.grid(row=1, column=0, padx=4, pady=6, sticky="ew")
        sg_frame.columnconfigure(1, weight=1)

        self._vars["sg_compute"] = tk.BooleanVar(value=True)
        self._vars["sg_mqtt"]    = tk.BooleanVar(value=True)
        self._vars["sg_output"]  = tk.BooleanVar(value=True)
        ttk.Checkbutton(sg_frame, text="Compute",
                        variable=self._vars["sg_compute"]).grid(
            row=0, column=0, columnspan=2, sticky="w", padx=5, pady=2)
        ttk.Checkbutton(sg_frame, text="Stream via MQTT",
                        variable=self._vars["sg_mqtt"]).grid(
            row=1, column=0, columnspan=2, sticky="w", padx=5, pady=2)
        ttk.Checkbutton(sg_frame, text="Save to disk",
                        variable=self._vars["sg_output"]).grid(
            row=2, column=0, columnspan=2, sticky="w", padx=5, pady=2)

        ttk.Separator(sg_frame, orient="horizontal").grid(
            row=3, column=0, columnspan=2, sticky="ew", padx=5, pady=4)

        ttk.Label(sg_frame, text="Reduce Op").grid(
            row=4, column=0, sticky="w", padx=5, pady=3)
        self._vars["sg_reduce_op"] = tk.StringVar(value="max")
        ttk.Combobox(
            sg_frame, textvariable=self._vars["sg_reduce_op"],
            values=["max", "min", "mean"], width=10, state="readonly",
        ).grid(row=4, column=1, sticky="ew", padx=5, pady=3)

        ttk.Label(sg_frame, text="SNR Min (dB)").grid(
            row=5, column=0, sticky="w", padx=5, pady=3)
        self._vars["sg_snr_min"] = tk.StringVar(value="-5")
        ttk.Entry(sg_frame, textvariable=self._vars["sg_snr_min"], width=8).grid(
            row=5, column=1, sticky="ew", padx=5, pady=3)

        ttk.Label(sg_frame, text="SNR Max (dB)").grid(
            row=6, column=0, sticky="w", padx=5, pady=3)
        self._vars["sg_snr_max"] = tk.StringVar(value="20")
        ttk.Entry(sg_frame, textvariable=self._vars["sg_snr_max"], width=8).grid(
            row=6, column=1, sticky="ew", padx=5, pady=3)

        ttk.Label(sg_frame, text="Spectra per Image").grid(
            row=7, column=0, sticky="w", padx=5, pady=3)
        self._vars["sg_spectra_per_output"] = tk.StringVar(value="600")
        ttk.Entry(sg_frame, textvariable=self._vars["sg_spectra_per_output"], width=8).grid(
            row=7, column=1, sticky="ew", padx=5, pady=3)

        ttk.Button(sg_frame, text="Send Now",
                   command=self._apply_rec_spectrogram).grid(
            row=8, column=0, columnspan=2, padx=4, pady=6, sticky="ew")

        # DRF Output
        drf_frame = ttk.LabelFrame(frame, text="DRF Output")
        drf_frame.grid(row=2, column=0, padx=4, pady=6, sticky="ew")
        drf_frame.columnconfigure(1, weight=1)

        ttk.Label(drf_frame, text="Batch Size").grid(
            row=0, column=0, sticky="w", padx=5, pady=3)
        self._vars["rec_batch_size"] = tk.StringVar(value="625")
        ttk.Entry(drf_frame, textvariable=self._vars["rec_batch_size"], width=8).grid(
            row=0, column=1, sticky="ew", padx=5, pady=3)

        ttk.Button(drf_frame, text="Send Now",
                   command=self._apply_rec_drf).grid(
            row=1, column=0, columnspan=2, padx=4, pady=6, sticky="ew")

        # Config Load
        cfg_frame = ttk.LabelFrame(frame, text="Config")
        cfg_frame.grid(row=3, column=0, padx=4, pady=6, sticky="ew")
        cfg_frame.columnconfigure(1, weight=1)

        ttk.Label(cfg_frame, text="Active Config").grid(
            row=0, column=0, sticky="w", padx=5, pady=3)
        self._vars["rec_active_config"] = tk.StringVar(value="—")
        ttk.Entry(cfg_frame, textvariable=self._vars["rec_active_config"],
                  state="readonly", width=12).grid(
            row=0, column=1, sticky="ew", padx=5, pady=3)
        ttk.Button(cfg_frame, text="Reload Config",
                   command=self._rec_reload_config).grid(
            row=1, column=0, columnspan=2, padx=4, pady=6, sticky="ew")

    # ------------------------------------------------------------------ #
    #  AFE helpers
    # ------------------------------------------------------------------ #

    def _afe_refresh(self):
        """Request current register state from AFE via MQTT."""
        self.bus.afe_get_registers("all")
        logging.info("AFE: register refresh requested")

    def _afe_apply_state(self, data: dict):
        """Update all AFE widgets from register data arriving on AFE_REGISTERS_TOPIC."""
        params = data.get("params", {})
        if isinstance(params, dict):
            imu_params = params.get("imu", {})
            if isinstance(imu_params, dict):
                self._set_var("tlm_imu_acc_odr", imu_params.get("acc_odr", "—"))
                self._set_var("tlm_imu_gyr_odr", imu_params.get("gyr_odr", "—"))

            mag_params = params.get("magnetometer", {})
            if isinstance(mag_params, dict):
                self._set_var("tlm_mag_ccr", mag_params.get("ccr", "—"))
                self._set_var("tlm_mag_updr", mag_params.get("updr", "—"))

        reg_pins = getattr(self, "_afe_reg_pins", None)
        if not reg_pins:
            logging.warning("AFE: register data arrived before announce — skipping")
            return

        regs = data.get("registers_named", data)
        if not isinstance(regs, dict):
            return

        self._afe_updating = True
        try:
            for device, pins in reg_pins.items():
                dev_regs = regs.get(device, {})
                if not isinstance(dev_regs, dict):
                    continue

                for reg in pins:
                    name = reg.get("name")
                    if not name or name.startswith("NOT_USED") or name.startswith("ATTEN_"):
                        continue
                    key = f"afe_{device}_{name}"
                    raw_val = dev_regs.get(name)
                    if isinstance(raw_val, dict):
                        raw_val = raw_val.get("value")

                    if name == "GNSS_ANT_SEL":
                        if raw_val is not None and key in self._vars:
                            try:
                                self._vars[key].set(
                                    "internal" if int(raw_val) == 1 else "external"
                                )
                            except (TypeError, ValueError):
                                pass
                        continue

                    if raw_val is None or key not in self._vars:
                        continue

                    try:
                        self._vars[key].set(bool(int(raw_val)))
                    except (TypeError, ValueError):
                        pass

                atten_key = f"afe_{device}_atten"
                if "ATTENUATION_DB" in dev_regs and atten_key in self._vars:
                    atten_raw = dev_regs.get("ATTENUATION_DB")
                    if isinstance(atten_raw, dict):
                        atten_raw = atten_raw.get("value")
                    try:
                        self._vars[atten_key].set(int(atten_raw))
                    except (TypeError, ValueError):
                        pass
        finally:
            self._afe_updating = False
        logging.info("AFE widgets updated from MQTT register data")

    def _afe_reset_defaults(self):
        """Restore all AFE widget vars to defaults from cached announce data."""
        reg_pins = getattr(self, "_afe_reg_pins", None)
        if not reg_pins:
            logging.warning("AFE: cannot reset — no announce data")
            return

        for device, pins in reg_pins.items():
            for reg in pins:
                name = reg["name"]
                if name.startswith("NOT_USED") or name.startswith("ATTEN_"):
                    continue
                key = f"afe_{device}_{name}"
                if name == "GNSS_ANT_SEL":
                    self._vars.get(f"afe_{device}_GNSS_ANT_SEL", tk.StringVar()).set(
                        "internal" if reg["default"] == 1 else "external")
                    continue
                if key in self._vars:
                    self._vars[key].set(bool(reg["default"]))
            # Reset attenuation for RX devices
            atten_key = f"afe_{device}_atten"
            if atten_key in self._vars:
                self._vars[atten_key].set(0)
        logging.info("AFE: all registers reset to defaults")

    # ------------------------------------------------------------------ #
    #  TLM helpers
    # ------------------------------------------------------------------ #

    # NOTE: GPS has no rate command — removed _tlm_apply_gnss_rate

    def _tlm_apply_time_config(self):
        source = self._vars["time_source"].get()
        epoch_mode = self._vars["epoch_mode"].get()

        if source == "gnss":
            self.bus.afe_set_time_source_gnss()
        elif source == "external":
            self.bus.afe_set_time_source_external()

        ts = int(time.time())
        if epoch_mode == "pps":
            self.bus.afe_set_time_epoch_pps(ts)
        elif epoch_mode == "nmea":
            self.bus.afe_set_time_epoch_nmea()
        elif epoch_mode == "immediate":
            self.bus.afe_set_time_epoch_immediate(ts)

        logging.info(f"TLM: time config set (source={source}, epoch={epoch_mode}, ts={ts})")

    def _tlm_get_time_params(self):
        self.bus.afe_get_time_params()
        logging.info("TLM: time params query sent")

    def _tlm_get_hk(self):
        self.bus.afe_telem_dump()
        logging.info("TLM: HK telemetry refresh requested")

    def _tlm_set_hk(self):
        self.bus.afe_telem_dump()
        logging.info("TLM: HK set requested (no settable HK params; refreshed telemetry)")

    def _tlm_get_polling_interval(self):
        self.bus.afe_get_polling_interval()
        logging.info("TLM: polling interval query sent")

    def _tlm_set_polling_interval(self):
        try:
            n = self._vars["poll_interval_s"].get()
        except tk.TclError:
            logging.error("TLM: invalid polling interval value")
            return
        self.bus.afe_set_polling_interval(n)
        logging.info(f"TLM: polling interval set to {n} s")

    def _tlm_refresh_telemetry(self):
        self.bus.afe_get_time_params()
        self.bus.afe_get_imu_params()
        self.bus.afe_get_mag_params()
        self.bus.afe_get_polling_interval()
        self.bus.afe_get_log_status()
        self.bus.afe_telem_dump()
        logging.info("TLM: full refresh requested")

    def _tlm_get_logging(self):
        self.bus.afe_get_log_status()
        logging.info("TLM: logging status query sent")

    def _tlm_set_logging(self):
        try:
            mode = self._vars["log_enabled"].get()
            path = self._vars["log_path"].get()
            rate = self._vars["log_rate"].get()
        except tk.TclError as e:
            logging.error(f"TLM: invalid logging setting: {e}")
            return

        if mode == "enabled":
            self.bus.afe_enable_logging()
        else:
            self.bus.afe_disable_logging()
        self.bus.afe_set_log_path(path)
        self.bus.afe_set_log_rate(rate)
        logging.info(f"TLM: logging set ({mode}, path={path}, rate={rate} s)")

    # ------------------------------------------------------------------ #
    #  CONFIG helpers (Logging)
    # ------------------------------------------------------------------ #

    def _log_enable(self):
        path = self._vars["log_path"].get()
        self.bus.afe_enable_logging()
        logging.info("CONFIG: logging enabled")
        self._vars["log_status"].set("Logging enabled")

    def _log_disable(self):
        self.bus.afe_disable_logging()
        logging.info("CONFIG: logging disabled")
        self._vars["log_status"].set("Logging disabled")

    def _log_apply_settings(self):
        try:
            path = self._vars["log_path"].get()
            rate = self._vars["log_rate"].get()
            mode = self._vars["service_log_mode"].get()
        except tk.TclError as e:
            logging.error(f"CONFIG: invalid logging setting: {e}")
            return
        
        self.bus.afe_set_log_path(path)
        self.bus.afe_set_log_rate(rate)
        self.bus.afe_set_service_log_mode(mode)
        logging.info(f"CONFIG: logging settings applied (path={path}, rate={rate} s, mode={mode})")
        self._vars["log_status"].set(f"Settings applied: rate={rate}s, mode={mode}")

    # ------------------------------------------------------------------ #
    #  DOCKER helpers
    # ------------------------------------------------------------------ #

    def _docker_set_action_busy(self, busy: bool):
        state = "disabled" if busy else "normal"
        for w in getattr(self, "_docker_action_widgets", []):
            try:
                w.configure(state=state)
            except Exception:
                pass

    def _docker_refresh_status_async(self):
        if self.docker.refresh_busy:
            return

        self.docker.refresh_busy = True

        def _worker():
            engine_status, _services, detail = self.docker.refresh_status()

            def _apply():
                self.docker.refresh_busy = False

                self._vars["docker_engine_status"].set(engine_status)
                self._vars["docker_compose_dir"].set(self.docker.compose_dir)
                running = sum(
                    1 for svc in self.docker.service_names
                    if self.docker.services.get(svc, {}).get("state", "").lower() == "running"
                )
                total = len(self.docker.service_names)
                self._vars["docker_services_summary"].set(f"{running}/{total}")
                self._vars["docker_last_refresh"].set(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

                selected = self._vars["docker_service"].get().strip()
                if selected and selected not in self.docker.services:
                    self._vars["docker_service"].set("")

                self._docker_render_service_list()
                self._docker_apply_selected_service()
                if detail:
                    logging.warning("DOCKER: %s", detail)

            self._gui_call(_apply)

        threading.Thread(target=_worker, daemon=True, name="docker_refresh").start()

    def _docker_render_service_list(self):
        tree = getattr(self, "_docker_services_tree", None)
        if tree is None:
            return

        for iid in tree.get_children():
            tree.delete(iid)

        for svc in self.docker.service_names:
            row = self.docker.services.get(svc, {})
            tree.insert(
                "",
                "end",
                iid=svc,
                values=(
                    row.get("state", "—"),
                    row.get("container", "—"),
                    row.get("command", "—"),
                    row.get("ports", "—"),
                ),
            )

        selected = self._vars["docker_service"].get().strip()
        if selected and selected in self.docker.services:
            self._docker_suppress_tree_stream = True
            tree.selection_set(selected)
            tree.focus(selected)
            tree.see(selected)
            self._docker_suppress_tree_stream = False

    def _docker_on_service_tree_select(self, _event=None):
        tree = getattr(self, "_docker_services_tree", None)
        if tree is None:
            return

        selected = list(tree.selection())
        if selected:
            self._vars["docker_service"].set(selected[0])
        self._docker_apply_selected_service()

        mode = self._vars.get("docker_log_mode", tk.StringVar()).get().strip().lower()
        if not self._docker_suppress_tree_stream:
            mode = self._vars.get("docker_log_mode", tk.StringVar()).get().strip().lower()
            if mode == "selected":
                self._docker_stream_start(restart=self.docker.log_busy)

    def _docker_apply_selected_service(self, *_):
        svc = self._vars.get("docker_service", tk.StringVar()).get().strip()
        row = self.docker.services.get(svc, {}) if svc else {}
        self._vars["docker_selected_state"].set(row.get("state", "—"))
        self._vars["docker_selected_container"].set(row.get("container", "—"))
        self._vars["docker_selected_command"].set(row.get("command", "—"))
        self._vars["docker_selected_ports"].set(row.get("ports", "—"))

    def _docker_selected_services(self) -> list[str]:
        tree = getattr(self, "_docker_services_tree", None)
        if tree is None:
            svc = self._docker_selected_service()
            return [svc] if svc else []

        picked = [s for s in tree.selection() if s in self.docker.services]
        if picked:
            return picked

        svc = self._docker_selected_service()
        return [svc] if svc else []

    def _docker_selected_service(self):
        svc = self._vars.get("docker_service", tk.StringVar()).get().strip()
        return svc or None

    def _docker_confirm_all_action(self, action_label: str) -> bool:
        return messagebox.askokcancel(
            title=f"Docker: {action_label}",
            message=(
                f"This will {action_label.lower()} all services in the compose project.\n\n"
                "Press OK to continue, or Cancel to abort."
            ),
            parent=self.root,
        )

    def _docker_run_compose_action_async(
        self,
        action: str,
        *,
        services: list[str] | None = None,
        confirm_all: bool = False,
        extra_args: list[str] | None = None,
    ):
        if self.docker.action_busy:
            logging.warning("DOCKER: another action is already in progress")
            return

        target_services = [s for s in (services or []) if s]

        if (not target_services) and confirm_all:
            if not self._docker_confirm_all_action(action.capitalize()):
                logging.info("DOCKER: %s all cancelled", action)
                return

        if not self.docker.get_compose_cmd():
            logging.error("DOCKER: docker compose command not found")
            return

        target_desc = ", ".join(target_services) if target_services else "all services"
        self.docker.action_busy = True
        self._docker_set_action_busy(True)
        logging.info("DOCKER: running %s on %s", action, target_desc)

        def _worker():
            rc, out, err = self.docker.run_compose_action(
                action, services=target_services or None, extra_args=extra_args,
            )

            def _done():
                self.docker.action_busy = False
                self._docker_set_action_busy(False)
                if rc == 0:
                    logging.info("DOCKER: %s complete on %s", action, target_desc)
                    if out:
                        logging.info("DOCKER: %s", out)
                else:
                    detail = err or out or f"exit code {rc}"
                    logging.error("DOCKER: %s failed on %s: %s", action, target_desc, detail)
                self._docker_refresh_status_async()
                if self.docker.log_busy:
                    self._docker_stream_start(restart=True)

            self._gui_call(_done)

        threading.Thread(target=_worker, daemon=True, name=f"docker_{action}").start()

    def _docker_action_targets(self, action: str, *, emit_errors: bool = True):
        scope = self._vars.get("docker_action_scope", tk.StringVar(value="selected")).get().strip().lower()
        if scope == "selected":
            picked = self._docker_selected_services()
            if action == "down":
                if emit_errors:
                    logging.error("DOCKER: 'down' applies to the whole compose project; switch scope to all")
                return None, None, None
            if not picked:
                if emit_errors:
                    logging.error("DOCKER: select at least one service first")
                return None, None, None
            return picked, False, "selected"

        # all scope
        confirm_all = action in ("stop", "restart", "down")
        return [], confirm_all, "all"

    def _docker_preview_command(self, action: str):
        targets, _confirm_all, scope = self._docker_action_targets(action, emit_errors=False)
        if scope is None:
            return self.docker.preview_command(action)

        extra_args = []
        if action == "up":
            extra_args.append("-d")
            if self._vars.get("docker_up_force_recreate", tk.BooleanVar(value=False)).get():
                extra_args.append("--force-recreate")

        return self.docker.preview_command(
            action, services=targets or None, extra_args=extra_args or None,
        )

    def _docker_set_hover_preview(self, text: str = ""):
        sv = self._vars.get("docker_cmd_preview")
        if sv is None:
            return
        if text:
            sv.set(text)
        else:
            sv.set("Hover over an action to preview the exact compose command")

    def _docker_bind_hover_preview(self, widget, action: str):
        widget.bind("<Enter>", lambda _e: self._docker_set_hover_preview(self._docker_preview_command(action)))
        widget.bind("<Leave>", lambda _e: self._docker_set_hover_preview(""))

    def _docker_run_from_controls(self, action: str):
        targets, confirm_all, scope = self._docker_action_targets(action)
        if scope is None:
            return

        extra_args = []
        if action == "up":
            extra_args = ["-d"]
            if self._vars.get("docker_up_force_recreate", tk.BooleanVar(value=False)).get():
                extra_args.append("--force-recreate")

        services = targets if scope == "selected" else []
        self._docker_run_compose_action_async(
            action,
            services=services,
            confirm_all=bool(confirm_all),
            extra_args=extra_args,
        )

    def _docker_stream_start(self, *, restart: bool = False):
        if self.docker.log_busy and not restart:
            self._docker_stream_resume()
            return

        mode = self._vars["docker_log_mode"].get().strip().lower() or "selected"
        service = self._docker_selected_service()
        scope = service if mode == "selected" else "all"

        tail_count = "30"
        if restart and self.docker.log_scope not in (None, scope):
            tail_count = "15"
            self._docker_clear_buffer_and_widget()

        if mode == "selected" and not service:
            logging.error("DOCKER: select a service to stream selected logs")
            return

        self._vars["docker_stream_state"].set("live")

        def _on_line(_line):
            if self._is_adv_tab_selected("DOC"):
                self._gui_call(self._docker_flush_buffer_to_widget)

        def _on_exit(rc):
            def _done():
                self._vars["docker_stream_state"].set("paused")
                if rc not in (None, 0):
                    logging.warning("DOCKER: log stream exited with code %s", rc)
            self._gui_call(_done)

        self.docker.stream_start(
            service=service if mode == "selected" else None,
            tail=tail_count,
            on_line=_on_line,
            on_exit=_on_exit,
        )

    def _docker_stream_pause(self):
        self.docker.stream_pause()
        self._vars["docker_stream_state"].set("paused")

    def _docker_stream_resume(self):
        if not self.docker.log_busy:
            self._docker_stream_start()
            return
        self.docker.stream_resume()
        self._vars["docker_stream_state"].set("live")
        self._docker_flush_buffer_to_widget()

    def _docker_on_log_mode_changed(self, _event=None):
        if self.docker.log_busy:
            self._docker_stream_start(restart=True)

    def _docker_flush_buffer_to_widget(self):
        if not hasattr(self, "_docker_log_text"):
            return
        if not self._is_adv_tab_selected("DOC"):
            return
        if self.docker.log_paused:
            return

        tail = self.docker.get_new_log_entries()
        if not tail:
            return

        for ts, line in tail:
            clean = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", line)
            self._docker_log_text.insert("end", f"{ts}  ", ("docker_ts",))
            if " | " in clean:
                svc, msg = clean.split(" | ", 1)
                svc = svc.strip()
                if svc:
                    self._docker_log_text.insert("end", f"{svc} | ", ("docker_svc",))
                self._docker_insert_pretty_log_message(msg)
            else:
                self._docker_insert_pretty_log_message(clean)
            self._docker_log_text.insert("end", "\n")

        self._docker_log_text.see("end")
        self._docker_trim_widget_lines()

    def _docker_insert_pretty_log_message(self, msg: str):
        text = (msg or "").rstrip()

        # Pretty-print JSON payloads when possible.
        if text.startswith("{") or text.startswith("["):
            try:
                parsed = json.loads(text)
                pretty = json.dumps(parsed, indent=2, sort_keys=True)
                self._docker_log_text.insert("end", pretty, ("docker_json",))
                return
            except Exception:
                pass

        m = re.match(r"^((?:\d{4}-\d{2}-\d{2}[ T])?\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)\s+(TRACE|DEBUG|INFO|WARN|WARNING|ERROR|CRITICAL)\s+(.*)$", text, re.IGNORECASE)
        if m:
            inner_ts, level, rest = m.groups()
            self._docker_log_text.insert("end", f"{inner_ts} ", ("docker_ts",))
            lvl = level.upper()
            lvl_tag = "docker_lvl"
            if lvl in ("ERROR", "CRITICAL"):
                lvl_tag = "docker_err"
            elif lvl in ("WARN", "WARNING"):
                lvl_tag = "docker_warn"
            elif lvl == "INFO":
                lvl_tag = "docker_info"
            self._docker_log_text.insert("end", f"{lvl:<8}", (lvl_tag,))
            self._docker_log_text.insert("end", rest, ("docker_msg",))
            return

        upper = text.upper()
        tag = "docker_msg"
        if any(k in upper for k in ("ERROR", "EXCEPTION", "CRITICAL", "TRACEBACK", "FAILED")):
            tag = "docker_err"
        elif any(k in upper for k in ("WARN", "WARNING")):
            tag = "docker_warn"
        elif "INFO" in upper:
            tag = "docker_info"
        self._docker_log_text.insert("end", text, (tag,))

    def _docker_trim_widget_lines(self, max_lines: int = 800):
        if not hasattr(self, "_docker_log_text"):
            return
        lines = int(self._docker_log_text.index("end-1c").split(".")[0])
        if lines > max_lines:
            self._docker_log_text.delete("1.0", f"{lines - max_lines}.0")

    def _docker_clear_buffer_and_widget(self):
        self.docker.clear_log()
        if hasattr(self, "_docker_log_text"):
            self._docker_log_text.delete("1.0", "end")

    def _build_docker_tab(self, frame: ttk.Frame):
        """DOC tab: compose service status, logs, and service controls."""
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(2, weight=1)

        def _ro_row(parent, row, col, label, key):
            sv = self._vars.get(key)
            if sv is None:
                sv = tk.StringVar(value="—")
                self._vars[key] = sv
            c0 = col * 3
            ttk.Label(parent, text=label).grid(row=row, column=c0, sticky="w", padx=5, pady=2)
            e = ttk.Entry(parent, textvariable=sv, state="readonly", width=24)
            e.grid(row=row, column=c0 + 1, sticky="ew", padx=5, pady=2)
            self._bind_copy_menu(e, sv)

        self._vars["docker_engine_status"] = tk.StringVar(value="Unknown")
        self._vars["docker_compose_dir"] = tk.StringVar(value=self.docker.compose_dir)
        self._vars["docker_services_summary"] = tk.StringVar(value="0/0")
        self._vars["docker_last_refresh"] = tk.StringVar(value="never")

        st_f = ttk.LabelFrame(frame, text="Status")
        st_f.grid(row=0, column=0, padx=4, pady=(4, 2), sticky="ew")
        for c in (1, 4):
            st_f.columnconfigure(c, weight=1)
        st_f.columnconfigure(6, weight=0)

        _ro_row(st_f, 0, 0, "Docker", "docker_engine_status")
        _ro_row(st_f, 0, 1, "Services", "docker_services_summary")
        _ro_row(st_f, 1, 0, "Compose Dir", "docker_compose_dir")
        _ro_row(st_f, 1, 1, "Last Refresh", "docker_last_refresh")
        ttk.Button(st_f, text="Refresh", width=9, command=self._docker_refresh_status_async).grid(
            row=0, column=6, rowspan=2, padx=(8, 6), pady=2, sticky="nsew"
        )

        svc_f = ttk.LabelFrame(frame, text="Services")
        svc_f.grid(row=1, column=0, padx=4, pady=(2, 2), sticky="ew")
        for c in (1, 3):
            svc_f.columnconfigure(c, weight=1)
        svc_f.rowconfigure(2, weight=1)

        self._vars["docker_service"] = tk.StringVar(value="")

        self._vars["docker_selected_state"] = tk.StringVar(value="—")
        self._vars["docker_selected_container"] = tk.StringVar(value="—")
        self._vars["docker_selected_command"] = tk.StringVar(value="—")
        self._vars["docker_selected_ports"] = tk.StringVar(value="—")
        ttk.Label(svc_f, text="Container").grid(row=0, column=0, sticky="w", padx=5, pady=2)
        _cn = ttk.Entry(svc_f, textvariable=self._vars["docker_selected_container"], state="readonly")
        _cn.grid(row=0, column=1, sticky="ew", padx=5, pady=2)
        self._bind_copy_menu(_cn, self._vars["docker_selected_container"])
        ttk.Label(svc_f, text="State").grid(row=0, column=2, sticky="w", padx=5, pady=2)
        _st = ttk.Entry(svc_f, textvariable=self._vars["docker_selected_state"], state="readonly")
        _st.grid(row=0, column=3, sticky="ew", padx=5, pady=2)
        self._bind_copy_menu(_st, self._vars["docker_selected_state"])
        ttk.Label(svc_f, text="Command").grid(row=1, column=0, sticky="w", padx=5, pady=2)
        _cmd = ttk.Entry(svc_f, textvariable=self._vars["docker_selected_command"], state="readonly")
        _cmd.grid(row=1, column=1, columnspan=3, sticky="ew", padx=5, pady=2)
        self._bind_copy_menu(_cmd, self._vars["docker_selected_command"])
        ttk.Label(svc_f, text="Ports").grid(row=2, column=0, sticky="w", padx=5, pady=2)
        _ports = ttk.Entry(svc_f, textvariable=self._vars["docker_selected_ports"], state="readonly")
        _ports.grid(row=2, column=1, columnspan=3, sticky="ew", padx=5, pady=2)
        self._bind_copy_menu(_ports, self._vars["docker_selected_ports"])

        table_f = ttk.Frame(svc_f)
        table_f.grid(row=3, column=0, columnspan=4, sticky="nsew", padx=5, pady=(2, 4))
        table_f.columnconfigure(0, weight=1)
        table_f.rowconfigure(0, weight=1)

        self._docker_services_tree = ttk.Treeview(
            table_f,
            columns=("state", "container", "command", "ports"),
            show="headings",
            selectmode="extended",
            height=8,
        )
        self._docker_services_tree.heading("state", text="State")
        self._docker_services_tree.heading("container", text="Container")
        self._docker_services_tree.heading("command", text="Command")
        self._docker_services_tree.heading("ports", text="Ports")
        self._docker_services_tree.column("state", width=90, minwidth=80, anchor="w")
        self._docker_services_tree.column("container", width=240, minwidth=160, anchor="w")
        self._docker_services_tree.column("command", width=420, minwidth=240, anchor="w")
        self._docker_services_tree.column("ports", width=320, minwidth=180, anchor="w")

        ysb = ttk.Scrollbar(table_f, orient="vertical", command=self._docker_services_tree.yview)
        xsb = ttk.Scrollbar(table_f, orient="horizontal", command=self._docker_services_tree.xview)
        self._docker_services_tree.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set)
        self._docker_services_tree.grid(row=0, column=0, sticky="nsew")
        ysb.grid(row=0, column=1, sticky="ns")
        xsb.grid(row=1, column=0, sticky="ew")
        self._docker_services_tree.bind("<<TreeviewSelect>>", self._docker_on_service_tree_select)

        ctl_f = ttk.LabelFrame(svc_f, text="Controls")
        ctl_f.grid(row=4, column=0, columnspan=4, sticky="ew", padx=5, pady=(0, 4))
        for i in range(5):
            ctl_f.columnconfigure(i, weight=1)

        self._vars["docker_action_scope"] = tk.StringVar(value="selected")
        self._vars["docker_up_force_recreate"] = tk.BooleanVar(value=False)

        scope_f = ttk.Frame(ctl_f)
        scope_f.grid(row=0, column=0, columnspan=2, sticky="w", padx=2, pady=(2, 2))
        ttk.Label(scope_f, text="Scope:").grid(row=0, column=0, sticky="w", padx=(0, 4))
        rb_selected = ttk.Radiobutton(scope_f, text="Selected", value="selected", variable=self._vars["docker_action_scope"])
        rb_selected.grid(row=0, column=1, sticky="w", padx=(0, 4))
        rb_all = ttk.Radiobutton(scope_f, text="All", value="all", variable=self._vars["docker_action_scope"])
        rb_all.grid(row=0, column=2, sticky="w")

        up_opt = ttk.Checkbutton(
            ctl_f,
            text="up: --force-recreate",
            variable=self._vars["docker_up_force_recreate"],
        )
        up_opt.grid(row=0, column=2, columnspan=3, sticky="e", padx=2, pady=(2, 2))

        b_start = ttk.Button(ctl_f, text="Start", command=lambda: self._docker_run_from_controls("start"))
        b_start.grid(row=1, column=0, sticky="ew", padx=(0, 2), pady=(0, 2))
        b_stop = ttk.Button(ctl_f, text="Stop", command=lambda: self._docker_run_from_controls("stop"))
        b_stop.grid(row=1, column=1, sticky="ew", padx=2, pady=(0, 2))
        b_restart = ttk.Button(ctl_f, text="Restart", command=lambda: self._docker_run_from_controls("restart"))
        b_restart.grid(row=1, column=2, sticky="ew", padx=2, pady=(0, 2))
        b_up = ttk.Button(ctl_f, text="Up", command=lambda: self._docker_run_from_controls("up"))
        b_up.grid(row=1, column=3, sticky="ew", padx=2, pady=(0, 2))
        b_down = ttk.Button(ctl_f, text="Down", command=lambda: self._docker_run_from_controls("down"))
        b_down.grid(row=1, column=4, sticky="ew", padx=(2, 0), pady=(0, 2))

        self._vars["docker_cmd_preview"] = tk.StringVar(
            value="Hover over an action to preview the exact compose command"
        )
        ttk.Label(ctl_f, text="Command:").grid(row=2, column=0, sticky="w", padx=(2, 4), pady=(2, 2))
        ttk.Label(
            ctl_f,
            textvariable=self._vars["docker_cmd_preview"],
            foreground="grey",
            font=("TkDefaultFont", 8),
        ).grid(row=2, column=1, columnspan=4, sticky="w", padx=(0, 2), pady=(2, 2))

        self._docker_action_widgets = [b_start, b_stop, b_restart, b_up, b_down]

        self._docker_bind_hover_preview(b_start, "start")
        self._docker_bind_hover_preview(b_stop, "stop")
        self._docker_bind_hover_preview(b_restart, "restart")
        self._docker_bind_hover_preview(b_up, "up")
        self._docker_bind_hover_preview(b_down, "down")

        log_f = ttk.LabelFrame(frame, text="Logs")
        log_f.grid(row=2, column=0, padx=4, pady=(2, 2), sticky="nsew")
        log_f.columnconfigure(0, weight=1)
        log_f.rowconfigure(1, weight=1)

        log_ctl = ttk.Frame(log_f)
        log_ctl.grid(row=0, column=0, sticky="ew", padx=4, pady=(4, 2))
        for i in range(6):
            log_ctl.columnconfigure(i, weight=1 if i == 0 else 0)

        self._vars["docker_log_mode"] = tk.StringVar(value="selected")
        mode_f = ttk.Frame(log_ctl)
        mode_f.grid(row=0, column=0, sticky="w", padx=(0, 6))
        ttk.Label(mode_f, text="Logs:").grid(row=0, column=0, sticky="w", padx=(0, 4))
        ttk.Radiobutton(
            mode_f,
            text="Selected",
            value="selected",
            variable=self._vars["docker_log_mode"],
            command=self._docker_on_log_mode_changed,
        ).grid(row=0, column=1, sticky="w", padx=(0, 4))
        ttk.Radiobutton(
            mode_f,
            text="All",
            value="all",
            variable=self._vars["docker_log_mode"],
            command=self._docker_on_log_mode_changed,
        ).grid(row=0, column=2, sticky="w")

        self._vars["docker_stream_state"] = tk.StringVar(value="paused")
        ttk.Label(
            log_ctl,
            textvariable=self._vars["docker_stream_state"],
            foreground="grey",
            font=("TkFixedFont", 8),
        ).grid(row=0, column=1, sticky="w", padx=(0, 8))

        ttk.Button(log_ctl, text="Stream", command=self._docker_stream_start).grid(
            row=0, column=2, sticky="ew", padx=(0, 4)
        )
        ttk.Button(log_ctl, text="Pause", command=self._docker_stream_pause).grid(
            row=0, column=3, sticky="ew", padx=(0, 4)
        )
        ttk.Button(log_ctl, text="Clear", command=self._docker_clear_buffer_and_widget).grid(
            row=0, column=4, sticky="ew"
        )

        self._docker_log_text = scrolledtext.ScrolledText(
            log_f,
            height=10,
            wrap="word",
            font=("TkFixedFont", 9),
            background="#f5f5f5",
        )
        self._docker_log_text.tag_configure("docker_ts", foreground="#6b7280")
        self._docker_log_text.tag_configure("docker_svc", foreground="#1d4ed8")
        self._docker_log_text.tag_configure("docker_lvl", foreground="#334155")
        self._docker_log_text.tag_configure("docker_msg", foreground="#111827")
        self._docker_log_text.tag_configure("docker_json", foreground="#0f172a")
        self._docker_log_text.tag_configure("docker_info", foreground="#0f766e")
        self._docker_log_text.tag_configure("docker_warn", foreground="#b45309")
        self._docker_log_text.tag_configure("docker_err", foreground="#b91c1c")
        self._docker_log_text.grid(row=1, column=0, sticky="nsew", padx=4, pady=(0, 4))
        self._docker_log_text.bind(
            "<Key>",
            lambda e: None if (e.state & 0x4 and e.keysym in ("c", "C", "a", "A")) else "break",
        )
        self._bind_copy_menu(self._docker_log_text)

        self._add_copyable_note(
            frame,
            "Source: docker compose project at /opt/radiohound/docker",
            row=3,
            wraplength=420,
        )
        self.root.after(100, self._docker_refresh_status_async)


    # ------------------------------------------------------------------ #
    #  SOC helpers
    # ------------------------------------------------------------------ #

    def _soc_refresh(self):
        def _query():
            self.bus.rfsoc_get_tlm()
            time.sleep(0.25)
            tlm = self.bus.get_cached_status(RFSOC_STATUS_TOPIC)
            if not isinstance(tlm, dict):
                logging.warning("SOC: no telemetry response")
                return
            self._gui_call(self._soc_apply, tlm)
        threading.Thread(target=_query, daemon=True).start()

    def _soc_apply(self, tlm: dict):
        self._vars["soc_state"].set(tlm.get("state", "—"))
        self._vars["soc_fc"].set(f"{float(tlm.get('f_c_hz', 0))/1e6:.3f}")
        self._vars["soc_fif"].set(f"{float(tlm.get('f_if_hz', 0))/1e6:.3f}")
        self._vars["soc_fs"].set(f"{float(tlm.get('f_s', 0))/1e6:.3f}")
        self._vars["soc_pps"].set(str(tlm.get("pps_count", "—")))
        self._vars["soc_channels"].set(str(tlm.get("channels", "—")))

    def _rfsoc_reset(self):
        self.bus.rfsoc_reset()
        logging.info("RFSoC reset sent")

    def _soc_arm_next_pps(self):
        self.bus.rfsoc_capture_next_pps()
        logging.info("SOC: capture_next_pps sent (cancel by Reset RFSoC)")

    def _soc_set_if_test(self):
        try:
            if_mhz = float(self._vars["soc_if_test"].get().strip())
        except ValueError:
            logging.error("SOC: invalid IF frequency value")
            return

        sweep_active = self._sweep_thread and self._sweep_thread.is_alive()
        recorder_running = bool(self.capture is not None and self.capture._recorder_running)
        if sweep_active or recorder_running:
            logging.warning("SOC: setting IF during active capture/sweep can disrupt capture")

        self.bus.rfsoc_set_if(if_mhz)
        logging.info(f"SOC: set freq_IF {if_mhz:.3f} MHz sent")

    # ------------------------------------------------------------------ #
    #  TUN helpers
    # ------------------------------------------------------------------ #

    def _tun_refresh(self):
        tuner_norm = self.bus.get_tuner_status_normalized()
        status = tuner_norm.get("raw") if isinstance(tuner_norm, dict) else None

        if not isinstance(tuner_norm, dict):
            self._vars["tun_state"].set("—")
            self._vars["tun_name"].set("—")
            text = "no status received"
        else:
            self._vars["tun_state"].set(str(tuner_norm.get("state", "—")))
            name_val = tuner_norm.get("name", "—")
            self._vars["tun_name"].set(str(name_val) if name_val else "—")

            freq_val = tuner_norm.get("lo_mhz")
            if freq_val is not None:
                self._vars["tun_set_freq"].set(str(freq_val))

            pwr_val = tuner_norm.get("pwr_dbm")
            if pwr_val is not None:
                self._vars["tun_set_power"].set(str(pwr_val))

            lines = []
            for k, v in (status or {}).items():
                if k == "info":
                    continue
                if isinstance(v, dict):
                    lines.append(f"{k}:")
                    for sk, sv in v.items():
                        if sk == "info":
                            continue
                        lines.append(f"  {sk}: {sv}")
                else:
                    lines.append(f"{k}: {v}")
            info = (status or {}).get("info")
            if info is None and isinstance((status or {}).get("tuner"), dict):
                info = (status or {})["tuner"].get("info")
            if info:
                lines.append("--- info ---")
                lines.append(str(info).replace("\\r\\n", "\n").replace("\r\n", "\n"))
            text = "\n".join(lines)

        self._tun_status_text.delete("1.0", "end")
        self._tun_status_text.insert("end", text)
        logging.info("TUN: status text updated")

    def _tun_handle_response(self, data: dict):
        task = data.get("task_name", "")
        value = data.get("value")
        if value is None:
            return
        if task == "get_freq":
            self._vars["tun_set_freq"].set(str(value))
            logging.info(f"TUN: freq = {value} MHz")
        elif task == "get_power":
            self._vars["tun_set_power"].set(str(value))
            logging.info(f"TUN: power = {value} dBm")
        elif task == "get_lock_status":
            if "tun_lock_status" not in self._vars:
                return
            if isinstance(value, dict):
                locks = [bool(v) for v in value.values() if isinstance(v, bool)]
                if locks:
                    state = "Locked" if all(locks) else "Unlocked"
                    detail = ", ".join(
                        f"{key}={'LOCK' if bool(val) else 'UNLOCK'}"
                        for key, val in value.items()
                    )
                    self._vars["tun_lock_status"].set(f"{state} ({detail})")
                else:
                    self._vars["tun_lock_status"].set(str(value))
            else:
                self._vars["tun_lock_status"].set(str(value))
            logging.info(f"TUN: lock status = {self._vars['tun_lock_status'].get()}")

    def _tun_init(self):
        tuner = self._vars["tuner"].get()
        force = None if tuner in ("None", "auto") else tuner
        self.bus.tuner_init(force_tuner=force)
        logging.info(f"TUN: init_tuner sent ({tuner})")
        if tuner.lower() == "valon":
            self.root.after(3000, self._tun_check_lock)

    def _tun_set_freq(self):
        try:
            freq = float(self._vars["tun_set_freq"].get())
        except ValueError:
            logging.error("TUN: invalid frequency value")
            return
        self.bus.tuner_set_freq(freq)
        logging.info(f"TUN: set_freq {freq:.3f} MHz sent")

    def _tun_get_freq(self):
        self.bus.tuner_get_freq()
        logging.info("TUN: get_freq sent")

    def _tun_set_power(self):
        try:
            pwr = float(self._vars["tun_set_power"].get())
        except ValueError:
            logging.error("TUN: invalid power value")
            return
        self.bus.tuner_set_power(pwr)
        logging.info(f"TUN: set_power {pwr:.1f} dBm sent")

    def _tun_get_power(self):
        self.bus.tuner_get_power()
        logging.info("TUN: get_power sent")

    def _tun_check_lock(self):
        self.bus.tuner_check_lock()
        logging.info("TUN: get_lock_status sent")

    def _tun_restart(self):
        self.bus.tuner_restart()
        logging.info("TUN: restart_tuner sent")

    def _tun_send_status(self):
        self.bus.tuner_status()
        logging.info("TUN: status command sent")

    def _tun_update_capability_buttons(self):
        name = self._vars.get("tun_name", tk.StringVar()).get().lower()
        state = "normal" if "valon" in name else "disabled"
        for w in getattr(self, "_valon_only_widgets", []):
            try:
                w.configure(state=state)
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    #  REC helpers
    # ------------------------------------------------------------------ #

    def _rec_status_update(self, data: dict):
        self._rec_status_cache = {
            "state": data.get("state", "—"),
            "file": (
                data.get("file")
                or data.get("filename")
                or data.get("path")
                or data.get("output_file")
                or "—"
            ),
        }
        if "rec_status" not in self._vars:
            return
        self._vars["rec_status"].set(data.get("state", "—"))
        fpath = (data.get("file") or data.get("filename") or
                 data.get("path") or data.get("output_file") or "—")
        self._vars["rec_status_file"].set(str(fpath))

    def _rec_status_seed(self):
        cached = self.bus.get_cached_status(RECORDER_STATUS_TOPIC)
        if isinstance(cached, dict):
            self._rec_status_update(cached)

    def _rec_reload_config(self):
        sr = self._vars["sample_rate_mhz"].get()
        config_name = f"sr{sr}MHz"
        self.bus.recorder_config_load(config_name)
        self._vars["rec_active_config"].set(config_name)
        logging.info(f"REC: config.load sent ({config_name})")

    def _apply_rec_spectrogram(self):
        self.bus.recorder_config_set("pipeline.spectrogram",
                                     self._vars["sg_compute"].get())
        self.bus.recorder_config_set("pipeline.spectrogram_mqtt",
                                     self._vars["sg_mqtt"].get())
        self.bus.recorder_config_set("pipeline.spectrogram_output",
                                     self._vars["sg_output"].get())
        self.bus.recorder_config_set("spectrogram.reduce_op",
                                     self._vars["sg_reduce_op"].get())
        try:
            self.bus.recorder_config_set("spectrogram_output.snr_db_min",
                                         float(self._vars["sg_snr_min"].get()))
            self.bus.recorder_config_set("spectrogram_output.snr_db_max",
                                         float(self._vars["sg_snr_max"].get()))
            self.bus.recorder_config_set("spectrogram_output.num_spectra_per_output",
                                         int(self._vars["sg_spectra_per_output"].get()))
        except ValueError as e:
            logging.error(f"Spectrogram config error: {e}")
            return
        logging.info("Spectrogram settings applied")

    def _apply_rec_drf(self):
        try:
            self.bus.recorder_config_set("packet.batch_size",
                                         int(self._vars["rec_batch_size"].get()))
        except ValueError as e:
            logging.error(f"DRF config error: {e}")
            return
        logging.info("DRF settings applied")

    # ------------------------------------------------------------------ #
    #  Tuner trace
    # ------------------------------------------------------------------ #

    def _on_tuner_change(self, *_):
        state = "normal" if self._vars["tuner"].get() != "None" else "disabled"
        self._if_entry.configure(state=state)
        self._update_synth_lo()

    def _update_synth_lo(self, *_):
        if self._vars["tuner"].get() == "None":
            self._vars["synth_lo"].set("—")
            return
        try:
            rf_mhz = float(self._vars["freq_start"].get())
            if_mhz = float(self._vars["adc_if_mhz"].get())
            mode   = self._vars["injection_mode"].get()
            lo_mhz = rf_mhz + if_mhz if mode == "High" else rf_mhz - if_mhz
            self._vars["synth_lo"].set(f"{lo_mhz:.3f}")
        except ValueError:
            self._vars["synth_lo"].set("—")

    def _toggle_advanced(self):
        if self._adv_frame.winfo_viewable():
            self._adv_frame.grid_remove()
            self._adv_btn_text.set("\u25b6 Advanced")
        else:
            self._adv_frame.grid()
            self._adv_btn_text.set("\u25c0 Advanced")

    # ------------------------------------------------------------------ #
    #  Logging
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
        self._text_log_handler = handler

    def _pump_text_log(self):
        """Drain queued log records onto Tk widgets on the main thread."""
        handler = getattr(self, "_text_log_handler", None)
        if handler is not None:
            try:
                handler.flush_pending()
            except Exception:
                pass
        try:
            self.root.after(50, self._pump_text_log)
        except tk.TclError:
            pass

    # ------------------------------------------------------------------ #
    #  Parameter parsing
    # ------------------------------------------------------------------ #

    def _parse_single_params(self) -> dict:
        freq_start = float(self._vars["freq_start"].get())
        channel    = self._vars["channel"].get()
        tuner_str  = self._vars["tuner"].get()
        tuner      = None if tuner_str == "None" else tuner_str

        adc_if_s   = self._vars["adc_if_mhz"].get().strip()
        adc_if_mhz = float(adc_if_s) if (adc_if_s and tuner) else None

        capture_name_s = self._vars["capture_name"].get().strip()
        capture_name   = capture_name_s if capture_name_s else None

        sample_rate_mhz = int(self._vars["sample_rate_mhz"].get())
        injection       = self._vars["injection_mode"].get().lower()
        dwell_enabled   = self._vars["single_dwell_enabled"].get()
        dwell_raw       = self._vars["dwell"].get().strip()

        try:
            dwell_s = float(dwell_raw) if (dwell_enabled and dwell_raw) else None
        except ValueError:
            raise ValueError(f"Invalid dwell value: {dwell_raw!r}")

        if dwell_s is not None and dwell_s <= 0:
            dwell_s = None

        return {
            "freq_start":       freq_start,
            "channel":          channel,
            "tuner":            tuner,
            "adc_if_mhz":       adc_if_mhz,
            "capture_name":     capture_name,
            "sample_rate_mhz":  sample_rate_mhz,
            "injection":        injection,
            "dwell":            dwell_s,
        }

    def _parse_sweep_params(self) -> dict:
        freq_start = float(self._vars["freq_start"].get())
        freq_end_s = self._vars["freq_end"].get().strip()
        freq_end   = float(freq_end_s) if freq_end_s else float("nan")
        step       = float(self._vars["step"].get())
        dwell      = float(self._vars["dwell"].get())

        channel    = self._vars["channel"].get()
        tuner_str  = self._vars["tuner"].get()
        tuner      = None if tuner_str == "None" else tuner_str

        adc_if_s   = self._vars["adc_if_mhz"].get().strip()
        adc_if_mhz = float(adc_if_s) if (adc_if_s and tuner) else None

        capture_name_s = self._vars["capture_name"].get().strip()
        capture_name   = capture_name_s if capture_name_s else None

        sample_rate_mhz = int(self._vars["sample_rate_mhz"].get())
        injection       = self._vars["injection_mode"].get().lower()

        return {
            "freq_start":       freq_start,
            "freq_end":         freq_end,
            "step":             step,
            "dwell":            dwell,
            "channel":          channel,
            "tuner":            tuner,
            "adc_if_mhz":       adc_if_mhz,
            "capture_name":     capture_name,
            "sample_rate_mhz":  sample_rate_mhz,
            "injection":        injection,
        }

    # ------------------------------------------------------------------ #
    #  Button handlers
    # ------------------------------------------------------------------ #

    def _configure_mep(self, params: dict):
        """Create or update CaptureController with GUI params."""
        self.capture = self._get_or_create_capture(params)

        if self._vars.get("sync_ntp") and self._vars["sync_ntp"].get():
            logging.info("Syncing NTP on RFSoC...")
            sync_ntp_on_rfsoc(os.path.dirname(os.path.abspath(__file__)))

        config_name = f"sr{params['sample_rate_mhz']}MHz"
        self._vars["rec_active_config"].set(config_name)

    # ------------------------------------------------------------------ #
    #  Button handlers
    # ------------------------------------------------------------------ #

    def _toggle_single_dwell(self):
        """Enable or disable the Single-tab dwell entry based on the checkbox state."""
        state = "normal" if self._vars["single_dwell_enabled"].get() else "disabled"
        self._single_dwell_entry.config(state=state)

    def _start(self):
        if self._tune_notebook.index("current") == 0:
            self._start_single()
        else:
            self._start_sweep()

    def _start_sweep(self):
        if self._sweep_thread and self._sweep_thread.is_alive():
            logging.warning("Sweep already running — use Stop first")
            return

        try:
            params = self._parse_sweep_params()
        except ValueError as e:
            logging.error(f"Parameter error: {e}")
            return

        def _worker():
            try:
                self._configure_mep(params)
                self.capture._stop_flag.clear()
                if not self.capture.wait_for_firmware_ready(max_wait_s=10):
                    logging.error("RFSoC firmware not ready — aborting sweep")
                    self._gui_call(self._status_var.set, "Idle")
                    return
                freqs_hz = get_frequency_list(
                    params["freq_start"], params["freq_end"], params["step"]
                )
                n = len(freqs_hz) if hasattr(freqs_hz, "__len__") else "?"
                logging.info(f"Starting sweep: {n} steps, dwell={params['dwell']}s")
                self.capture.run_sweep(freqs_hz, params["dwell"])
            except Exception as e:
                logging.error(f"Sweep error: {e}", exc_info=True)
            finally:
                self._gui_call(self._status_var.set, "Idle")

        self._sweep_thread = threading.Thread(target=_worker, daemon=True, name="sweep")
        self._status_var.set("Sweeping...")
        self._sweep_thread.start()

    def _start_single(self):
        if self._sweep_thread and self._sweep_thread.is_alive():
            logging.warning("Capture already running — use Stop first")
            return

        try:
            params = self._parse_single_params()
        except ValueError as e:
            logging.error(f"Parameter error: {e}")
            return

        def _worker():
            try:
                self._configure_mep(params)
                self.capture._stop_flag.clear()
                f_hz = int(params["freq_start"] * 1e6)
                dwell_s = params["dwell"]
                if dwell_s is not None:
                    logging.info(
                        f"Starting single capture at {params['freq_start']} MHz, dwell={dwell_s}s"
                    )
                else:
                    logging.info(f"Starting single capture at {params['freq_start']} MHz (no dwell)")
                self.capture.run_single(f_hz, dwell_s=dwell_s)
                self._gui_call(
                    self._status_var.set,
                    "Idle" if dwell_s is not None else "Single capture running",
                )
            except Exception as e:
                logging.error(f"Single capture error: {e}", exc_info=True)
                self._gui_call(self._status_var.set, "Error")

        self._sweep_thread = threading.Thread(target=_worker, daemon=True, name="single")
        self._status_var.set("Starting single capture...")
        self._sweep_thread.start()

    def _stop_all(self):
        def _worker():
            logging.info("Stop All — requesting sweep stop")
            if self.capture:
                self.capture.request_stop()

            if self._sweep_thread and self._sweep_thread.is_alive():
                logging.info("Waiting for sweep thread to exit...")
                self._sweep_thread.join(timeout=5.0)
                if self._sweep_thread.is_alive():
                    logging.warning("Sweep thread did not exit within 5s")

            if self.capture:
                self.capture.stop_recorder()
            self.bus.rfsoc_reset()
            self._gui_call(self._status_var.set, "Idle")
            logging.info("Stop All complete")

        threading.Thread(target=_worker, daemon=True, name="stop_all").start()

    # ------------------------------------------------------------------ #
    #  Housekeeping polling (Jetson health only)
    # ------------------------------------------------------------------ #

    def _schedule_housekeeping(self):
        self.root.after(1000, self._poll_housekeeping)

    def _poll_housekeeping(self):
        self._jetson_health_poll()
        self.root.after(1000, self._poll_housekeeping)


# ===== ENTRY POINT ===== #

def main():
    root = tk.Tk()
    print("Loading MEP Control App...", flush=True)
    app  = MEPGui(root)
    print("MEP GUI: initialization complete", flush=True)
    print("MEP GUI: waiting 200 ms for X11 kickstart", flush=True)

    def _on_close():
        logging.info("Window closed — cleaning up")
        app._gui_queue_closed = True
        handler = getattr(app, "_text_log_handler", None)
        if handler is not None:
            try:
                logging.getLogger().removeHandler(handler)
            except Exception:
                pass
            try:
                handler.close()
            except Exception:
                pass
        try:
            if getattr(app, "capture", None) is not None:
                app.capture.stop_recorder()
        except Exception as e:
            logging.debug(f"Exception stopping recorder during cleanup: {e}")
        try:
            if getattr(app, "bus", None) is not None:
                app.bus.disconnect()
        except Exception as e:
            logging.debug(f"Exception during cleanup: {e}")
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", _on_close)

    # --- X11 REMOTE FIX FOR MAC START ---
    # Ensure all pending draw operations are processed
    root.update_idletasks()
    
    # Programmatic "Minimize/Restore" to wake up XQuartz
    # 200ms delay gives the SSH tunnel a moment to stabilize
    def kickstart():
        root.withdraw()
        root.deiconify()
        logging.info("X11 render kickstart complete")

    root.after(200, kickstart)
    # --- X11 REMOTE FIX FOR MAC END ---

    root.mainloop()


if __name__ == "__main__":
    main()
