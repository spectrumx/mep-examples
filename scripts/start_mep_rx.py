#!/opt/radiohound/python313/bin/python
"""
start_mep_rx.py

MEP system controller — MQTT gateway for RFSoC, recorder, tuner, and AFE.

Architecture:
    MEPBus            — always-on MQTT connection, listener registry, thin command publishers
    CaptureController — on-demand sweep/record orchestrator (owns sync-wait + recipes)
    System functions   — pure subprocess utilities (Jetson power, network info, NTP)

Usage (CLI):
    python start_mep_rx.py -f1 7000 -f2 8000 -s 10 -d 60 -c A -r 10

Usage (imported by mep_gui.py):
    from start_mep_rx import MEPBus, CaptureController
    bus = MEPBus()
    cap = CaptureController(bus)
    cap.configure_sweep(channel="A", sample_rate_mhz=10)
    cap.run_sweep(freqs_hz, dwell_s=60)

Author: john.marino@colorado.edu
"""

# ===== IMPORTS ===== #
import argparse
import base64
import time
import logging
import json
import os
import re
import math
import socket
import subprocess
import queue
import struct
from collections import deque
from datetime import datetime
import threading
from typing import Optional, Callable
import paho.mqtt.client as mqtt_lib

# ===== CONFIG ===== #
LOG_DIR     = os.path.join(os.path.expanduser("~"), "log", "spectrumx")
MQTT_BROKER = "localhost"
MQTT_PORT   = 1883

# AFE logging defaults shared with GUI/CLI callers.
AFE_DEFAULT_LOG_PATH = "/data/log_telemetry"
AFE_DEFAULT_LOG_RATE_S = 1
AFE_DEFAULT_LOG_RATE_RANGE = (1, 3600)

# Command and Status Topics
RFSOC_CMD_TOPIC       = "rfsoc/command"
RFSOC_STATUS_TOPIC    = "rfsoc/status"
RECORDER_CMD_TOPIC    = "recorder/command"
RECORDER_STATUS_TOPIC = "recorder/status"
TUNER_CMD_TOPIC       = "tuner_control/command"
TUNER_STATUS_TOPIC    = "tuner_control/status"
AFE_CMD_TOPIC         = "afe/command"
AFE_RESPONSE_TOPIC    = "afe/response"
AFE_STATUS_TOPIC      = "afe/status"
AFE_ANNOUNCE_TOPIC    = "afe/announce"
AFE_EVENT_TOPIC       = "afe/event"
AFE_GNSS_TOPIC        = "afe/data/gps"
AFE_IMU_TOPIC         = "afe/data/imu"
AFE_MAG_TOPIC         = "afe/data/mag"
AFE_HK_TOPIC          = "afe/data/hk"
AFE_REGISTERS_TOPIC   = "afe/status/registers"

# SPEC data topic pattern (matches any radiohound client spectrum stream)
SPEC_TOPIC_PATTERN    = "radiohound/clients/data/#"

# Topics that support synchronous _wait_for_status() during sweep orchestration
_SYNC_STATUS_TOPICS = (RFSOC_STATUS_TOPIC, RECORDER_STATUS_TOPIC, TUNER_STATUS_TOPIC)

# Owned by the Recorder service. This is not the source of truth of this information.
RECORDER_CHANNEL_PORTS = {"A": 60134, "B": 60133, "C": 60132, "D": 60131}

TUNER_INJECTION_SIDE = {
    "VALON":   "high",
    "LMX2820": "high",
    "TEST":    "high",
}

# Hardware configuration options (for dropdowns/validation)
# CHANNEL_OPTIONS derived from RECORDER_CHANNEL_PORTS
CHANNEL_OPTIONS     = list(sorted(RECORDER_CHANNEL_PORTS.keys()))
TUNER_OPTIONS       = ["None"] + list(TUNER_INJECTION_SIDE.keys()) + ["auto"]

RECORDER_CONFIG_DIR = "/opt/radiohound/docker/recorder/configs"
DOCKER_COMPOSE_DIR = "/opt/radiohound/docker"

GREEN = "\033[92m"
RESET = "\033[0m"


# ===== HELPERS ===== #

def get_frequency_list(start_mhz: float, end_mhz: float, step_mhz: float):
    start_hz = int(start_mhz * 1e6)
    step_hz  = int(step_mhz  * 1e6)
    if math.isnan(end_mhz):
        return [start_hz]
    end_hz = int(end_mhz * 1e6)
    return range(start_hz, end_hz + step_hz, step_hz)


def resolve_injection(tuner: str, injection_override: str = None) -> str:
    """Determine injection side for a given tuner.
    Returns 'high' or 'low'. Raises ValueError for unknown tuners.
    """
    if injection_override:
        return injection_override
    if tuner.lower() == "auto":
        return "high"
    if tuner.upper() in TUNER_INJECTION_SIDE:
        return TUNER_INJECTION_SIDE[tuner.upper()]
    raise ValueError(f"Tuner {tuner!r} not in TUNER_INJECTION_SIDE — add it or pass --injection")


def tuner_type_arg(x: str):
    """argparse type handler — allows None/none/auto or a known tuner name."""
    x_lower = x.strip().lower()
    if x_lower == "none":
        return None
    if x_lower == "auto":
        return "auto"
    x_upper = x.strip().upper()
    if x_upper not in TUNER_INJECTION_SIDE:
        raise argparse.ArgumentTypeError(
            f"Invalid tuner '{x}'. Valid: {list(TUNER_INJECTION_SIDE.keys())}, auto, None"
        )
    return x_upper


def discover_sample_rate_options(recorder_config_dir: str = RECORDER_CONFIG_DIR) -> list[str]:
    """Discover available sample rates from recorder config filenames (sr{N}MHz.yaml).
    
    Falls back to default options if directory doesn't exist or no configs match.
    """
    default_rates = ["1", "2", "4", "8", "10", "16", "20", "32", "64"]
    pattern = re.compile(r"^sr(\d+)MHz\.yaml$")
    rates = set()
    
    try:
        for name in os.listdir(recorder_config_dir):
            match = pattern.match(name)
            if match:
                rates.add(int(match.group(1)))
    except OSError as e:
        logging.warning(
            "Could not read recorder config directory '%s': %s. Using default sample rates.",
            recorder_config_dir,
            e,
        )
        return default_rates.copy()
    
    if not rates:
        logging.warning(
            "No sample-rate configs matching 'sr{N}MHz.yaml' in '%s'. Using default sample rates.",
            recorder_config_dir,
        )
        return default_rates.copy()
    
    return [str(rate) for rate in sorted(rates)]


def derive_spec_topic_from_primary_mac(spec_topic_prefix: str = "radiohound/clients/data/") -> Optional[str]:
    """Build radiohound data topic from the system primary-route MAC address.
    
    Returns the derived topic string, or None if system network info cannot be read.
    Logs detailed errors but does not raise exceptions.
    """
    primary_if = None
    try:
        with open("/proc/net/route", "r", encoding="utf-8") as fh:
            next(fh, None)  # header
            for line in fh:
                cols = line.strip().split()
                if len(cols) < 4:
                    continue
                iface, dest_hex, _gateway_hex, flags_hex = cols[0], cols[1], cols[2], cols[3]
                if dest_hex != "00000000":
                    continue
                flags = int(flags_hex, 16)
                if (flags & 0x2) == 0:
                    continue
                primary_if = iface
                break
    except Exception as e:
        logging.warning(f"SPEC topic derivation failed reading /proc/net/route: {e}")
        return None
    
    if not primary_if:
        logging.warning("SPEC topic derivation failed: no primary route interface found")
        return None
    
    mac_path = f"/sys/class/net/{primary_if}/address"
    try:
        with open(mac_path, "r", encoding="utf-8") as fh:
            mac = fh.read().strip().lower()
    except Exception as e:
        logging.warning(f"SPEC topic derivation failed reading {mac_path}: {e}")
        return None
    
    if not re.fullmatch(r"[0-9a-f]{2}(?::[0-9a-f]{2}){5}", mac):
        logging.warning(f"SPEC topic derivation failed: invalid MAC '{mac}' on {primary_if}")
        return None
    
    node_id = mac.replace(":", "")
    topic = f"{spec_topic_prefix}{node_id}"
    logging.info(f"Derived SPEC topic: {topic} (from {primary_if} MAC {mac})")
    return topic


# ===== Discover available sample rates from recorder configs ===== #
SAMPLE_RATE_OPTIONS = discover_sample_rate_options()

# ===== SYSTEM-LEVEL FUNCTIONS (pure subprocess, no MQTT) ===== #

def sync_ntp_on_rfsoc(scripts_dir: str = None) -> bool:
    """Run NTP sync script on RFSoC. Returns True if successful."""
    if scripts_dir is None:
        scripts_dir = os.path.dirname(os.path.abspath(__file__))

    script_path = os.path.join(scripts_dir, "rfsoc_update_ntp.bash")
    try:
        result = os.system(script_path)
        if result == 0:
            logging.info("NTP sync completed successfully")
            return True
        else:
            logging.warning(f"NTP sync script exited with code {result}")
            return False
    except Exception as e:
        logging.warning(f"Failed to run NTP sync: {e}")
        return False


def get_primary_network_info_detailed() -> dict:
    """Query primary network interface info with structured diagnostics.

    Returns:
      {
        "status": str,
        "mac": str,
        "ipv4": str,
        "error_code": str|None,
        "detail": str|None,
      }
    """
    result = {
        "status": "Offline",
        "mac": "-",
        "ipv4": "-",
        "error_code": None,
        "detail": None,
    }

    iface = None
    try:
        out = subprocess.check_output(["ip", "route", "show", "default"], text=True, timeout=1.0)
        m = re.search(r"\bdev\s+(\S+)", out)
        if m:
            iface = m.group(1)
    except Exception as e:
        logging.debug(f"Failed to get primary interface via 'ip route': {e}")
        result["error_code"] = "ip_route_failed"
        result["detail"] = str(e)

    if not iface:
        try:
            ifaces = [n for n in os.listdir("/sys/class/net") if n != "lo"]
            if ifaces:
                iface = sorted(ifaces)[0]
        except Exception as e:
            logging.debug(f"Failed to list network interfaces: {e}")
            result["error_code"] = "list_interfaces_failed"
            result["detail"] = str(e)

    if not iface:
        if result["error_code"] is None:
            result["error_code"] = "no_interface"
            result["detail"] = "No primary network interface found"
        return result

    mac = "-"
    ip4 = "-"
    oper = "unknown"

    try:
        with open(f"/sys/class/net/{iface}/address", "r", encoding="utf-8") as f:
            mac = f.read().strip() or "-"
    except Exception as e:
        logging.debug(f"Failed to read MAC for {iface}: {e}")
        if result["error_code"] is None:
            result["error_code"] = "read_mac_failed"
            result["detail"] = str(e)

    try:
        with open(f"/sys/class/net/{iface}/operstate", "r", encoding="utf-8") as f:
            oper = (f.read().strip() or "unknown").lower()
    except Exception as e:
        logging.debug(f"Failed to read operstate for {iface}: {e}")
        if result["error_code"] is None:
            result["error_code"] = "read_operstate_failed"
            result["detail"] = str(e)

    try:
        out = subprocess.check_output(["ip", "-4", "addr", "show", "dev", iface], text=True, timeout=1.0)
        m = re.search(r"\binet\s+(\d+\.\d+\.\d+\.\d+)\/", out)
        if m:
            ip4 = m.group(1)
    except Exception as e:
        logging.debug(f"Failed to get IPv4 address for {iface}: {e}")
        if result["error_code"] is None:
            result["error_code"] = "read_ipv4_failed"
            result["detail"] = str(e)

    online = (oper == "up") and (ip4 != "-")
    result["status"] = f"{'Online' if online else 'Offline'} ({iface})"
    result["mac"] = mac
    result["ipv4"] = ip4
    if not online and result["error_code"] is None:
        result["error_code"] = "interface_not_online"
        result["detail"] = f"operstate={oper}, ipv4={ip4}"
    return result


def get_primary_network_info() -> tuple[str, str, str]:
    """Query primary network interface info. Returns (status, mac, ipv4)."""
    details = get_primary_network_info_detailed()
    return details["status"], details["mac"], details["ipv4"]


def get_thermal_info_detailed(limit: int = 6) -> dict:
    """Read thermal zones with structured diagnostics.

    Returns:
      {
        "temps": list[tuple[str, float]],
        "error_code": str|None,
        "detail": str|None,
      }
    """
    result = {
        "temps": [],
        "error_code": None,
        "detail": None,
    }
    base = "/sys/class/thermal"
    try:
        entries = sorted(name for name in os.listdir(base) if name.startswith("thermal_zone"))
    except Exception as e:
        result["error_code"] = "list_thermal_failed"
        result["detail"] = str(e)
        return result

    if not entries:
        result["error_code"] = "no_thermal_zones"
        result["detail"] = "No thermal_zone entries found"
        return result

    read_errors = []
    temps = []
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
                temps.append((tname or name, temp_c))
        except Exception as e:
            read_errors.append(f"{name}: {e}")
            continue

        if len(temps) >= limit:
            break

    result["temps"] = temps
    if not temps:
        result["error_code"] = "thermal_read_failed"
        result["detail"] = "; ".join(read_errors) if read_errors else "No valid thermal readings"
    elif read_errors:
        result["error_code"] = "thermal_partial"
        result["detail"] = "; ".join(read_errors)
    return result


def get_docker_status() -> dict:
    """Query docker engine and compose service status."""
    result = {
        "engine_status": "unavailable",
        "services_summary": "0/0",
        "last_refresh": datetime.now().strftime("%H:%M:%S"),
    }

    try:
        subprocess.run(
            ["docker", "ps", "-q"],
            capture_output=True, text=True, timeout=5,
        )
        result["engine_status"] = "running"
    except Exception as e:
        logging.debug(f"Docker query failed: {e}")
        return result

    try:
        compose_output = subprocess.run(
            ["docker", "compose", "-f", "/opt/radiohound/docker/docker-compose.yaml", "ps", "--format=json"],
            capture_output=True, text=True, timeout=5,
        )
        if compose_output.returncode == 0:
            try:
                services = json.loads(compose_output.stdout) if compose_output.stdout.strip() else []
                running = sum(1 for s in services if isinstance(s, dict) and s.get("State") == "running")
                total = len(services)
                result["services_summary"] = f"{running}/{total}"
            except Exception:
                result["services_summary"] = "?/?"
    except Exception as e:
        logging.debug(f"Docker compose query failed: {e}")

    return result


def get_jetson_power_mode() -> Optional[tuple[str, str]]:
    """Query Jetson nvpmodel power mode. Returns (mode_id, mode_name) or None."""
    try:
        out = subprocess.check_output(
            ["nvpmodel", "-q"],
            stderr=subprocess.DEVNULL,
            timeout=1.5,
            text=True,
        )
    except Exception as e:
        logging.debug(f"Failed to query nvpmodel: {e}")
        return None

    mode_name = None
    mode_id = None
    for line in out.splitlines():
        s = line.strip()
        m = re.search(r"NV\s*Power\s*Mode\s*:\s*(.+)$", s, re.IGNORECASE)
        if m:
            mode_name = m.group(1).strip()
            continue
        m = re.search(r"Power\s*Mode\s*:\s*(.+)$", s, re.IGNORECASE)
        if m:
            mode_name = m.group(1).strip()
            continue
        if s.isdigit():
            mode_id = s

    return (mode_id, mode_name) if mode_id or mode_name else None


def set_jetson_power_mode(mode_id: str) -> bool:
    """Set Jetson nvpmodel power mode. Returns True if successful."""
    result = set_jetson_power_mode_detailed(mode_id)
    if result.get("ok"):
        return True

    logging.error(
        "Failed to set Jetson power mode %s (%s): %s",
        mode_id,
        result.get("error_code") or "unknown",
        result.get("detail") or "no detail",
    )
    return False


def set_jetson_power_mode_detailed(mode_id: str) -> dict:
    """Set Jetson nvpmodel mode with structured diagnostics.

    Returns a dict:
      {"ok": bool, "mode_id": str, "error_code": str|None, "detail": str|None}
    """
    result = {
        "ok": False,
        "mode_id": str(mode_id),
        "error_code": None,
        "detail": None,
    }

    try:
        cmd = ["nvpmodel", "-m", str(mode_id)]
        cmd_prefix = []

        if os.geteuid() != 0:
            try:
                check = subprocess.run(
                    ["sudo", "-n", "true"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=2.0,
                )
            except subprocess.TimeoutExpired:
                result["error_code"] = "sudo_check_timeout"
                result["detail"] = "sudo availability check timed out"
                return result

            if check.returncode != 0:
                result["error_code"] = "sudo_not_available"
                result["detail"] = (check.stderr or check.stdout or "passwordless sudo unavailable").strip()
                return result

            cmd_prefix = ["sudo", "-n"]
            cmd = [*cmd_prefix, *cmd]

        try:
            probe = subprocess.run(
                [*cmd_prefix, "nvpmodel", "-q"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=8.0,
            )
        except subprocess.TimeoutExpired:
            result["error_code"] = "probe_timeout"
            result["detail"] = "nvpmodel probe timed out"
            return result

        if probe.returncode != 0:
            result["error_code"] = "probe_failed"
            result["detail"] = (probe.stderr or probe.stdout or "nvpmodel -q failed").strip()
            return result

        try:
            proc = subprocess.run(
                cmd,
                input="YES\n",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=15.0,
            )
        except subprocess.TimeoutExpired:
            result["error_code"] = "apply_timeout"
            result["detail"] = "nvpmodel apply timed out"
            return result

        if proc.returncode != 0:
            result["error_code"] = "apply_failed"
            result["detail"] = (proc.stderr or proc.stdout or "nvpmodel -m failed").strip()
            return result

        result["ok"] = True
        output = ((proc.stdout or "") + " " + (proc.stderr or "")).strip()
        result["detail"] = output or "nvpmodel accepted mode"
        logging.info(f"Jetson power mode set to {mode_id}")
        return result
    except FileNotFoundError:
        result["error_code"] = "nvpmodel_not_found"
        result["detail"] = "nvpmodel command not found"
        return result
    except Exception as e:
        result["error_code"] = "exception"
        result["detail"] = str(e)
        return result


# ===== GPSD MONITOR (business logic; GUI-agnostic) ===== #

class GPSDMonitor:
    """Persistent gpsd stream monitor with semantic GPS state decoding.

    Responsibilities:
    - Maintain one reconnecting socket session to gpsd.
    - Decode gpsd JSON + NMEA into stable semantic state fields.
    - Accept outbound gpsd commands via queue.
    - Emit line/state updates through registered callbacks.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 2947, reconnect_delay_s: float = 5.0):
        self._host = host
        self._port = port
        self._reconnect_delay_s = reconnect_delay_s

        self._cmd_queue: queue.Queue[str] = queue.Queue()
        self._run = False
        self._thread: Optional[threading.Thread] = None

        self._state_lock = threading.Lock()
        self._state = self._state_defaults()

        self._line_callbacks: list[Callable[[str, str], None]] = []
        self._state_callbacks: list[Callable[[dict], None]] = []

        self._fix_hints = {
            "rmc_valid": None,
            "gga_quality": None,
            "tpv_mode": None,
            "gsa_mode": None,
        }
        self._gsv_partial: dict[str, dict] = {}
        self._gsv_last_complete: dict[str, dict] = {}

    def _state_defaults(self) -> dict:
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

    def on_line(self, callback: Callable[[str, str], None]):
        self._line_callbacks.append(callback)

    def on_state(self, callback: Callable[[dict], None], emit_initial: bool = True):
        self._state_callbacks.append(callback)
        if emit_initial:
            callback(self.get_state())

    def get_state(self) -> dict:
        with self._state_lock:
            return dict(self._state)

    def start(self):
        if self._run:
            return
        self._run = True
        self._thread = threading.Thread(target=self._worker, daemon=True, name="gpsd_monitor")
        self._thread.start()

    def stop(self):
        self._run = False
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None

    def send_command(self, cmd: str):
        cmd = (cmd or "").strip()
        if not cmd:
            raise ValueError("GPSD command cannot be empty")
        self._cmd_queue.put(cmd)

    def set_watch(self, enable: bool, raw: int = 0):
        if enable:
            self.send_command(f'?WATCH={{"enable":true,"raw":{int(raw)}}}')
        else:
            self.send_command('?WATCH={"enable":false}')

    def _emit_line(self, direction: str, line: str):
        for cb in list(self._line_callbacks):
            try:
                cb(direction, line)
            except Exception as e:
                logging.warning(f"GPSD line callback failed: {e}")

    def _emit_state(self):
        snap = self.get_state()
        for cb in list(self._state_callbacks):
            try:
                cb(snap)
            except Exception as e:
                logging.warning(f"GPSD state callback failed: {e}")

    def _set(self, key: str, value, force: bool = False):
        if value is None and not force:
            return
        val = "" if value is None else str(value)
        if not force and not val.strip():
            return
        with self._state_lock:
            self._state[key] = val

    def _set_float(self, key: str, value, fmt: str):
        self._set(key, format(float(value), fmt))

    def _worker(self):
        while self._run:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(5.0)
                    s.connect((self._host, self._port))
                    s.settimeout(0.5)

                    self._set("gpsd_conn_status", "Connected", force=True)
                    self._emit_state()

                    init_cmd = '?WATCH={"enable":true,"raw":0};'
                    s.sendall((init_cmd + "\n").encode("ascii", errors="ignore"))
                    self._emit_line("TX", init_cmd)
                    self._set("gpsd_watch_state", "enabled raw=0", force=True)
                    self._emit_state()

                    buf = ""
                    while self._run:
                        self._drain_command_queue(s)

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
                            self._handle_line(line)

            except Exception as e:
                self._set("gpsd_conn_status", f"Disconnected ({e})", force=True)
                self._set("gps_fix_status", "Unknown", force=True)
                self._recompute_fix_and_summary()
                self._emit_state()

            if self._run:
                time.sleep(self._reconnect_delay_s)

    def _drain_command_queue(self, sock: socket.socket):
        while True:
            try:
                cmd = self._cmd_queue.get_nowait().strip()
            except queue.Empty:
                return

            if not cmd:
                continue
            if not cmd.endswith(";"):
                cmd += ";"
            sock.sendall((cmd + "\n").encode("ascii", errors="ignore"))
            self._emit_line("TX", cmd)

    def _handle_line(self, line: str):
        self._emit_line("RX", line)

        if line.startswith("$"):
            self._parse_nmea(line)
            self._emit_state()
            return

        if not line.startswith("{"):
            return

        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            return

        self._apply_json(msg)
        self._emit_state()

    def _apply_json(self, msg: dict):
        if not isinstance(msg, dict):
            return

        cls = str(msg.get("class", "")).upper()
        if not cls:
            return

        if cls == "DEVICE":
            self._set("gpsd_device", msg.get("path"))
            self._set("gpsd_driver", msg.get("driver"))
            if msg.get("bps") is not None:
                self._set("gpsd_baud", str(int(msg.get("bps"))))
            if msg.get("cycle") is not None:
                self._set_float("gpsd_update_rate_s", msg.get("cycle"), ".2f")

        elif cls == "DEVICES":
            devs = msg.get("devices")
            if isinstance(devs, list) and devs and isinstance(devs[0], dict):
                self._apply_json(dict(devs[0], **{"class": "DEVICE"}))

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
            self._set("gpsd_watch_state", state, force=True)

        elif cls == "VERSION":
            rel = msg.get("release")
            rev = msg.get("rev")
            if rel:
                text = f"gpsd {rel}"
                if rev:
                    text += f" ({rev})"
                self._set("gpsd_driver", text, force=True)

        elif cls == "TPV":
            if msg.get("mode") is not None:
                self._fix_hints["tpv_mode"] = int(msg.get("mode"))
            if msg.get("time"):
                self._set("gps_utc_time", msg.get("time"))
            if msg.get("lat") is not None:
                self._set_float("gps_lat", msg.get("lat"), ".6f")
            if msg.get("lon") is not None:
                self._set_float("gps_lon", msg.get("lon"), ".6f")
            if msg.get("alt") is not None:
                self._set_float("gps_alt_m", msg.get("alt"), ".2f")
            if msg.get("speed") is not None:
                self._set_float("gps_speed_kn", float(msg.get("speed")) * 1.943844, ".3f")

        elif cls == "SKY":
            sats = msg.get("satellites")
            if isinstance(sats, list):
                visible = len(sats)
                used = 0
                counts = {"GPS": 0, "GLONASS": 0, "GALILEO": 0, "BEIDOU": 0}
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

                self._set("gps_sats_visible", str(visible), force=True)
                self._set("gps_sats_used", str(used), force=True)
                self._set("gps_sats_gps", str(counts["GPS"]), force=True)
                self._set("gps_sats_glonass", str(counts["GLONASS"]), force=True)
                self._set("gps_sats_galileo", str(counts["GALILEO"]), force=True)
                self._set("gps_sats_beidou", str(counts["BEIDOU"]), force=True)

            if msg.get("pdop") is not None:
                self._set_float("gps_pdop", msg.get("pdop"), ".2f")
            if msg.get("hdop") is not None:
                self._set_float("gps_hdop", msg.get("hdop"), ".2f")
            if msg.get("vdop") is not None:
                self._set_float("gps_vdop", msg.get("vdop"), ".2f")

        self._recompute_fix_and_summary()

    def _parse_nmea(self, line: str):
        clean = line.split("*")[0]
        parts = clean.lstrip("$").split(",")
        if not parts or len(parts[0]) < 3:
            return

        talker = parts[0][:2]
        sentence = parts[0][2:]

        if sentence == "RMC":
            self._parse_rmc(parts)
        elif sentence == "GGA":
            self._parse_gga(parts)
        elif sentence == "GSA":
            self._parse_gsa(parts)
        elif sentence == "GSV":
            self._parse_gsv(talker, parts)
        elif sentence == "ZDA":
            self._parse_zda(parts)

        self._recompute_fix_and_summary()

    def _parse_rmc(self, parts: list[str]):
        if len(parts) < 10:
            return

        t_utc = self._fmt_iso_from_rmc(parts[1], parts[9])
        if t_utc:
            self._set("gps_utc_time", t_utc)

        status = (parts[2] or "").upper()
        if status:
            self._fix_hints["rmc_valid"] = (status == "A")

        lat = self._nmea_to_decimal(parts[3], parts[4])
        lon = self._nmea_to_decimal(parts[5], parts[6])
        if lat is not None:
            self._set("gps_lat", f"{lat:.6f}")
        if lon is not None:
            self._set("gps_lon", f"{lon:.6f}")

        if parts[7]:
            self._set_float("gps_speed_kn", parts[7], ".3f")

    def _parse_gga(self, parts: list[str]):
        if len(parts) < 10:
            return

        t_utc = self._fmt_hms(parts[1])
        if t_utc:
            self._set("gps_utc_time", t_utc)

        lat = self._nmea_to_decimal(parts[2], parts[3])
        lon = self._nmea_to_decimal(parts[4], parts[5])
        if lat is not None:
            self._set("gps_lat", f"{lat:.6f}")
        if lon is not None:
            self._set("gps_lon", f"{lon:.6f}")

        if parts[6]:
            q = int(parts[6])
            self._fix_hints["gga_quality"] = q
            self._set("gps_fix_quality", self._fix_quality_text(q), force=True)

        if parts[7]:
            self._set("gps_sats_used", str(int(parts[7])), force=True)
        if parts[8]:
            self._set_float("gps_hdop", parts[8], ".2f")
        if parts[9]:
            self._set_float("gps_alt_m", parts[9], ".2f")

    def _parse_gsa(self, parts: list[str]):
        if len(parts) < 18:
            return

        if parts[2]:
            self._fix_hints["gsa_mode"] = int(parts[2])

        used = sum(1 for sv in parts[3:15] if sv.strip())
        self._set("gps_sats_used", str(used), force=True)

        if parts[15]:
            self._set_float("gps_pdop", parts[15], ".2f")
        if parts[16]:
            self._set_float("gps_hdop", parts[16], ".2f")
        if parts[17]:
            self._set_float("gps_vdop", parts[17], ".2f")

    def _parse_gsv(self, talker: str, parts: list[str]):
        if len(parts) < 4:
            return

        total_msgs = int(parts[1] or 0)
        msg_num = int(parts[2] or 0)
        total_visible = int(parts[3] or 0)
        if total_msgs <= 0 or msg_num <= 0:
            return

        key = talker.upper()
        cycle = self._gsv_partial.get(key)
        if cycle is None or msg_num == 1 or cycle.get("expected") != total_msgs:
            cycle = {
                "expected": total_msgs,
                "seen": set(),
                "visible": total_visible,
                "counts": {"GPS": 0, "GLONASS": 0, "GALILEO": 0, "BEIDOU": 0},
            }
            self._gsv_partial[key] = cycle

        cycle["seen"].add(msg_num)
        cycle["visible"] = max(cycle["visible"], total_visible)

        idx = 4
        while idx + 3 < len(parts):
            prn_txt = parts[idx].strip()
            if prn_txt:
                prn = int(prn_txt)
                const = self._constellation_from_prn(key, prn)
                if const in cycle["counts"]:
                    cycle["counts"][const] += 1
            idx += 4

        if len(cycle["seen"]) >= cycle["expected"]:
            self._gsv_last_complete[key] = {
                "visible": cycle["visible"],
                "counts": dict(cycle["counts"]),
            }
            self._gsv_partial.pop(key, None)

            totals = {"GPS": 0, "GLONASS": 0, "GALILEO": 0, "BEIDOU": 0}
            visible_total = 0
            for data in self._gsv_last_complete.values():
                visible_total += int(data.get("visible", 0))
                for const in totals:
                    totals[const] += int(data.get("counts", {}).get(const, 0))

            if visible_total > 0:
                self._set("gps_sats_visible", str(visible_total), force=True)
            self._set("gps_sats_gps", str(totals["GPS"]), force=True)
            self._set("gps_sats_glonass", str(totals["GLONASS"]), force=True)
            self._set("gps_sats_galileo", str(totals["GALILEO"]), force=True)
            self._set("gps_sats_beidou", str(totals["BEIDOU"]), force=True)

    def _parse_zda(self, parts: list[str]):
        if len(parts) < 5:
            return
        t = self._fmt_hms(parts[1])
        d = parts[2]
        m = parts[3]
        y = parts[4]
        if t and d and m and y:
            self._set("gps_utc_time", f"{y}-{m.zfill(2)}-{d.zfill(2)}T{t}")

    def _recompute_fix_and_summary(self):
        fix = "Unknown"
        if self._fix_hints.get("rmc_valid") is False:
            fix = "No fix"
        elif isinstance(self._fix_hints.get("gga_quality"), int):
            q = self._fix_hints["gga_quality"]
            fix = "No fix" if q <= 0 else self._fix_quality_text(q)
        elif isinstance(self._fix_hints.get("tpv_mode"), int):
            mode = self._fix_hints["tpv_mode"]
            fix = "No fix" if mode <= 1 else ("2D" if mode == 2 else "3D")
        elif isinstance(self._fix_hints.get("gsa_mode"), int):
            mode = self._fix_hints["gsa_mode"]
            fix = "No fix" if mode <= 1 else ("2D" if mode == 2 else "3D")

        self._set("gps_fix_status", fix, force=True)
        if fix == "No fix":
            self._set("gps_fix_quality", "Invalid", force=True)

        with self._state_lock:
            summary = (
                f"{self._state.get('gpsd_conn_status', 'Unknown')} | "
                f"Fix: {self._state.get('gps_fix_status', 'Unknown')} | "
                f"Sats used/visible: {self._state.get('gps_sats_used', '0')}/"
                f"{self._state.get('gps_sats_visible', '0')} | "
                f"Device: {self._state.get('gpsd_device', 'Not reported')}"
            )
        self._set("gps_summary", summary, force=True)

    def _nmea_to_decimal(self, raw: str, hemi: str):
        if not raw:
            return None
        v = float(raw)
        deg = int(v // 100)
        minutes = v - (deg * 100)
        decimal = deg + minutes / 60.0
        hemi = (hemi or "").upper()
        if hemi in ("S", "W"):
            decimal *= -1.0
        return decimal

    def _fmt_hms(self, hhmmss: str):
        if not hhmmss or len(hhmmss) < 6:
            return None
        core = hhmmss.split(".")[0]
        if len(core) < 6:
            return None
        return f"{core[0:2]}:{core[2:4]}:{core[4:6]}Z"

    def _fmt_iso_from_rmc(self, hhmmss: str, ddmmyy: str):
        if not hhmmss or not ddmmyy or len(ddmmyy) != 6:
            return self._fmt_hms(hhmmss)
        t = self._fmt_hms(hhmmss)
        if t is None:
            return None
        day = ddmmyy[0:2]
        month = ddmmyy[2:4]
        year = int(ddmmyy[4:6])
        year += 2000 if year < 80 else 1900
        return f"{year:04d}-{month}-{day}T{t}"

    def _fix_quality_text(self, quality: int):
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

    def _constellation_from_prn(self, talker: str, prn: int):
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


# ===== MEP BUS ===== #

class MEPBus:
    """Always-on MQTT connection, listener registry, and thin command publishers.

    Created at startup (GUI or CLI). Subscribes to all topics (#) so callers
    can register listeners for any status/data feed. Never None once constructed.

    Thin publishers: rfsoc_reset(), tuner_*, recorder_*, afe_* — fire-and-forget
    MQTT commands. No sweep state, no sync waits, no subprocess calls.
    """

    def __init__(self, broker: str = MQTT_BROKER, port: int = MQTT_PORT):
        self._broker = broker
        self._port = port

        # ---- Listener registry ----
        self._listeners: dict[str, list[Callable]] = {}
        self._global_listeners: list[Callable] = []
        self._pattern_listeners: list[tuple[str, Callable]] = []
        self._connection_listeners: list[Callable[[dict], None]] = []
        self._status_cache: dict[str, dict] = {}
        self._cache_lock = threading.Lock()

        # ---- MQTT connection state ----
        self._connected = False
        self._last_error: Optional[str] = None
        self._loop_started = False

        # ---- AFE announce (retained — full service schema + capabilities) ----
        self.afe_announce: Optional[dict] = None

        # ---- SPEC topic (pattern for radiohound client spectrum streams) ----
        self.spec_topic = SPEC_TOPIC_PATTERN

        # ---- MQTT client ----
        self._client = mqtt_lib.Client(
            callback_api_version=mqtt_lib.CallbackAPIVersion.VERSION1,
            client_id=f"mep_bus_{int(time.time())}",
        )
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.on_disconnect = self._on_disconnect

        logging.info(f"Connecting to MQTT broker {broker}:{port}")
        try:
            self._client.connect(broker, port, keepalive=60)
            self._client.loop_start()
            self._loop_started = True
            time.sleep(0.5)
        except OSError as e:
            self._last_error = str(e)
            self._connected = False
            logging.warning(
                "MQTT offline: could not connect to %s:%s (%s). Running in offline mode.",
                broker,
                port,
                e,
            )

    # ------------------------------------------------------------------ #
    #  Listener registry                                                   #
    # ------------------------------------------------------------------ #

    def on_status(self, topic: str, callback: Callable[[dict], None]):
        """Register callback(data: dict) for JSON messages on a specific topic.

        If cached data already exists for this topic (e.g. a retained message
        that arrived before the listener was registered), the callback is fired
        immediately on the calling thread.
        """
        self._listeners.setdefault(topic, []).append(callback)
        cached = self.get_cached_status(topic)
        if isinstance(cached, dict):
            try:
                callback(cached)
            except Exception as e:
                logging.warning("Status listener initial emit failed for %s: %s", topic, e)

    def on_message(self, callback: Callable[[str, bytes], None]):
        """Register callback(topic, payload_bytes) for ALL MQTT messages."""
        self._global_listeners.append(callback)

    def on_status_pattern(self, pattern: str, callback: Callable[[str, dict], None]):
        """Register callback(topic, data) for JSON messages matching MQTT wildcard pattern.
        
        Supports MQTT wildcards:
        - '+' matches exactly one level between slashes
        - '#' matches zero or more levels at end (must be last character)
        
        Example: on_status_pattern("radiohound/clients/data/+", handler)
        """
        self._pattern_listeners.append((pattern, callback))

    def on_connection_state(self, callback: Callable[[dict], None], emit_initial: bool = True):
        """Register callback(status_dict) for MQTT connection state changes."""
        self._connection_listeners.append(callback)
        if emit_initial:
            try:
                callback(self.get_connection_status())
            except Exception as e:
                logging.warning(f"Connection listener failed during initial emit: {e}")

    def remove_connection_listener(self, callback: Callable[[dict], None]):
        """Unregister a previously registered connection state listener."""
        listeners = self._connection_listeners
        if callback in listeners:
            listeners.remove(callback)

    def remove_listener(self, topic: str, callback: Callable):
        """Unregister a previously registered topic listener."""
        listeners = self._listeners.get(topic, [])
        if callback in listeners:
            listeners.remove(callback)

    def get_cached_status(self, topic: str) -> Optional[dict]:
        """Return last seen JSON message on topic, or None."""
        with self._cache_lock:
            return self._status_cache.get(topic)

    def get_tuner_status_normalized(self) -> Optional[dict]:
        """Return normalized tuner status from cached MQTT payload, or None."""
        return self.normalize_tuner_status(self.get_cached_status(TUNER_STATUS_TOPIC))

    def is_connected(self) -> bool:
        """Return True when MQTT client is currently connected to the broker."""
        return self._connected

    def get_connection_status(self) -> dict:
        """Return current MQTT connection state for UI/CLI display."""
        return {
            "connected": self._connected,
            "broker": self._broker,
            "port": self._port,
            "last_error": self._last_error,
        }

    @staticmethod
    def _is_spec_topic(topic: str) -> bool:
        return str(topic).startswith("radiohound/clients/data/")

    @staticmethod
    def _spec_payload_is_finite(data: dict) -> bool:
        payload = data.get("data")
        if not isinstance(payload, str):
            return False
        try:
            raw = base64.b64decode(payload, validate=True)
        except Exception:
            return False
        if len(raw) < 4 or len(raw) % 4 != 0:
            return False
        try:
            for (value,) in struct.iter_unpack("<f", raw):
                if not math.isfinite(value):
                    return False
        except Exception:
            return False
        return True

    def reconnect(self) -> bool:
        """Attempt one MQTT reconnect cycle. Returns True on success."""
        try:
            self._client.reconnect()
            if not self._loop_started:
                self._client.loop_start()
                self._loop_started = True
            return True
        except OSError as e:
            self._last_error = str(e)
            self._connected = False
            logging.warning(
                "MQTT reconnect failed for %s:%s (%s)",
                self._broker,
                self._port,
                e,
            )
            self._emit_connection_state()
            return False

    def _emit_connection_state(self):
        """Notify registered listeners of current MQTT connection state."""
        status = self.get_connection_status()
        for cb in list(self._connection_listeners):
            try:
                cb(status)
            except Exception as e:
                logging.warning(f"Connection listener failed: {e}")

    # ------------------------------------------------------------------ #
    #  MQTT internals                                                      #
    # ------------------------------------------------------------------ #

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self._connected = True
            self._last_error = None
            logging.info("MQTT connected")
            self._emit_connection_state()
            client.subscribe("#")
        else:
            self._connected = False
            self._last_error = f"rc={rc}"
            logging.error(f"MQTT connect failed: rc={rc}")
            self._emit_connection_state()

    def _on_message(self, client, userdata, msg):
        # Fire global listeners (raw bytes — for MQTT log tab)
        for cb in self._global_listeners:
            cb(msg.topic, msg.payload)

        # Parse JSON
        try:
            data = json.loads(msg.payload.decode())
        except Exception:
            return

        if self._is_spec_topic(msg.topic) and isinstance(data, dict) and not self._spec_payload_is_finite(data):
            logging.warning("Dropping invalid SPEC frame on %s", msg.topic)
            return

        # Update status cache
        if isinstance(data, dict):
            with self._cache_lock:
                self._status_cache[msg.topic] = data

        # Intercept afe/announce (retained) — cache full schema
        if msg.topic == AFE_ANNOUNCE_TOPIC and isinstance(data, dict):
            self.afe_announce = data
            logging.info(f"AFE announce received: v{data.get('version', '?')}")

        # Fire exact-match topic-specific listeners
        for cb in self._listeners.get(msg.topic, []):
            cb(data)
        
        # Fire pattern-match listeners
        if isinstance(data, dict):
            for pattern, cb in self._pattern_listeners:
                if self._mqtt_topic_matches(msg.topic, pattern):
                    cb(msg.topic, data)

    def _on_disconnect(self, client, userdata, rc):
        self._connected = False
        if rc != 0:
            self._last_error = f"disconnect rc={rc}"
            logging.warning(f"MQTT unexpectedly disconnected: rc={rc}")
        self._emit_connection_state()

    def _mqtt_topic_matches(self, topic: str, pattern: str) -> bool:
        """Check if topic matches MQTT wildcard pattern.
        
        '+' matches exactly one level between slashes
        '#' matches zero or more levels at end
        """
        topic_parts = topic.split('/')
        pattern_parts = pattern.split('/')
        
        # Quick exit: pattern longer than topic (unless ends with #)
        if len(pattern_parts) > len(topic_parts) and pattern_parts[-1] != '#':
            return False
        
        for i, p_part in enumerate(pattern_parts):
            if p_part == '#':
                # Multi-level wildcard matches rest of topic
                return True
            if i >= len(topic_parts):
                return False
            if p_part == '+':
                # Single-level wildcard matches this level
                continue
            if p_part != topic_parts[i]:
                # Exact match required, failed
                return False
        
        # All pattern parts matched; topic must be same length
        return len(topic_parts) == len(pattern_parts)

    def publish_command(self, topic: str, payload: dict, sleep_s: float = 0.1):
        """Publish a JSON command to a topic. Used by CaptureController and thin publishers."""
        if not self._connected:
            logging.warning(
                "MQTT offline: command not sent to %s payload=%s",
                topic,
                payload,
            )
            return False

        info = self._client.publish(topic, json.dumps(payload))
        if info.rc != mqtt_lib.MQTT_ERR_SUCCESS:
            self._last_error = f"publish rc={info.rc}"
            logging.warning("MQTT publish failed: topic=%s rc=%s", topic, info.rc)
            return False

        if sleep_s:
            time.sleep(sleep_s)
        return True

    def publish(self, topic: str, payload_str: str):
        """Publish a raw string payload to a topic. For debug/manual use."""
        if not self._connected:
            logging.warning("MQTT offline: publish not sent to %s", topic)
            return False

        info = self._client.publish(topic, payload_str)
        if info.rc != mqtt_lib.MQTT_ERR_SUCCESS:
            self._last_error = f"publish rc={info.rc}"
            logging.warning("MQTT publish failed: topic=%s rc=%s", topic, info.rc)
            return False
        return True

    def disconnect(self):
        if self._loop_started:
            self._client.loop_stop()
            self._loop_started = False
        self._client.disconnect()

    # ------------------------------------------------------------------ #
    #  RFSoC                                                               #
    # ------------------------------------------------------------------ #

    def rfsoc_reset(self):
        """Send reset command to RFSoC."""
        self.publish_command(RFSOC_CMD_TOPIC, {"task_name": "reset"})

    def rfsoc_get_tlm(self):
        """Request RFSoC telemetry publish on rfsoc/status."""
        self.publish_command(RFSOC_CMD_TOPIC, {"task_name": "get", "arguments": ["tlm"]}, sleep_s=0)

    def rfsoc_capture_next_pps(self):
        """Arm capture on next PPS edge."""
        self.publish_command(RFSOC_CMD_TOPIC, {"task_name": "capture_next_pps"})

    def rfsoc_set_if(self, if_mhz: float):
        """Set RFSoC IF frequency in MHz."""
        self.publish_command(RFSOC_CMD_TOPIC, {"task_name": "set", "arguments": f"freq_IF {if_mhz}"})

    def rfsoc_set_tx_center_freq(self, freq_mhz: float):
        """Set TX DAC RFDC mixer/NCO center frequency on all TX channels."""
        self.publish_command(RFSOC_CMD_TOPIC, {"task_name": "set", "arguments": f"tx_center_freq {freq_mhz}"})

    def rfsoc_set_tx_offset_freq(self, freq_mhz: float):
        """Set TX function-generator baseband offset frequency."""
        self.publish_command(RFSOC_CMD_TOPIC, {"task_name": "set", "arguments": f"tx_offset_freq {freq_mhz}"})

    def rfsoc_set_tx_amplitude(self, amplitude_bins: int):
        """Set TX waveform peak amplitude in DAC bins (0..8191)."""
        self.publish_command(RFSOC_CMD_TOPIC, {"task_name": "set", "arguments": f"tx_amplitude {amplitude_bins}"})

    def rfsoc_set_tx_channel(self, channel_list: str):
        """Set TX DAC channel(s): A, B, or A,B."""
        self.publish_command(RFSOC_CMD_TOPIC, {"task_name": "set", "arguments": f"tx_channel {channel_list}"})

    # ------------------------------------------------------------------ #
    #  Tuner                                                               #
    # ------------------------------------------------------------------ #

    def tuner_init(self, force_tuner: str = None):
        """Send init_tuner command."""
        args = {"force_tuner": force_tuner} if force_tuner else {}
        self.publish_command(TUNER_CMD_TOPIC, {"task_name": "init_tuner", "arguments": args})

    def tuner_set_freq(self, freq_mhz: float):
        self.publish_command(TUNER_CMD_TOPIC, {"task_name": "set_freq", "arguments": {"freq_mhz": freq_mhz}})

    def tuner_get_freq(self):
        self.publish_command(TUNER_CMD_TOPIC, {"task_name": "get_freq", "arguments": {}})

    def tuner_set_power(self, pwr_dbm: float):
        self.publish_command(TUNER_CMD_TOPIC, {"task_name": "set_power", "arguments": {"pwr_dbm": pwr_dbm}})

    def tuner_get_power(self):
        self.publish_command(TUNER_CMD_TOPIC, {"task_name": "get_power", "arguments": {}})

    def tuner_check_lock(self):
        self.publish_command(TUNER_CMD_TOPIC, {"task_name": "get_lock_status", "arguments": {}})

    def tuner_restart(self):
        self.publish_command(TUNER_CMD_TOPIC, {"task_name": "restart_tuner", "arguments": {}})

    def tuner_status(self):
        self.publish_command(TUNER_CMD_TOPIC, {"task_name": "status", "arguments": {}})

    # ------------------------------------------------------------------ #
    #  Recorder                                                            #
    # ------------------------------------------------------------------ #

    def recorder_config_set(self, key: str, value):
        """Send a config.set command to the recorder."""
        self.publish_command(RECORDER_CMD_TOPIC, {
            "task_name": "config.set",
            "arguments": {"key": key, "value": value},
        })

    def recorder_config_load(self, config_name: str):
        """Send a config.load command to the recorder."""
        self.publish_command(RECORDER_CMD_TOPIC, {
            "task_name": "config.load",
            "arguments": {"name": config_name},
        })

    def recorder_enable(self):
        self.publish_command(RECORDER_CMD_TOPIC, {"task_name": "enable"})

    def recorder_disable(self):
        self.publish_command(RECORDER_CMD_TOPIC, {"task_name": "disable"})

    # ------------------------------------------------------------------ #
    #  AFE (MQTT-based service)                                            #
    # ------------------------------------------------------------------ #

    def afe_set_register(self, device: str, register: str, value: int):
        """Set a single AFE register by name."""
        self.publish_command(f"{AFE_CMD_TOPIC}/registers", {
            "task_name": "set_register",
            "arguments": {"device": device, "register": register, "value": value},
        })

    def afe_set_registers(self, device: str, registers: dict):
        """Bulk set multiple registers for a device."""
        self.publish_command(f"{AFE_CMD_TOPIC}/registers", {
            "task_name": "set_registers",
            "arguments": {device: registers},
        })

    def afe_set_attenuation(self, device: str, db: int, session_id: str = None):
        """Set RX attenuation 0-31 dB."""
        if not (0 <= db <= 31):
            raise ValueError(f"Attenuation must be 0-31 dB, got {db}")
        payload = {
            "task_name": "set_attenuation_db",
            "arguments": {"device": device, "db": db},
        }
        if session_id:
            payload["session_id"] = session_id
        self.publish_command(f"{AFE_CMD_TOPIC}/registers", payload)

    def afe_get_registers(self, device: str = "all"):
        """Query register state from AFE firmware."""
        self.publish_command(f"{AFE_CMD_TOPIC}/registers", {
            "task_name": "get_registers",
            "arguments": {"device": device},
        })

    def afe_status(self):
        """Request AFE service status."""
        self.publish_command(AFE_CMD_TOPIC, {"task_name": "status", "arguments": {}})

    def afe_describe(self):
        """Request AFE service capabilities and available commands."""
        self.publish_command(AFE_CMD_TOPIC, {"task_name": "describe", "arguments": {}})

    def afe_telem_dump(self):
        """Request one-shot telemetry dump from AFE service."""
        self.publish_command(AFE_CMD_TOPIC, {"task_name": "telem_dump", "arguments": {}})

    # ---- IMU ----

    def afe_set_acc_odr(self, odr: str):
        self.publish_command(f"{AFE_CMD_TOPIC}/imu", {
            "task_name": "set_acc_odr",
            "arguments": {"odr": odr},
        })

    def afe_set_gyr_odr(self, odr: str):
        self.publish_command(f"{AFE_CMD_TOPIC}/imu", {
            "task_name": "set_gyr_odr",
            "arguments": {"odr": odr},
        })

    def afe_set_imu_config(self, acc_odr: str = None, gyr_odr: str = None,
                           ahiperf: int = None, aulp: int = None, glp: int = None):
        args = {}
        if acc_odr is not None:
            args["acc_odr"] = acc_odr
        if gyr_odr is not None:
            args["gyr_odr"] = gyr_odr
        if ahiperf is not None:
            args["ahiperf"] = ahiperf
        if aulp is not None:
            args["aulp"] = aulp
        if glp is not None:
            args["glp"] = glp
        self.publish_command(f"{AFE_CMD_TOPIC}/imu", {
            "task_name": "set_imu",
            "arguments": args,
        })

    def afe_get_imu_params(self):
        self.publish_command(f"{AFE_CMD_TOPIC}/imu", {
            "task_name": "get_imu_params",
            "arguments": {},
        })

    # ---- Magnetometer ----

    def afe_set_mag_cycle_count(self, ccr: int):
        self.publish_command(f"{AFE_CMD_TOPIC}/mag", {
            "task_name": "set_cycle_count",
            "arguments": {"ccr": ccr},
        })

    def afe_set_mag_update_rate(self, updr: int):
        self.publish_command(f"{AFE_CMD_TOPIC}/mag", {
            "task_name": "set_update_rate",
            "arguments": {"updr": updr},
        })

    def afe_set_mag_config(self, ccr: int = None, updr: int = None):
        args = {}
        if ccr is not None:
            args["ccr"] = ccr
        if updr is not None:
            args["updr"] = updr
        self.publish_command(f"{AFE_CMD_TOPIC}/mag", {
            "task_name": "set_mag",
            "arguments": args,
        })

    def afe_get_mag_params(self):
        self.publish_command(f"{AFE_CMD_TOPIC}/mag", {
            "task_name": "get_mag_params",
            "arguments": {},
        })

    def afe_get_hk_rate(self):
        self.publish_command(f"{AFE_CMD_TOPIC}/hk", {
            "task_name": "get_rate",
            "arguments": {},
        })

    # ---- Telemetry polling interval (mode-13 compatible) ----

    def afe_set_polling_interval(self, n: int):
        self.publish_command(f"{AFE_CMD_TOPIC}/polling", {
            "task_name": "set_interval",
            "arguments": {"n": n},
        })

    def afe_get_polling_interval(self):
        self.publish_command(f"{AFE_CMD_TOPIC}/polling", {
            "task_name": "get_interval",
            "arguments": {},
        })

    # ---- Rates (per-subsystem set_rate with interval in seconds) ----

    def afe_set_hk_rate(self, n: int):
        self.publish_command(f"{AFE_CMD_TOPIC}/hk", {
            "task_name": "set_rate",
            "arguments": {"n": n},
        })

    def afe_set_mag_rate(self, n: int):
        self.publish_command(f"{AFE_CMD_TOPIC}/mag", {
            "task_name": "set_rate",
            "arguments": {"n": n},
        })

    def afe_set_imu_rate(self, n: int):
        self.publish_command(f"{AFE_CMD_TOPIC}/imu", {
            "task_name": "set_rate",
            "arguments": {"n": n},
        })

    # ---- Time ----

    def afe_set_time_source_gnss(self):
        self.publish_command(f"{AFE_CMD_TOPIC}/time", {
            "task_name": "set_source_gnss",
            "arguments": {},
        })

    def afe_set_time_source_external(self):
        self.publish_command(f"{AFE_CMD_TOPIC}/time", {
            "task_name": "set_source_external",
            "arguments": {},
        })

    def afe_set_time_epoch_pps(self, ts: int):
        self.publish_command(f"{AFE_CMD_TOPIC}/time", {
            "task_name": "set_epoch_pps",
            "arguments": {"ts": ts},
        })

    def afe_set_time_epoch_nmea(self):
        self.publish_command(f"{AFE_CMD_TOPIC}/time", {
            "task_name": "set_epoch_nmea",
            "arguments": {},
        })

    def afe_set_time_epoch_immediate(self, ts: int):
        self.publish_command(f"{AFE_CMD_TOPIC}/time", {
            "task_name": "set_epoch_immediate",
            "arguments": {"ts": ts},
        })

    def afe_get_time_params(self):
        self.publish_command(f"{AFE_CMD_TOPIC}/time", {
            "task_name": "get_time_params",
            "arguments": {},
        })

    # ---- Logging ----

    def afe_enable_logging(self):
        self.publish_command(f"{AFE_CMD_TOPIC}/logging", {
            "task_name": "enable_logging",
            "arguments": {},
        })

    def afe_disable_logging(self):
        self.publish_command(f"{AFE_CMD_TOPIC}/logging", {
            "task_name": "disable_logging",
            "arguments": {},
        })

    def afe_get_log_status(self):
        self.publish_command(f"{AFE_CMD_TOPIC}/logging", {
            "task_name": "get_log_status",
            "arguments": {},
        })

    def afe_set_log_path(self, path: str):
        self.publish_command(f"{AFE_CMD_TOPIC}/logging", {
            "task_name": "set_log_path",
            "arguments": {"path": path},
        })

    def afe_set_log_rate(self, n: int):
        self.publish_command(f"{AFE_CMD_TOPIC}/logging", {
            "task_name": "set_log_rate_sec",
            "arguments": {"n": n},
        })

    def afe_set_service_log_mode(self, mode: str):
        if mode not in ("normal", "debug"):
            raise ValueError(f"Mode must be 'normal' or 'debug', got {mode}")
        self.publish_command(f"{AFE_CMD_TOPIC}/logging", {
            "task_name": "set_service_log_mode",
            "arguments": {"mode": mode},
        })

    def afe_get_service_log_mode(self):
        self.publish_command(f"{AFE_CMD_TOPIC}/logging", {
            "task_name": "get_service_log_mode",
            "arguments": {},
        })

    # ------------------------------------------------------------------ #
    #  Static helpers                                                      #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _extract_tuner_name(status_payload: dict) -> Optional[str]:
        """Best-effort extraction of resolved tuner type from tuner status payload."""
        if not isinstance(status_payload, dict):
            return None

        candidate_keys = ("tuner", "tuner_type", "active_tuner", "name", "model", "device")

        def _normalize(value):
            if isinstance(value, str):
                token = value.strip().upper()
                if token in TUNER_INJECTION_SIDE:
                    return token
            return None

        for key in candidate_keys:
            resolved = _normalize(status_payload.get(key))
            if resolved:
                return resolved

        for nested_key in ("arguments", "status", "data", "result"):
            nested = status_payload.get(nested_key)
            if isinstance(nested, dict):
                for key in candidate_keys:
                    resolved = _normalize(nested.get(key))
                    if resolved:
                        return resolved
        return None

    @staticmethod
    def normalize_tuner_status(status_payload: Optional[dict]) -> Optional[dict]:
        """Normalize tuner status payload into a stable shape for GUI/CLI.

        Returns:
          {
            "state": str,
            "name": str,
            "lo_mhz": float|None,
            "pwr_dbm": float|None,
            "raw": dict,
          }
        """
        if not isinstance(status_payload, dict):
            return None

        candidates = [status_payload]
        for key in ("tuner", "status", "result", "data", "arguments"):
            nested = status_payload.get(key)
            if isinstance(nested, dict):
                candidates.append(nested)

        def _first_nonempty(*keys):
            for blob in candidates:
                for key in keys:
                    value = blob.get(key)
                    if value not in (None, ""):
                        return value
            return None

        def _first_float(*keys):
            value = _first_nonempty(*keys)
            if value is None:
                return None
            try:
                return float(value)
            except (TypeError, ValueError):
                return None

        state = _first_nonempty("state", "service_state")
        if state is None:
            state = "unknown"
        state = str(state).strip().lower() or "unknown"

        name = (
            MEPBus._extract_tuner_name(status_payload)
            or _first_nonempty("name", "tuner_name", "active_tuner", "tuner_type", "model", "device")
            or "—"
        )

        lo_mhz = _first_float("freq_mhz", "lo_mhz")
        pwr_dbm = _first_float("pwr_dbm", "power_dbm", "power")

        return {
            "state": state,
            "name": str(name),
            "lo_mhz": lo_mhz,
            "pwr_dbm": pwr_dbm,
            "raw": status_payload,
        }

    @staticmethod
    def _tlm_to_str(tlm) -> str:
        if tlm is None:
            return "<no tlm>"
        return (
            f"state={tlm.get('state')} "
            f"f_c={float(tlm.get('f_c_hz', 0)) / 1e6:.2f} MHz "
            f"f_if={float(tlm.get('f_if_hz', 0)) / 1e6:.2f} MHz "
            f"f_s={float(tlm.get('f_s', 0)) / 1e6:.2f} MHz "
            f"pps={tlm.get('pps_count')} "
            f"ch={tlm.get('channels')}"
        )


# ===== CAPTURE CONTROLLER ===== #

class CaptureController:
    """On-demand sweep/record orchestrator — owns sync-wait infra and recipes.

    Created when the user clicks Start (or from CLI). Takes a MEPBus reference
    for all MQTT communication. Owns transient session state: sweep config,
    tuner session tracking, recorder state, stop flag, synchronous wait.
    """

    def __init__(self, bus: MEPBus):
        self.bus = bus

        # ---- Sweep config (set via configure_sweep) ----
        self.channel: Optional[str] = None
        self.sample_rate_mhz: Optional[int] = None
        self.tuner: Optional[str] = None
        self.adc_if_mhz: Optional[float] = None
        self.injection: Optional[str] = None
        self.capture_name: Optional[str] = None
        self._tuner_initialized_for: Optional[str] = None
        self._last_tuner_session_id: Optional[str] = None
        self._tuner_session_counter = 0

        # ---- Recorder "what changed" state ----
        self._active_channel = None
        self._active_sample_rate = None
        self._recorder_running = False

        # ---- Stop flag for sweeps ----
        self._stop_flag = threading.Event()

        # ---- Synchronous wait infrastructure (for sweep orchestration) ----
        self._tlm = None
        self._tlm_lock = threading.Lock()
        self._tlm_event = threading.Event()

        self._status = {t: None for t in _SYNC_STATUS_TOPICS}
        self._status_lock = threading.Lock()
        self._status_events = {t: threading.Event() for t in _SYNC_STATUS_TOPICS}

        # ---- Register sync-wait listeners on bus ----
        self._sync_cbs = {}

        def _make_rfsoc_cb():
            def _cb(data):
                with self._tlm_lock:
                    self._tlm = data
                self._tlm_event.set()
            return _cb

        self._sync_cbs[RFSOC_STATUS_TOPIC] = _make_rfsoc_cb()
        self.bus.on_status(RFSOC_STATUS_TOPIC, self._sync_cbs[RFSOC_STATUS_TOPIC])

        for topic in _SYNC_STATUS_TOPICS:
            def _make_status_cb(t):
                def _cb(data):
                    with self._status_lock:
                        self._status[t] = data
                    self._status_events[t].set()
                return _cb
            self._sync_cbs[topic] = _make_status_cb(topic)
            self.bus.on_status(topic, self._sync_cbs[topic])

        # ---- Check for already-initialized tuner ----
        self._query_and_cache_tuner_state()

    def _require_mqtt(self, action: str) -> bool:
        """Return False and log once when broker is offline for a control action."""
        if self.bus.is_connected():
            return True
        status = self.bus.get_connection_status()
        logging.error(
            "Cannot %s while MQTT is offline (%s:%s). Last error: %s",
            action,
            status.get("broker"),
            status.get("port"),
            status.get("last_error") or "none",
        )
        return False

    def close(self):
        """Remove sync-wait listeners from bus and stop recorder (best-effort)."""
        for topic, cb in self._sync_cbs.items():
            self.bus.remove_listener(topic, cb)
        self._sync_cbs.clear()
        if self._recorder_running:
            try:
                self.stop_recorder()
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    #  Sweep configuration                                                 #
    # ------------------------------------------------------------------ #

    def configure_sweep(
        self,
        channel: str,
        sample_rate_mhz: int,
        tuner: str = None,
        adc_if_mhz: float = None,
        injection: str = None,
        capture_name: str = None,
    ):
        """Set parameters used by run_sweep / run_single / start_recorder."""
        self.channel = channel.upper()
        self.sample_rate_mhz = sample_rate_mhz
        self.tuner = tuner
        self.adc_if_mhz = adc_if_mhz
        self.capture_name = capture_name

        if tuner is None:
            self.injection = None
        else:
            self.injection = resolve_injection(tuner, injection)
            self._initialize_tuner_if_needed()

    # ------------------------------------------------------------------ #
    #  Synchronous wait helpers (used during sweep orchestration)          #
    # ------------------------------------------------------------------ #

    def get_tlm(self, timeout_s: float = 2.0):
        """Request and return the latest RFSoC telemetry, or None on timeout."""
        if not self.bus.is_connected():
            return None
        with self._tlm_lock:
            self._tlm = None
        self._tlm_event.clear()
        self.bus.publish_command(
            RFSOC_CMD_TOPIC,
            {"task_name": "get", "arguments": ["tlm"]},
            sleep_s=0,
        )
        if self._tlm_event.wait(timeout=timeout_s):
            with self._tlm_lock:
                return self._tlm
        return None

    def _wait_for_status(self, topic: str, timeout_s: float = 2.0, pre_armed: bool = False):
        """Block until a status message arrives on topic, return payload or None.

        Set pre_armed=True when the event has already been cleared before the
        triggering command was sent (avoids missing a fast response).
        """
        if topic not in self._status_events:
            raise ValueError(f"Unknown sync status topic: {topic!r}")
        if not self.bus.is_connected():
            logging.warning(f"Cannot wait for {topic}: MQTT offline")
            return None
        if not pre_armed:
            self._status_events[topic].clear()
            with self._status_lock:
                self._status[topic] = None
        if self._status_events[topic].wait(timeout=timeout_s):
            with self._status_lock:
                return self._status[topic]
        logging.warning(f"No status from {topic} within {timeout_s}s — service may not be running")
        return None

    def wait_for_firmware_ready(self, max_wait_s: int = 30) -> bool:
        """Poll rfsoc/status until f_s is a valid non-NaN positive number."""
        if not self._require_mqtt("wait for RFSoC firmware"):
            return False
        logging.info("Waiting for RFSoC firmware to be ready...")
        deadline = time.time() + max_wait_s
        while time.time() < deadline:
            tlm = self.get_tlm(timeout_s=2.0)
            if tlm is not None:
                f_s = tlm.get("f_s")
                if isinstance(f_s, (int, float)) and f_s == f_s and f_s > 0:
                    logging.info(f"RFSoC firmware ready: f_s={f_s / 1e6:.2f} MHz")
                    return True
            logging.debug("RFSoC not ready yet, waiting...")
            time.sleep(1)
        logging.error(f"RFSoC firmware not ready after {max_wait_s}s")
        return False

    # ------------------------------------------------------------------ #
    #  Tuner session management                                            #
    # ------------------------------------------------------------------ #

    def _generate_tuner_session_id(self) -> str:
        self._tuner_session_counter += 1
        return f"mep-tuner-{self._tuner_session_counter}"

    def _query_and_cache_tuner_state(self):
        """Query tuner status at startup; cache what tuner (if any) is already initialized."""
        if not self.bus.is_connected():
            logging.debug("Skipping tuner startup query: MQTT offline")
            return

        session_id = self._generate_tuner_session_id()
        self._last_tuner_session_id = session_id
        self.bus.publish_command(TUNER_CMD_TOPIC, {
            "task_name": "status",
            "arguments": {},
            "session_id": session_id,
        })
        status = self._wait_for_status(TUNER_STATUS_TOPIC, timeout_s=2.0)

        if not isinstance(status, dict):
            logging.debug("No tuner status response at startup")
            return

        if not self._is_status_fresh(status, expected_session_id=session_id):
            logging.debug("Startup tuner status is stale or session mismatch")
            return

        if status.get("state") != "online":
            logging.debug(f"Tuner service not online (state={status.get('state')})")
            return

        tuner_name = MEPBus._extract_tuner_name(status)
        if tuner_name:
            self._tuner_initialized_for = tuner_name.upper()
            logging.info(f"Tuner already initialized at startup: {tuner_name}")

    def _is_status_fresh(self, status: dict, max_age_s: float = 5.0, expected_session_id: str = None) -> bool:
        if not isinstance(status, dict):
            return False

        if expected_session_id is not None and "session_id" in status:
            if status.get("session_id") == expected_session_id:
                logging.debug(f"Tuner status validated via session_id: {expected_session_id}")
                return True
            else:
                logging.warning(
                    f"Tuner status session_id mismatch: expected {expected_session_id!r}, got {status.get('session_id')!r}"
                )
                return False

        msg_timestamp = status.get("timestamp")
        if msg_timestamp is None:
            logging.debug("Tuner status has no timestamp or session_id; assuming fresh")
            return True

        current_time = time.time()
        age_s = current_time - msg_timestamp

        if age_s > max_age_s:
            logging.warning(f"Tuner status is stale ({age_s:.1f}s old)")
            return False

        return True

    def _initialize_tuner_if_needed(self):
        """Initialize tuner once if not already initialized for current config."""
        if not self._require_mqtt("initialize tuner"):
            return

        if self.tuner == self._tuner_initialized_for:
            logging.debug(f"Tuner already initialized for {self.tuner}, skipping re-init")
            return

        logging.info(f"Initializing tuner: {self.tuner}")

        if self.tuner.lower() == "auto":
            session_id = self._generate_tuner_session_id()
            self._last_tuner_session_id = session_id
            self.bus.publish_command(TUNER_CMD_TOPIC, {
                "task_name": "init_tuner",
                "arguments": {},
                "session_id": session_id,
            })
            init_status = self._wait_for_status(TUNER_STATUS_TOPIC, timeout_s=2.0)
            if self._is_status_fresh(init_status, expected_session_id=session_id):
                resolved_tuner = MEPBus._extract_tuner_name(init_status) or "AUTO"
                logging.info(f"Auto tuner resolved to: {resolved_tuner}")
                self._tuner_initialized_for = resolved_tuner
            else:
                logging.warning("Auto tuner init status stale or missing")
        else:
            session_id = self._generate_tuner_session_id()
            self._last_tuner_session_id = session_id
            self.bus.publish_command(TUNER_CMD_TOPIC, {
                "task_name": "init_tuner",
                "arguments": {"force_tuner": self.tuner},
                "session_id": session_id,
            })
            self._tuner_initialized_for = self.tuner

        time.sleep(0.1)

    # ------------------------------------------------------------------ #
    #  Recorder recipe (sweep orchestration)                               #
    # ------------------------------------------------------------------ #

    def start_recorder(self, freq_idx_offset: float = 0.0):
        """Configure and enable the DigitalRF recorder."""
        if not self._require_mqtt("start recorder"):
            return False

        if self.channel not in RECORDER_CHANNEL_PORTS:
            raise ValueError(f"channel must be one of {list(RECORDER_CHANNEL_PORTS.keys())}")

        dst_port = RECORDER_CHANNEL_PORTS[self.channel]
        config_name = f"sr{self.sample_rate_mhz}MHz"

        logging.info(
            f"Starting recorder: channel={self.channel}, config={config_name}, port={dst_port}"
        )

        self.bus.recorder_disable()

        self.bus.publish_command(RECORDER_CMD_TOPIC, {
            "task_name": "config.load",
            "arguments": {"name": config_name},
            "response_topic": "recorder/config/response",
        })

        self.bus.recorder_config_set("packet.freq_idx_offset", str(freq_idx_offset))

        if self.capture_name:
            channel_dir = f"{self.capture_name}/{config_name}/ch{self.channel}"
        else:
            channel_dir = f"ringbuffer/{config_name}/ch{self.channel}"

        self.bus.recorder_config_set("drf_sink.channel_dir", channel_dir)
        self.bus.recorder_config_set("basic_network.dst_port", str(dst_port))

        # Arm the wait BEFORE sending enable to avoid missing a fast response
        self._status_events[RECORDER_STATUS_TOPIC].clear()
        with self._status_lock:
            self._status[RECORDER_STATUS_TOPIC] = None
        self.bus.recorder_enable()
        status = self._wait_for_status(RECORDER_STATUS_TOPIC, timeout_s=3.0, pre_armed=True)
        if status is not None:
            logging.info(f"Recorder enabled — status: {status}")
        else:
            logging.warning("Recorder enable sent but no status response received")

        self._active_channel = self.channel
        self._active_sample_rate = self.sample_rate_mhz
        self._recorder_running = True
        return True

    def stop_recorder(self):
        """Disable the DigitalRF recorder."""
        logging.info("Stopping recorder")
        self.bus.recorder_disable()
        self._recorder_running = False

    # ------------------------------------------------------------------ #
    #  Tune and Arm recipe (from "Tune and Capture" flowchart)             #
    # ------------------------------------------------------------------ #

    def tune_and_arm(self, f_hz: float) -> bool:
        """Full tune + capture-arm sequence for one frequency step."""
        if not self._require_mqtt("tune and arm"):
            return False

        f_mhz = f_hz / 1e6

        self.bus.rfsoc_reset()

        if self.tuner is None:
            logging.info(f"[TUNER_NO] RFSoC NCO → {GREEN}{f_mhz:.2f} MHz{RESET}")
            self.bus.publish_command(RFSOC_CMD_TOPIC, {"task_name": "set", "arguments": f"freq_IF {f_mhz}"})
            time.sleep(0.1)
        else:
            if self.adc_if_mhz is None:
                raise ValueError("adc_if_mhz is required when a tuner is specified")

            resolved_tuner = self.tuner.upper()

            self.bus.publish_command(RFSOC_CMD_TOPIC, {"task_name": "set", "arguments": f"freq_IF {self.adc_if_mhz}"})
            time.sleep(0.1)

            lo_mhz = (f_mhz + self.adc_if_mhz) if self.injection == "high" else (f_mhz - self.adc_if_mhz)

            logging.info(
                f"[TUNER_YES/{self.injection}-side] RF → {GREEN}{f_mhz:.2f} MHz{RESET}  "
                f"LO={lo_mhz:.2f} MHz  IF={self.adc_if_mhz:.2f} MHz"
            )

            apply_conjugate = (self.injection == "high")
            self.bus.recorder_config_set("packet.apply_conjugate", str(apply_conjugate).lower())

            self.bus.tuner_set_freq(lo_mhz)
            time.sleep(0.1)

            if resolved_tuner == "VALON":
                self.bus.tuner_check_lock()
                lock_status = self._wait_for_status(TUNER_STATUS_TOPIC, timeout_s=2.0)
                if lock_status is not None:
                    logging.info(f"Tuner lock status: {lock_status}")
                else:
                    logging.warning("No tuner lock status response received")

        # Common tail: metadata → channel → capture → TLM
        self.bus.publish_command(RFSOC_CMD_TOPIC, {"task_name": "set", "arguments": f"freq_metadata {f_hz}"})
        self.bus.publish_command(RFSOC_CMD_TOPIC, {"task_name": "set", "arguments": f"channel {self.channel}"})
        self.bus.publish_command(RFSOC_CMD_TOPIC, {"task_name": "capture_next_pps"})

        tlm = self.get_tlm()
        if not tlm or tlm.get("state") != "active":
            logging.error(f"RFSoC capture failed or inactive: {MEPBus._tlm_to_str(tlm)}")
            return False
        logging.info(f"Armed — {MEPBus._tlm_to_str(tlm)}")
        return True

    # ------------------------------------------------------------------ #
    #  Scan recipes (from "Start Scan" flowchart)                          #
    # ------------------------------------------------------------------ #

    def run_single(self, f_hz: float, dwell_s: float = None):
        """Single-frequency capture with optional dwell-based auto-stop."""
        if not self._require_mqtt("run single capture"):
            return False

        sample_rate_changed = (self.sample_rate_mhz != self._active_sample_rate)
        channel_changed = (self.channel != self._active_channel)

        if self._recorder_running and (sample_rate_changed or channel_changed):
            logging.info(
                "Sample rate or channel changed — restarting recorder "
                f"(sr: {self._active_sample_rate}→{self.sample_rate_mhz}, "
                f"ch: {self._active_channel}→{self.channel})"
            )
            self.stop_recorder()
            if not self.tune_and_arm(f_hz):
                return False
            if not self.start_recorder():
                return False
        else:
            if not self.tune_and_arm(f_hz):
                return False
            if not self._recorder_running:
                if not self.start_recorder():
                    return False

        if dwell_s is not None and dwell_s > 0:
            self._dwell(dwell_s)
            self.stop_recorder()
        return True

    def run_sweep(self, freqs_hz, dwell_s: float, restart_interval: int = None):
        """Sweep: start recorder once, tune_and_arm + dwell per step."""
        if not self._require_mqtt("run sweep"):
            return False

        logging.info(
            f"Sweep: {len(freqs_hz)} steps, dwell={dwell_s}s, "
            f"restart_interval={restart_interval}s"
        )

        if not self.start_recorder():
            return False
        last_restart = time.time()

        try:
            for f_hz in freqs_hz:
                if self._stop_flag.is_set():
                    logging.info("Sweep interrupted by stop flag")
                    break

                if restart_interval and time.time() - last_restart >= restart_interval:
                    logging.info("Restart interval reached — restarting recorder")
                    if not self.start_recorder():
                        return False
                    last_restart = time.time()

                if not self.tune_and_arm(f_hz):
                    return False
                self._dwell(dwell_s)
        finally:
            self.stop_recorder()
        return True

    # ------------------------------------------------------------------ #
    #  Utilities                                                           #
    # ------------------------------------------------------------------ #

    def _dwell(self, dwell_s: float):
        """Sleep for dwell_s, logging TLM each second. Exits early on stop flag."""
        start = time.time()
        while (time.time() - start) < dwell_s:
            if self._stop_flag.is_set():
                logging.info("Dwell interrupted by stop flag")
                return
            tlm = self.get_tlm(timeout_s=1.5)
            logging.debug(MEPBus._tlm_to_str(tlm))
            time.sleep(1)

    def request_stop(self):
        """Signal the current sweep or dwell to exit early."""
        logging.info("Stop requested")
        self._stop_flag.set()


# ===== DOCKER MANAGER ===== #
class DockerManager:
    """Manage docker compose services: status queries, action execution, log streaming.

    Pure system-level orchestrator — no GUI dependencies.
    Used by MEPGui for the DOC tab, and available for CLI/scripting.
    """

    def __init__(self, compose_dir: str = DOCKER_COMPOSE_DIR):
        self.compose_dir = compose_dir
        self._services: dict = {}
        self._service_names: list[str] = []
        self._log_messages: deque = deque(maxlen=2000)
        self._log_lock = threading.Lock()
        self._log_rendered_count: int = 0
        self._log_paused: bool = False
        self._log_proc = None
        self._log_busy: bool = False
        self._log_scope = None
        self._action_busy: bool = False
        self._compose_cmd_cache = None
        self._refresh_busy: bool = False

    # -- Properties --------------------------------------------------------

    @property
    def services(self) -> dict:
        return self._services

    @property
    def service_names(self) -> list[str]:
        return self._service_names

    @property
    def action_busy(self) -> bool:
        return self._action_busy

    @action_busy.setter
    def action_busy(self, value: bool):
        self._action_busy = value

    @property
    def log_busy(self) -> bool:
        return self._log_busy

    @property
    def log_paused(self) -> bool:
        return self._log_paused

    @log_paused.setter
    def log_paused(self, value: bool):
        self._log_paused = value

    @property
    def log_scope(self):
        return self._log_scope

    @property
    def refresh_busy(self) -> bool:
        return self._refresh_busy

    @refresh_busy.setter
    def refresh_busy(self, value: bool):
        self._refresh_busy = value

    # -- Command execution -------------------------------------------------

    def run_cmd(self, cmd: list[str], *, cwd: str | None = None,
                timeout: float = 10.0) -> tuple[int, str, str]:
        """Run a command and return (returncode, stdout, stderr)."""
        try:
            proc = subprocess.run(
                cmd, cwd=cwd,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, timeout=timeout,
            )
            return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()
        except subprocess.TimeoutExpired as e:
            out = (e.stdout or "") if isinstance(e.stdout, str) else ""
            err = (e.stderr or "") if isinstance(e.stderr, str) else ""
            return 124, out.strip(), (err or "command timed out").strip()
        except Exception as e:
            return 125, "", str(e)

    def get_compose_cmd(self) -> tuple | list:
        """Detect and cache the docker compose command (v2 plugin or legacy v1)."""
        if self._compose_cmd_cache is not None:
            return self._compose_cmd_cache
        for cmd in (["docker", "compose"], ["docker-compose"]):
            rc, _, _ = self.run_cmd([*cmd, "version"], timeout=3.0)
            if rc == 0:
                self._compose_cmd_cache = cmd
                return cmd
        self._compose_cmd_cache = ()
        return ()

    def parse_ps_json(self, text: str) -> dict:
        """Parse ``docker compose ps --format=json`` output into {service: info_dict}."""
        if not text.strip():
            return {}

        rows = None
        try:
            rows = json.loads(text)
        except Exception:
            parsed = []
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    parsed.append(json.loads(line))
                except Exception:
                    continue
            rows = parsed

        if isinstance(rows, dict):
            rows = [rows]
        if not isinstance(rows, list):
            return {}

        services = {}
        for item in rows:
            if not isinstance(item, dict):
                continue
            service = str(item.get("Service") or item.get("Name") or "").strip()
            if not service:
                continue

            command = str(item.get("Command") or item.get("command") or "—")
            publishers = item.get("Publishers")
            ports = item.get("Ports")
            port_items = []
            if isinstance(publishers, list):
                for p in publishers:
                    if not isinstance(p, dict):
                        continue
                    host = str(p.get("URL") or p.get("HostIP") or "")
                    pub = p.get("PublishedPort")
                    tgt = p.get("TargetPort")
                    proto = str(p.get("Protocol") or "tcp")
                    if pub is not None and tgt is not None:
                        left = f"{host}:{pub}" if host else str(pub)
                        port_items.append(f"{left}->{tgt}/{proto}")
                    elif tgt is not None:
                        port_items.append(f"{tgt}/{proto}")
            elif isinstance(ports, str) and ports.strip():
                port_items.append(ports.strip())
            elif isinstance(ports, list):
                for p in ports:
                    if p is not None:
                        port_items.append(str(p))

            services[service] = {
                "container": str(item.get("Name") or "—"),
                "state": str(item.get("State") or "—"),
                "command": command,
                "ports": ", ".join(port_items) if port_items else "—",
                "status": str(item.get("Status") or "—"),
            }
        return services

    # -- Service status ----------------------------------------------------

    def refresh_status(self) -> tuple[str, dict, str]:
        """Query docker engine and compose services.

        Returns (engine_status, services_dict, detail_message).
        Updates internal services cache.  Synchronous — run in a thread if needed.
        """
        engine_status = "Unavailable"
        services = {}
        detail = ""

        rc, _, err = self.run_cmd(["docker", "info"], timeout=5.0)
        if rc == 0:
            engine_status = "Reachable"
        else:
            return engine_status, services, (err or "docker daemon not reachable")

        compose_cmd = self.get_compose_cmd()
        if not compose_cmd:
            return engine_status, services, "docker compose command not found"

        rc, out, err = self.run_cmd(
            [*compose_cmd, "ps", "-a", "--format", "json", "--no-trunc"],
            cwd=self.compose_dir, timeout=8.0,
        )
        if rc != 0 and ("unknown flag" in (err or "").lower()):
            rc, out, err = self.run_cmd(
                [*compose_cmd, "ps", "-a", "--format", "json"],
                cwd=self.compose_dir, timeout=8.0,
            )
        if rc == 0:
            services = self.parse_ps_json(out)
        else:
            detail = err or f"compose ps failed ({rc})"

        self._services = dict(services)
        self._service_names = sorted(services.keys())
        return engine_status, services, detail

    # -- Compose actions ---------------------------------------------------

    def run_compose_action(self, action: str, *,
                           services: list[str] | None = None,
                           extra_args: list[str] | None = None) -> tuple[int, str, str]:
        """Run a compose action (start/stop/restart/up/down).

        Returns (rc, stdout, stderr).  Synchronous — run in a thread if needed.
        Caller manages the ``action_busy`` flag.
        """
        compose_cmd = self.get_compose_cmd()
        if not compose_cmd:
            return 1, "", "docker compose command not found"

        cmd = [*compose_cmd, action]
        if extra_args:
            cmd.extend(extra_args)
        if services:
            cmd.extend(services)
        return self.run_cmd(cmd, cwd=self.compose_dir, timeout=40.0)

    def preview_command(self, action: str, *,
                        services: list[str] | None = None,
                        extra_args: list[str] | None = None) -> str:
        """Build the compose command string for display (no execution)."""
        compose_cmd = self.get_compose_cmd()
        compose_text = " ".join(compose_cmd) if compose_cmd else "docker compose"
        parts = [compose_text, action]
        if extra_args:
            parts.extend(extra_args)
        if services:
            parts.extend(services)
        return " ".join(parts)

    # -- Log streaming -----------------------------------------------------

    def stream_start(self, *, service: str | None = None, tail: str = "30",
                     on_line: Optional[Callable] = None,
                     on_exit: Optional[Callable] = None):
        """Start streaming docker compose logs.

        Args:
            service: Service name, or None for all services.
            tail:    Number of historical lines to fetch.
            on_line: Called (from reader thread) for each log line.
            on_exit: Called (from reader thread) with return code when the
                     stream ends — only if this stream is still the current one.
        """
        compose_cmd = self.get_compose_cmd()
        if not compose_cmd:
            logging.error("DOCKER: docker compose command not found")
            return

        self.stream_stop()
        self._log_paused = False

        cmd = [*compose_cmd, "logs", "-f", "--tail", tail]
        if service:
            cmd.append(service)

        try:
            proc = subprocess.Popen(
                cmd, cwd=self.compose_dir,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
            )
        except Exception as e:
            logging.error("DOCKER: log stream failed to start: %s", e)
            return

        self._log_proc = proc
        self._log_busy = True
        self._log_scope = service or "all"
        scope_label = service or "all services"
        logging.info("DOCKER: streaming logs (%s)", scope_label)

        def _reader():
            try:
                if proc.stdout is None:
                    return
                for line in proc.stdout:
                    self.log_append(line)
                    if on_line:
                        on_line(line)
            finally:
                rc = proc.poll()
                same_proc = (self._log_proc is proc)
                if same_proc:
                    self._log_proc = None
                    self._log_busy = False
                if on_exit and same_proc:
                    on_exit(rc)

        threading.Thread(target=_reader, daemon=True, name="docker_logs").start()

    def stream_stop(self):
        """Stop the current log stream."""
        proc = self._log_proc
        self._log_proc = None
        self._log_busy = False
        self._log_scope = None
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=1.0)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def stream_pause(self):
        self._log_paused = True

    def stream_resume(self):
        self._log_paused = False

    # -- Log buffer --------------------------------------------------------

    def log_append(self, line: str):
        """Append a timestamped log line to the internal buffer."""
        ts = datetime.now().strftime("%H:%M:%S")
        with self._log_lock:
            self._log_messages.append((ts, line.rstrip("\n")))

    def get_new_log_entries(self) -> list[tuple[str, str]]:
        """Return log entries not yet rendered and advance the render counter."""
        with self._log_lock:
            entries = list(self._log_messages)
            start = min(self._log_rendered_count, len(entries))
            tail = entries[start:]
        self._log_rendered_count = len(entries)
        return tail

    def clear_log(self):
        """Clear the log buffer and reset the render counter."""
        with self._log_lock:
            self._log_messages.clear()
        self._log_rendered_count = 0


# ===== ENTRY POINT ===== #

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="RFSoC MEP sweep/record — all control via MQTT"
    )
    parser.add_argument("--freq_start", "-f1", type=float, default=7000,
                        help="Start frequency in MHz")
    parser.add_argument("--freq_end",   "-f2", type=float, default=float("nan"),
                        help="End frequency in MHz (omit for single-frequency capture)")
    parser.add_argument("--channel",    "-c",  type=str,   default="A",
                        help="RFSoC channel (A, B, C, or D)")
    parser.add_argument("--sample-rate-mhz", "-r", type=int, default=None,
                        help="Recording sample rate in MHz (default: step size)")
    parser.add_argument("--step",       "-s",  type=float, default=10,
                        help="Sweep step size in MHz")
    parser.add_argument("--dwell",      "-d",  type=float, default=60,
                        help="Dwell time per step in seconds")
    parser.add_argument("--tuner",      "-t",  type=tuner_type_arg, default=None,
                        help="Tuner: VALON, LMX2820, TEST, auto, or None")
    parser.add_argument("--adc_if_mhz",       type=float, default=None,
                        help="Fixed RFSoC IF in MHz (required if tuner is used)")
    parser.add_argument("--injection",         type=str,   default=None,
                        choices=["high", "low"],
                        help="Override injection side (default: from TUNER_INJECTION_SIDE table)")
    parser.add_argument("--log-level",  "-l",  type=str,   default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
    parser.add_argument("--skip_ntp",          action="store_true",
                        help="Skip NTP sync step")
    parser.add_argument("--restart_interval",  type=int,   default=None,
                        help="Force recorder restart every N seconds (sweep only)")
    parser.add_argument("--capture_name",      type=str,   default=None,
                        help="Save data under captures/{name}/... (default: ringbuffer)")
    args = parser.parse_args()
    args.channel = args.channel.upper()

    if args.tuner is not None and args.adc_if_mhz is None:
        parser.error("--adc_if_mhz is required when --tuner is set")

    if args.sample_rate_mhz is None:
        args.sample_rate_mhz = int(args.step)

    # === Logging === #
    os.makedirs(LOG_DIR, exist_ok=True)
    timestamp = datetime.now().isoformat().replace(":", "-").replace(".", "-")
    log_path = os.path.join(LOG_DIR, f"capture_sweep_{timestamp}.log")
    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        filename=log_path,
    )
    logging.getLogger().addHandler(logging.StreamHandler())

    # === NTP sync === #
    if not args.skip_ntp:
        logging.info("Updating NTP on RFSoC")
        sync_ntp_on_rfsoc(os.getcwd())

    # === Build controller === #
    bus = MEPBus()
    capture = CaptureController(bus)

    capture.configure_sweep(
        channel=args.channel,
        sample_rate_mhz=args.sample_rate_mhz,
        tuner=args.tuner,
        adc_if_mhz=args.adc_if_mhz,
        injection=args.injection,
        capture_name=args.capture_name,
    )

    # === Wait for RFSoC firmware === #
    if not capture.wait_for_firmware_ready(max_wait_s=30):
        logging.error("RFSoC firmware not ready — aborting")
        capture.close()
        bus.disconnect()
        exit(1)

    # === Run === #
    freqs_hz = get_frequency_list(args.freq_start, args.freq_end, args.step)
    is_sweep = not math.isnan(args.freq_end)

    try:
        if is_sweep:
            capture.run_sweep(freqs_hz, dwell_s=args.dwell, restart_interval=args.restart_interval)
        else:
            capture.run_single(freqs_hz[0])
    finally:
        logging.info("Stopping recorder")
        capture.stop_recorder()
        capture.close()
        bus.disconnect()
