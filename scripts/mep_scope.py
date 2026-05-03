#!/usr/bin/env python3
"""
mep_scope.py

Standalone Tkinter oscilloscope viewer for live MEP DigitalRF ringbuffers.

This GUI is read-only. It does not send MQTT commands and does not control
RFSoC, recorder, tuner, AFE, or Xilinx tools.

Usage:
    python3 scripts/mep_scope.py --root /data/tmp-ringbuffer --name <buffer-name> --sample-rate-mhz 10 --channel B
"""

import argparse
import math
import os
import queue
import re
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
import tkinter as tk
from tkinter import ttk

try:
    import numpy as np
except Exception:  # pragma: no cover - target systems should have numpy, but keep UI importable.
    np = None

try:
    import h5py
except Exception:  # pragma: no cover - metadata display is optional.
    h5py = None

try:
    from digital_rf import DigitalRFReader
except Exception as exc:  # pragma: no cover - handled at runtime in the GUI.
    DigitalRFReader = None
    DIGITAL_RF_IMPORT_ERROR = exc
else:
    DIGITAL_RF_IMPORT_ERROR = None


DEFAULT_ROOT = "/data/tmp-ringbuffer"
DEFAULT_NAME = ""
DEFAULT_SAMPLE_RATE_MHZ = "10"
DEFAULT_CHANNEL = "B"
DEFAULT_WIDTH_MS = 1.0
DEFAULT_UNITS_PER_DIV = 0.25
DEFAULT_REFRESH_MS = 200
DEFAULT_LAG_MS = 100.0
CHANNEL_OPTIONS = ("A", "B", "C", "D")
SR_DIR_RE = re.compile(r"^sr(\d+(?:\.\d+)?)MHz$")
TRIGGER_OPTIONS = ("Free Run", "I Rising", "I Falling", "Q Rising", "Q Falling")


@dataclass(frozen=True)
class ScopeConfig:
    root: str
    name: str
    sample_rate_mhz: float
    channel: str
    width_ms: float
    horizontal_position_ms: float
    trigger_mode: str
    trigger_level: float
    refresh_ms: int
    lag_ms: float

    @property
    def sample_rate_hz(self) -> float:
        return self.sample_rate_mhz * 1e6

    @property
    def window_samples(self) -> int:
        return max(2, int(round(self.sample_rate_hz * self.width_ms / 1000.0)))

    @property
    def buffer_dir(self) -> str:
        return os.path.join(self.root, self.name) if self.name else self.root

    @property
    def top_level_dir(self) -> str:
        return os.path.join(self.buffer_dir, f"sr{self.sample_rate_mhz:g}MHz")

    @property
    def drf_channel(self) -> str:
        ch = self.channel.strip()
        if ch.lower().startswith("ch"):
            return "ch" + ch[2:].upper()
        return "ch" + ch.upper()


@dataclass(frozen=True)
class TraceSnapshot:
    i_values: object
    q_values: object
    start_index: int
    end_index: int
    bounds_start: int
    bounds_end: int
    sample_rate_hz: float
    top_level_dir: str
    channel: str
    center_frequency_hz: Optional[float] = None


@dataclass(frozen=True)
class ReaderStatus:
    kind: str
    message: str


def discover_names(root: str) -> list[str]:
    """Return named buffer directories under the root path."""
    try:
        entries = list(Path(root).iterdir())
    except OSError:
        return []
    return sorted(entry.name for entry in entries if entry.is_dir() and not SR_DIR_RE.match(entry.name))


def discover_sample_rates(buffer_dir: str) -> list[str]:
    """Return numeric sample-rate MHz strings from sr{N}MHz directories."""
    rates = []
    try:
        entries = list(Path(buffer_dir).iterdir())
    except OSError:
        return []

    for entry in entries:
        if not entry.is_dir():
            continue
        match = SR_DIR_RE.match(entry.name)
        if not match:
            continue
        value = match.group(1)
        try:
            rates.append((float(value), value))
        except ValueError:
            continue

    return [value for _num, value in sorted(rates, key=lambda item: item[0])]


def _to_iq_arrays(samples):
    if np is not None:
        arr = np.asarray(samples)
        if arr.size == 0:
            return [], []
        return np.real(arr), np.imag(arr)

    i_vals = []
    q_vals = []
    for sample in samples:
        try:
            i_vals.append(float(sample.real))
            q_vals.append(float(sample.imag))
        except AttributeError:
            i_vals.append(float(sample))
            q_vals.append(0.0)
    return i_vals, q_vals


def _scalar_or_first(value):
    if np is not None:
        arr = np.asarray(value)
        if arr.shape == ():
            return arr.item()
        if arr.size:
            return arr.flat[0].item() if hasattr(arr.flat[0], "item") else arr.flat[0]
        return None
    try:
        return value[0]
    except Exception:
        return value


def _metadata_center_frequency_hz(top_level_dir: str, channel: str, sample_index: int) -> Optional[float]:
    if h5py is None:
        return None

    metadata_dir = Path(top_level_dir) / channel / "metadata"
    try:
        paths = sorted(metadata_dir.rglob("metadata@*.h5"), reverse=True)
    except OSError:
        return None

    best_index = None
    best_value = None
    for path in paths[:32]:
        try:
            with h5py.File(path, "r") as h5:
                for key in h5.keys():
                    try:
                        idx = int(key)
                    except ValueError:
                        continue
                    if idx > sample_index:
                        continue
                    if best_index is not None and idx <= best_index:
                        continue

                    data = h5[key]
                    value = None
                    if "center_frequency" in data:
                        value = _scalar_or_first(data["center_frequency"][()])
                    elif "center_frequencies" in data:
                        value = _scalar_or_first(data["center_frequencies"][()])
                    elif "center_frequency" in data.attrs:
                        value = _scalar_or_first(data.attrs["center_frequency"])
                    elif "center_frequencies" in data.attrs:
                        value = _scalar_or_first(data.attrs["center_frequencies"])

                    if value is not None:
                        try:
                            best_value = float(value)
                            best_index = idx
                        except (TypeError, ValueError):
                            pass
        except Exception:
            continue

    return best_value


def _find_trigger_index(i_values, q_values, cfg: ScopeConfig):
    mode = cfg.trigger_mode
    if mode == "Free Run":
        return None

    values = q_values if mode.startswith("Q ") else i_values
    rising = mode.endswith("Rising")
    level = cfg.trigger_level
    n = len(values)
    if n < 2:
        return None

    for idx in range(n - 1, 0, -1):
        try:
            prev_v = float(values[idx - 1])
            cur_v = float(values[idx])
        except Exception:
            continue
        if not (math.isfinite(prev_v) and math.isfinite(cur_v)):
            continue
        if rising and prev_v < level <= cur_v:
            return idx
        if (not rising) and prev_v > level >= cur_v:
            return idx
    return None


def _latest_read_range(reader, cfg: ScopeConfig):
    bounds_start, bounds_end = reader.get_bounds(cfg.drf_channel)
    if bounds_end <= bounds_start:
        raise RuntimeError(f"No samples available in {cfg.drf_channel}")

    lag_samples = max(0, int(round(cfg.sample_rate_hz * cfg.lag_ms / 1000.0)))
    position_samples = int(round(cfg.sample_rate_hz * cfg.horizontal_position_ms / 1000.0))
    desired_end = max(bounds_start + 1, bounds_end - lag_samples)
    desired_start = max(bounds_start, desired_end - cfg.window_samples - max(0, position_samples))
    if desired_end <= desired_start:
        raise RuntimeError("Not enough samples available after latest-read lag")

    if cfg.trigger_mode != "Free Run":
        search_samples = max(cfg.window_samples * 4, cfg.window_samples + abs(position_samples) + 2)
        desired_start = max(bounds_start, desired_end - search_samples)

    blocks = reader.get_continuous_blocks(desired_start, desired_end - 1, cfg.drf_channel)
    if not blocks:
        raise RuntimeError(f"No continuous blocks in [{desired_start}, {desired_end})")

    best_start = None
    best_len = None
    for block_start, block_len in sorted(blocks.items()):
        block_end = int(block_start) + int(block_len)
        read_start = max(desired_start, int(block_start))
        read_end = min(desired_end, block_end)
        if read_end <= read_start:
            continue
        if best_start is None or read_end > best_start + best_len:
            best_start = read_start
            best_len = read_end - read_start

    if best_start is None or best_len is None:
        raise RuntimeError(f"No readable samples in [{desired_start}, {desired_end})")

    return bounds_start, bounds_end, best_start, best_len


class DigitalRFScopeReader(threading.Thread):
    def __init__(self, out_queue: queue.Queue):
        super().__init__(daemon=True, name="digitalrf_scope_reader")
        self._out_queue = out_queue
        self._stop_event = threading.Event()
        self._config_lock = threading.Lock()
        self._config: Optional[ScopeConfig] = None
        self._reader = None
        self._reader_key = None
        self._last_read_end = None
        self._metadata_last_check = 0.0
        self._metadata_center_frequency_hz = None

    def stop(self):
        self._stop_event.set()

    def set_config(self, cfg: Optional[ScopeConfig]):
        with self._config_lock:
            self._config = cfg
            self._last_read_end = None

    def run(self):
        while not self._stop_event.is_set():
            with self._config_lock:
                cfg = self._config

            if cfg is None:
                time.sleep(0.05)
                continue

            try:
                self._read_once(cfg)
            except Exception as exc:
                self._out_queue.put(ReaderStatus("error", f"{exc}"))
                time.sleep(max(0.1, cfg.refresh_ms / 1000.0))

    def _read_once(self, cfg: ScopeConfig):
        if DigitalRFReader is None:
            raise RuntimeError(f"digital_rf import failed: {DIGITAL_RF_IMPORT_ERROR}")

        reader_key = (cfg.top_level_dir, cfg.drf_channel)
        if self._reader is None or reader_key != self._reader_key:
            self._reader = DigitalRFReader(cfg.top_level_dir)
            self._reader_key = reader_key
            self._last_read_end = None
            self._metadata_last_check = 0.0
            self._metadata_center_frequency_hz = None

        bounds_start, bounds_end, read_start, read_len = _latest_read_range(self._reader, cfg)
        read_end = read_start + read_len

        if self._last_read_end == read_end:
            time.sleep(max(0.05, cfg.refresh_ms / 1000.0))
            return

        samples = self._reader.read_vector(read_start, read_len, cfg.drf_channel)
        i_values, q_values = _to_iq_arrays(samples)

        triggered = False
        if cfg.trigger_mode != "Free Run":
            trigger_idx = _find_trigger_index(i_values, q_values, cfg)
            if trigger_idx is not None:
                trigger_abs = read_start + trigger_idx
                position_samples = int(round(cfg.sample_rate_hz * cfg.horizontal_position_ms / 1000.0))
                display_start = max(bounds_start, trigger_abs - position_samples)
                display_end = min(bounds_end, display_start + cfg.window_samples)
                display_start = max(bounds_start, display_end - cfg.window_samples)
                if display_end > display_start:
                    samples = self._reader.read_vector(display_start, display_end - display_start, cfg.drf_channel)
                    i_values, q_values = _to_iq_arrays(samples)
                    read_start = display_start
                    read_end = display_end
                    triggered = True

        if cfg.trigger_mode == "Free Run" or not triggered:
            position_samples = max(0, int(round(cfg.sample_rate_hz * cfg.horizontal_position_ms / 1000.0)))
            display_end = max(bounds_start + 1, bounds_end - int(round(cfg.sample_rate_hz * cfg.lag_ms / 1000.0)) - position_samples)
            display_start = max(bounds_start, display_end - cfg.window_samples)
            if display_start != read_start or display_end != read_end:
                samples = self._reader.read_vector(display_start, display_end - display_start, cfg.drf_channel)
                i_values, q_values = _to_iq_arrays(samples)
                read_start = display_start
                read_end = display_end

        self._last_read_end = read_end
        now = time.monotonic()
        if now - self._metadata_last_check >= 1.0 or reader_key != self._reader_key:
            self._metadata_center_frequency_hz = _metadata_center_frequency_hz(
                cfg.top_level_dir,
                cfg.drf_channel,
                read_start,
            )
            self._metadata_last_check = now
        self._out_queue.put(
            TraceSnapshot(
                i_values=i_values,
                q_values=q_values,
                start_index=read_start,
                end_index=read_end,
                bounds_start=bounds_start,
                bounds_end=bounds_end,
                sample_rate_hz=cfg.sample_rate_hz,
                top_level_dir=cfg.top_level_dir,
                channel=cfg.drf_channel,
                center_frequency_hz=self._metadata_center_frequency_hz,
            )
        )
        time.sleep(max(0.05, cfg.refresh_ms / 1000.0))


class MEPScopeGui:
    def __init__(self, root: tk.Tk, args):
        self.root = root
        self.root.title("MEP Scope")
        self.root.geometry("900x620")
        self.root.minsize(760, 520)
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)

        self._vars = {
            "root_path": tk.StringVar(value=args.root),
            "buffer_name": tk.StringVar(value=args.name),
            "sample_rate_mhz": tk.StringVar(value=args.sample_rate_mhz),
            "channel": tk.StringVar(value=args.channel),
            "width_ms": tk.StringVar(value=f"{args.width_ms:g}"),
            "horizontal_position_ms": tk.StringVar(value=f"{args.horizontal_position_ms:g}"),
            "units_per_div": tk.StringVar(value=f"{args.units_per_div:g}"),
            "trigger_mode": tk.StringVar(value=args.trigger),
            "trigger_level": tk.StringVar(value=f"{args.trigger_level:g}"),
            "refresh_ms": tk.StringVar(value=str(args.refresh_ms)),
            "lag_ms": tk.StringVar(value=f"{args.lag_ms:g}"),
            "center_frequency": tk.StringVar(value="DRF Fc: -"),
            "pk_pk": tk.StringVar(value="Pk-Pk: -"),
            "state": tk.StringVar(value="paused"),
            "status": tk.StringVar(value="Paused"),
            "cursor": tk.StringVar(value="Cursor: -"),
        }

        self._latest_snapshot: Optional[TraceSnapshot] = None
        self._queue = queue.Queue(maxsize=4)
        self._reader = DigitalRFScopeReader(self._queue)
        self._reader.start()
        self._settings_update_after = None
        self._suppress_var_update = False

        self._build_ui()
        self._refresh_buffer_names(select_current=True)
        self._refresh_sample_rate_options(select_current=True)
        self._bind_live_settings()
        self.root.after(50, self._pump_queue)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self):
        cfg_f = ttk.LabelFrame(self.root, text="DigitalRF Source")
        cfg_f.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 4))
        for col in (1, 3, 5):
            cfg_f.columnconfigure(col, weight=1)

        ttk.Label(cfg_f, text="Root").grid(row=0, column=0, sticky="w", padx=5, pady=4)
        ttk.Entry(cfg_f, textvariable=self._vars["root_path"], width=36).grid(
            row=0, column=1, columnspan=3, sticky="ew", padx=5, pady=4
        )
        ttk.Button(cfg_f, text="Scan", command=self._scan_source).grid(
            row=0, column=4, sticky="ew", padx=5, pady=4
        )

        ttk.Label(cfg_f, text="Name").grid(row=1, column=0, sticky="w", padx=5, pady=4)
        self._buffer_name_combo = ttk.Combobox(
            cfg_f,
            textvariable=self._vars["buffer_name"],
            width=18,
            state="readonly",
        )
        self._buffer_name_combo.grid(row=1, column=1, sticky="ew", padx=5, pady=4)
        self._buffer_name_combo.bind("<<ComboboxSelected>>", self._on_buffer_name_selected)

        ttk.Label(cfg_f, text="Sample Rate (MHz)").grid(row=1, column=2, sticky="w", padx=5, pady=4)
        self._sample_rate_combo = ttk.Combobox(
            cfg_f,
            textvariable=self._vars["sample_rate_mhz"],
            width=12,
            state="readonly",
        )
        self._sample_rate_combo.grid(row=1, column=3, sticky="ew", padx=5, pady=4)

        ttk.Label(cfg_f, text="Channel").grid(row=1, column=4, sticky="w", padx=5, pady=4)
        ttk.Combobox(
            cfg_f,
            textvariable=self._vars["channel"],
            values=CHANNEL_OPTIONS,
            width=8,
            state="readonly",
        ).grid(row=1, column=5, sticky="ew", padx=5, pady=4)

        ttk.Button(cfg_f, text="Run", command=self._run).grid(row=2, column=4, sticky="ew", padx=5, pady=4)
        ttk.Button(cfg_f, text="Pause", command=self._pause).grid(row=2, column=5, sticky="ew", padx=5, pady=4)

        display_f = ttk.Frame(self.root)
        display_f.grid(row=2, column=0, sticky="ew", padx=8, pady=(4, 4))
        for col in (0, 1, 2, 3):
            display_f.columnconfigure(col, weight=1, uniform="control_groups")

        vertical_f = ttk.LabelFrame(display_f, text="Vertical Controls")
        vertical_f.grid(row=0, column=0, sticky="nsew", padx=(0, 4), pady=0)
        vertical_f.columnconfigure(1, weight=1)
        ttk.Label(vertical_f, text="Units/Div").grid(row=0, column=0, sticky="w", padx=5, pady=4)
        ttk.Entry(vertical_f, textvariable=self._vars["units_per_div"], width=10).grid(
            row=0, column=1, sticky="ew", padx=5, pady=4
        )
        ttk.Button(vertical_f, text="+", width=3, command=lambda: self._nudge_numeric_var("units_per_div", 0.05)).grid(
            row=0, column=2, sticky="ew", padx=(0, 2), pady=4
        )
        ttk.Button(vertical_f, text="-", width=3, command=lambda: self._nudge_numeric_var("units_per_div", -0.05, minimum=0.05)).grid(
            row=0, column=3, sticky="ew", padx=(0, 5), pady=4
        )

        horizontal_f = ttk.LabelFrame(display_f, text="Horizontal Controls")
        horizontal_f.grid(row=0, column=1, sticky="nsew", padx=4, pady=0)
        horizontal_f.columnconfigure(1, weight=1)
        ttk.Label(horizontal_f, text="Width (ms)").grid(row=0, column=0, sticky="w", padx=5, pady=4)
        ttk.Entry(horizontal_f, textvariable=self._vars["width_ms"], width=10).grid(
            row=0, column=1, sticky="ew", padx=5, pady=4
        )
        ttk.Button(horizontal_f, text="+", width=3, command=lambda: self._nudge_numeric_var("width_ms", 0.01)).grid(
            row=0, column=2, sticky="ew", padx=(0, 2), pady=4
        )
        ttk.Button(horizontal_f, text="-", width=3, command=lambda: self._nudge_numeric_var("width_ms", -0.01, minimum=0.01)).grid(
            row=0, column=3, sticky="ew", padx=(0, 5), pady=4
        )
        ttk.Label(horizontal_f, text="Position (ms)").grid(row=1, column=0, sticky="w", padx=5, pady=4)
        ttk.Entry(horizontal_f, textvariable=self._vars["horizontal_position_ms"], width=10).grid(
            row=1, column=1, sticky="ew", padx=5, pady=4
        )
        ttk.Label(horizontal_f, text="Refresh (ms)").grid(row=2, column=0, sticky="w", padx=5, pady=4)
        ttk.Entry(horizontal_f, textvariable=self._vars["refresh_ms"], width=10).grid(
            row=2, column=1, sticky="ew", padx=5, pady=4
        )
        ttk.Label(horizontal_f, text="Read Lag (ms)").grid(row=2, column=2, sticky="e", padx=5, pady=4)
        ttk.Entry(horizontal_f, textvariable=self._vars["lag_ms"], width=10).grid(
            row=2, column=3, sticky="ew", padx=(0, 5), pady=4
        )

        trigger_f = ttk.LabelFrame(display_f, text="Trigger")
        trigger_f.grid(row=0, column=2, sticky="nsew", padx=4, pady=0)
        trigger_f.columnconfigure(1, weight=1)
        ttk.Combobox(
            trigger_f,
            textvariable=self._vars["trigger_mode"],
            values=TRIGGER_OPTIONS,
            width=14,
            state="readonly",
        ).grid(row=0, column=0, columnspan=2, sticky="ew", padx=5, pady=4)
        ttk.Label(trigger_f, text="Level").grid(row=1, column=0, sticky="w", padx=5, pady=4)
        ttk.Entry(trigger_f, textvariable=self._vars["trigger_level"], width=10).grid(
            row=1, column=1, sticky="ew", padx=5, pady=4
        )
        ttk.Button(trigger_f, text="Apply", command=self._apply_live_settings).grid(
            row=2, column=0, columnspan=2, sticky="ew", padx=5, pady=4
        )

        measure_f = ttk.LabelFrame(display_f, text="Measure")
        measure_f.grid(row=0, column=3, sticky="nsew", padx=(4, 0), pady=0)
        measure_f.columnconfigure(0, weight=1)
        ttk.Label(measure_f, textvariable=self._vars["center_frequency"], width=1).grid(
            row=0, column=0, sticky="ew", padx=5, pady=4
        )
        ttk.Label(measure_f, textvariable=self._vars["pk_pk"], width=1).grid(
            row=1, column=0, sticky="ew", padx=5, pady=4
        )

        self._canvas = tk.Canvas(self.root, background="#101010", highlightthickness=1, highlightbackground="#333333")
        self._canvas.grid(row=1, column=0, sticky="nsew", padx=8, pady=4)
        self._canvas.bind("<Configure>", lambda _event: self._render_latest())
        self._canvas.bind("<Motion>", self._cursor_update)
        self._canvas.bind("<Button-1>", self._cursor_update)

        status_f = ttk.Frame(self.root)
        status_f.grid(row=3, column=0, sticky="ew", padx=8, pady=(0, 8))
        status_f.columnconfigure(0, weight=1)
        ttk.Label(status_f, textvariable=self._vars["status"], foreground="grey", width=1).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(status_f, textvariable=self._vars["cursor"], foreground="grey", width=1).grid(
            row=1, column=0, sticky="w"
        )
        ttk.Label(status_f, textvariable=self._vars["state"], foreground="grey").grid(
            row=0, column=1, rowspan=2, sticky="e"
        )

    def _bind_live_settings(self):
        redraw_only = ("units_per_div",)
        restart_reader = (
            "root_path", "buffer_name", "sample_rate_mhz", "channel", "width_ms",
            "horizontal_position_ms", "trigger_mode", "trigger_level", "refresh_ms", "lag_ms",
        )

        for key in redraw_only:
            self._vars[key].trace_add("write", lambda *_args: self._schedule_render_latest())
        for key in restart_reader:
            self._vars[key].trace_add("write", lambda *_args: self._schedule_settings_update())

    def _schedule_render_latest(self):
        if self._suppress_var_update:
            return
        self.root.after_idle(self._render_latest)

    def _schedule_settings_update(self):
        if self._suppress_var_update:
            return
        if self._settings_update_after is not None:
            try:
                self.root.after_cancel(self._settings_update_after)
            except tk.TclError:
                pass
        self._settings_update_after = self.root.after(250, self._apply_live_settings)

    def _apply_live_settings(self):
        self._settings_update_after = None
        if self._vars["state"].get() != "running":
            self._render_latest()
            return
        cfg = self._build_config()
        if cfg is None:
            return
        self._vars["status"].set(f"Reading {cfg.top_level_dir}/{cfg.drf_channel}")
        self._reader.set_config(cfg)

    def _on_buffer_name_selected(self, _event=None):
        self._refresh_sample_rate_options(select_current=True)
        self._schedule_settings_update()

    def _nudge_numeric_var(self, key: str, delta: float, minimum: Optional[float] = None):
        try:
            value = float(self._vars[key].get())
        except (KeyError, ValueError, tk.TclError):
            value = 0.0
        value += delta
        if minimum is not None and value < minimum:
            value = minimum
        self._vars[key].set(f"{value:.6g}")

    def _scan_source(self):
        self._refresh_buffer_names(select_current=True)
        self._refresh_sample_rate_options(select_current=True)

    def _refresh_buffer_names(self, select_current=False):
        names = discover_names(self._vars["root_path"].get().strip())
        self._buffer_name_combo.configure(values=names)
        current = self._vars["buffer_name"].get().strip()
        if names and (select_current or current not in names):
            self._vars["buffer_name"].set(current if current in names else names[0])
        if names:
            self._vars["status"].set(f"Found buffers under {self._vars['root_path'].get()}: {', '.join(names)}")
        else:
            self._vars["status"].set(f"No named buffer directories found under {self._vars['root_path'].get()}")

    def _selected_buffer_dir(self) -> str:
        root = self._vars["root_path"].get().strip()
        name = self._vars["buffer_name"].get().strip()
        return os.path.join(root, name) if name else root

    def _refresh_sample_rate_options(self, select_current=False):
        buffer_dir = self._selected_buffer_dir()
        rates = discover_sample_rates(buffer_dir)
        self._sample_rate_combo.configure(values=rates)
        current = self._vars["sample_rate_mhz"].get().strip()
        if rates and (select_current or current not in rates):
            self._vars["sample_rate_mhz"].set(current if current in rates else rates[0])
        if rates:
            self._vars["status"].set(f"Found sample rates under {buffer_dir}: {', '.join(rates)} MHz")
        else:
            self._vars["status"].set(f"No sr{{N}}MHz directories found under {buffer_dir}")

    def _build_config(self) -> Optional[ScopeConfig]:
        try:
            cfg = ScopeConfig(
                root=self._vars["root_path"].get().strip() or DEFAULT_ROOT,
                name=self._vars["buffer_name"].get().strip(),
                sample_rate_mhz=float(self._vars["sample_rate_mhz"].get().strip()),
                channel=self._vars["channel"].get().strip() or DEFAULT_CHANNEL,
                width_ms=max(0.000001, float(self._vars["width_ms"].get().strip())),
                horizontal_position_ms=max(0.0, float(self._vars["horizontal_position_ms"].get().strip())),
                trigger_mode=self._vars["trigger_mode"].get().strip() or "Free Run",
                trigger_level=float(self._vars["trigger_level"].get().strip()),
                refresh_ms=max(20, int(float(self._vars["refresh_ms"].get().strip()))),
                lag_ms=max(0.0, float(self._vars["lag_ms"].get().strip())),
            )
        except Exception as exc:
            self._vars["status"].set(f"Invalid scope setting: {exc}")
            return None

        if cfg.sample_rate_hz <= 0:
            self._vars["status"].set("Sample rate must be greater than zero")
            return None
        return cfg

    def _run(self):
        cfg = self._build_config()
        if cfg is None:
            return
        self._vars["state"].set("running")
        self._vars["status"].set(f"Reading {cfg.top_level_dir}/{cfg.drf_channel}")
        self._reader.set_config(cfg)

    def _pause(self):
        self._vars["state"].set("paused")
        self._reader.set_config(None)
        self._vars["status"].set("Paused")

    def _pump_queue(self):
        latest = None
        while True:
            try:
                latest = self._queue.get_nowait()
            except queue.Empty:
                break

        if isinstance(latest, TraceSnapshot):
            self._latest_snapshot = latest
            self._vars["center_frequency"].set(
                f"DRF Fc: {self._fmt_frequency(latest.center_frequency_hz)}"
            )
            self._vars["pk_pk"].set(f"Pk-Pk: {self._fmt_number(self._pk_pk(latest))}")
            self._render_latest()
        elif isinstance(latest, ReaderStatus):
            self._vars["status"].set(latest.message)

        self.root.after(50, self._pump_queue)

    def _render_latest(self):
        snap = self._latest_snapshot
        if snap is None or not hasattr(self, "_canvas"):
            return

        self._canvas.delete("all")
        w = max(20, self._canvas.winfo_width())
        h = max(20, self._canvas.winfo_height())
        pad_l, pad_r, pad_t, pad_b = 56, 12, 18, 34
        plot_w = max(1, w - pad_l - pad_r)
        plot_h = max(1, h - pad_t - pad_b)

        i_vals = snap.i_values
        q_vals = snap.q_values
        n = len(i_vals)
        if n <= 1:
            self._vars["status"].set("Not enough samples to render")
            return

        try:
            units_per_div = abs(float(self._vars["units_per_div"].get()))
        except ValueError:
            units_per_div = self._default_units_per_div(i_vals, q_vals)
        if units_per_div <= 0.0:
            units_per_div = self._default_units_per_div(i_vals, q_vals)

        vertical_divs = 8.0
        span = units_per_div * vertical_divs
        ymin = -span / 2.0
        ymax = span / 2.0

        self._draw_grid(w, h, pad_l, pad_r, pad_t, pad_b, ymin, ymax)
        self._draw_trace(i_vals, "#6ad7ff", pad_l, pad_t, plot_w, plot_h, ymin, ymax)
        self._draw_trace(q_vals, "#ffcc66", pad_l, pad_t, plot_w, plot_h, ymin, ymax)

        duration_ms = (snap.end_index - snap.start_index) / snap.sample_rate_hz * 1000.0
        self._canvas.create_text(8, 6, anchor="nw", fill="#cccccc", text="I", font=("TkDefaultFont", 9, "bold"))
        self._canvas.create_text(28, 6, anchor="nw", fill="#6ad7ff", text="I", font=("TkDefaultFont", 9, "bold"))
        self._canvas.create_text(48, 6, anchor="nw", fill="#ffcc66", text="Q", font=("TkDefaultFont", 9, "bold"))
        self._canvas.create_text(w - 8, 6, anchor="ne", fill="#aaaaaa", text=f"{duration_ms:.3f} ms")
        self._canvas.create_text(pad_l, h - 18, anchor="sw", fill="#aaaaaa", text="0")
        self._canvas.create_text(w - pad_r, h - 18, anchor="se", fill="#aaaaaa", text=f"{duration_ms:.3f} ms")

        self._vars["status"].set(
            f"{snap.top_level_dir}/{snap.channel}  bounds=[{snap.bounds_start}, {snap.bounds_end})  "
            f"display=[{snap.start_index}, {snap.end_index})  samples={n}"
        )

    def _default_units_per_div(self, i_vals, q_vals):
        if np is not None:
            combined = np.concatenate((np.asarray(i_vals).ravel(), np.asarray(q_vals).ravel()))
            finite = combined[np.isfinite(combined)]
            if finite.size:
                ymin = float(np.min(finite))
                ymax = float(np.max(finite))
            else:
                ymin, ymax = -1.0, 1.0
        else:
            vals = [float(v) for v in list(i_vals) + list(q_vals) if math.isfinite(float(v))]
            ymin = min(vals) if vals else -1.0
            ymax = max(vals) if vals else 1.0

        if ymax <= ymin:
            span = max(1.0, abs(ymax))
            ymin -= 0.5 * span
            ymax += 0.5 * span
        return max((ymax - ymin) / 8.0, 1e-12)

    def _fmt_frequency(self, hz: Optional[float]) -> str:
        if hz is None:
            return "-"
        try:
            hz = float(hz)
        except (TypeError, ValueError):
            return "-"
        if abs(hz) >= 1e9:
            return f"{hz / 1e9:.6f} GHz"
        if abs(hz) >= 1e6:
            return f"{hz / 1e6:.6f} MHz"
        if abs(hz) >= 1e3:
            return f"{hz / 1e3:.6f} kHz"
        return f"{hz:.6f} Hz"

    def _pk_pk(self, snap: TraceSnapshot) -> Optional[float]:
        if np is not None:
            combined = np.concatenate((np.asarray(snap.i_values).ravel(), np.asarray(snap.q_values).ravel()))
            finite = combined[np.isfinite(combined)]
            if finite.size:
                return float(np.max(finite) - np.min(finite))
            return None

        vals = []
        for value in list(snap.i_values) + list(snap.q_values):
            try:
                v = float(value)
            except Exception:
                continue
            if math.isfinite(v):
                vals.append(v)
        if vals:
            return max(vals) - min(vals)
        return None

    def _fmt_number(self, value: Optional[float]) -> str:
        if value is None:
            return "-"
        try:
            value = float(value)
        except (TypeError, ValueError):
            return "-"
        return f"{value:.6g}"

    def _draw_grid(self, w, h, pad_l, pad_r, pad_t, pad_b, ymin, ymax):
        plot_w = w - pad_l - pad_r
        plot_h = h - pad_t - pad_b
        x0, y0 = pad_l, pad_t
        x1, y1 = pad_l + plot_w, pad_t + plot_h
        self._canvas.create_rectangle(x0, y0, x1, y1, outline="#444444")
        for i in range(1, 5):
            x = x0 + int(plot_w * i / 5)
            self._canvas.create_line(x, y0, x, y1, fill="#222222")
        for i in range(1, 4):
            y = y0 + int(plot_h * i / 4)
            self._canvas.create_line(x0, y, x1, y, fill="#222222")
        self._canvas.create_text(6, y0, anchor="nw", fill="#aaaaaa", text=f"{ymax:.4g}")
        self._canvas.create_text(6, y1, anchor="sw", fill="#aaaaaa", text=f"{ymin:.4g}")
        self._canvas.create_text(6, y0 + plot_h // 2, anchor="w", fill="#777777", text="Amplitude")
        if ymin < 0.0 < ymax:
            y_zero = y0 + int((1.0 - ((0.0 - ymin) / (ymax - ymin))) * plot_h)
            self._canvas.create_line(x0, y_zero, x1, y_zero, fill="#555555")

    def _draw_trace(self, values, color, pad_l, pad_t, plot_w, plot_h, ymin, ymax):
        n = len(values)
        if n <= 1:
            return
        max_points = max(2, plot_w)
        stride = max(1, int(math.ceil(n / max_points)))
        denom = max(1, n - 1)
        yrange = ymax - ymin if ymax > ymin else 1.0
        points = []
        for idx in range(0, n, stride):
            try:
                val = float(values[idx])
            except Exception:
                continue
            if not math.isfinite(val):
                continue
            x = pad_l + int(idx * plot_w / denom)
            y_norm = (val - ymin) / yrange
            y_norm = 0.0 if y_norm < 0.0 else (1.0 if y_norm > 1.0 else y_norm)
            y = pad_t + int((1.0 - y_norm) * plot_h)
            points.extend((x, y))
        if len(points) >= 4:
            self._canvas.create_line(*points, fill=color, width=1)

    def _cursor_update(self, event):
        snap = self._latest_snapshot
        if snap is None:
            return
        w = max(20, self._canvas.winfo_width())
        pad_l, pad_r = 56, 12
        plot_w = max(1, w - pad_l - pad_r)
        x = min(max(event.x, pad_l), w - pad_r)
        n = len(snap.i_values)
        if n <= 1:
            return
        idx = int(round((x - pad_l) * (n - 1) / plot_w))
        idx = min(max(idx, 0), n - 1)
        t_ms = idx / snap.sample_rate_hz * 1000.0
        try:
            i_val = float(snap.i_values[idx])
            q_val = float(snap.q_values[idx])
        except Exception:
            return
        self._vars["cursor"].set(f"Cursor: t={t_ms:.6f} ms  I={i_val:.6g}  Q={q_val:.6g}")

    def _on_close(self):
        try:
            self._reader.stop()
        except Exception:
            traceback.print_exc()
        self.root.destroy()


def parse_args():
    parser = argparse.ArgumentParser(description="Read-only DigitalRF oscilloscope GUI for MEP ringbuffers.")
    parser.add_argument("--root", default=DEFAULT_ROOT, help="Ringbuffer root containing named buffer directories.")
    parser.add_argument("--name", default=DEFAULT_NAME, help="Initial named buffer directory under the root.")
    parser.add_argument("--sample-rate-mhz", default=DEFAULT_SAMPLE_RATE_MHZ, help="Initial sample rate in MHz.")
    parser.add_argument("--channel", default=DEFAULT_CHANNEL, choices=CHANNEL_OPTIONS, help="Initial channel.")
    parser.add_argument("--width-ms", default=DEFAULT_WIDTH_MS, type=float, help="Horizontal display width in ms.")
    parser.add_argument("--horizontal-position-ms", default=0.0, type=float, help="Time from left edge to trigger/latest reference.")
    parser.add_argument("--units-per-div", default=DEFAULT_UNITS_PER_DIV, type=float, help="Vertical units per division.")
    parser.add_argument("--trigger", default="Free Run", choices=TRIGGER_OPTIONS, help="Trigger mode.")
    parser.add_argument("--trigger-level", default=0.0, type=float, help="Trigger crossing level in sample units.")
    parser.add_argument("--refresh-ms", default=DEFAULT_REFRESH_MS, type=int, help="Reader refresh interval.")
    parser.add_argument("--lag-ms", default=DEFAULT_LAG_MS, type=float, help="Read this far behind latest bound.")
    return parser.parse_args()


def main():
    args = parse_args()
    root = tk.Tk()
    MEPScopeGui(root, args)
    root.mainloop()


if __name__ == "__main__":
    main()
