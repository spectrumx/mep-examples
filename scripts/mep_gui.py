#!/usr/bin/env python3
"""
mep_gui.py

Tkinter GUI for MEP RFSoC sweep/record control via X11 forwarding.

Wraps MEPController from start_mep_rx.py.

Usage:
    ssh -X mep@<jetson> python3 ~/mep-examples/scripts/mep_gui.py

Author: john.marino@colorado.edu
"""

import sys
import os
import re
import math
import json
import socket
import queue
import threading
import logging
import tkinter as tk
from tkinter import ttk, scrolledtext
import paho.mqtt.publish as mqtt_publish
import paho.mqtt.client as mqtt_client

# Allow importing start_mep_rx from the same directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from start_mep_rx import (
    MEPController,
    get_frequency_list,
    TUNER_INJECTION_SIDE,
    RFSOC_CMD_TOPIC,
    RECORDER_CMD_TOPIC,
    RECORDER_STATUS_TOPIC,
    TUNER_CMD_TOPIC,
    TUNER_STATUS_TOPIC,
    RFSOC_STATUS_TOPIC,
    MQTT_BROKER,
    MQTT_PORT,
)


# ===== AFE HARDWARE CONSTANTS ===== #

_AFE_SOCKET = "/tmp/afe_service.sock"

# Main block register map (block=0, channel=-1)
# Each entry: (addr, gui_label, hardware_default_value, gui_inverted)
# gui_inverted=True when BoolVar True maps to hardware 0 (complementary sense)
_AFE_MAIN_REGS = [
    (0, "External TX Trigger Source",  1, False),
    (1, "External RX Trigger Source",  1, False),
    (2, "External TX Trigger Enabled", 1, True),   # 0=enable (complementary)
    (3, "External RX Trigger Enabled", 1, True),   # 0=enable (complementary)
    (5, "External Bias Enabled",       0, False),
    (7, "GNSS PPS Source",             1, False),
    (8, "OCXO Reference Source",       1, False),
    # addr 9 (GNSS_ANT_SEL) handled separately as radio buttons
]

# TX block register map (block=1, channel=1-2, shared)
# Each entry: (addr, gui_label, hardware_default_value, gui_inverted)
_AFE_TX_REGS = [
    (1, "Transmitter Not Blanked", 1, False),  # 1=not blanked
    (2, "Bypass TX Filters",       1, False),  # 1=bypass
]

# RX block register map (block=2, channel=1-4, shared)
_AFE_RX_REGS = [
    (0, "Channel Bias Enabled",  0, True),   # 0=enabled (complementary)
    (1, "Internal RF Trigger",   0, False),
    (2, "Route Through Filters", 1, False),  # 1=through filters
    (3, "Amplifier Enabled",     1, False),  # 1=enabled
    # addrs 4-8 are attenuation bits; handled via Spinbox
]


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
    CHANNEL_OPTIONS     = ["A", "B", "C", "D"]
    TUNER_OPTIONS       = ["None"] + list(TUNER_INJECTION_SIDE.keys()) + ["auto"]
    RECORDER_CONFIG_DIR = "/opt/radiohound/docker/recorder/configs"
    DEFAULT_SAMPLE_RATE_OPTIONS = ["1", "2", "4", "8", "10", "16", "20", "32", "64"]
    SAMPLE_RATE_OPTIONS = DEFAULT_SAMPLE_RATE_OPTIONS.copy()

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("MEP Control App")
        self.root.resizable(True, True)

        # Read available recorder configs (sr{N}MHz.yaml) and present N values.
        self.SAMPLE_RATE_OPTIONS = self._discover_sample_rate_options()

        self.mep: MEPController = None
        self._sweep_thread: threading.Thread = None
        self._afe_updating = False  # suppresses trace callbacks during state load
        self._gpsd_cmd_queue = queue.Queue()
        self._gpsd_run = True
        self._gps_state = self._gps_state_defaults()
        self._gps_fix_hints = {
            "tpv_mode": None,
            "gga_quality": None,
            "gsa_mode": None,
            "rmc_valid": None,
        }
        self._gps_gsv_partial = {}
        self._gps_gsv_last_complete = {}

        self._build_ui()
        self._setup_logging()
        self._start_status_monitor()
        self._start_gps_monitor()          # GPSD: single shared stream for GPS tab + fields
        self._schedule_poll()
        self.root.after(2000, self._tun_refresh)   # TUN: read monitor cache once connected
        self.root.after(2000, self._rec_status_seed)  # REC: seed status from cache
        self.root.after(3000, self._afe_refresh)   # AFE/TLM: load true hardware state

    def _discover_sample_rate_options(self) -> list[str]:
        """Return sorted sample-rate strings discovered from recorder config filenames."""
        pattern = re.compile(r"^sr(\d+)MHz\.yaml$")
        rates = set()

        try:
            for name in os.listdir(self.RECORDER_CONFIG_DIR):
                match = pattern.match(name)
                if match:
                    rates.add(int(match.group(1)))
        except OSError as e:
            logging.warning(
                "Could not read recorder config directory '%s': %s. Using default sample rates.",
                self.RECORDER_CONFIG_DIR,
                e,
            )
            return self.DEFAULT_SAMPLE_RATE_OPTIONS.copy()

        if not rates:
            logging.warning(
                "No sample-rate configs matching 'sr{N}MHz.yaml' in '%s'. Using default sample rates.",
                self.RECORDER_CONFIG_DIR,
            )
            return self.DEFAULT_SAMPLE_RATE_OPTIONS.copy()

        return [str(rate) for rate in sorted(rates)]

    def _default_sample_rate(self) -> str:
        """Prefer 10 MHz when available, otherwise use the first discovered option."""
        if "10" in self.SAMPLE_RATE_OPTIONS:
            return "10"
        return self.SAMPLE_RATE_OPTIONS[0]

    # ------------------------------------------------------------------ #
    #  Direct gpsd monitor                                                #
    # ------------------------------------------------------------------ #

    def _gps_state_defaults(self) -> dict:
        return {
            "gpsd_conn_status": "Connecting...",
            "gpsd_device": "Not reported",
            "gpsd_driver": "Not reported",
            "gpsd_baud": "Not reported",
            "gpsd_update_rate_s": "Not reported",
            "gpsd_watch_state": "Not reported",
            "gps_summary": "Waiting for gpsd data",
            "gps_fix_status": "Unknown",
            "gps_fix_quality": "Not reported",
            "gps_utc_time": "Unknown",
            "gps_lat": "Unknown",
            "gps_lon": "Unknown",
            "gps_alt_m": "Not reported",
            "gps_speed_kn": "0.000",
            "gps_sats_visible": "0",
            "gps_sats_used": "0",
            "gps_sats_gps": "0",
            "gps_sats_glonass": "0",
            "gps_sats_galileo": "0",
            "gps_sats_beidou": "0",
            "gps_pdop": "Not reported",
            "gps_hdop": "Not reported",
            "gps_vdop": "Not reported",
        }

    def _gps_set(self, key: str, value, *, allow_empty: bool = False, force: bool = False):
        """Set one semantic GPS state key safely and update matching UI field."""
        if value is None and not force:
            return
        val = "" if value is None else str(value)
        if (not allow_empty) and (not val.strip()) and (not force):
            return
        self._gps_state[key] = val
        if key in self._vars:
            self._vars[key].set(val)

    def _gps_set_float(self, key: str, value, fmt: str):
        try:
            self._gps_set(key, format(float(value), fmt))
        except Exception:
            return

    def _gps_nmea_to_decimal(self, raw: str, hemi: str):
        """Convert ddmm.mmmm / dddmm.mmmm NMEA coordinates to decimal degrees."""
        if not raw:
            return None
        try:
            v = float(raw)
        except Exception:
            return None

        deg = int(v // 100)
        minutes = v - (deg * 100)
        decimal = deg + minutes / 60.0
        hemi = (hemi or "").upper()
        if hemi in ("S", "W"):
            decimal *= -1.0
        return decimal

    def _gps_fmt_hms(self, hhmmss: str):
        if not hhmmss or len(hhmmss) < 6:
            return None
        core = hhmmss.split(".")[0]
        if len(core) < 6:
            return None
        return f"{core[0:2]}:{core[2:4]}:{core[4:6]}Z"

    def _gps_fmt_iso_from_rmc(self, hhmmss: str, ddmmyy: str):
        if not hhmmss or not ddmmyy or len(ddmmyy) != 6:
            return self._gps_fmt_hms(hhmmss)
        t = self._gps_fmt_hms(hhmmss)
        if t is None:
            return None
        day = ddmmyy[0:2]
        month = ddmmyy[2:4]
        year = int(ddmmyy[4:6])
        year += 2000 if year < 80 else 1900
        return f"{year:04d}-{month}-{day}T{t}"

    def _gps_fix_quality_text(self, quality: int):
        table = {
            0: "Invalid",
            1: "GPS",
            2: "DGPS",
            3: "PPS",
            4: "RTK",
            5: "Float RTK",
            6: "Estimated",
            7: "Manual",
            8: "Simulation",
        }
        return table.get(quality, f"Quality {quality}")

    def _gps_recompute_fix_and_summary(self):
        """Resolve cross-source fix precedence and publish a stable summary."""
        h = self._gps_fix_hints

        fix = "Unknown"
        if h.get("rmc_valid") is False:
            fix = "No fix"
        elif isinstance(h.get("gga_quality"), int):
            q = h["gga_quality"]
            fix = "No fix" if q <= 0 else self._gps_fix_quality_text(q)
        elif isinstance(h.get("tpv_mode"), int):
            mode = h["tpv_mode"]
            if mode <= 1:
                fix = "No fix"
            elif mode == 2:
                fix = "2D"
            else:
                fix = "3D"
        elif isinstance(h.get("gsa_mode"), int):
            mode = h["gsa_mode"]
            if mode <= 1:
                fix = "No fix"
            elif mode == 2:
                fix = "2D"
            else:
                fix = "3D"

        self._gps_set("gps_fix_status", fix, force=True)
        if fix == "No fix":
            self._gps_set("gps_fix_quality", "Invalid", force=True)

        summary = (
            f"{self._gps_state.get('gpsd_conn_status', 'Unknown')} | "
            f"Fix: {self._gps_state.get('gps_fix_status', 'Unknown')} | "
            f"Sats used/visible: {self._gps_state.get('gps_sats_used', '0')}/"
            f"{self._gps_state.get('gps_sats_visible', '0')} | "
            f"Device: {self._gps_state.get('gpsd_device', 'Not reported')}"
        )
        self._gps_set("gps_summary", summary, force=True)

    def _start_gps_monitor(self):
        """Run one persistent gpsd stream and fan out data to the GPSD tab/UI."""
        import time as _time

        def _worker():
            while self._gpsd_run:
                try:
                    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                        s.settimeout(5.0)
                        s.connect(("127.0.0.1", 2947))
                        s.settimeout(0.5)
                        self.root.after(0, lambda: self._gps_set("gpsd_conn_status", "Connected", force=True))

                        # Default stream includes raw NMEA and JSON classes.
                        s.sendall(b'?WATCH={"enable":true,"raw":1};\n')
                        self.root.after(
                            0, lambda: self._gpsd_log("TX", '?WATCH={"enable":true,"raw":1};'))
                        self.root.after(0, lambda: self._gps_set("gpsd_watch_state", "enabled raw=1", force=True))

                        buf = ""
                        while self._gpsd_run:
                            try:
                                while True:
                                    cmd = self._gpsd_cmd_queue.get_nowait().strip()
                                    if not cmd:
                                        continue
                                    if not cmd.endswith(";"):
                                        cmd += ";"
                                    wire = (cmd + "\n").encode("ascii", errors="ignore")
                                    s.sendall(wire)
                                    self.root.after(0, lambda c=cmd: self._gpsd_log("TX", c))
                            except queue.Empty:
                                pass

                            try:
                                data = s.recv(4096)
                            except socket.timeout:
                                continue

                            if not data:
                                raise ConnectionError("gpsd closed socket")

                            buf += data.decode("ascii", errors="ignore")
                            while "\n" in buf:
                                line, buf = buf.split("\n", 1)
                                line = line.strip()
                                if not line:
                                    continue
                                self.root.after(0, lambda l=line: self._gpsd_handle_line(l))
                except Exception as e:
                    self.root.after(0, lambda err=str(e): self._gps_set("gpsd_conn_status", f"Disconnected ({err})", force=True))
                    self.root.after(0, lambda: self._gps_set("gps_fix_status", "Unknown", force=True))
                    self.root.after(0, self._gps_recompute_fix_and_summary)
                    _time.sleep(5)

        threading.Thread(target=_worker, daemon=True, name="gps_monitor").start()

    def _gpsd_log(self, direction: str, line: str):
        if not hasattr(self, "_gpsd_text"):
            return
        import datetime
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        self._gpsd_text.insert("end", f"{ts}  {direction}  {line}\n")
        self._gpsd_text.see("end")
        lines = int(self._gpsd_text.index("end-1c").split(".")[0])
        if lines > 800:
            self._gpsd_text.delete("1.0", f"{lines - 800}.0")

    def _gpsd_handle_line(self, line: str):
        self._gpsd_log("RX", line)

        if line.startswith("$"):
            self._gps_parse_nmea(line)
            return

        if line.startswith("{"):
            try:
                msg = json.loads(line)
            except Exception:
                return
            self._gps_apply_json(msg)

    def _gps_apply_json(self, msg: dict):
        """Apply optional gpsd JSON updates into semantic GPS state keys."""
        if not isinstance(msg, dict):
            return

        cls = str(msg.get("class", "")).upper()
        if not cls:
            return

        if cls == "DEVICE":
            self._gps_set("gpsd_device", msg.get("path"))
            self._gps_set("gpsd_driver", msg.get("driver"))
            if msg.get("bps") is not None:
                self._gps_set("gpsd_baud", str(int(msg.get("bps"))))
            if msg.get("cycle") is not None:
                self._gps_set_float("gpsd_update_rate_s", msg.get("cycle"), ".2f")

        elif cls == "DEVICES":
            devs = msg.get("devices")
            if isinstance(devs, list) and devs:
                first = devs[0] if isinstance(devs[0], dict) else {}
                self._gps_apply_json(dict(first, **{"class": "DEVICE"}))

        elif cls == "WATCH":
            enabled = msg.get("enable")
            raw = msg.get("raw")
            nmea = msg.get("nmea")
            state = "Not reported"
            if enabled is not None:
                state = "enabled" if bool(enabled) else "disabled"
                if raw is not None:
                    state += f" raw={raw}"
                if nmea is not None:
                    state += f" nmea={bool(nmea)}"
            self._gps_set("gpsd_watch_state", state, force=True)

        elif cls == "VERSION":
            rel = msg.get("release")
            rev = msg.get("rev")
            if rel:
                txt = f"gpsd {rel}"
                if rev:
                    txt += f" ({rev})"
                self._gps_set("gpsd_driver", txt, force=True)

        elif cls == "TPV":
            if msg.get("mode") is not None:
                try:
                    self._gps_fix_hints["tpv_mode"] = int(msg.get("mode"))
                except Exception:
                    pass

            if msg.get("time"):
                self._gps_set("gps_utc_time", msg.get("time"))
            if msg.get("lat") is not None:
                self._gps_set_float("gps_lat", msg.get("lat"), ".6f")
            if msg.get("lon") is not None:
                self._gps_set_float("gps_lon", msg.get("lon"), ".6f")
            if msg.get("alt") is not None:
                self._gps_set_float("gps_alt_m", msg.get("alt"), ".2f")
            if msg.get("speed") is not None:
                self._gps_set_float("gps_speed_kn", float(msg.get("speed")) * 1.943844, ".3f")

        elif cls == "SKY":
            sats = msg.get("satellites")
            if isinstance(sats, list):
                visible = len(sats)
                used = 0
                counts = {
                    "GPS": 0,
                    "GLONASS": 0,
                    "GALILEO": 0,
                    "BEIDOU": 0,
                }
                for sat in sats:
                    if not isinstance(sat, dict):
                        continue
                    if sat.get("used") is True:
                        used += 1
                    gnssid = sat.get("gnssid")
                    if gnssid == 0:
                        counts["GPS"] += 1
                    elif gnssid == 2:
                        counts["GALILEO"] += 1
                    elif gnssid == 3:
                        counts["BEIDOU"] += 1
                    elif gnssid == 6:
                        counts["GLONASS"] += 1

                self._gps_set("gps_sats_visible", str(visible), force=True)
                self._gps_set("gps_sats_used", str(used), force=True)
                self._gps_set("gps_sats_gps", str(counts["GPS"]), force=True)
                self._gps_set("gps_sats_glonass", str(counts["GLONASS"]), force=True)
                self._gps_set("gps_sats_galileo", str(counts["GALILEO"]), force=True)
                self._gps_set("gps_sats_beidou", str(counts["BEIDOU"]), force=True)

            if msg.get("pdop") is not None:
                self._gps_set_float("gps_pdop", msg.get("pdop"), ".2f")
            if msg.get("hdop") is not None:
                self._gps_set_float("gps_hdop", msg.get("hdop"), ".2f")
            if msg.get("vdop") is not None:
                self._gps_set_float("gps_vdop", msg.get("vdop"), ".2f")

        self._gps_recompute_fix_and_summary()

    def _gpsd_send_command(self, cmd: str):
        """Queue one raw gpsd command for the single monitor connection."""
        self._gpsd_cmd_queue.put(cmd)

    def _gpsd_send_manual(self):
        cmd = self._vars["gpsd_cmd"].get().strip()
        if not cmd:
            logging.error("GPSD: command is empty")
            return
        self._gpsd_send_command(cmd)

    def _gpsd_watch_raw_on(self):
        self._gpsd_send_command('?WATCH={"enable":true,"raw":1}')
        self._gps_set("gpsd_watch_state", "enabled raw=1", force=True)

    def _gpsd_watch_off(self):
        self._gpsd_send_command('?WATCH={"enable":false}')
        self._gps_set("gpsd_watch_state", "disabled", force=True)

    def _gps_parse_nmea(self, line: str):
        clean = line.split("*")[0]
        body = clean.lstrip("$")
        parts = body.split(",")
        if not parts:
            return
        talker_sentence = parts[0]
        if len(talker_sentence) < 3:
            return
        talker = talker_sentence[:2]
        sentence = talker_sentence[2:]

        if sentence == "RMC":
            self._gps_parse_rmc(parts)
        elif sentence == "GGA":
            self._gps_parse_gga(parts)
        elif sentence == "GSA":
            self._gps_parse_gsa(parts)
        elif sentence == "GSV":
            self._gps_parse_gsv(talker, parts)
        elif sentence == "ZDA":
            self._gps_parse_zda(parts)

        self._gps_recompute_fix_and_summary()

    def _gps_parse_rmc(self, parts: list):
        if len(parts) < 10:
            return
        t_utc = self._gps_fmt_iso_from_rmc(parts[1], parts[9])
        if t_utc:
            self._gps_set("gps_utc_time", t_utc)

        status = (parts[2] or "").upper()
        if status:
            self._gps_fix_hints["rmc_valid"] = (status == "A")

        lat = self._gps_nmea_to_decimal(parts[3], parts[4])
        lon = self._gps_nmea_to_decimal(parts[5], parts[6])
        if lat is not None:
            self._gps_set("gps_lat", f"{lat:.6f}")
        if lon is not None:
            self._gps_set("gps_lon", f"{lon:.6f}")

        if parts[7]:
            self._gps_set_float("gps_speed_kn", parts[7], ".3f")

    def _gps_parse_gga(self, parts: list):
        if len(parts) < 10:
            return
        t_utc = self._gps_fmt_hms(parts[1])
        if t_utc:
            self._gps_set("gps_utc_time", t_utc)

        lat = self._gps_nmea_to_decimal(parts[2], parts[3])
        lon = self._gps_nmea_to_decimal(parts[4], parts[5])
        if lat is not None:
            self._gps_set("gps_lat", f"{lat:.6f}")
        if lon is not None:
            self._gps_set("gps_lon", f"{lon:.6f}")

        if parts[6]:
            try:
                q = int(parts[6])
                self._gps_fix_hints["gga_quality"] = q
                self._gps_set("gps_fix_quality", self._gps_fix_quality_text(q), force=True)
            except Exception:
                pass

        if parts[7]:
            try:
                self._gps_set("gps_sats_used", str(int(parts[7])), force=True)
            except Exception:
                pass

        if parts[8]:
            self._gps_set_float("gps_hdop", parts[8], ".2f")

        if parts[9]:
            self._gps_set_float("gps_alt_m", parts[9], ".2f")

    def _gps_parse_gsa(self, parts: list):
        if len(parts) < 18:
            return
        if parts[2]:
            try:
                self._gps_fix_hints["gsa_mode"] = int(parts[2])
            except Exception:
                pass

        used = 0
        for sv in parts[3:15]:
            if sv.strip():
                used += 1
        self._gps_set("gps_sats_used", str(used), force=True)

        if parts[15]:
            self._gps_set_float("gps_pdop", parts[15], ".2f")
        if parts[16]:
            self._gps_set_float("gps_hdop", parts[16], ".2f")
        if parts[17]:
            self._gps_set_float("gps_vdop", parts[17], ".2f")

    def _gps_constellation_from_prn(self, talker: str, prn: int):
        talker = (talker or "").upper()
        if talker == "GP":
            return "GPS"
        if talker == "GL":
            return "GLONASS"
        if talker == "GA":
            return "GALILEO"
        if talker in ("GB", "BD"):
            return "BEIDOU"
        if 65 <= prn <= 96:
            return "GLONASS"
        if 201 <= prn <= 237:
            return "BEIDOU"
        if 301 <= prn <= 336:
            return "GALILEO"
        return "GPS"

    def _gps_parse_gsv(self, talker: str, parts: list):
        if len(parts) < 4:
            return
        try:
            total_msgs = int(parts[1] or 0)
            msg_num = int(parts[2] or 0)
            total_visible = int(parts[3] or 0)
        except Exception:
            return

        if total_msgs <= 0 or msg_num <= 0:
            return

        key = talker.upper()
        cycle = self._gps_gsv_partial.get(key)
        if cycle is None or msg_num == 1 or cycle.get("expected") != total_msgs:
            cycle = {
                "expected": total_msgs,
                "seen": set(),
                "visible": total_visible,
                "counts": {"GPS": 0, "GLONASS": 0, "GALILEO": 0, "BEIDOU": 0},
            }
            self._gps_gsv_partial[key] = cycle

        cycle["seen"].add(msg_num)
        cycle["visible"] = max(cycle["visible"], total_visible)

        idx = 4
        while idx + 3 < len(parts):
            prn_txt = parts[idx].strip()
            if prn_txt:
                try:
                    prn = int(prn_txt)
                    const = self._gps_constellation_from_prn(key, prn)
                    if const in cycle["counts"]:
                        cycle["counts"][const] += 1
                except Exception:
                    pass
            idx += 4

        # Update global counters only when cycle is complete to avoid flicker.
        if len(cycle["seen"]) >= cycle["expected"]:
            self._gps_gsv_last_complete[key] = {
                "visible": cycle["visible"],
                "counts": dict(cycle["counts"]),
            }
            self._gps_gsv_partial.pop(key, None)

            totals = {"GPS": 0, "GLONASS": 0, "GALILEO": 0, "BEIDOU": 0}
            total_visible_all = 0
            for data in self._gps_gsv_last_complete.values():
                total_visible_all += int(data.get("visible", 0))
                for c in totals:
                    totals[c] += int(data.get("counts", {}).get(c, 0))

            if total_visible_all > 0:
                self._gps_set("gps_sats_visible", str(total_visible_all), force=True)
            self._gps_set("gps_sats_gps", str(totals["GPS"]), force=True)
            self._gps_set("gps_sats_glonass", str(totals["GLONASS"]), force=True)
            self._gps_set("gps_sats_galileo", str(totals["GALILEO"]), force=True)
            self._gps_set("gps_sats_beidou", str(totals["BEIDOU"]), force=True)

    def _gps_parse_zda(self, parts: list):
        if len(parts) < 5:
            return
        t = self._gps_fmt_hms(parts[1])
        d = parts[2]
        m = parts[3]
        y = parts[4]
        if t and d and m and y:
            self._gps_set("gps_utc_time", f"{y}-{m.zfill(2)}-{d.zfill(2)}T{t}")

    # ------------------------------------------------------------------ #
    #  Background MQTT status monitor                                      #
    # ------------------------------------------------------------------ #

    def _start_status_monitor(self):
        """Connect a persistent background MQTT client that logs every state
        change from the recorder, RFSoC, and tuner services.
        Active from GUI launch, independent of any sweep.
        """
        self._monitor_states = {}   # last seen state per topic

        def _on_connect(client, userdata, flags, reason_code, properties):
            rc_value = getattr(reason_code, "value", reason_code)
            try:
                rc_num = int(rc_value)
            except (TypeError, ValueError):
                rc_num = None
            is_failure = getattr(reason_code, "is_failure", None)
            connected = (not is_failure) if isinstance(is_failure, bool) else (rc_num == 0)

            if connected:
                client.subscribe("#")   # wildcard – all topics
            else:
                logging.warning(
                    f"Status monitor MQTT connect failed: rc={rc_num} ({reason_code})")

        def _on_message(client, userdata, msg):
            # ---- MQTT tab live log ---- #
            self.root.after(0, lambda t=msg.topic, p=msg.payload:
                self._mqtt_log_message(t, p))

            try:
                data = json.loads(msg.payload.decode())
            except Exception:
                return

            if not isinstance(data, dict):
                return

            topic   = msg.topic
            state   = data.get("state", "—")
            prev    = self._monitor_states.get(topic)

            if topic == RECORDER_STATUS_TOPIC:
                prev_state = prev.get("state") if isinstance(prev, dict) else prev
                if state != prev_state:
                    logging.info(f"Recorder: {state}")
                self._monitor_states[topic] = data
                self.root.after(0, lambda d=data: self._rec_status_update(d))

            elif topic == RFSOC_STATUS_TOPIC:
                f_c = float(data.get("f_c_hz", 0)) / 1e6
                pps = data.get("pps_count", "?")
                key = (state, round(f_c, 2))
                if key != prev:
                    logging.info(
                        f"RFSoC: {state}  f_c={f_c:.2f} MHz  pps={pps}")
                    self._monitor_states[topic] = key

            elif topic == TUNER_STATUS_TOPIC:
                # Distinguish response messages (get_freq/get_power reply) from
                # full state updates.  Response messages carry 'task_name'+'value'
                # but no 'state' key; don't let them overwrite the state cache.
                if "task_name" in data and "value" in data and "state" not in data:
                    self.root.after(
                        0, lambda d=data: self._tun_handle_response(d))
                else:
                    prev_state = prev.get("state") if isinstance(prev, dict) else prev
                    if state != prev_state:
                        logging.info(f"Tuner: {state}")
                    self._monitor_states[topic] = data
                    self.root.after(0, self._tun_refresh)

        mon = mqtt_client.Client(
            callback_api_version=mqtt_client.CallbackAPIVersion.VERSION2,
            client_id="mep_gui_monitor",
        )
        mon.on_connect = _on_connect
        mon.on_message = _on_message
        try:
            mon.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
            mon.loop_start()
            self._monitor_client = mon
        except Exception as e:
            logging.warning(f"Status monitor could not connect: {e}")

    # ------------------------------------------------------------------ #
    #  UI Construction                                                     #
    # ------------------------------------------------------------------ #

    def _build_ui(self):
        self.root.columnconfigure(0, weight=1)
        self.root.columnconfigure(1, weight=0)
        self.root.rowconfigure(0, weight=1)

        self._vars = {}

        # ---- Left pane ---- #
        left = ttk.Frame(self.root)
        left.grid(row=0, column=0, sticky="nsew")
        left.columnconfigure(0, weight=1)
        left.rowconfigure(5, weight=1)  # Log row expands

        self._build_tune_section(left, row=0)
        self._build_record_section(left, row=1)
        self._build_updown_section(left, row=2)
        self._build_control_section(left, row=3)

        # ---- Status bar ---- #
        status_frame = ttk.LabelFrame(left, text="Status")
        status_frame.grid(row=4, column=0, padx=10, pady=4, sticky="ew")
        status_frame.columnconfigure(0, weight=1)
        self._status_var = tk.StringVar(value="Idle — no controller connected")
        ttk.Label(status_frame, textvariable=self._status_var,
                  anchor="w").grid(row=0, column=0, padx=6, pady=3, sticky="ew")

        # ---- Log box ---- #
        log_frame = ttk.LabelFrame(left, text="Log")
        log_frame.grid(row=5, column=0, padx=10, pady=6, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)

        self._log_text = scrolledtext.ScrolledText(
            log_frame, height=16, width=80, state="disabled",
            font=("Courier", 9),
        )
        self._log_text.grid(row=0, column=0, padx=5, pady=5, sticky="nsew")
        self._bind_copy_menu(self._log_text)

        # ---- Right pane: Advanced Options ---- #
        self._build_advanced_section(self.root)

    # ---- Section builders ---- #

    def _build_tune_section(self, parent: ttk.Frame, row: int):
        """Tune section: Single/Sweep sub-tabs."""
        frame = ttk.LabelFrame(parent, text="Tune")
        frame.grid(row=row, column=0, padx=10, pady=6, sticky="ew")
        frame.columnconfigure(0, weight=1)

        self._tune_notebook = ttk.Notebook(frame)
        self._tune_notebook.grid(row=0, column=0, padx=5, pady=5, sticky="ew")

        single_f = ttk.Frame(self._tune_notebook, padding=8)
        sweep_f  = ttk.Frame(self._tune_notebook, padding=8)
        self._tune_notebook.add(single_f, text="Single")
        self._tune_notebook.add(sweep_f,  text="Sweep")

        # Single tab: Freq only
        single_f.columnconfigure(1, weight=1)
        self._vars["freq_start"] = tk.StringVar(value="7000")
        ttk.Label(single_f, text="Freq (MHz)").grid(
            row=0, column=0, sticky="w", padx=5, pady=4)
        ttk.Entry(single_f, textvariable=self._vars["freq_start"], width=20).grid(
            row=0, column=1, sticky="ew", padx=5, pady=4)

        # Sweep tab: Start, End, Step, Dwell
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
        """Record section: Channel, Sample Rate, Capture Name."""
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
            values=self.CHANNEL_OPTIONS, width=16, state="readonly",
        ).grid(row=1, column=1, sticky="ew", padx=5, pady=4)

        ttk.Label(frame, text="Sample Rate (MHz)").grid(
            row=1, column=2, sticky="w", padx=5, pady=4)
        self._vars["sample_rate"] = tk.StringVar(value=self._default_sample_rate())
        ttk.Combobox(
            frame, textvariable=self._vars["sample_rate"],
            values=self.SAMPLE_RATE_OPTIONS, width=16, state="readonly",
        ).grid(row=1, column=3, sticky="ew", padx=5, pady=4)

    def _build_updown_section(self, parent: ttk.Frame, row: int):
        """Up/Down Convert section: Tuner, RFSoC IF, Injection Mode, Synth LO."""
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
            values=self.TUNER_OPTIONS, width=16, state="readonly",
        ).grid(row=0, column=1, sticky="ew", padx=5, pady=4)

        ttk.Label(frame, text="RFSoC IF (MHz)").grid(
            row=0, column=2, sticky="w", padx=5, pady=4)
        self._vars["adc_if"] = tk.StringVar(value="1090")
        self._if_entry = ttk.Entry(
            frame, textvariable=self._vars["adc_if"], width=16, state="disabled")
        self._if_entry.grid(row=0, column=3, sticky="ew", padx=5, pady=4)

        # Row 1: Injection Mode | Synth LO (read-only)
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

        # Enable/disable IF entry; recalculate LO whenever relevant vars change
        self._vars["tuner"].trace_add("write", self._on_tuner_change)
        self._vars["freq_start"].trace_add("write", self._update_synth_lo)
        self._vars["adc_if"].trace_add("write", self._update_synth_lo)
        self._vars["injection_mode"].trace_add("write", self._update_synth_lo)
        self._update_synth_lo()  # initial display

    def _build_advanced_section(self, parent: ttk.Frame):
        """Advanced Options section: AFE and REC tabs (right column, hidden by default)."""
        frame = ttk.LabelFrame(parent, text="Advanced Options")
        frame.grid(row=0, column=1, padx=(0, 10), pady=6, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        self._adv_frame = frame
        frame.grid_remove()  # hidden until the user toggles it

        nb = ttk.Notebook(frame)
        nb.grid(row=0, column=0, padx=5, pady=5, sticky="nsew")

        afe_f = ttk.Frame(nb, padding=8)
        rec_f = ttk.Frame(nb, padding=8)
        tlm_f = ttk.Frame(nb, padding=8)
        soc_f = ttk.Frame(nb, padding=8)
        tun_f = ttk.Frame(nb, padding=8)
        mqtt_f = ttk.Frame(nb, padding=8)
        gpsd_f = ttk.Frame(nb, padding=8)
        nb.add(afe_f, text="AFE")
        nb.add(rec_f, text="REC")
        nb.add(gpsd_f, text="GPSD")
        nb.add(tlm_f, text="TLM")
        nb.add(soc_f, text="SOC")
        nb.add(tun_f, text="TUN")
        nb.add(mqtt_f, text="MQTT")

        self._build_afe_tab(afe_f)
        self._build_rec_tab(rec_f)
        self._build_gpsd_tab(gpsd_f)
        self._build_tlm_tab(tlm_f)
        self._build_soc_tab(soc_f)
        self._build_tun_tab(tun_f)
        self._build_mqtt_tab(mqtt_f)

    def _build_gpsd_tab(self, frame: ttk.Frame):
        """GPSD tab: connection controls, semantic diagnostics, stream, and commands."""
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(2, weight=1)

        def _ro_row(parent, row, col, label, key, unit=""):
            sv = tk.StringVar(value=self._gps_state.get(key, "Not reported"))
            self._vars[key] = sv
            c0 = col * 4
            ttk.Label(parent, text=label).grid(
                row=row, column=c0, sticky="w", padx=5, pady=2)
            e = ttk.Entry(parent, textvariable=sv, state="readonly", width=18)
            e.grid(row=row, column=c0 + 1, sticky="ew", padx=5, pady=2)
            self._bind_copy_menu(e, sv)
            if unit:
                ttk.Label(parent, text=unit, foreground="grey").grid(
                    row=row, column=c0 + 2, sticky="w")

        ctl_f = ttk.LabelFrame(frame, text="Connection")
        ctl_f.grid(row=0, column=0, padx=4, pady=(4, 2), sticky="ew")
        ctl_f.columnconfigure(0, weight=1)
        ctl_f.columnconfigure(1, weight=1)

        ttk.Button(ctl_f, text="WATCH Raw On",
                   command=self._gpsd_watch_raw_on).grid(
            row=1, column=0, padx=5, pady=(0, 4), sticky="ew")
        ttk.Button(ctl_f, text="WATCH Off",
                   command=self._gpsd_watch_off).grid(
            row=1, column=1, padx=5, pady=(0, 4), sticky="ew")

        st_f = ttk.LabelFrame(frame, text="Status")
        st_f.grid(row=1, column=0, padx=4, pady=(2, 2), sticky="ew")
        for c in (1, 5):
            st_f.columnconfigure(c, weight=1)

        _ro_row(st_f, 0, 0, "Status", "gpsd_conn_status")
        _ro_row(st_f, 0, 1, "Device", "gpsd_device")
        _ro_row(st_f, 1, 0, "Driver", "gpsd_driver")
        _ro_row(st_f, 1, 1, "Baud", "gpsd_baud", "bps")
        _ro_row(st_f, 2, 0, "Update Rate", "gpsd_update_rate_s", "s")
        _ro_row(st_f, 2, 1, "WATCH", "gpsd_watch_state")
        _ro_row(st_f, 3, 0, "Summary", "gps_summary")

        _ro_row(st_f, 4, 0, "Fix Status", "gps_fix_status")
        _ro_row(st_f, 4, 1, "Fix Quality", "gps_fix_quality")
        _ro_row(st_f, 5, 0, "UTC Time", "gps_utc_time")
        _ro_row(st_f, 5, 1, "Altitude", "gps_alt_m", "m")
        _ro_row(st_f, 6, 0, "Latitude", "gps_lat", "deg")
        _ro_row(st_f, 6, 1, "Longitude", "gps_lon", "deg")
        _ro_row(st_f, 7, 0, "Speed", "gps_speed_kn", "knots")

        _ro_row(st_f, 8, 0, "Visible Total", "gps_sats_visible")
        _ro_row(st_f, 8, 1, "Used In Fix", "gps_sats_used")
        _ro_row(st_f, 9, 0, "GPS Visible", "gps_sats_gps")
        _ro_row(st_f, 9, 1, "GLONASS Visible", "gps_sats_glonass")
        _ro_row(st_f, 10, 0, "Galileo Visible", "gps_sats_galileo")
        _ro_row(st_f, 10, 1, "BeiDou Visible", "gps_sats_beidou")
        _ro_row(st_f, 11, 0, "PDOP", "gps_pdop")
        _ro_row(st_f, 11, 1, "HDOP", "gps_hdop")
        _ro_row(st_f, 12, 0, "VDOP", "gps_vdop")

        log_f = ttk.LabelFrame(frame, text="GPSD Stream")
        log_f.grid(row=2, column=0, padx=4, pady=(2, 2), sticky="nsew")
        log_f.columnconfigure(0, weight=1)
        log_f.rowconfigure(0, weight=1)

        self._gpsd_text = tk.Text(
            log_f,
            height=12,
            wrap="none",
            font=("TkFixedFont", 9),
            background="#f5f5f5",
        )
        ysb = ttk.Scrollbar(log_f, orient="vertical", command=self._gpsd_text.yview)
        xsb = ttk.Scrollbar(log_f, orient="horizontal", command=self._gpsd_text.xview)
        self._gpsd_text.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set)
        self._gpsd_text.grid(row=0, column=0, sticky="nsew", padx=(4, 0), pady=(4, 0))
        ysb.grid(row=0, column=1, sticky="ns", pady=(4, 0), padx=(0, 4))
        xsb.grid(row=1, column=0, sticky="ew", padx=(4, 0), pady=(0, 4))
        self._gpsd_text.bind("<Key>",
            lambda e: None if (e.state & 0x4 and e.keysym in ("c", "C", "a", "A"))
                      else "break")
        self._bind_copy_menu(self._gpsd_text)

        cmd_f = ttk.LabelFrame(frame, text="Manual Command")
        cmd_f.grid(row=3, column=0, padx=4, pady=(2, 6), sticky="ew")
        cmd_f.columnconfigure(1, weight=1)

        ttk.Label(cmd_f, text="Command").grid(
            row=0, column=0, sticky="w", padx=5, pady=3)
        self._vars["gpsd_cmd"] = tk.StringVar(value='?VERSION;')
        ttk.Entry(cmd_f, textvariable=self._vars["gpsd_cmd"]).grid(
            row=0, column=1, sticky="ew", padx=5, pady=3)
        ttk.Button(cmd_f, text="Send",
                   command=self._gpsd_send_manual).grid(
            row=0, column=2, padx=5, pady=3)
        ttk.Button(cmd_f, text="Clear Log",
                   command=lambda: self._gpsd_text.delete("1.0", "end")).grid(
            row=1, column=0, columnspan=3, padx=5, pady=(0, 4), sticky="ew")

        self._add_copyable_note(
            frame,
            "Source: gpsd service (config: /etc/default/gpsd)",
            row=4,
            wraplength=420,
        )

    def _build_mqtt_tab(self, frame: ttk.Frame):
        """MQTT tab: live log of all incoming MQTT messages + manual publish."""
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)   # log row expands

        # ---- Options bar ---- #
        opt_f = ttk.Frame(frame)
        opt_f.grid(row=0, column=0, sticky="ew", padx=4, pady=(4, 2))
        self._vars["mqtt_suppress_announce"] = tk.BooleanVar(value=True)
        ttk.Checkbutton(opt_f, text="Suppress announce topics",
                        variable=self._vars["mqtt_suppress_announce"]).pack(
            side="left", padx=4)
        ttk.Button(opt_f, text="Clear",
                   command=lambda: self._mqtt_text.delete("1.0", "end")).pack(
            side="right", padx=4)

        # ---- Message log (shorter to leave room for publish panel) ---- #
        self._mqtt_text = scrolledtext.ScrolledText(
            frame, height=12, wrap="word", font=("TkFixedFont", 9),
            background="#f5f5f5")
        self._mqtt_text.grid(row=1, column=0, sticky="nsew", padx=4, pady=(0, 2))
        self._mqtt_text.bind("<Key>",
            lambda e: None if (e.state & 0x4 and e.keysym in ("c", "C", "a", "A"))
                      else "break")
        self._bind_copy_menu(self._mqtt_text)

        # ---- Manual publish ---- #
        pub_f = ttk.LabelFrame(frame, text="Publish Message")
        pub_f.grid(row=2, column=0, sticky="ew", padx=4, pady=(2, 6))
        pub_f.columnconfigure(1, weight=1)

        ttk.Label(pub_f, text="Topic").grid(
            row=0, column=0, sticky="w", padx=5, pady=3)
        self._vars["mqtt_pub_topic"] = tk.StringVar(value="")
        ttk.Entry(pub_f, textvariable=self._vars["mqtt_pub_topic"]).grid(
            row=0, column=1, sticky="ew", padx=5, pady=3)

        ttk.Label(pub_f, text="Payload").grid(
            row=1, column=0, sticky="nw", padx=5, pady=3)
        self._mqtt_pub_payload = tk.Text(pub_f, height=4, wrap="word",
                                         font=("TkFixedFont", 9))
        self._mqtt_pub_payload.grid(row=1, column=1, sticky="ew", padx=5, pady=3)

        ttk.Button(pub_f, text="Publish",
                   command=self._mqtt_publish_manual).grid(
            row=2, column=0, columnspan=2, padx=5, pady=(0, 5), sticky="ew")

    def _mqtt_publish_manual(self):
        """Publish an arbitrary MQTT message from the manual publish panel."""
        topic = self._vars["mqtt_pub_topic"].get().strip()
        payload = self._mqtt_pub_payload.get("1.0", "end-1c").strip()
        if not topic:
            logging.error("MQTT publish: topic is empty")
            return
        try:
            mqtt_publish.single(topic, payload,
                                hostname=MQTT_BROKER, port=MQTT_PORT)
            logging.info(f"MQTT published → {topic}")
        except Exception as e:
            logging.error(f"MQTT publish failed: {e}")

    def _mqtt_log_message(self, topic: str, payload: bytes):
        """Append one MQTT message to the MQTT tab log."""
        if not hasattr(self, "_mqtt_text"):
            return
        if self._vars.get("mqtt_suppress_announce", tk.BooleanVar()).get():
            if "announce" in topic.lower():
                return
        import datetime
        ts = datetime.datetime.now().strftime("%H:%M:%S")
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
        line = f"{ts}  {topic}\n{body}\n"
        self._mqtt_text.insert("end", line)
        self._mqtt_text.see("end")  # auto-scroll
        # cap log at 500 lines
        lines = int(self._mqtt_text.index("end-1c").split(".")[0])
        if lines > 500:
            self._mqtt_text.delete("1.0", f"{lines - 500}.0")

    def _bind_copy_menu(self, widget, strvar=None):
        """Attach a right-click Copy menu + Ctrl+C to any Entry or Text widget."""
        menu = tk.Menu(widget, tearoff=0)
        def _copy():
            try:
                sel = widget.selection_get()
            except tk.TclError:
                if strvar is not None:
                    sel = strvar.get()
                else:
                    try:
                        sel = widget.get("1.0", "end-1c")
                    except Exception:
                        try:
                            sel = widget.get()
                        except Exception:
                            return
            self.root.clipboard_clear()
            self.root.clipboard_append(sel)
        menu.add_command(label="Copy", command=_copy)
        widget.bind("<Button-3>", lambda e: menu.post(e.x_root, e.y_root))
        widget.bind("<Control-c>", lambda e: (_copy(), "break")[1])

    def _add_copyable_note(self, parent, text: str, row: int, wraplength: int = 420):
        """Render subtle gray footer text that still allows selection/copy."""
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

    def _build_soc_tab(self, frame: ttk.Frame):
        """SOC tab: RFSoC live telemetry and control."""
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

        # ---- Status ---- #
        st_f = ttk.LabelFrame(frame, text="RFSoC Status")
        st_f.grid(row=0, column=0, padx=4, pady=(4, 2), sticky="ew")
        st_f.columnconfigure(1, weight=1)
        _ro_row(st_f, 0, "State",       "soc_state")
        _ro_row(st_f, 1, "Centre Freq", "soc_fc",       "MHz")
        _ro_row(st_f, 2, "IF Freq",     "soc_fif",      "MHz")
        _ro_row(st_f, 3, "Sample Rate", "soc_fs",       "MHz")
        _ro_row(st_f, 4, "PPS Count",   "soc_pps")
        _ro_row(st_f, 5, "Channels",    "soc_channels")

        # ---- Settings ---- #
        cfg_f = ttk.LabelFrame(frame, text="Settings")
        cfg_f.grid(row=1, column=0, padx=4, pady=(2, 4), sticky="ew")
        cfg_f.columnconfigure(0, weight=1)
        self._vars["sync_ntp"] = tk.BooleanVar(value=False)
        ttk.Checkbutton(cfg_f, text="Sync NTP on connect",
                        variable=self._vars["sync_ntp"]).grid(
            row=0, column=0, sticky="w", padx=6, pady=4)

        # ---- Buttons ---- #
        btn_f = ttk.Frame(frame)
        btn_f.grid(row=2, column=0, padx=4, pady=(0, 6), sticky="ew")
        btn_f.columnconfigure(0, weight=1)
        btn_f.columnconfigure(1, weight=1)
        ttk.Button(btn_f, text="Reset RFSoC",
                   command=self._rfsoc_reset).grid(
            row=0, column=0, padx=(0, 2), sticky="ew")
        ttk.Button(btn_f, text="Refresh",
                   command=self._soc_refresh).grid(
            row=0, column=1, padx=(2, 0), sticky="ew")

    def _build_tun_tab(self, frame: ttk.Frame):
        """TUN tab: tuner status (text dump) and manual control."""
        frame.columnconfigure(0, weight=1)
        # no row weight — let widgets size naturally so controls stay visible

        # ---- Summary fields ---- #
        sum_f = ttk.LabelFrame(frame, text="Tuner Summary")
        sum_f.grid(row=0, column=0, padx=4, pady=(4, 2), sticky="ew")
        sum_f.columnconfigure(1, weight=1)

        ttk.Label(sum_f, text="State").grid(
            row=0, column=0, sticky="w", padx=5, pady=2)
        self._vars["tun_state"] = tk.StringVar(value="—")
        _s = ttk.Entry(sum_f, textvariable=self._vars["tun_state"],
                       state="readonly", width=18)
        _s.grid(row=0, column=1, sticky="ew", padx=5, pady=2)
        self._bind_copy_menu(_s, self._vars["tun_state"])

        ttk.Label(sum_f, text="Name").grid(
            row=1, column=0, sticky="w", padx=5, pady=2)
        self._vars["tun_name"] = tk.StringVar(value="—")
        _n = ttk.Entry(sum_f, textvariable=self._vars["tun_name"],
                       state="readonly", width=18)
        _n.grid(row=1, column=1, sticky="ew", padx=5, pady=2)
        self._bind_copy_menu(_n, self._vars["tun_name"])

        # ---- Status dump ---- #
        st_f = ttk.LabelFrame(frame, text="Tuner Status (full)")
        st_f.grid(row=1, column=0, padx=4, pady=(2, 2), sticky="ew")
        st_f.columnconfigure(0, weight=1)
        self._tun_status_text = scrolledtext.ScrolledText(
            st_f, height=14, wrap="word", font=("TkFixedFont", 9),
            background="#f5f5f5")
        self._tun_status_text.insert("end", "no status received")
        self._tun_status_text.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)
        # block typing but allow selection/copy natively
        self._tun_status_text.bind("<Key>",
            lambda e: None if (e.state & 0x4 and e.keysym in ("c", "C", "a", "A"))
                      else "break")
        self._bind_copy_menu(self._tun_status_text)

        # ---- Controls ---- #
        ctrl_f = ttk.LabelFrame(frame, text="Manual Control")
        ctrl_f.grid(row=2, column=0, padx=4, pady=(2, 4), sticky="ew")
        ctrl_f.columnconfigure(1, weight=1)

        # Freq row: Set + Get (all tuners)
        ttk.Label(ctrl_f, text="Freq (MHz)").grid(
            row=0, column=0, sticky="w", padx=5, pady=3)
        self._vars["tun_set_freq"] = tk.StringVar(value="")
        ttk.Entry(ctrl_f, textvariable=self._vars["tun_set_freq"],
                  width=10).grid(row=0, column=1, sticky="ew", padx=5, pady=3)
        ttk.Button(ctrl_f, text="Set",
                   command=self._tun_set_freq).grid(
            row=0, column=2, padx=2, pady=3)
        ttk.Button(ctrl_f, text="Get",
                   command=self._tun_get_freq).grid(
            row=0, column=3, padx=2, pady=3)

        # Power row: Set + Get (Valon only)
        ttk.Label(ctrl_f, text="Power (dBm)").grid(
            row=1, column=0, sticky="w", padx=5, pady=3)
        self._vars["tun_set_power"] = tk.StringVar(value="")
        _pw_entry = ttk.Entry(ctrl_f, textvariable=self._vars["tun_set_power"],
                              width=10)
        _pw_entry.grid(row=1, column=1, sticky="ew", padx=5, pady=3)
        _pw_set_btn = ttk.Button(ctrl_f, text="Set",
                                 command=self._tun_set_power)
        _pw_set_btn.grid(row=1, column=2, padx=2, pady=3)
        _pw_get_btn = ttk.Button(ctrl_f, text="Get",
                                 command=self._tun_get_power)
        _pw_get_btn.grid(row=1, column=3, padx=2, pady=3)
        ttk.Label(ctrl_f, text="(Valon only)",
                  foreground="grey", font=("TkDefaultFont", 8)).grid(
            row=2, column=0, columnspan=4, sticky="w", padx=5, pady=(0, 4))

        ttk.Separator(ctrl_f, orient="horizontal").grid(
            row=3, column=0, columnspan=4, sticky="ew", padx=4, pady=2)

        # Service-level commands
        ttk.Button(ctrl_f, text="Init Tuner",
                   command=self._tun_init).grid(
            row=4, column=0, columnspan=4, padx=4, pady=3, sticky="ew")
        ttk.Button(ctrl_f, text="Restart Tuner",
                   command=self._tun_restart).grid(
            row=5, column=0, columnspan=4, padx=4, pady=3, sticky="ew")
        _lock_btn = ttk.Button(ctrl_f, text="Check Lock  (Valon only)",
                               command=self._tun_check_lock)
        _lock_btn.grid(row=6, column=0, columnspan=4, padx=4, pady=3, sticky="ew")
        ttk.Button(ctrl_f, text="Publish: Get Status",
                   command=self._tun_send_status).grid(
            row=7, column=0, columnspan=4, padx=4, pady=3, sticky="ew")

        # Gate Valon-only widgets on the active tuner name
        self._valon_only_widgets = [_pw_entry, _pw_set_btn, _pw_get_btn, _lock_btn]
        self._vars["tun_name"].trace_add(
            "write", lambda *_: self._tun_update_capability_buttons())
        self._tun_update_capability_buttons()   # apply correct state at build time


    def _build_tlm_tab(self, frame: ttk.Frame):
        """TLM tab: read-only telemetry fields populated by _afe_refresh."""
        frame.columnconfigure(0, weight=1)

        def _ro_row(parent, row, label, key, unit=""):
            """Add a label + read-only entry pair and register the StringVar."""
            sv = tk.StringVar(value="—")
            self._vars[key] = sv
            ttk.Label(parent, text=label).grid(
                row=row, column=0, sticky="w", padx=5, pady=2)
            e = ttk.Entry(parent, textvariable=sv, state="readonly", width=16)
            e.grid(row=row, column=1, sticky="ew", padx=5, pady=2)
            self._bind_copy_menu(e, sv)
            if unit:
                ttk.Label(parent, text=unit, foreground="grey").grid(
                    row=row, column=2, sticky="w")

        # ---- Accelerometer ---- #
        acc_f = ttk.LabelFrame(frame, text="Accelerometer")
        acc_f.grid(row=0, column=0, padx=4, pady=(4, 2), sticky="ew")
        acc_f.columnconfigure(1, weight=1)
        _ro_row(acc_f, 0, "X", "tlm_acc_x", "g")
        _ro_row(acc_f, 1, "Y", "tlm_acc_y", "g")
        _ro_row(acc_f, 2, "Z", "tlm_acc_z", "g")

        # ---- Gyroscope ---- #
        gyr_f = ttk.LabelFrame(frame, text="Gyroscope")
        gyr_f.grid(row=1, column=0, padx=4, pady=(2, 2), sticky="ew")
        gyr_f.columnconfigure(1, weight=1)
        _ro_row(gyr_f, 0, "X", "tlm_gyr_x", "deg/s")
        _ro_row(gyr_f, 1, "Y", "tlm_gyr_y", "deg/s")
        _ro_row(gyr_f, 2, "Z", "tlm_gyr_z", "deg/s")

        # ---- Magnetometer ---- #
        mag_f = ttk.LabelFrame(frame, text="Magnetometer")
        mag_f.grid(row=2, column=0, padx=4, pady=(2, 2), sticky="ew")
        mag_f.columnconfigure(1, weight=1)
        _ro_row(mag_f, 0, "X", "tlm_mag_x", "uT")
        _ro_row(mag_f, 1, "Y", "tlm_mag_y", "uT")
        _ro_row(mag_f, 2, "Z", "tlm_mag_z", "uT")

        # ---- Housekeeping ---- #
        hk_f = ttk.LabelFrame(frame, text="Housekeeping")
        hk_f.grid(row=3, column=0, padx=4, pady=(2, 4), sticky="ew")
        hk_f.columnconfigure(1, weight=1)
        _ro_row(hk_f, 0, "Timestamp",  "tlm_hk_ts")
        _ro_row(hk_f, 1, "Temp 1",     "tlm_hk_t1", "°C")
        _ro_row(hk_f, 2, "Temp 2",     "tlm_hk_t2", "°C")
        _ro_row(hk_f, 3, "Temp 3",     "tlm_hk_t3", "°C")

        ttk.Label(frame, text="Updated on AFE Refresh (IMU/HK)",
                  foreground="grey", font=("TkDefaultFont", 8)).grid(
            row=4, column=0, pady=(0, 2))

        ttk.Button(frame, text="Refresh Telemetry",
                   command=self._afe_refresh).grid(
            row=5, column=0, padx=4, pady=(0, 6), sticky="ew")

        self._add_copyable_note(
            frame,
            "Source: AFE service (/opt/afe/afe_service.py), helper client: /opt/mep-examples/scripts/afe.py",
            row=6,
            wraplength=420,
        )

    def _tlm_apply_state(self, telem: dict):
        """Populate TLM tab read-only fields from parsed telemetry dict."""
        def _set(key, val):
            if key in self._vars:
                self._vars[key].set(val)

        acc = telem.get("PMITACC", [])
        if len(acc) >= 4:
            _set("tlm_acc_x", acc[2])
            _set("tlm_acc_y", acc[3])
            _set("tlm_acc_z", acc[4])

        gyr = telem.get("PMITGYR", [])
        if len(gyr) >= 4:
            _set("tlm_gyr_x", gyr[2])
            _set("tlm_gyr_y", gyr[3])
            _set("tlm_gyr_z", gyr[4])

        mag = telem.get("PMITMAG", [])
        if len(mag) >= 4:
            _set("tlm_mag_x", mag[2])
            _set("tlm_mag_y", mag[3])
            _set("tlm_mag_z", mag[4])

        hk = telem.get("PMITHK", [])
        if len(hk) >= 9:
            _set("tlm_hk_ts", hk[1])
            _set("tlm_hk_t1", hk[6])
            _set("tlm_hk_t2", hk[7])
            _set("tlm_hk_t3", hk[8])

    def _build_afe_tab(self, frame: ttk.Frame):
        """AFE tab: hardware register controls via Unix socket.
        All widgets initialise to CSV defaults and send on every change.
        """
        frame.columnconfigure(0, weight=1)

        # ---- Main Block ---- #
        main_f = ttk.LabelFrame(frame, text="Main Block  (afe.py -m <addr> <value>)")
        main_f.grid(row=0, column=0, padx=4, pady=(4, 2), sticky="ew")
        main_f.columnconfigure(0, weight=1)

        for row_i, (addr, label, hw_default, inverted) in enumerate(_AFE_MAIN_REGS):
            key = f"afe_main_{addr}"
            gui_default = (not bool(hw_default)) if inverted else bool(hw_default)
            self._vars[key] = tk.BooleanVar(value=gui_default)

            def _main_cb(addr=addr, inverted=inverted, key=key):
                if self._afe_updating:
                    return
                v = self._vars[key].get()
                hw_val = int(not v) if inverted else int(v)
                self._afe_send(0, -1, addr, hw_val)

            self._vars[key].trace_add("write", lambda *_, cb=_main_cb: cb())
            ttk.Checkbutton(main_f, text=label,
                            variable=self._vars[key]).grid(
                row=row_i, column=0, sticky="w", padx=6, pady=1)

        # GNSS Antenna (addr 9) — radio buttons
        ant_row = len(_AFE_MAIN_REGS)
        ant_f = ttk.Frame(main_f)
        ant_f.grid(row=ant_row, column=0, sticky="w", padx=6, pady=(4, 2))
        ttk.Label(ant_f, text="GNSS Antenna:").grid(row=0, column=0, sticky="w")
        self._vars["afe_main_9"] = tk.StringVar(value="internal")  # default=1=internal

        def _ant_cb(*_):
            if self._afe_updating:
                return
            v = self._vars["afe_main_9"].get()
            self._afe_send(0, -1, 9, 1 if v == "internal" else 0)

        self._vars["afe_main_9"].trace_add("write", _ant_cb)
        ttk.Radiobutton(ant_f, text="Internal", variable=self._vars["afe_main_9"],
                        value="internal").grid(row=0, column=1, padx=6)
        ttk.Radiobutton(ant_f, text="External", variable=self._vars["afe_main_9"],
                        value="external").grid(row=0, column=2, padx=6)

        # ---- RX Channels ---- #
        rx_outer = ttk.LabelFrame(frame, text="RX Channels  (afe.py -rx1 … -rx4)")
        rx_outer.grid(row=1, column=0, padx=4, pady=(2, 4), sticky="ew")
        rx_outer.columnconfigure(0, weight=1)

        rx_nb = ttk.Notebook(rx_outer)
        rx_nb.grid(row=0, column=0, padx=4, pady=4, sticky="ew")

        for ch in range(1, 5):
            ch_f = ttk.Frame(rx_nb, padding=6)
            ch_f.columnconfigure(1, weight=1)
            rx_nb.add(ch_f, text=f"RX{ch}")

            # Boolean registers
            for row_i, (addr, label, hw_default, inverted) in enumerate(_AFE_RX_REGS):
                key = f"afe_rx{ch}_{addr}"
                gui_default = (not bool(hw_default)) if inverted else bool(hw_default)
                self._vars[key] = tk.BooleanVar(value=gui_default)

                def _rx_cb(ch=ch, addr=addr, inverted=inverted, key=key):
                    if self._afe_updating:
                        return
                    v = self._vars[key].get()
                    hw_val = int(not v) if inverted else int(v)
                    self._afe_send(2, ch, addr, hw_val)

                self._vars[key].trace_add("write", lambda *_, cb=_rx_cb: cb())
                ttk.Checkbutton(ch_f, text=label,
                                variable=self._vars[key]).grid(
                    row=row_i, column=0, columnspan=2, sticky="w", pady=1)

            # Attenuation spinbox 0-31 dB (C1+C2+C4+C8+C16 bits, addrs 4-8)
            sep_row = len(_AFE_RX_REGS)
            ttk.Separator(ch_f, orient="horizontal").grid(
                row=sep_row, column=0, columnspan=2, sticky="ew", pady=4)
            ttk.Label(ch_f, text="Attenuation (dB)").grid(
                row=sep_row + 1, column=0, sticky="w")
            atten_key = f"afe_rx{ch}_atten"
            self._vars[atten_key] = tk.IntVar(value=0)

            def _atten_cb(ch=ch, key=atten_key):
                if self._afe_updating:
                    return
                atten = int(self._vars[key].get())
                for bit, addr in enumerate([4, 5, 6, 7, 8]):   # C1 C2 C4 C8 C16
                    self._afe_send(2, ch, addr, (atten >> bit) & 1)

            self._vars[atten_key].trace_add("write", lambda *_, cb=_atten_cb: cb())
            ttk.Spinbox(ch_f, from_=0, to=31, increment=1,
                        textvariable=self._vars[atten_key],
                        width=6, state="readonly").grid(
                row=sep_row + 1, column=1, sticky="w", padx=5)
            ttk.Label(ch_f, text="dB", foreground="grey").grid(
                row=sep_row + 1, column=2, sticky="w")

        # ---- TX Channels ---- #
        tx_outer = ttk.LabelFrame(frame, text="TX Channels  (afe.py -tx1 / -tx2)")
        tx_outer.grid(row=2, column=0, padx=4, pady=(2, 4), sticky="ew")
        tx_outer.columnconfigure(0, weight=1)

        tx_nb = ttk.Notebook(tx_outer)
        tx_nb.grid(row=0, column=0, padx=4, pady=4, sticky="ew")

        for ch in range(1, 3):
            ch_f = ttk.Frame(tx_nb, padding=6)
            ch_f.columnconfigure(0, weight=1)
            tx_nb.add(ch_f, text=f"TX{ch}")

            for row_i, (addr, label, hw_default, inverted) in enumerate(_AFE_TX_REGS):
                key = f"afe_tx{ch}_{addr}"
                gui_default = (not bool(hw_default)) if inverted else bool(hw_default)
                self._vars[key] = tk.BooleanVar(value=gui_default)

                def _tx_cb(ch=ch, addr=addr, inverted=inverted, key=key):
                    if self._afe_updating:
                        return
                    v = self._vars[key].get()
                    hw_val = int(not v) if inverted else int(v)
                    self._afe_send(1, ch, addr, hw_val)

                self._vars[key].trace_add("write", lambda *_, cb=_tx_cb: cb())
                ttk.Checkbutton(ch_f, text=label,
                                variable=self._vars[key]).grid(
                    row=row_i, column=0, sticky="w", pady=1)

        # ---- Bottom buttons ---- #
        btn_f = ttk.Frame(frame)
        btn_f.grid(row=3, column=0, padx=4, pady=(0, 6), sticky="ew")
        btn_f.columnconfigure(0, weight=1)
        btn_f.columnconfigure(1, weight=1)
        ttk.Button(btn_f, text="Refresh State",
                   command=self._afe_refresh).grid(
            row=0, column=0, padx=(0, 2), sticky="ew")
        ttk.Button(btn_f, text="Reset to Defaults",
                   command=self._afe_reset_defaults).grid(
            row=0, column=1, padx=(2, 0), sticky="ew")

    def _afe_send(self, block: int, channel: int, addr: int, value: int):
        """Send a register write to the AFE service on a background thread.
        Avoids blocking the GUI if the socket is unavailable.
        """
        def _send():
            msg = f"{block} {channel} {addr} {value}".encode()
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                    s.settimeout(2.0)
                    s.connect(_AFE_SOCKET)
                    s.sendall(msg)
                    try:
                        reply = s.recv(4096).decode(errors="ignore").strip()
                        if reply:
                            logging.info(f"AFE [{block} {channel} {addr} {value}] \u2192 {reply}")
                    except socket.timeout:
                        pass  # write-only command; no reply expected
            except Exception as e:
                logging.warning(f"AFE send failed [{block} {channel} {addr} {value}]: {e}")

        threading.Thread(target=_send, daemon=True).start()

    def _afe_refresh(self):
        """Request current register state from AFE service (block=3),
        parse the reply, and update all AFE widgets. Non-blocking.
        """
        def _query():
            msg = b"3 -1 -1 -1"
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                    s.settimeout(12.0)   # service needs up to 5s; give extra margin
                    s.connect(_AFE_SOCKET)
                    s.sendall(msg)
                    chunks = []
                    try:
                        while True:
                            chunk = s.recv(4096)
                            if not chunk:
                                break
                            chunks.append(chunk)
                    except socket.timeout:
                        pass
                    reply = b"".join(chunks).decode(errors="ignore").strip()
                    if not reply:
                        logging.warning("AFE: no reply to state request (block=3)")
                        return
                    # Parse lines like "MAINREG:[0, 0, 1, ...]" or "TX1REG: [0, 1, ...]"
                    reg_data = {}
                    telem_data = {}
                    for line in reply.splitlines():
                        # Register lines: MAINREG:[...] or TX1REG: [...]
                        m = re.match(r'(\w+REG)\s*:\s*\[([^\]]+)\]', line)
                        if m:
                            reg_data[m.group(1)] = [
                                int(x.strip()) for x in m.group(2).split(',')]
                            continue
                        # NMEA / PMIT sentences
                        if line.startswith('$'):
                            clean = line.split('*')[0]  # strip checksum
                            parts = clean.lstrip('$').split(',')
                            telem_data[parts[0]] = parts
                    if reg_data:
                        self.root.after(0, lambda d=reg_data: self._afe_apply_state(d))
                    if telem_data:
                        self.root.after(0, lambda d=telem_data: self._tlm_apply_state(d))
                    if not reg_data and not telem_data:
                        logging.warning("AFE: reply received but no data found")
                        logging.info(f"AFE raw reply:\n{reply}")
            except Exception as e:
                logging.warning(f"AFE refresh failed: {e}")

        logging.info("AFE: requesting state (may take up to 12s)...")
        threading.Thread(target=_query, daemon=True).start()

    def _afe_apply_state(self, reg_data: dict):
        """Update all AFE widgets from a parsed register state dict.
        _afe_updating is set True so traces don't fire hardware sends.
        """
        self._afe_updating = True
        try:
            if "MAINREG" in reg_data:
                vals = reg_data["MAINREG"]
                for addr, _lbl, _def, inverted in _AFE_MAIN_REGS:
                    key = f"afe_main_{addr}"
                    gui_val = (not bool(vals[addr])) if inverted else bool(vals[addr])
                    self._vars[key].set(gui_val)
                # addr 9: GNSS antenna (1=internal, 0=external)
                self._vars["afe_main_9"].set("internal" if vals[9] == 1 else "external")
            for ch in range(1, 3):
                key = f"TX{ch}REG"
                if key in reg_data:
                    vals = reg_data[key]
                    for addr, _lbl, _def, inverted in _AFE_TX_REGS:
                        vkey = f"afe_tx{ch}_{addr}"
                        gui_val = (not bool(vals[addr])) if inverted else bool(vals[addr])
                        self._vars[vkey].set(gui_val)
            for ch in range(1, 5):
                key = f"RX{ch}REG"
                if key in reg_data:
                    vals = reg_data[key]
                    for addr, _lbl, _def, inverted in _AFE_RX_REGS:
                        vkey = f"afe_rx{ch}_{addr}"
                        gui_val = (not bool(vals[addr])) if inverted else bool(vals[addr])
                        self._vars[vkey].set(gui_val)
                    # Attenuation: addrs 4-8 are C1,C2,C4,C8,C16 bits
                    atten = sum(vals[4 + i] << i for i in range(5))
                    self._vars[f"afe_rx{ch}_atten"].set(atten)
        finally:
            self._afe_updating = False
        logging.info(f"AFE widgets updated from hardware state: {list(reg_data.keys())}")

    def _afe_reset_defaults(self):
        """Restore all AFE widget vars to CSV defaults (no hardware send)."""
        for addr, _label, hw_default, inverted in _AFE_MAIN_REGS:
            key = f"afe_main_{addr}"
            gui_val = (not bool(hw_default)) if inverted else bool(hw_default)
            self._vars[key].set(gui_val)
        self._vars["afe_main_9"].set("internal")   # GNSS_ANT_SEL default=1=internal
        for ch in range(1, 3):
            for addr, _label, hw_default, inverted in _AFE_TX_REGS:
                key = f"afe_tx{ch}_{addr}"
                gui_val = (not bool(hw_default)) if inverted else bool(hw_default)
                self._vars[key].set(gui_val)
        for ch in range(1, 5):
            for addr, _label, hw_default, inverted in _AFE_RX_REGS:
                key = f"afe_rx{ch}_{addr}"
                gui_val = (not bool(hw_default)) if inverted else bool(hw_default)
                self._vars[key].set(gui_val)
            self._vars[f"afe_rx{ch}_atten"].set(0)
        logging.info("AFE: all registers reset to defaults")

    # ---- SOC helpers ---- #

    def _soc_refresh(self):
        """Query RFSoC telemetry and update SOC tab fields."""
        if self.mep is None:
            logging.warning("SOC: no controller connected — start a capture first")
            return
        def _query():
            tlm = self.mep._get_tlm(timeout_s=3.0)
            if tlm is None:
                logging.warning("SOC: no telemetry response")
                return
            self.root.after(0, lambda t=tlm: self._soc_apply(t))
        threading.Thread(target=_query, daemon=True).start()

    def _soc_apply(self, tlm: dict):
        """Populate SOC tab read-only fields from a tlm dict."""
        self._vars["soc_state"].set(tlm.get("state", "—"))
        self._vars["soc_fc"].set(f"{float(tlm.get('f_c_hz', 0))/1e6:.3f}")
        self._vars["soc_fif"].set(f"{float(tlm.get('f_if_hz', 0))/1e6:.3f}")
        self._vars["soc_fs"].set(f"{float(tlm.get('f_s', 0))/1e6:.3f}")
        self._vars["soc_pps"].set(str(tlm.get("pps_count", "—")))
        self._vars["soc_channels"].set(str(tlm.get("channels", "—")))

    def _rfsoc_reset(self):
        """Send a reset command to the RFSoC."""
        try:
            mqtt_publish.single(RFSOC_CMD_TOPIC,
                                json.dumps({"task_name": "reset"}),
                                hostname=MQTT_BROKER, port=MQTT_PORT)
            logging.info("RFSoC reset sent")
        except Exception as e:
            logging.error(f"RFSoC reset failed: {e}")

    # ---- TUN helpers ---- #

    def _tun_refresh(self):
        """Read latest tuner status from monitor cache, update summary fields and text dump."""
        status = self._monitor_states.get(TUNER_STATUS_TOPIC)
        if not isinstance(status, dict):
            text = "no status received" if status is None else str(status)
            self._vars["tun_state"].set("—")
            self._vars["tun_name"].set("—")
        else:
            self._vars["tun_state"].set(status.get("state", "—"))
            # name lives in the nested 'tuner' sub-dict
            tuner_sub = status.get("tuner", {})
            if isinstance(tuner_sub, dict):
                name_val = tuner_sub.get("name") or status.get("name", "—")
                # Populate freq / power fields from tuner state
                freq_val = tuner_sub.get("freq_mhz")
                if freq_val is not None:
                    self._vars["tun_set_freq"].set(str(freq_val))
                pwr_val = tuner_sub.get("pwr_dbm")
                if pwr_val is not None:
                    self._vars["tun_set_power"].set(str(pwr_val))
            else:
                name_val = status.get("name", "—")
            self._vars["tun_name"].set(str(name_val) if name_val else "—")
            lines = []
            for k, v in status.items():
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
            info = status.get("info")
            if info is None and isinstance(status.get("tuner"), dict):
                info = status["tuner"].get("info")
            if info:
                lines.append("--- info ---")
                lines.append(str(info).replace("\\r\\n", "\n").replace("\r\n", "\n"))
            text = "\n".join(lines)
        self._tun_status_text.delete("1.0", "end")
        self._tun_status_text.insert("end", text)
        logging.info("TUN: status text updated")

    def _tun_handle_response(self, data: dict):
        """Handle a tuner command response (get_freq / get_power reply).
        These messages carry 'task_name' and 'value' but no 'state' key.
        Update the corresponding entry field directly.
        """
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

    def _tun_init(self):
        """Send init_tuner to tuner_control service."""
        tuner = self._vars["tuner"].get()
        if tuner == "None":
            payload = {"task_name": "init_tuner", "arguments": {}}
        elif tuner == "auto":
            payload = {"task_name": "init_tuner", "arguments": {}}
        else:
            payload = {"task_name": "init_tuner",
                       "arguments": {"force_tuner": tuner}}
        try:
            mqtt_publish.single(TUNER_CMD_TOPIC, json.dumps(payload),
                                hostname=MQTT_BROKER, port=MQTT_PORT)
            logging.info(f"TUN: init_tuner sent ({tuner})")
            # Only Valon supports get_lock_status
            if tuner.lower() == "valon":
                self.root.after(3000, self._tun_check_lock)
        except Exception as e:
            logging.error(f"TUN init failed: {e}")

    def _tun_set_freq(self):
        """Send set_freq to tuner_control service."""
        try:
            freq = float(self._vars["tun_set_freq"].get())
        except ValueError:
            logging.error("TUN: invalid frequency value")
            return
        payload = {"task_name": "set_freq", "arguments": {"freq_mhz": freq}}
        try:
            mqtt_publish.single(TUNER_CMD_TOPIC, json.dumps(payload),
                                hostname=MQTT_BROKER, port=MQTT_PORT)
            logging.info(f"TUN: set_freq {freq:.3f} MHz sent")
        except Exception as e:
            logging.error(f"TUN set_freq failed: {e}")

    def _tun_check_lock(self):
        """Send get_lock_status to tuner_control (Valon only), then refresh."""
        payload = {"task_name": "get_lock_status", "arguments": {}}
        def _query():
            try:
                mqtt_publish.single(TUNER_CMD_TOPIC, json.dumps(payload),
                                    hostname=MQTT_BROKER, port=MQTT_PORT)
                import time; time.sleep(1.5)  # give service time to reply
                self.root.after(0, self._tun_refresh)
            except Exception as e:
                logging.error(f"TUN check_lock failed: {e}")
        threading.Thread(target=_query, daemon=True).start()
        logging.info("TUN: get_lock_status sent")

    def _tun_get_freq(self):
        """Send get_freq to tuner_control, then refresh status dump."""
        payload = {"task_name": "get_freq", "arguments": {}}
        def _query():
            try:
                mqtt_publish.single(TUNER_CMD_TOPIC, json.dumps(payload),
                                    hostname=MQTT_BROKER, port=MQTT_PORT)
                import time; time.sleep(1.5)
                self.root.after(0, self._tun_refresh)
            except Exception as e:
                logging.error(f"TUN get_freq failed: {e}")
        threading.Thread(target=_query, daemon=True).start()
        logging.info("TUN: get_freq sent")

    def _tun_set_power(self):
        """Send set_power to tuner_control (Valon only)."""
        try:
            pwr = float(self._vars["tun_set_power"].get())
        except ValueError:
            logging.error("TUN: invalid power value")
            return
        payload = {"task_name": "set_power", "arguments": {"pwr_dbm": pwr}}
        try:
            mqtt_publish.single(TUNER_CMD_TOPIC, json.dumps(payload),
                                hostname=MQTT_BROKER, port=MQTT_PORT)
            logging.info(f"TUN: set_power {pwr:.1f} dBm sent")
        except Exception as e:
            logging.error(f"TUN set_power failed: {e}")

    def _tun_get_power(self):
        """Send get_power to tuner_control (Valon only), then refresh status dump."""
        payload = {"task_name": "get_power", "arguments": {}}
        def _query():
            try:
                mqtt_publish.single(TUNER_CMD_TOPIC, json.dumps(payload),
                                    hostname=MQTT_BROKER, port=MQTT_PORT)
                import time; time.sleep(1.5)
                self.root.after(0, self._tun_refresh)
            except Exception as e:
                logging.error(f"TUN get_power failed: {e}")
        threading.Thread(target=_query, daemon=True).start()
        logging.info("TUN: get_power sent")

    def _tun_restart(self):
        """Send restart_tuner to tuner_control service."""
        payload = {"task_name": "restart_tuner", "arguments": {}}
        try:
            mqtt_publish.single(TUNER_CMD_TOPIC, json.dumps(payload),
                                hostname=MQTT_BROKER, port=MQTT_PORT)
            logging.info("TUN: restart_tuner sent")
            self.root.after(3000, self._tun_refresh)   # let service re-init
        except Exception as e:
            logging.error(f"TUN restart failed: {e}")

    def _tun_send_status(self):
        """Send 'status' command to tuner_control service, then refresh dump."""
        payload = {"task_name": "status", "arguments": {}}
        def _query():
            try:
                mqtt_publish.single(TUNER_CMD_TOPIC, json.dumps(payload),
                                    hostname=MQTT_BROKER, port=MQTT_PORT)
                import time; time.sleep(1.5)
                self.root.after(0, self._tun_refresh)
            except Exception as e:
                logging.error(f"TUN status command failed: {e}")
        threading.Thread(target=_query, daemon=True).start()
        logging.info("TUN: status command sent")

    def _tun_update_capability_buttons(self):
        """Enable Valon-only widgets only when the active tuner name contains 'valon'."""
        name = self._vars.get("tun_name", tk.StringVar()).get().lower()
        state = "normal" if "valon" in name else "disabled"
        for w in getattr(self, "_valon_only_widgets", []):
            try:
                w.configure(state=state)
            except Exception:
                pass

    # ---- REC config helpers ---- #

    def _rec_status_update(self, data: dict):
        """Called on the main thread whenever a recorder status message arrives."""
        if "rec_status" not in self._vars:
            return
        self._vars["rec_status"].set(data.get("state", "—"))
        # try a few common field names for the current file/path
        fpath = (data.get("file") or data.get("filename") or
                 data.get("path") or data.get("output_file") or "—")
        self._vars["rec_status_file"].set(str(fpath))

    def _rec_status_seed(self):
        """Seed recorder status fields from the monitor cache at startup."""
        cached = self._monitor_states.get(RECORDER_STATUS_TOPIC)
        if isinstance(cached, dict):
            self._rec_status_update(cached)

    def _rec_reload_config(self):
        """Re-send config.load with the current sample rate."""
        sr = self._vars["sample_rate"].get()
        config_name = f"sr{sr}MHz"
        payload = json.dumps({"task_name": "config.load",
                              "arguments": {"name": config_name}})
        try:
            mqtt_publish.single(RECORDER_CMD_TOPIC, payload,
                                hostname=MQTT_BROKER, port=MQTT_PORT)
            self._vars["rec_active_config"].set(config_name)
            logging.info(f"REC: config.load sent ({config_name})")
        except Exception as e:
            logging.error(f"REC config reload failed: {e}")

    def _build_rec_tab(self, frame: ttk.Frame):
        """REC tab: recorder pipeline controls."""
        frame.columnconfigure(0, weight=1)

        # ---- Status ---- #
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

        # ---- Spectrograms ---- #
        sg_frame = ttk.LabelFrame(frame, text="Spectrograms")
        sg_frame.grid(row=1, column=0, padx=4, pady=6, sticky="ew")
        sg_frame.columnconfigure(1, weight=1)

        # Enable/disable checkboxes for the three pipeline stages
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

        # Reduce operation
        ttk.Label(sg_frame, text="Reduce Op").grid(
            row=4, column=0, sticky="w", padx=5, pady=3)
        self._vars["sg_reduce_op"] = tk.StringVar(value="max")
        ttk.Combobox(
            sg_frame, textvariable=self._vars["sg_reduce_op"],
            values=["max", "min", "mean"], width=10, state="readonly",
        ).grid(row=4, column=1, sticky="ew", padx=5, pady=3)

        # SNR range
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

        # Spectra per saved image
        ttk.Label(sg_frame, text="Spectra per Image").grid(
            row=7, column=0, sticky="w", padx=5, pady=3)
        self._vars["sg_spectra_per_output"] = tk.StringVar(value="600")
        ttk.Entry(sg_frame, textvariable=self._vars["sg_spectra_per_output"], width=8).grid(
            row=7, column=1, sticky="ew", padx=5, pady=3)

        ttk.Button(sg_frame, text="Send Now",
                   command=self._apply_rec_spectrogram).grid(
            row=8, column=0, columnspan=2, padx=4, pady=6, sticky="ew")

        # ---- DRF Output ---- #
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

        # ---- Config Load ---- #
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

    def _build_control_section(self, parent: ttk.Frame, row: int):
        """Control section: Start, Stop, and Advanced Options toggle."""
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

    def _toggle_advanced(self):
        """Show or hide the Advanced Options right-column panel."""
        if self._adv_frame.winfo_viewable():
            self._adv_frame.grid_remove()
            self._adv_btn_text.set("\u25b6 Advanced")
        else:
            self._adv_frame.grid()
            self._adv_btn_text.set("\u25c0 Advanced")

    # ---- Recorder config helpers ---- #

    def _publish_recorder(self, key: str, value):
        """Send a config.set command directly to the recorder via MQTT.
        Works whether or not a sweep is active.
        """
        payload = json.dumps({"task_name": "config.set",
                              "arguments": {"key": key, "value": value}})
        try:
            mqtt_publish.single(RECORDER_CMD_TOPIC, payload,
                                hostname=MQTT_BROKER, port=MQTT_PORT)
        except Exception as e:
            logging.error(f"Recorder publish error: {e}")

    def _apply_rec_spectrogram(self):
        """Push all spectrogram config.set commands to the recorder."""
        self._publish_recorder("pipeline.spectrogram",
                               self._vars["sg_compute"].get())
        self._publish_recorder("pipeline.spectrogram_mqtt",
                               self._vars["sg_mqtt"].get())
        self._publish_recorder("pipeline.spectrogram_output",
                               self._vars["sg_output"].get())
        self._publish_recorder("spectrogram.reduce_op",
                               self._vars["sg_reduce_op"].get())
        try:
            self._publish_recorder("spectrogram_output.snr_db_min",
                                   float(self._vars["sg_snr_min"].get()))
            self._publish_recorder("spectrogram_output.snr_db_max",
                                   float(self._vars["sg_snr_max"].get()))
            self._publish_recorder("spectrogram_output.num_spectra_per_output",
                                   int(self._vars["sg_spectra_per_output"].get()))
        except ValueError as e:
            logging.error(f"Spectrogram config error: {e}")
            return
        logging.info("Spectrogram settings applied")

    def _apply_rec_drf(self):
        """Push DRF recorder settings."""
        try:
            self._publish_recorder("packet.batch_size",
                                   int(self._vars["rec_batch_size"].get()))
        except ValueError as e:
            logging.error(f"DRF config error: {e}")
            return
        logging.info("DRF settings applied")

    # ---- Tuner trace ---- #

    def _on_tuner_change(self, *_):
        """Enable RFSoC IF entry only when a tuner is selected, then recalculate LO."""
        state = "normal" if self._vars["tuner"].get() != "None" else "disabled"
        self._if_entry.configure(state=state)
        self._update_synth_lo()

    def _update_synth_lo(self, *_):
        """Compute Synth LO = RF + IF (High) or RF - IF (Low) and display it.

        High-side injection: LO = RF + IF  (LO sits above the RF band)
        Low-side  injection: LO = RF - IF  (LO sits below the RF band)
        Only meaningful when a tuner is selected.
        """
        if self._vars["tuner"].get() == "None":
            self._vars["synth_lo"].set("—")
            return
        try:
            rf_mhz = float(self._vars["freq_start"].get())
            if_mhz = float(self._vars["adc_if"].get())
            mode   = self._vars["injection_mode"].get()
            lo_mhz = rf_mhz + if_mhz if mode == "High" else rf_mhz - if_mhz
            self._vars["synth_lo"].set(f"{lo_mhz:.3f}")
        except ValueError:
            self._vars["synth_lo"].set("—")

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

    def _parse_single_params(self) -> dict:
        """Parse fields for a single-frequency capture. Raises ValueError on bad input."""
        freq_start = float(self._vars["freq_start"].get())

        channel   = self._vars["channel"].get()
        tuner_str = self._vars["tuner"].get()
        tuner     = None if tuner_str == "None" else tuner_str

        adc_if_s = self._vars["adc_if"].get().strip()
        adc_if   = float(adc_if_s) if (adc_if_s and tuner) else None

        capture_name_s = self._vars["capture_name"].get().strip()
        capture_name   = capture_name_s if capture_name_s else None

        sample_rate = int(self._vars["sample_rate"].get())
        injection   = self._vars["injection_mode"].get().lower()  # "high" or "low"

        return {
            "freq_start":   freq_start,
            "channel":      channel,
            "tuner":        tuner,
            "adc_if":       adc_if,
            "capture_name": capture_name,
            "sample_rate":  sample_rate,
            "injection":    injection,
        }

    def _parse_sweep_params(self) -> dict:
        """Parse fields for a frequency sweep. Raises ValueError on bad input."""
        freq_start = float(self._vars["freq_start"].get())

        freq_end_s = self._vars["freq_end"].get().strip()
        freq_end   = float(freq_end_s) if freq_end_s else float("nan")

        step  = float(self._vars["step"].get())
        dwell = float(self._vars["dwell"].get())

        channel   = self._vars["channel"].get()
        tuner_str = self._vars["tuner"].get()
        tuner     = None if tuner_str == "None" else tuner_str

        adc_if_s = self._vars["adc_if"].get().strip()
        adc_if   = float(adc_if_s) if (adc_if_s and tuner) else None

        capture_name_s = self._vars["capture_name"].get().strip()
        capture_name   = capture_name_s if capture_name_s else None

        sample_rate = int(self._vars["sample_rate"].get())
        injection   = self._vars["injection_mode"].get().lower()  # "high" or "low"

        return {
            "freq_start":   freq_start,
            "freq_end":     freq_end,
            "step":         step,
            "dwell":        dwell,
            "channel":      channel,
            "tuner":        tuner,
            "adc_if":       adc_if,
            "capture_name": capture_name,
            "sample_rate":  sample_rate,
            "injection":    injection,
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
            or self.mep.injection     != params["injection"]
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

            if self._vars.get("sync_ntp") and self._vars["sync_ntp"].get():
                logging.info("Syncing NTP on RFSoC...")
                script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      "rfsoc_update_ntp.bash")
                os.system(script)

            logging.info("Connecting to MQTT broker")
            self.mep = MEPController(
                channel      = params["channel"],
                sample_rate  = params["sample_rate"],
                tuner        = params["tuner"],
                adc_if       = params["adc_if"],
                injection    = params["injection"],
                capture_name = params["capture_name"],
            )
            # Update active config display
            config_name = f"sr{params['sample_rate']}MHz"
            self._vars["rec_active_config"].set(config_name)

        return self.mep

    # ------------------------------------------------------------------ #
    #  Button handlers                                                     #
    # ------------------------------------------------------------------ #

    def _start(self):
        """Dispatch to single or sweep based on the active Tune tab."""
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
                mep = self._get_or_create_mep(params)
                mep._stop_flag.clear()
                if not mep.wait_for_firmware_ready(max_wait_s=10):
                    logging.error("RFSoC firmware not ready — aborting sweep")
                    self._status_var.set("Idle")
                    return
                freqs_hz = get_frequency_list(
                    params["freq_start"], params["freq_end"], params["step"]
                )
                n = len(freqs_hz) if hasattr(freqs_hz, "__len__") else "?"
                logging.info(f"Starting sweep: {n} steps, dwell={params['dwell']}s")
                mep.run_sweep(freqs_hz, params["dwell"])
            except Exception as e:
                logging.error(f"Sweep error: {e}", exc_info=True)
            finally:
                self._status_var.set("Idle")

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
        """Read the cached RFSoC telemetry and update the status bar and SOC tab."""
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
                if "soc_state" in self._vars:
                    self._soc_apply(tlm)
        self.root.after(1000, self._poll_status)


# ===== ENTRY POINT ===== #

def main():
    root = tk.Tk()
    app  = MEPGui(root)

    def _on_close():
        # Clean up controller on window close
        app._gpsd_run = False
        if app.mep is not None:
            logging.info("Window closed — cleaning up")
            try:
                app.mep.stop_recorder()
                app.mep.disconnect()
            except Exception:
                pass
        if hasattr(app, "_monitor_client"):
            try:
                app._monitor_client.loop_stop()
                app._monitor_client.disconnect()
            except Exception:
                pass
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", _on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
