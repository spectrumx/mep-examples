#!/opt/radiohound/python313/bin/python
"""
start_mep_rx.py

MEP system controller — MQTT gateway for RFSoC, recorder, tuner, and AFE.

Architecture:
    MEPBus          — always-on MQTT connection, listener registry, thin command publishers
    ControllerTuner — shared external-tuner state (one physical oscillator, used by both RX/TX)
    Recorder        — recorder-specific configuration, preset resolution, override staging,
                      live start/stop lifecycle, channel/sample-rate state, and telemetry logging
    ControllerRx    — on-demand sweep/record orchestrator (owns sync-wait + RFSoC/tuner recipes)
    ControllerTx    — DAC function-generator (transmit) orchestrator, independent of RX
    HostPlatform    — local host identity, health, power, network, and thermal access
    GPSDMonitor     — persistent gpsd connection, parsing, and GPS state callbacks
    CaptureTelemetryLogger — capture-scoped AFE telemetry CSV writer
    DockerManager   — Docker Compose status, actions, and log streaming

Usage (CLI):
    python start_mep_rx.py -f1 7000 -f2 8000 -s 10 -d 60 -c A -r 10

Usage (imported by mep_gui.py):
    from start_mep_rx import MEPBus, ControllerTuner, ControllerRx, ControllerTx
    bus = MEPBus()
    tuner_ctrl = ControllerTuner(bus)
    tuner_ctrl.configure(tuner="VALON", adc_if_mhz=1090)
    rx = ControllerRx(bus, tuner_ctrl)
    rx.recorder.configure_capture(channel="A", sample_rate_mhz=10)
    rx.start_sweep(freqs_hz, dwell_s=60)
    tx = ControllerTx(bus, tuner_ctrl)
    tx.start(channel="A", center_freq_mhz=2400, offset_freq_mhz=1, amplitude_bins=4096)
    tx.stop()

Author: john.marino@colorado.edu
"""

# ===== IMPORTS ===== #
import argparse
import time
import logging
import json
import os
import shutil
import base64
import re
import math
import uuid
import socket
import subprocess
import queue
import copy
import csv
from fractions import Fraction
from collections import deque
from datetime import datetime
import threading
from typing import Optional, Callable
import numpy as np
import paho.mqtt.client as mqtt_lib

# ===== CONFIG ===== #
LOG_DIR     = os.path.join(os.path.expanduser("~"), "log", "spectrumx")
MQTT_BROKER = "localhost"
MQTT_PORT   = 1883
SPEC_POWER_FLOOR = np.float32(1e-12)
SPEC_DB_SCALE = np.float32(10.0)

# Command and Status Topics
RFSOC_CMD_TOPIC       = "rfsoc/command"
RFSOC_STATUS_TOPIC    = "rfsoc/status"
RFSOC_PLL_CONFIG_TOPIC = "rfsoc/pll_config"
RECORDER_CMD_TOPIC    = "recorder/command"
RECORDER_STATUS_TOPIC = "recorder/status"
TUNER_CMD_TOPIC       = "tuner_control/command"
TUNER_STATUS_TOPIC    = "tuner_control/status"
TUNER_RESPONSE_TOPIC  = "tuner_control/response"
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

# Owned by the FPGA UDP packet emitter. This is not the source of truth of this information.
RECORDER_CHANNEL_PORTS = {"A": 60134, "B": 60133, "C": 60132, "D": 60131}

# Single source of truth for tuner metadata, keyed by the canonical/friendly
# name (the form the GUI dropdown, CLI, and the rest of this program use).
# 'backend' is the lower-case name the tuner_control service expects (it calls
# the test tuner 'dummy'); 'injection_side' feeds resolve_injection.
TUNERS = {
    "VALON":   {"backend": "valon",   "injection_side": "high"},
    "LMX2820": {"backend": "lmx2820", "injection_side": "high"},
    "TEST":    {"backend": "dummy",   "injection_side": "high"},
}
_TUNER_CANONICAL_BY_BACKEND = {meta["backend"]: name for name, meta in TUNERS.items()}

CONJUGATE_POLICY_DEFAULT = "auto"
CONJUGATE_POLICY_OPTIONS = ("auto", "force_on", "force_off")

# RFSoC TX hardware limits (source of truth: RFSoC firmware / DAC RFDC).
TX_AMPLITUDE_BINS_MAX = 8191          # 14-bit signed DAC peak
TX_OFFSET_FREQ_MAX_MHZ = 32           # function-gen baseband offset magnitude bound (exclusive)
TX_CHANNEL_OPTIONS = ("None", "A", "B", "A,B")

# Hardware configuration options (for dropdowns/validation)
# CHANNEL_OPTIONS derived from RECORDER_CHANNEL_PORTS
CHANNEL_OPTIONS     = list(sorted(RECORDER_CHANNEL_PORTS.keys()))
TUNER_OPTIONS       = ["None"] + list(TUNERS.keys()) + ["auto"]

RECORDER_CONFIG_DIR = "/opt/radiohound/docker/recorder/configs"
DOCKER_COMPOSE_DIR = "/opt/radiohound/docker"
PREVIEW_DATA_DIR = "/data/captures/preview/data"
CAPTURES_ROOT_DIR = "/data/captures"

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


def _normalize_tuner(tuner: Optional[str]) -> Optional[str]:
    """Canonicalize a tuner selection at the system boundary.

    None / '' / 'none' -> None (NCO mode), 'auto' (any case) -> 'auto', and a
    concrete tuner name is upper-cased to match TUNERS keys.
    """
    if tuner is None:
        return None
    token = tuner.strip()
    if not token or token.lower() == "none":
        return None
    if token.lower() == "auto":
        return "auto"
    return token.upper()


def resolve_injection(tuner: str, injection_override: str = None) -> str:
    """Determine injection side for a given tuner.
    Returns 'high' or 'low'. Raises ValueError for unknown tuners.
    """
    if injection_override:
        return injection_override
    if tuner.lower() == "auto":
        return "high"
    if tuner.upper() in TUNERS:
        return TUNERS[tuner.upper()]["injection_side"]
    raise ValueError(f"Tuner {tuner!r} not in TUNERS — add it or pass --injection")


def resolve_lo_mhz(f_mhz: float, if_mhz: float, injection: str) -> float:
    """External-tuner LO (MHz) for a target RF frequency, given IF and injection side.

    Single source of truth for this formula — shared by RX (tune_and_arm),
    TX (ControllerTx), capture-settings metadata, and the GUI's Synth LO preview.
    """
    return f_mhz + if_mhz if str(injection).lower() == "high" else f_mhz - if_mhz


def tuner_type_arg(x: str):
    """argparse type handler — allows None/none/auto or a known tuner name."""
    x_lower = x.strip().lower()
    if x_lower == "none":
        return None
    if x_lower == "auto":
        return "auto"
    x_upper = x.strip().upper()
    if x_upper not in TUNERS:
        raise argparse.ArgumentTypeError(
            f"Invalid tuner '{x}'. Valid: {list(TUNERS.keys())}, auto, None"
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


def _load_yaml_mapping(path: str) -> dict:
    """Load one recorder YAML file without making YAML a GUI dependency."""
    try:
        from ruamel.yaml import YAML

        with open(path, "r", encoding="utf-8") as fh:
            data = YAML(typ="safe").load(fh)
    except ImportError:
        try:
            import yaml
        except ImportError as exc:
            raise RuntimeError(
                "Recorder preset parsing requires ruamel.yaml or PyYAML"
            ) from exc
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"Recorder preset must contain a YAML mapping: {path}")
    return data


def _dump_yaml_text(mapping: dict) -> str:
    """Serialize a config mapping to YAML text (ruamel or PyYAML)."""
    try:
        import io
        from ruamel.yaml import YAML

        buf = io.StringIO()
        yml = YAML(typ="safe")
        yml.default_flow_style = False
        yml.dump(mapping, buf)
        return buf.getvalue()
    except ImportError:
        import yaml

        return yaml.safe_dump(mapping, default_flow_style=False, sort_keys=False)


def _set_dotted_value(mapping: dict, key: str, value):
    """Apply recorder-service-style dotted configuration replacement."""
    parts = key.split(".")
    target = mapping
    for part in parts[:-1]:
        child = target.get(part)
        if not isinstance(child, dict):
            child = {}
            target[part] = child
        target = child
    target[parts[-1]] = value


def recorder_preset_path(sample_rate_mhz: int, config_dir: str = None) -> tuple[str, str]:
    """Return the authoritative path and availability of the selected preset."""
    config_dir = config_dir or RECORDER_CONFIG_DIR
    filename = f"sr{int(sample_rate_mhz)}MHz.yaml"
    deployed_path = os.path.join(config_dir, filename)
    if os.path.isfile(deployed_path):
        return deployed_path, "deployed"
    return deployed_path, "unavailable"


def _normalize_recorder_pipeline(config: dict) -> None:
    """Enforce recorder pipeline dependencies for safe runtime graph construction."""
    pipeline = config.get("pipeline")
    if not isinstance(pipeline, dict):
        return

    # int_converter and metadata only make sense when DigitalRF sink is active.
    if not bool(pipeline.get("digital_rf", False)):
        pipeline["int_converter"] = False
        pipeline["metadata"] = False


def resolve_recorder_preset(
    sample_rate_mhz: int,
    overrides: dict[str, object] = None,
    config_dir: str = None,
) -> dict:
    """Resolve a recorder preset into editable values and calculated metrics."""
    preset_name = f"sr{int(sample_rate_mhz)}MHz"
    path, source = recorder_preset_path(sample_rate_mhz, config_dir)
    base = {
        "available": False,
        "preset_name": preset_name,
        "preset_path": path,
        "preset_source": source,
        "error": None,
        "values": {},
        "metrics": {},
        "enabled_resamplers": [],
    }
    if source == "unavailable":
        base["error"] = f"Recorder preset not found: {path}"
        return base

    try:
        config = copy.deepcopy(_load_yaml_mapping(path))
        for key, value in (overrides or {}).items():
            _set_dotted_value(config, key, value)
        _normalize_recorder_pipeline(config)

        packet = config["packet"]
        pipeline = config["pipeline"]
        spectrogram = config["spectrogram"]
        output = config["spectrogram_output"]
        metadata = packet["header_metadata"]

        input_rate = Fraction(
            int(metadata["sample_rate_numerator"]),
            int(metadata.get("sample_rate_denominator", 1)),
        )
        chunk_size = int(packet["num_samples"])
        if input_rate <= 0 or chunk_size <= 0:
            raise ValueError("Input sample rate and packet.num_samples must be positive")

        effective_rate = input_rate
        enabled_resamplers = []
        for name in ("resampler0", "resampler1", "resampler2"):
            if not bool(pipeline.get(name, False)):
                continue
            params = config.get(name)
            if not isinstance(params, dict):
                raise ValueError(f"{name} is enabled but has no configuration")
            up = int(params["up"])
            down = int(params["down"])
            if up <= 0 or down <= 0:
                raise ValueError(f"{name}.up and {name}.down must be positive")
            scaled_chunk = chunk_size * up
            if scaled_chunk % down:
                raise ValueError(
                    f"{name} produces a non-integral chunk: {chunk_size} * {up} / {down}"
                )
            chunk_size = scaled_chunk // down
            effective_rate *= Fraction(up, down)
            enabled_resamplers.append({"name": name, "up": up, "down": down})

        nperseg = int(spectrogram.get("nperseg", 1024))
        noverlap_raw = spectrogram.get("noverlap")
        noverlap = nperseg // 2 if noverlap_raw is None else int(noverlap_raw)
        nfft_raw = spectrogram.get("nfft")
        nfft = nperseg if nfft_raw is None else int(nfft_raw)
        spectra_per_chunk = int(spectrogram.get("num_spectra_per_chunk", 1))
        spectra_per_output = int(output.get("num_spectra_per_output", 600))
        if nperseg <= 0 or nfft < nperseg:
            raise ValueError("nperseg must be positive and nfft must be >= nperseg")
        if noverlap < 0 or noverlap >= nperseg:
            raise ValueError("noverlap must satisfy 0 <= noverlap < nperseg")
        if spectra_per_chunk <= 0 or chunk_size % spectra_per_chunk:
            raise ValueError("num_spectra_per_chunk must evenly divide the effective chunk")
        if spectra_per_output <= 0:
            raise ValueError("num_spectra_per_output must be positive")

        samples_per_row = chunk_size // spectra_per_chunk
        if samples_per_row < nperseg:
            raise ValueError("nperseg does not fit in each spectrum input chunk")
        hop_samples = nperseg - noverlap
        segments_per_row = 1 + (samples_per_row - nperseg) // hop_samples
        scan_time = Fraction(samples_per_row, 1) / effective_rate
        fft_hop_time = Fraction(hop_samples, 1) / effective_rate
        frequency_resolution = effective_rate / nfft
        waterfall_duration = scan_time * spectra_per_output

        values = {
            "batch_size": int(packet.get("batch_size", 0)),
            "max_packet_size": int(packet.get("max_packet_size", 0)),
            "chunk_size": int(packet["num_samples"]),
            "batch_capacity": int(packet.get("batch_capacity", 4)),
            "buffer_size": int(packet.get("buffer_size", 4)),
            "worker_thread_number": int(config["scheduler"].get("worker_thread_number", 8)),
            "nperseg": nperseg,
            "nfft": nfft,
            "noverlap": noverlap,
            "window": str(spectrogram.get("window", "hann")),
            "reduce_op": str(spectrogram.get("reduce_op", "max")),
            "num_spectra_per_chunk": spectra_per_chunk,
            "num_spectra_per_output": spectra_per_output,
            "snr_db_min": float(output.get("snr_db_min", -5)),
            "snr_db_max": float(output.get("snr_db_max", 20)),
            "cmap": str(output.get("cmap", "viridis")),
            "dpi": int(output.get("dpi", 200)),
            "figsize": tuple(output.get("figsize", (6.4, 4.8))),
            "compute": bool(pipeline.get("spectrogram", True)),
            "mqtt": bool(pipeline.get("spectrogram_mqtt", True)),
            "output": bool(pipeline.get("spectrogram_output", True)),
            "digital_rf": bool(pipeline.get("digital_rf", True)),
            "metadata": bool(pipeline.get("metadata", True)),
        }
        metrics = {
            "input_sample_rate_hz": float(input_rate),
            "effective_sample_rate_hz": float(effective_rate),
            "input_chunk_size": int(packet["num_samples"]),
            "effective_chunk_size": chunk_size,
            "frequency_bins": nfft,
            "frequency_resolution_hz": float(frequency_resolution),
            "fft_hop_samples": hop_samples,
            "fft_hop_time_s": float(fft_hop_time),
            "segments_per_row": segments_per_row,
            "samples_per_row": samples_per_row,
            "scan_time_s": float(scan_time),
            "spectrum_rate_hz": float(1 / scan_time),
            "waterfall_rows": spectra_per_output,
            "waterfall_duration_s": float(waterfall_duration),
        }
        base.update(
            available=True,
            values=values,
            metrics=metrics,
            enabled_resamplers=enabled_resamplers,
            config=config,
        )
    except Exception as exc:
        base["error"] = f"Invalid recorder preset {path}: {exc}"
    return base


def recorder_draft_to_overrides(draft: dict[str, object]) -> dict[str, object]:
    """Validate GUI-neutral draft values and map them to recorder config keys."""
    figsize_value = draft["figsize"]
    if isinstance(figsize_value, str):
        figsize = tuple(float(part.strip()) for part in figsize_value.split(","))
    else:
        figsize = tuple(float(part) for part in figsize_value)
    if len(figsize) != 2 or any(value <= 0 for value in figsize):
        raise ValueError("Figure size must contain two positive values")

    batch_size = int(draft["batch_size"])
    max_packet_size = int(draft["max_packet_size"])
    chunk_size = int(draft["chunk_size"])
    batch_capacity = int(draft["batch_capacity"])
    buffer_size = int(draft["buffer_size"])
    worker_thread_number = int(draft["worker_thread_number"])
    if batch_size <= 0:
        raise ValueError("Batch size must be positive")
    if max_packet_size <= 0:
        raise ValueError("Max packet size must be positive")
    if chunk_size <= 0:
        raise ValueError("Chunk size must be positive")
    if batch_capacity <= 0:
        raise ValueError("Batch capacity must be positive")
    if buffer_size <= 0:
        raise ValueError("Buffer size must be positive")
    if worker_thread_number <= 0:
        raise ValueError("Worker threads must be positive")

    overrides = {
        "packet.batch_size": batch_size,
        "packet.max_packet_size": max_packet_size,
        "packet.num_samples": chunk_size,
        "packet.batch_capacity": batch_capacity,
        "packet.buffer_size": buffer_size,
        "scheduler.worker_thread_number": worker_thread_number,
        "spectrogram.nperseg": int(draft["nperseg"]),
        "spectrogram.nfft": int(draft["nfft"]),
        "spectrogram.noverlap": int(draft["noverlap"]),
        "spectrogram.window": str(draft["window"]),
        "spectrogram.reduce_op": str(draft["reduce_op"]),
        "spectrogram.num_spectra_per_chunk": int(draft["num_spectra_per_chunk"]),
        "spectrogram_output.num_spectra_per_output": int(draft["num_spectra_per_output"]),
        "spectrogram_output.snr_db_min": float(draft["snr_db_min"]),
        "spectrogram_output.snr_db_max": float(draft["snr_db_max"]),
        "spectrogram_output.cmap": str(draft["cmap"]),
        "spectrogram_output.dpi": int(draft["dpi"]),
        "spectrogram_output.figsize": figsize,
        "pipeline.spectrogram": bool(draft["compute"]),
        "pipeline.spectrogram_mqtt": bool(draft["mqtt"]),
        "pipeline.spectrogram_output": bool(draft["output"]),
        "pipeline.digital_rf": bool(draft["digital_rf"]),
        "pipeline.metadata": bool(draft["metadata"]),
    }

    if not bool(draft["digital_rf"]):
        overrides["pipeline.int_converter"] = False

    return overrides


def preview_recorder_settings(
    sample_rate_mhz: int,
    draft: dict[str, object],
    config_dir: str = None,
) -> dict:
    """Resolve draft REC values without mutating controller or recorder state."""
    preset_model = resolve_recorder_preset(sample_rate_mhz, config_dir=config_dir)
    if not preset_model.get("available"):
        preset_model["draft_valid"] = False
        preset_model["draft_error"] = preset_model.get("error", "Preset unavailable")
        return preset_model

    try:
        overrides = recorder_draft_to_overrides(draft)
    except (KeyError, TypeError, ValueError) as exc:
        preset_model["draft_valid"] = False
        preset_model["draft_error"] = str(exc)
        return preset_model

    model = resolve_recorder_preset(sample_rate_mhz, overrides, config_dir)
    if not model.get("available"):
        preset_model["draft_valid"] = False
        error = model.get("error", "Invalid REC settings")
        prefix = f"Invalid recorder preset {model.get('preset_path')}: "
        preset_model["draft_error"] = error.removeprefix(prefix)
        return preset_model

    model["overrides"] = overrides
    model["draft_valid"] = True
    model["draft_error"] = ""
    return model


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
                iface, dest_hex, flags_hex = cols[0], cols[1], cols[3]
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

class HostPlatform:
    """Local host health and platform-management interface."""

    def __init__(self, nvpmodel_config_path: str = "/etc/nvpmodel.conf"):
        self._nvpmodel_config_path = nvpmodel_config_path
        self._cpu_previous = None

    def get_hostname(self) -> str:
        try:
            host = socket.gethostname().strip()
        except Exception:
            host = ""
        return host.split(".", 1)[0] if host else "unknown-host"

    def get_power_modes(self):
        return self._read_nvpmodel_config()

    def get_current_power_mode(self) -> Optional[tuple[str, str]]:
        try:
            output = subprocess.check_output(
                ["nvpmodel", "-q"], stderr=subprocess.DEVNULL, timeout=1.5, text=True
            )
        except Exception as exc:
            logging.debug("Failed to query nvpmodel: %s", exc)
            return None
        mode_id = None
        mode_name = None
        for line in output.splitlines():
            text = line.strip()
            match = re.search(r"NV\s*Power\s*Mode\s*:\s*(.+)$", text, re.IGNORECASE)
            if match:
                mode_name = match.group(1).strip()
                continue
            match = re.search(r"Power\s*Mode\s*:\s*(.+)$", text, re.IGNORECASE)
            if match:
                mode_name = match.group(1).strip()
                continue
            if text.isdigit():
                mode_id = text
        return (mode_id, mode_name) if mode_id or mode_name else None

    def set_power_mode(self, mode_id: str) -> dict:
        result = {
            "ok": False,
            "mode_id": str(mode_id),
            "error_code": None,
            "detail": None,
        }
        try:
            command_prefix = []
            command = ["nvpmodel", "-m", str(mode_id)]
            if os.geteuid() != 0:
                sudo_check = subprocess.run(
                    ["sudo", "-n", "true"], stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, text=True, timeout=2.0
                )
                if sudo_check.returncode != 0:
                    result["error_code"] = "sudo_not_available"
                    result["detail"] = (sudo_check.stderr or sudo_check.stdout or "passwordless sudo unavailable").strip()
                    return result
                command_prefix = ["sudo", "-n"]
                command = [*command_prefix, *command]
            probe = subprocess.run(
                [*command_prefix, "nvpmodel", "-q"], stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, timeout=8.0
            )
            if probe.returncode != 0:
                result["error_code"] = "probe_failed"
                result["detail"] = (probe.stderr or probe.stdout or "nvpmodel -q failed").strip()
                return result
            applied = subprocess.run(
                command, input="YES\n", stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, timeout=15.0
            )
            if applied.returncode != 0:
                result["error_code"] = "apply_failed"
                result["detail"] = (applied.stderr or applied.stdout or "nvpmodel -m failed").strip()
                return result
            result["ok"] = True
            result["detail"] = ((applied.stdout or "") + " " + (applied.stderr or "")).strip() or "nvpmodel accepted mode"
            return result
        except subprocess.TimeoutExpired as exc:
            result["error_code"] = "timeout"
            result["detail"] = str(exc)
            return result
        except FileNotFoundError:
            result["error_code"] = "nvpmodel_not_found"
            result["detail"] = "nvpmodel command not found"
            return result
        except Exception as exc:
            result["error_code"] = "exception"
            result["detail"] = str(exc)
            return result

    def get_power_snapshot(self) -> tuple[dict, list]:
        """Return one tegrastats power snapshot as (rails, temperatures)."""
        return self._read_tegrastats()

    def _read_nvpmodel_config(self):
        modes = []
        default_id = None
        mode_re = re.compile(r"<\s*POWER_MODEL\s+ID\s*=\s*(\d+)\s+NAME\s*=\s*([^>]+?)\s*>", re.IGNORECASE)
        default_re = re.compile(r"<\s*PM_CONFIG\s+DEFAULT\s*=\s*(\d+)\s*>", re.IGNORECASE)
        try:
            with open(self._nvpmodel_config_path, "r", encoding="utf-8", errors="ignore") as file_handle:
                for line in file_handle:
                    text = line.strip()
                    if not text or text.startswith("#"):
                        continue
                    match = mode_re.search(text)
                    if match:
                        modes.append((match.group(1), match.group(2).strip()))
                        continue
                    match = default_re.search(text)
                    if match:
                        default_id = match.group(1)
        except Exception as exc:
            logging.debug("Failed to read nvpmodel modes from %s: %s", self._nvpmodel_config_path, exc)
            return [], None
        return modes, default_id

    def read_thermal(self, limit: int = 6):
        result = {"temps": [], "error_code": None, "detail": None}
        base = "/sys/class/thermal"
        try:
            entries = sorted(name for name in os.listdir(base) if name.startswith("thermal_zone"))
        except Exception as exc:
            result["error_code"], result["detail"] = "list_thermal_failed", str(exc)
            return result
        read_errors = []
        for name in entries:
            try:
                with open(os.path.join(base, name, "temp"), "r", encoding="utf-8") as file_handle:
                    temperature = float(file_handle.read().strip()) / 1000.0
                with open(os.path.join(base, name, "type"), "r", encoding="utf-8") as file_handle:
                    label = file_handle.read().strip()
                if -100.0 <= temperature <= 250.0:
                    result["temps"].append((label or name, temperature))
            except Exception as exc:
                read_errors.append(f"{name}: {exc}")
            if len(result["temps"]) >= limit:
                break
        if not result["temps"]:
            result["error_code"] = "thermal_read_failed"
            result["detail"] = "; ".join(read_errors) or "No valid thermal readings"
        elif read_errors:
            result["error_code"] = "thermal_partial"
            result["detail"] = "; ".join(read_errors)
        return result

    def read_memory(self):
        total_kb = None
        available_kb = None
        try:
            with open("/proc/meminfo", "r", encoding="utf-8") as file_handle:
                for line in file_handle:
                    if line.startswith("MemTotal:"):
                        total_kb = int(line.split()[1])
                    elif line.startswith("MemAvailable:"):
                        available_kb = int(line.split()[1])
        except Exception as exc:
            logging.warning("Failed to read memory info: %s", exc)
            return None
        if total_kb is None or available_kb is None or total_kb <= 0:
            return None
        return max(total_kb - available_kb, 0), total_kb

    def read_disk(self, path: str = "/"):
        try:
            usage = shutil.disk_usage(path)
        except Exception as exc:
            logging.warning("Failed to read disk usage for %s: %s", path, exc)
            return None
        return usage.free, usage.total

    def read_cpu(self):
        try:
            with open("/proc/stat", "r", encoding="utf-8") as file_handle:
                parts = file_handle.readline().strip().split()
            if len(parts) < 5 or parts[0] != "cpu":
                return None
            values = [int(value) for value in parts[1:]]
        except Exception as exc:
            logging.warning("Failed to read CPU stats: %s", exc)
            return None
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        total = sum(values)
        previous = self._cpu_previous
        self._cpu_previous = (total, idle)
        if previous is None:
            return None
        total_delta = total - previous[0]
        idle_delta = idle - previous[1]
        if total_delta <= 0:
            return None
        return (total_delta - idle_delta) * 100.0 / total_delta

    def _read_tegrastats(self):
        try:
            output = subprocess.check_output(
                ["tegrastats", "--interval", "1000"],
                stderr=subprocess.DEVNULL,
                timeout=3.0,
                text=True,
            )
        except subprocess.TimeoutExpired as exc:
            output = exc.output or ""
        except Exception as exc:
            logging.debug("Failed to read tegrastats snapshot: %s", exc)
            return {}, []
        if isinstance(output, bytes):
            output = output.decode(errors="ignore")
        blob = " ".join(line.strip() for line in output.splitlines() if line.strip())
        rails = {
            match.group(1): f"{match.group(2)}/{match.group(3)} mW"
            for match in re.finditer(r"([A-Z0-9_]+)\s+(\d+)mW/(\d+)mW", blob)
        }
        temperatures = [
            (match.group(1), f"{float(match.group(2)):.1f} C")
            for match in re.finditer(r"([A-Za-z0-9_]+)@(-?\d+(?:\.\d+)?)C", blob)
        ]
        return rails, temperatures

    def get_status(self) -> dict:
        return {"state": "online", "hostname": self.get_hostname(), "timestamp": time.time()}

    def get_resources(self) -> dict:
        memory = self.read_memory()
        disk = self.read_disk()
        return {
            "timestamp": time.time(),
            "cpu_usage_percent": self.read_cpu(),
            "memory": {
                "used_kb": memory[0] if memory else None,
                "total_kb": memory[1] if memory else None,
            },
            "disk": {
                "path": "/",
                "free_bytes": disk[0] if disk else None,
                "total_bytes": disk[1] if disk else None,
            },
        }

    def get_network(self) -> dict:
        result = {"status": "Offline", "mac": "-", "ipv4": "-", "error_code": None, "detail": None}
        interface = None
        try:
            output = subprocess.check_output(["ip", "route", "show", "default"], text=True, timeout=1.0)
            match = re.search(r"\bdev\s+(\S+)", output)
            interface = match.group(1) if match else None
        except Exception as exc:
            result["error_code"], result["detail"] = "ip_route_failed", str(exc)
        if not interface:
            try:
                interfaces = sorted(name for name in os.listdir("/sys/class/net") if name != "lo")
                interface = interfaces[0] if interfaces else None
            except Exception as exc:
                result["error_code"], result["detail"] = "list_interfaces_failed", str(exc)
        if not interface:
            result["error_code"] = result["error_code"] or "no_interface"
            result["detail"] = result["detail"] or "No primary network interface found"
            return {"timestamp": time.time(), **result}
        try:
            with open(f"/sys/class/net/{interface}/address", "r", encoding="utf-8") as file_handle:
                result["mac"] = file_handle.read().strip() or "-"
            with open(f"/sys/class/net/{interface}/operstate", "r", encoding="utf-8") as file_handle:
                operstate = (file_handle.read().strip() or "unknown").lower()
            output = subprocess.check_output(["ip", "-4", "addr", "show", "dev", interface], text=True, timeout=1.0)
            match = re.search(r"\binet\s+(\d+\.\d+\.\d+\.\d+)/", output)
            result["ipv4"] = match.group(1) if match else "-"
            online = operstate == "up" and result["ipv4"] != "-"
            result["status"] = f"{'Online' if online else 'Offline'} ({interface})"
            if not online and result["error_code"] is None:
                result["error_code"] = "interface_not_online"
                result["detail"] = f"operstate={operstate}, ipv4={result['ipv4']}"
        except Exception as exc:
            result["error_code"] = result["error_code"] or "network_read_failed"
            result["detail"] = result["detail"] or str(exc)
        return {"timestamp": time.time(), **result}

    def get_thermal(self, limit: int = 6) -> dict:
        result = self.read_thermal(limit)
        return {"timestamp": time.time(), **result}

    def get_power(self, include_tegrastats: bool = False) -> dict:
        modes, default_id = self.get_power_modes()
        current = self.get_current_power_mode()
        result = {
            "timestamp": time.time(),
            "mode": {"id": current[0], "name": current[1]} if current else None,
            "default_mode_id": default_id,
            "available_modes": [{"id": mode_id, "name": name} for mode_id, name in modes],
            "rails": [],
        }
        if include_tegrastats:
            rails, temperatures = self.get_power_snapshot()
            result["rails"] = [{"name": name, "value": value} for name, value in rails.items()]
            result["tegrastats_temperatures"] = [
                {"name": name, "value": value} for name, value in temperatures
            ]
        return result

    @staticmethod
    def _format_power_mode(mode_id, mode_name):
        if mode_name and mode_id:
            return f"{mode_name} (ID {mode_id})"
        return mode_name or (f"ID {mode_id}" if mode_id else None)


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
            self.send_command(f'?WATCH={{"enable":true,"json":true,"raw":{int(raw)}}}')
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

                    init_cmd = '?WATCH={"enable":true,"json":true,"raw":0};'
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

    Created at startup (GUI or CLI). Subscribes only to the specific topics and
    wildcard patterns that callers register listeners for (via on_status /
    on_status_pattern / subscribe), never the broker-wide '#'. Callers may
    subscribe and unsubscribe at runtime; the active set is restored on
    reconnect. Never None once constructed.

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
        self._subscriptions: set[str] = set()
        self._subscription_lock = threading.Lock()
        self._registry_lock = threading.RLock()  # protects _listeners and _pattern_listeners
        self._status_cache: dict[str, dict] = {}
        self._cache_lock = threading.Lock()

        # ---- Synchronous wait plumbing (lazy, persistent per topic) ----
        # Listener is registered once per topic and kept for the bus's life —
        # never re-registered per call.
        self._sync_data: dict[str, dict] = {}
        self._sync_events: dict[str, threading.Event] = {}

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
        with self._registry_lock:
            self._listeners.setdefault(topic, []).append(callback)
        self.subscribe(topic)
        # Emit any cached value that arrived before this listener was registered
        cached = self.get_cached_status(topic)
        if isinstance(cached, dict):
            try:
                callback(cached)
            except Exception as e:
                logging.warning("Status listener initial emit failed for %s: %s", topic, e)

    def on_message(self, callback: Callable[[str, bytes], None]):
        """Register callback(topic, payload_bytes) for subscribed MQTT messages."""
        with self._registry_lock:
            self._global_listeners.append(callback)

    def on_status_pattern(
        self,
        pattern: str,
        callback: Callable[[str, dict], None],
        subscribe: bool = True,
    ):
        """Register callback(topic, data) for JSON messages matching MQTT wildcard pattern.
        
        Supports MQTT wildcards:
        - '+' matches exactly one level between slashes
        - '#' matches zero or more levels at end (must be last character)
        
        Example: on_status_pattern("radiohound/clients/data/+", handler)
        """
        with self._registry_lock:
            self._pattern_listeners.append((pattern, callback))
        if subscribe:
            self.subscribe(pattern)

    def subscribe(self, topic: str):
        """Keep a topic active across the current and future MQTT connections."""
        with self._subscription_lock:
            self._subscriptions.add(topic)
        if self._connected:
            self._client.subscribe(topic)

    def unsubscribe(self, topic: str):
        """Stop receiving a topic until it is explicitly subscribed again."""
        with self._subscription_lock:
            self._subscriptions.discard(topic)
        if self._connected:
            self._client.unsubscribe(topic)

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

    def _ensure_sync_topic(self, topic: str):
        """Lazily register a persistent listener for topic (idempotent).

        Registered once and kept for the bus's life — never re-registered per
        wait call.
        """
        if topic in self._sync_events:
            return
        self._sync_events[topic] = threading.Event()
        self._sync_data[topic] = None

        def _cb(data):
            self._sync_data[topic] = data
            self._sync_events[topic].set()

        self.on_status(topic, _cb)

    def wait_for_status(self, topic: str, timeout_s: float = 2.0, pre_armed: bool = False) -> Optional[dict]:
        """Block until a message arrives on topic, return payload or None on timeout.

        Set pre_armed=True when the event has already been cleared before the
        triggering command was sent (avoids missing a fast response).
        """
        self._ensure_sync_topic(topic)
        if not pre_armed:
            self._sync_events[topic].clear()
            self._sync_data[topic] = None
        if self._sync_events[topic].wait(timeout=timeout_s):
            return self._sync_data[topic]
        return None

    def get_tlm(self, timeout_s: float = 2.0, expected_rx_state: str = None) -> Optional[dict]:
        """Publish a "get tlm" request and wait for the reply.

        Used by RX (arm verification, dwell polling) and firmware readiness.
        When expected_rx_state is set, intermediate status messages are ignored.
        """
        if not self.is_connected():
            return None
        self._ensure_sync_topic(RFSOC_STATUS_TOPIC)
        self._sync_events[RFSOC_STATUS_TOPIC].clear()
        self._sync_data[RFSOC_STATUS_TOPIC] = None
        self.publish_command(
            RFSOC_CMD_TOPIC,
            {"task_name": "get", "arguments": ["tlm"]},
            sleep_s=0,
        )
        deadline = time.time() + timeout_s
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                return None
            tlm = self.wait_for_status(
                RFSOC_STATUS_TOPIC,
                timeout_s=remaining,
                pre_armed=True,
            )
            if expected_rx_state is None or (
                isinstance(tlm, dict) and tlm.get("state_RX") == expected_rx_state
            ):
                return tlm
            self._sync_events[RFSOC_STATUS_TOPIC].clear()
            self._sync_data[RFSOC_STATUS_TOPIC] = None

    def wait_for_firmware_ready(self, max_wait_s: int = 30) -> bool:
        """Poll rfsoc/status until f_s is a valid non-NaN positive number.

        Bus-level (not RX-specific): TX depends on RFSoC firmware being up too.
        """
        if not self.is_connected():
            logging.error("Cannot wait for RFSoC firmware while MQTT is offline")
            return False

        def _f_s_ready(tlm) -> Optional[float]:
            if not isinstance(tlm, dict):
                return None
            f_s = tlm.get("f_s")
            if isinstance(f_s, (int, float)) and f_s == f_s and f_s > 0:
                return f_s
            return None

        logging.info("Waiting for RFSoC firmware to be ready...")
        tlm = self.get_cached_status(RFSOC_STATUS_TOPIC)
        f_s = _f_s_ready(tlm)
        if isinstance(tlm, dict) and tlm.get("state") in ("ready", "active") and f_s is not None:
            logging.info(f"RFSoC firmware ready: f_s={f_s / 1e6:.2f} MHz")
            return True

        deadline = time.time() + max_wait_s
        while time.time() < deadline:
            tlm = self.get_tlm(timeout_s=2.0)
            f_s = _f_s_ready(tlm)
            if isinstance(tlm, dict) and tlm.get("state") in ("ready", "active") and f_s is not None:
                logging.info(f"RFSoC firmware ready: f_s={f_s / 1e6:.2f} MHz")
                return True
            logging.debug("RFSoC not ready yet, waiting...")
            time.sleep(1)
        logging.error(f"RFSoC firmware not ready after {max_wait_s}s")
        return False

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
            with self._subscription_lock:
                subscriptions = tuple(self._subscriptions)
            for topic in subscriptions:
                client.subscribe(topic)
        else:
            self._connected = False
            self._last_error = f"rc={rc}"
            logging.error(f"MQTT connect failed: rc={rc}")
            self._emit_connection_state()

    def _on_message(self, client, userdata, msg):
        # Fire global listeners (raw bytes — for MQTT log tab). Snapshot the
        # whole registry once under the lock so we can both deliver raw bytes
        # and decide below whether any functional listener will consume this
        # message. Failures are isolated so one bad listener cannot abort the
        # rest of the dispatch.
        with self._registry_lock:
            global_cbs = list(self._global_listeners)
            exact_cbs = list(self._listeners.get(msg.topic, []))
            pattern_cbs = list(self._pattern_listeners)
        for cb in global_cbs:
            try:
                cb(msg.topic, msg.payload)
            except Exception:
                logging.exception("Global MQTT listener failed for topic %s", msg.topic)

        # Smart decode: only parse JSON when something will actually consume it.
        # The global (raw-bytes) listeners above already saw every message, so
        # the whole-bus monitor stays complete without forcing the MQTT thread
        # to JSON/base64-decode high-rate traffic (e.g. spectrum frames) that no
        # functional listener is registered for. This keeps decode cost
        # proportional to what the UI uses, even under a broad subscription.
        matching_pattern_cbs = [
            (pattern, cb)
            for pattern, cb in pattern_cbs
            if self.topic_matches(msg.topic, pattern)
        ]
        is_announce = msg.topic == AFE_ANNOUNCE_TOPIC
        if not (exact_cbs or matching_pattern_cbs or is_announce):
            return

        # Parse JSON
        try:
            data = json.loads(msg.payload.decode())
        except Exception:
            return

        # Cache only topics that have a registered exact-match listener. The
        # status grid reads exactly those topics via get_cached_status(); caching
        # every distinct topic seen would grow unbounded with device/topic
        # cardinality (especially under a wildcard subscription). Pattern/spectrum
        # traffic is consumed directly by its listeners and never needs caching.
        if exact_cbs and isinstance(data, dict):
            with self._cache_lock:
                self._status_cache[msg.topic] = data

        # Intercept afe/announce (retained) — cache full schema
        if is_announce and isinstance(data, dict):
            self.afe_announce = data
            logging.info(f"AFE announce received: v{data.get('version', '?')}")

        # Fire exact-match topic-specific listeners
        for cb in exact_cbs:
            try:
                cb(data)
            except Exception:
                logging.exception("Listener callback failed for topic %s", msg.topic)

        # Fire pattern-match listeners (only meaningful for dict payloads)
        if isinstance(data, dict):
            for pattern, cb in matching_pattern_cbs:
                try:
                    cb(msg.topic, data)
                except Exception:
                    logging.exception(
                        "Pattern listener callback failed for pattern %s", pattern
                    )

    def _on_disconnect(self, client, userdata, rc):
        self._connected = False
        if rc != 0:
            self._last_error = f"disconnect rc={rc}"
            logging.warning(f"MQTT unexpectedly disconnected: rc={rc}")
        self._emit_connection_state()

    def topic_matches(self, topic: str, pattern: str) -> bool:
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
        """Publish a JSON command to a topic. Used by ControllerRx and thin publishers."""
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

    def publish(self, topic: str, payload_str: str = "", retain: bool = False):
        """Publish a raw string payload to a topic. For debug/manual use."""
        if not self._connected:
            logging.warning("MQTT offline: publish not sent to %s", topic)
            return False

        info = self._client.publish(topic, payload_str, retain=retain)
        if info.rc != mqtt_lib.MQTT_ERR_SUCCESS:
            self._last_error = f"publish rc={info.rc}"
            logging.warning("MQTT publish failed: topic=%s rc=%s", topic, info.rc)
            return False
        return True

    def clear_retained(self, topic: str):
        """Clear the retained message for a topic by publishing empty retained payload."""
        return self.publish(topic, payload_str="", retain=True)

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

    def rfsoc_status(self):
        """Request RFSoC status refresh via status task."""
        self.publish_command(RFSOC_CMD_TOPIC, {"task_name": "status", "arguments": {}})

    def rfsoc_capture_next_pps(self):
        """Arm capture on next PPS edge."""
        self.publish_command(RFSOC_CMD_TOPIC, {"task_name": "capture_next_pps"})

    def rfsoc_capture_now(self):
        """Start capture immediately (no PPS sync)."""
        self.publish_command(RFSOC_CMD_TOPIC, {"task_name": "capture"})

    def rfsoc_set_channel(self, channels: str):
        """Set active RX channels (e.g. 'A', 'B', 'A,B'). Resets FPGA control register."""
        self.publish_command(RFSOC_CMD_TOPIC, {"task_name": "set", "arguments": f"channel {channels}"})

    def rfsoc_set_freq_metadata(self, freq_hz: float):
        """Set the frequency metadata tag written into UDP packet headers (Hz)."""
        self.publish_command(RFSOC_CMD_TOPIC, {"task_name": "set", "arguments": f"freq_metadata {freq_hz}"})

    def rfsoc_set_if(self, if_mhz: float):
        """Set RFSoC IF frequency in MHz."""
        self.publish_command(RFSOC_CMD_TOPIC, {"task_name": "set", "arguments": f"freq_IF {if_mhz}"})

    def rfsoc_set_pps_publish_interval(self, interval_s: int):
        """Set RFSoC PPS status publish interval in seconds (0 disables periodic PPS publish)."""
        self.publish_command(
            RFSOC_CMD_TOPIC,
            {"task_name": "set_pps_publish_interval", "arguments": int(interval_s)},
        )

    def rfsoc_get_pll_config(self, converter: str, tile: int):
        """Query RFSoC PLL configuration for converter ('adc'|'dac') and tile index."""
        converter_norm = str(converter).strip().lower()
        if converter_norm not in ("adc", "dac"):
            raise ValueError(f"converter must be 'adc' or 'dac', got {converter!r}")
        self.publish_command(
            RFSOC_CMD_TOPIC,
            {"task_name": "get", "arguments": f"pll_config {converter_norm} {int(tile)}"},
            sleep_s=0,
        )

    def rfsoc_set_tx_center_freq(self, freq_mhz: float):
        """Set TX DAC RFDC mixer/NCO center frequency on all TX channels."""
        self.publish_command(RFSOC_CMD_TOPIC, {"task_name": "set", "arguments": f"tx_center_freq {freq_mhz}"})

    def rfsoc_set_tx_offset_freq(self, freq_mhz: float):
        """Set TX function-generator baseband offset frequency (|f| < 32 MHz)."""
        if abs(freq_mhz) >= TX_OFFSET_FREQ_MAX_MHZ:
            raise ValueError(
                f"TX offset frequency magnitude must be < {TX_OFFSET_FREQ_MAX_MHZ} MHz, got {freq_mhz} MHz"
            )
        self.publish_command(RFSOC_CMD_TOPIC, {"task_name": "set", "arguments": f"tx_offset_freq {freq_mhz}"})

    def rfsoc_set_tx_amplitude(self, amplitude_bins: int):
        """Set TX waveform peak amplitude in DAC bins (0..8191)."""
        if not (0 <= amplitude_bins <= TX_AMPLITUDE_BINS_MAX):
            raise ValueError(
                f"TX amplitude must be 0..{TX_AMPLITUDE_BINS_MAX} bins, got {amplitude_bins}"
            )
        self.publish_command(RFSOC_CMD_TOPIC, {"task_name": "set", "arguments": f"tx_amplitude {amplitude_bins}"})

    def rfsoc_set_tx_channel(self, channel_list: str):
        """Set TX DAC channel(s): None, A, B, or A,B."""
        if channel_list not in TX_CHANNEL_OPTIONS:
            raise ValueError(
                f"TX channel must be one of {list(TX_CHANNEL_OPTIONS)}, got {channel_list!r}"
            )
        self.publish_command(RFSOC_CMD_TOPIC, {"task_name": "set", "arguments": f"tx_channel {channel_list}"})

    def rfsoc_tx_start(self):
        """Send explicit TX start command to RFSoC."""
        self.publish_command(RFSOC_CMD_TOPIC, {"task_name": "tx_start"})

    def rfsoc_tx_stop(self):
        """Send explicit TX stop command to RFSoC."""
        self.publish_command(RFSOC_CMD_TOPIC, {"task_name": "tx_stop"})

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

    def afe_set_log_rate(self, n: float):
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
            if not isinstance(value, str):
                return None
            token = value.strip()
            # Backend service name (e.g. 'valon', 'dummy') -> canonical.
            canonical = _TUNER_CANONICAL_BY_BACKEND.get(token.lower())
            if canonical:
                return canonical
            # Already canonical (e.g. 'VALON').
            if token.upper() in TUNERS:
                return token.upper()
            return None

        for key in candidate_keys:
            resolved = _normalize(status_payload.get(key))
            if resolved:
                return resolved

        for nested_key in ("tuner", "arguments", "status", "data", "result"):
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
    def normalize_tx_status(payload: Optional[dict]) -> Optional[dict]:
        """Normalize RFSoC status payload into a stable TX view for GUI/CLI.

        Owns the RFSoC TX wire-field schema so consumers never read raw keys.

        Uses the firmware's explicit "state_TX" field for transmission state.
        Reports only what the DAC itself is doing — nothing downstream of the RFSoC
        output connector is observable, so a caller must not read "idle" as "no RF".

        Returns:
          {
            "channels": list,
            "center_freq": float|None,
            "offset_freq": float|None,
            "amplitude_bins": int|None,
            "tx_state": "transmitting"|"idle"|"unknown",
            "transmitting": bool,
            "raw": dict,
          }
        """
        if not isinstance(payload, dict):
            return None
        channels = payload.get("tx_channels") or []
        amplitude_bins = payload.get("tx_amplitude_bins")
        tx = payload.get("state_TX")
        # A dead service (retained LWT) or a payload with no DAC field proves nothing;
        # report unknown rather than letting it read as "not transmitting".
        if payload.get("state") == "offline" or tx is None:
            tx_state = "unknown"
        elif tx == "active":
            tx_state = "transmitting"
        else:
            tx_state = "idle"
        return {
            "channels": channels,
            "center_freq": payload.get("tx_center_freq"),
            "offset_freq": payload.get("tx_offset_freq"),
            "amplitude_bins": amplitude_bins,
            "tx_state": tx_state,
            "transmitting": tx_state == "transmitting",
            "raw": payload,
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

    @staticmethod
    def normalize_spec_payload(payload: Optional[dict]) -> Optional[dict]:
        """Normalize SPEC MQTT payload into display-ready dBFS row metadata."""
        if not isinstance(payload, dict):
            return None
        data_b64 = payload.get("data", "")
        if not isinstance(data_b64, str) or not data_b64:
            return None
        try:
            raw = base64.b64decode(data_b64)
        except Exception:
            return None
        n = len(raw) // 4
        if n <= 0:
            return None
        bins = np.frombuffer(raw, dtype="<f4", count=n)
        if bins.size == 0:
            return None
        row = bins.copy()
        np.maximum(row, SPEC_POWER_FLOOR, out=row)
        np.log10(row, out=row)
        row *= SPEC_DB_SCALE
        row_min = float(np.min(row))
        row_max = float(np.max(row))
        if not (math.isfinite(row_min) and math.isfinite(row_max)):
            return None
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        return {
            "row": row,
            "row_min": row_min,
            "row_max": row_max,
            "ts": payload.get("timestamp"),
            "center_frequency": payload.get("center_frequency"),
            "sample_rate": payload.get("sample_rate"),
            "n": int(row.size),
            "fmin": metadata.get("fmin"),
            "fmax": metadata.get("fmax"),
            "scan_time": metadata.get("scan_time"),
            "units": "dBFS",
        }


# ===== CAPTURE CONTROLLER ===== #

class CaptureTelemetryLogger:
    """Capture-scoped GPS/IMU/MAG/HK track, independent of afe_service.py's own logger.

    Subscribes to the AFE data topics via plain MQTT fan-out for the duration of a
    capture and writes a merged row into the capture folder each time GPS updates
    (so track density follows the GNSS rate, not a fixed timer). No coordination
    with afe_service.py is required — it never redirects or controls the AFE
    service's own always-on logging, it just listens to the same public topics.
    Every row is flushed+fsynced immediately; nothing here depends on a clean stop.

    Columns come from afe/announce's "schema" block, never from a local field list:
    a second copy of the schema is what silently blanked lat/lon for months. No
    timestamp is generated here — rows carry only the service and device clocks, so
    the writing process's clock never enters the data.
    """

    _STREAMS = ("gps", "mag", "imu", "hk")

    def __init__(self, bus: "MEPBus"):
        self.bus = bus
        self._fh = None
        self._writer = None
        self._schema: dict = {}
        self._last_imu: dict = {}
        self._last_mag: dict = {}
        self._last_hk: dict = {}
        self._gps_cb = None
        self._imu_cb = None
        self._mag_cb = None
        self._hk_cb = None
        self._active = False

    @staticmethod
    def schema_from_announce(announce: Optional[dict]) -> Optional[dict]:
        """Return the telemetry column schema, or None if this service predates it."""
        if not isinstance(announce, dict):
            return None
        schema = announce.get("schema")
        if not isinstance(schema, dict):
            return None
        if not all(isinstance(schema.get(s), list) and schema[s] for s in CaptureTelemetryLogger._STREAMS):
            return None
        return schema

    def start(self, capture_dir: str):
        """Begin logging a GPS/telemetry track into capture_dir. Idempotent.

        Telemetry is supporting metadata, never a precondition for recording: if the
        schema is unavailable this logs and returns, and the RF capture proceeds.
        """
        if self._active:
            return
        schema = self.schema_from_announce(self.bus.get_cached_status(AFE_ANNOUNCE_TOPIC))
        if schema is None:
            logging.error("Capture telemetry not logged: no schema in afe/announce")
            return
        self._schema = schema

        data_dir = os.path.join(capture_dir, "data")
        os.makedirs(data_dir, exist_ok=True)
        path = os.path.join(data_dir, "gps_telemetry.csv")
        header = []
        for stream, prefix in (("gps", "gnss"), ("mag", "mag"), ("imu", "imu"), ("hk", "hk")):
            header.extend(f"{prefix}_{k}" for k in self._schema[stream])
        header.append("registers_json")

        # Append so a resumed capture extends its track instead of truncating it.
        existing = None
        if os.path.exists(path) and os.path.getsize(path) > 0:
            with open(path, newline="", encoding="utf-8") as fh:
                existing = next(csv.reader(fh), None)
        if existing is not None and existing != header:
            logging.error("Capture telemetry not logged: %s was written with a different schema", path)
            return

        self._fh = open(path, "a", newline="", encoding="utf-8")
        self._writer = csv.writer(self._fh)
        if existing is None:
            self._writer.writerow(header)
            self._fh.flush()
            os.fsync(self._fh.fileno())

        self._imu_cb = lambda data: self._last_imu.update(data)
        self._mag_cb = lambda data: self._last_mag.update(data)
        self._hk_cb = lambda data: self._last_hk.update(data)
        self._gps_cb = self._on_gps

        self.bus.on_status(AFE_IMU_TOPIC, self._imu_cb)
        self.bus.on_status(AFE_MAG_TOPIC, self._mag_cb)
        self.bus.on_status(AFE_HK_TOPIC, self._hk_cb)
        self.bus.on_status(AFE_GNSS_TOPIC, self._gps_cb)  # GPS arrival drives the row cadence
        self._active = True
        logging.info(f"Capture telemetry log started: {path}")

    def _on_gps(self, gps: dict):
        if not self._active or self._writer is None:
            return
        row = []
        for source, stream in ((gps, "gps"), (self._last_mag, "mag"),
                               (self._last_imu, "imu"), (self._last_hk, "hk")):
            row.extend(source.get(k) for k in self._schema[stream])
        regs = self.bus.get_cached_status(AFE_REGISTERS_TOPIC) or {}
        row.append(json.dumps(regs.get("registers", {}), sort_keys=True, separators=(",", ":")))
        try:
            self._writer.writerow(row)
            self._fh.flush()
            os.fsync(self._fh.fileno())  # durable before the next line executes, not "eventually"
        except Exception:
            logging.exception("Capture telemetry write error")

    def stop(self):
        """Stop logging and release MQTT listeners. Safe to call even if never started."""
        if not self._active:
            return
        for topic, cb in (
            (AFE_GNSS_TOPIC, self._gps_cb),
            (AFE_IMU_TOPIC, self._imu_cb),
            (AFE_MAG_TOPIC, self._mag_cb),
            (AFE_HK_TOPIC, self._hk_cb),
        ):
            if cb is not None:
                self.bus.remove_listener(topic, cb)
        if self._fh is not None:
            try:
                self._fh.flush()
                os.fsync(self._fh.fileno())
                self._fh.close()
            except Exception:
                logging.exception("Capture telemetry close error")
        self._fh, self._writer = None, None
        self._active = False
        logging.info("Capture telemetry log stopped")


class ControllerTuner:
    """Shared external-tuner state: selection/init/readiness/LO-apply logic.

    Instantiated once and held by both ControllerRx (RX) and ControllerTx (TX)
    so the two independent operational paths read/apply the same tuner state —
    there is only one physical oscillator: whichever side last calls apply_lo
    is what the hardware is actually tuned to.
    """

    def __init__(self, bus: MEPBus):
        self.bus = bus
        self._init_tuner_state()

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

    def _init_tuner_state(self):
        self.tuner: Optional[str] = None
        self.adc_if_mhz: Optional[float] = None
        self.injection: Optional[str] = None
        self._tuner_request_counter = 0

    def configure(self, tuner: str = None, adc_if_mhz: float = None, injection: str = None):
        """Stage tuner selection/IF/injection and kick off init if needed."""
        self.tuner = _normalize_tuner(tuner)
        self.adc_if_mhz = adc_if_mhz

        if self.tuner is None:
            self.injection = None
        else:
            self.injection = resolve_injection(self.tuner, injection)
            self._ensure_tuner_initialized()

    def _resolved_tuner_name(self) -> Optional[str]:
        """Concrete hardware tuner name, resolving 'auto' via cached status.

        Returns the resolved model (e.g. 'VALON'), or None when no tuner is
        selected or 'auto' has not yet been resolved by the service.
        """
        if self.tuner is None:
            return None
        if self.tuner.lower() != "auto":
            return self.tuner.upper()
        return MEPBus._extract_tuner_name(self.bus.get_cached_status(TUNER_STATUS_TOPIC))

    def _tuner_ready(self) -> bool:
        """True when the tuner service reports online with a tuner that
        satisfies the current selection. Any resolved tuner satisfies 'auto'.

        Reads the asynchronously-cached MQTT status — never blocks, never
        issues a request.
        """
        if self.tuner is None:
            return True  # NCO mode needs no tuner
        status = self.bus.get_cached_status(TUNER_STATUS_TOPIC)
        if not isinstance(status, dict) or status.get("state") != "online":
            return False
        resolved = MEPBus._extract_tuner_name(status)
        if not resolved:
            return False
        if self.tuner.lower() == "auto":
            return True
        return resolved == self.tuner.upper()

    def _send_init_tuner(self):
        """Publish a fire-and-forget init_tuner command for the selected tuner."""
        if self.tuner.lower() == "auto":
            args = {}
        else:
            args = {"force_tuner": TUNERS[self.tuner.upper()]["backend"]}
        logging.info(f"Requesting tuner init: {self.tuner}")
        self.bus.publish_command(TUNER_CMD_TOPIC, {"task_name": "init_tuner", "arguments": args})

    def _ensure_tuner_initialized(self):
        """Request tuner init only when the service isn't already online with the
        selected tuner.

        Fire-and-forget: the cached MQTT status is the source of truth, so
        repeated Starts with an already-online tuner do no work and never block.
        """
        if self.tuner is None:
            return  # NCO mode
        if not self._require_mqtt("initialize tuner"):
            return
        if self._tuner_ready():
            logging.debug(f"Tuner already online for {self.tuner}; skipping init")
            return
        self._send_init_tuner()

    def _wait_for_tuner_ready(self, timeout_s: float = 5.0) -> bool:
        """Bounded poll for the tuner to report online for the selected tuner.

        Used only as a cold-start guard before the first frequency set; warm
        captures return immediately. Polls the async status cache rather than
        issuing a blocking request/response handshake.
        """
        if self._tuner_ready():
            return True
        logging.info("Waiting for tuner to come online...")
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if self._tuner_ready():
                logging.info(f"Tuner online: {self._resolved_tuner_name()}")
                return True
            time.sleep(0.1)
        return False

    def _next_tuner_request_id(self) -> str:
        self._tuner_request_counter += 1
        return f"mep-tuner-req-{self._tuner_request_counter}"

    def _query_tuner_lock(self, timeout_s: float = 2.0) -> Optional[dict]:
        """Request get_lock_status and return the correlated response payload.

        The tuner service publishes command responses to TUNER_RESPONSE_TOPIC
        (separate from the periodic status on TUNER_STATUS_TOPIC). Correlates on
        a unique session_id: on_status replays the last cached message on
        registration, so without correlation a prior lock response could be
        mistaken for this one. Returns None on timeout.
        """
        session_id = self._next_tuner_request_id()
        matched = {"payload": None}
        done = threading.Event()

        def _listener(data):
            if isinstance(data, dict) and data.get("session_id") == session_id:
                matched["payload"] = data
                done.set()

        self.bus.on_status(TUNER_RESPONSE_TOPIC, _listener)
        try:
            self.bus.publish_command(TUNER_CMD_TOPIC, {
                "task_name": "get_lock_status",
                "arguments": {},
                "session_id": session_id,
            })
            if done.wait(timeout=timeout_s):
                return matched["payload"]
            return None
        finally:
            self.bus.remove_listener(TUNER_RESPONSE_TOPIC, _listener)

    @staticmethod
    def _interpret_lock(value) -> Optional[bool]:
        """Map a get_lock_status value to locked(True)/unlocked(False), or None
        when the shape is unrecognized.

        The tuner returns a dict of per-PLL booleans; locked means every PLL is
        locked. Matches the GUI's lock-status interpretation.
        """
        if isinstance(value, bool):
            return value
        if isinstance(value, dict):
            locks = [v for v in value.values() if isinstance(v, bool)]
            if locks:
                return all(locks)
        return None

    def _report_tuner_lock(self) -> None:
        """Log the VALON PLL lock state as an informational status update.

        This is never a go/no-go gate — PLL lock is not guaranteed on every
        capture and is reported purely for the user's benefit. Only the VALON
        exposes a real lock status (the caller invokes this for VALON only). The
        single case worth surfacing loudly is an explicit "not locked", which
        gets a WARNING. Every other case (locked, no response, unrecognized
        shape) is quiet, and the capture always proceeds regardless.
        """
        resp = self._query_tuner_lock(timeout_s=2.0)
        if resp is None:
            logging.debug("No lock-status response from tuner")
            return
        locked = self._interpret_lock(resp.get("value"))
        if locked is True:
            logging.info("Tuner PLL locked")
        elif locked is False:
            logging.warning("Tuner PLL NOT locked")
        else:
            logging.debug(f"Unrecognized lock-status value {resp.get('value')!r}")

    def apply_lo(self, f_mhz: float) -> Optional[float]:
        """Wait for the tuner, compute LO from f_mhz via configured IF/injection, apply it.

        Caller must ensure self.tuner is not None and self.adc_if_mhz is set.
        Returns the applied LO (MHz), or None if the tuner never came online.
        """
        if not self._wait_for_tuner_ready():
            return None
        resolved_tuner = self._resolved_tuner_name()
        lo_mhz = resolve_lo_mhz(f_mhz, self.adc_if_mhz, self.injection)
        self.bus.tuner_set_freq(lo_mhz)
        time.sleep(0.1)
        if resolved_tuner == "VALON":
            self._report_tuner_lock()
        return lo_mhz


class Recorder:
    """Recorder-specific state and configuration lifecycle.

    This keeps the recorder's selected channel, sample rate, named capture, staged
    overrides, and live recorder-running observations in one place while leaving
    ControllerRx responsible for RFSoC, tuner, and sweep orchestration.
    """

    def __init__(self, bus: MEPBus, controller: "ControllerRx"):
        self.bus = bus
        self.controller = controller
        self.host_platform = HostPlatform()
        self.telemetry_logger = CaptureTelemetryLogger(bus)

        self.channel: Optional[str] = None
        self.sample_rate_mhz: Optional[int] = None
        self.capture_name: Optional[str] = None
        self._active_channel = None
        self._active_sample_rate = None
        self._recorder_running = False
        self.recorder_overrides: dict[str, object] = {}

    @property
    def tuner(self):
        return self.controller.tuner

    @property
    def adc_if_mhz(self):
        return self.controller.adc_if_mhz

    @property
    def injection(self):
        return self.controller.injection

    def configure_capture(self, channel: str, sample_rate_mhz: int, capture_name: str = None):
        """Set parameters used by start_sweep / start_single / start_recorder."""
        self.channel = channel.upper()
        self.sample_rate_mhz = sample_rate_mhz
        self.capture_name = capture_name

    def set_recorder_overrides(self, overrides: dict[str, object]):
        """Replace the persistent recorder overrides used after config.load."""
        self.recorder_overrides = dict(overrides)

    def get_recorder_preset_model(self) -> dict:
        """Return the selected preset resolved with no REC overrides."""
        return resolve_recorder_preset(self.sample_rate_mhz)

    def preview_recorder_settings(self, draft: dict[str, object]) -> dict:
        """Resolve draft REC settings without changing staged state."""
        return preview_recorder_settings(self.sample_rate_mhz, draft)

    def stage_recorder_settings(self, draft: dict[str, object]) -> dict:
        """Atomically validate and replace staged REC overrides."""
        model = self.preview_recorder_settings(draft)
        if not model.get("available"):
            raise ValueError(model.get("error") or "Recorder settings are unavailable")
        self.recorder_overrides = dict(model["overrides"])
        return model

    def get_staged_recorder_model(self) -> dict:
        """Return the selected preset resolved with current staged overrides."""
        return resolve_recorder_preset(self.sample_rate_mhz, self.recorder_overrides)

    def clear_recorder_overrides(self):
        """Clear the persistent recorder overrides."""
        self.recorder_overrides.clear()

    def apply_recorder_overrides(self):
        """Reapply the persistent recorder overrides after config.load."""
        if not self.recorder_overrides:
            return

        logging.info(
            "Applying recorder overrides: %s",
            ", ".join(sorted(self.recorder_overrides.keys())),
        )
        for key, value in self.recorder_overrides.items():
            self.bus.recorder_config_set(key, value)

    def prepare_preview_data_dir(self):
        parent = os.path.dirname(PREVIEW_DATA_DIR)
        stale_dir = os.path.join(parent, f".preview_data_stale_{int(time.time() * 1000)}")
        if os.path.isdir(PREVIEW_DATA_DIR):
            logging.info("Preview sample rate changed: rotating %s", PREVIEW_DATA_DIR)
            os.replace(PREVIEW_DATA_DIR, stale_dir)
        os.makedirs(PREVIEW_DATA_DIR, exist_ok=True)
        if os.path.isdir(stale_dir):
            shutil.rmtree(stale_dir, ignore_errors=True)

    def _capture_root_dir(self) -> str:
        """Return capture root folder that contains both settings.json and data/."""
        capture_folder = (self.capture_name or "").strip() or "preview"
        return os.path.join(CAPTURES_ROOT_DIR, capture_folder)

    def _format_sds_path(self) -> str:
        """Generate SDS path token: <capture>_<hostname>_<random6>."""
        capture_folder = (self.capture_name or "").strip() or "preview"
        host_token = self.host_platform.get_hostname().strip().lower()
        host_token = re.sub(r"[^a-z0-9_-]", "", host_token)
        if not host_token:
            raise RuntimeError("Unable to derive hostname token for sds_path")

        random6 = uuid.uuid4().hex[:6]
        return f"{capture_folder}_{host_token}_{random6}"

    def write_capture_settings(self, f_hz: float, sweep: bool) -> None:
        """Write capture settings.json beside the capture data directory."""
        try:
            f_mhz = float(f_hz) / 1e6
            if self.tuner is None:
                if_mhz = f_mhz
                lo_mhz = f_mhz
            else:
                if_mhz = float(self.adc_if_mhz) if self.adc_if_mhz is not None else f_mhz
                injection_mode = str(self.injection or "high").lower()
                lo_mhz = resolve_lo_mhz(f_mhz, if_mhz, injection_mode)

            payload = {
                "capture_name": (self.capture_name or "").strip() or "preview",
                "sds_path": self._format_sds_path(),
                "f_hz": int(round(float(f_hz))),
                "channel": self.channel,
                "sample_rate": int(self.sample_rate_mhz) if self.sample_rate_mhz is not None else None,
                "sweep": bool(sweep),
                "tuner": "none" if self.tuner is None else str(self.tuner).lower(),
                "injection": "none" if self.injection is None else str(self.injection).lower(),
                "if_mhz": if_mhz,
                "lo_mhz": lo_mhz,
                "created_at": datetime.utcnow().isoformat(),
            }

            capture_root = self._capture_root_dir()
            os.makedirs(capture_root, exist_ok=True)
            settings_path = os.path.join(capture_root, "settings.json")
            with open(settings_path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
                fh.write("\n")
            logging.info("Wrote capture settings: %s", settings_path)
        except Exception as exc:
            logging.warning("Failed to write capture settings.json: %s", exc)

    def start_recorder(self, freq_idx_offset: float = 0.0):
        """Configure and enable the DigitalRF recorder."""
        if not self.controller._require_mqtt("start recorder"):
            return False

        recorder_model = self.get_staged_recorder_model()
        if not recorder_model.get("available"):
            logging.error(
                "Recorder preset validation failed before start: %s",
                recorder_model.get("error") or "unknown error",
            )
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

        if not self.capture_name:
            self.prepare_preview_data_dir()

        self.bus.recorder_config_set("packet.freq_idx_offset", str(freq_idx_offset))

        if self.capture_name:
            channel_dir = f"{self.capture_name}/data/ch{self.channel}"
            spectrogram_subdir = f"{self.capture_name}/data/ch{self.channel}_spectrogram_images"
        else:
            channel_dir = f"preview/data/ch{self.channel}"
            spectrogram_subdir = f"preview/data/ch{self.channel}_spectrogram_images"

        self.bus.recorder_config_set("drf_sink.channel_dir", channel_dir)
        self.bus.recorder_config_set("spectrogram_output.plot_subdir", spectrogram_subdir)
        self.bus.recorder_config_set("basic_network.dst_port", str(dst_port))
        state = self.controller.get_conjugate_state()
        apply_conjugate = bool(state["apply_conjugate"])
        logging.info(
            "Recorder pre-enable conjugate: policy=%s tuner=%r injection=%r apply_conjugate=%s",
            state["policy"],
            state["tuner"],
            state["injection"],
            str(apply_conjugate).lower(),
        )
        self.bus.recorder_config_set("packet.apply_conjugate", str(apply_conjugate).lower())
        self.apply_recorder_overrides()

        self.controller.prepare_status_wait(RECORDER_STATUS_TOPIC)
        self.bus.recorder_enable()
        status = self.controller.wait_for_status(RECORDER_STATUS_TOPIC, timeout_s=3.0, pre_armed=True)
        if status is not None:
            logging.info(f"Recorder enabled — status: {status}")
        else:
            logging.warning("Recorder enable sent but no status response received")

        self._active_channel = self.channel
        self._active_sample_rate = self.sample_rate_mhz
        self._recorder_running = True
        if self.capture_name:
            self.telemetry_logger.start(self._capture_root_dir())
        return True

    def stop_recorder(self):
        """Disable the DigitalRF recorder."""
        logging.info("Stopping recorder")
        self.bus.recorder_disable()
        self._recorder_running = False
        self.telemetry_logger.stop()

    def build_config(self, apply_conjugate: bool) -> dict:
        """
        Resolve the recorder configuration for the active channel.

        Loads the authoritative preset for the current sample rate, applies the staged
        GUI overrides, enforces pipeline dependencies, and sets the channel-specific
        runtime keys (UDP port, DigitalRF channel directory, spectrogram subdir, conjugate
        policy). This is the same config the live recorder path resolves; the profiler
        serializes it to inline JSON and hands it to ``mep_recorder.py``.
        """
        preset_path, source = recorder_preset_path(self.sample_rate_mhz)
        if source == "unavailable":
            raise FileNotFoundError(f"Recorder preset not found: {preset_path}")

        config = copy.deepcopy(_load_yaml_mapping(preset_path))

        for key, value in self.recorder_overrides.items():
            _set_dotted_value(config, key, value)

        _normalize_recorder_pipeline(config)

        _set_dotted_value(config, "basic_network.dst_port", RECORDER_CHANNEL_PORTS[self.channel])
        capture_folder = (self.capture_name or "").strip() or "preview"
        _set_dotted_value(config, "drf_sink.channel_dir", f"{capture_folder}/data/ch{self.channel}")
        _set_dotted_value(config, "spectrogram_output.plot_subdir", f"{capture_folder}/data/ch{self.channel}_spectrogram_images")
        _set_dotted_value(config, "packet.freq_idx_offset", 0)

        _set_dotted_value(config, "packet.apply_conjugate", bool(apply_conjugate))

        return config

    def prepare_holoscan_profile(self):
        """Stop live recording and prepare the preview data directory."""
        logging.info("Disabling live recording so the profiler owns the pipeline...")
        self.stop_recorder()
        time.sleep(1)
        self.prepare_preview_data_dir()

    def run_holoscan_profile(
        self,
        config: dict,
        trace: str = "cuda,nvtx,osrt",
        duration: int = 60,
        output_path: str = "/data/captures/holoscan_profile",
        force_overwrite: bool = True,
        cudabacktrace: str = "all",
        flush_on_cudaprofilerstop: bool = False,
    ) -> dict:
        """Run nsys against one recorder process using a resolved config.

        The caller owns RFSoC preparation and cleanup. This method owns the
        recorder process lifecycle, profiler invocation, and report metadata.
        The recorder remains disabled when profiling finishes.
        """
        compose_file = "/opt/radiohound/docker/compose.yaml"
        output_file = f"{output_path}.nsys-rep"
        yaml_file = f"{output_path}.yaml"

        try:
            force_flag = "--force-overwrite=true" if force_overwrite else "--force-overwrite=false"
            flush_flag = f"--flush-on-cudaprofilerstop={'true' if flush_on_cudaprofilerstop else 'false'}"
            config_json = json.dumps(config)
            cmd = [
                "docker", "compose", "-f", compose_file, "exec", "-T",
                "-e", "HOLOSCAN_ENABLE_PROFILE=1",
                "recorder",
                "nsys", "profile",
                f"--trace={trace}",
                f"--cudabacktrace={cudabacktrace}",
                flush_flag,
                f"--duration={duration}",
                f"--output={output_path}",
                force_flag,
                "python3", "/app/mep_recorder.py",
                "--config", config_json,
                "--ram_ringbuffer_path", "/ramdisk",
                "--output_path", CAPTURES_ROOT_DIR,
            ]

            reproducible_cmd = (
                "HOLOSCAN_ENABLE_PROFILE=1 nsys profile \\\n"
                f"  --trace={trace} \\\n"
                f"  --cudabacktrace={cudabacktrace} \\\n"
                f"  {flush_flag} \\\n"
                f"  --duration={duration} \\\n"
                f"  --output={output_path} \\\n"
                f"  {force_flag} \\\n"
                "  python3 /app/mep_recorder.py \\\n"
                f"  --config {yaml_file} \\\n"
                "  --ram_ringbuffer_path /ramdisk \\\n"
                f"  --output_path {CAPTURES_ROOT_DIR}"
            )
            logging.info(
                "Profiling command (run inside the 'recorder' container to reproduce):\n%s",
                reproducible_cmd,
            )

            logging.info("Launching nsys profiling: duration=%ss, output=%s", duration, output_path)
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=duration + 60,
            )
            if result.stdout:
                logging.info("nsys stdout:\n%s", result.stdout)
            if result.stderr:
                logging.info("nsys stderr:\n%s", result.stderr)

            combined_output = f"{result.stdout or ''}\n{result.stderr or ''}"
            report_generated = "Generated:" in combined_output and ".nsys-rep" in combined_output
            if not report_generated:
                stderr_short = (result.stderr or "(no stderr)").strip()[:1000]
                logging.error("nsys did not report a generated file (exit %s)", result.returncode)
                return {
                    "success": False,
                    "error": f"No report generated (nsys exit {result.returncode}): {stderr_short}",
                    "status": "Failed",
                }

            rc = result.returncode
            rc_descriptions = {
                0: "(clean exit)",
                143: "(recorder stopped by SIGTERM at duration limit)",
                137: "(recorder killed by SIGKILL at duration limit)",
                139: "(recorder crashed with segfault)",
            }
            rc_note = rc_descriptions.get(rc, "")
            status = f"Profile saved to {output_file}, exit code {rc} {rc_note}".strip()
            logging.info(
                "Profiling complete (nsys reported a generated report, exit %s): %s",
                rc,
                output_file,
            )

            try:
                yaml_text = _dump_yaml_text(config)
                subprocess.run(
                    ["docker", "compose", "-f", compose_file, "exec", "-T", "recorder", "tee", yaml_file],
                    input=yaml_text,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                logging.info("Saved profiling config: %s", yaml_file)
            except Exception as exc:
                logging.warning("Could not save profiling config YAML: %s", exc)

            return {
                "success": True,
                "status": status,
                "output_file": output_file,
                "returncode": rc,
            }
        except subprocess.TimeoutExpired:
            msg = f"nsys timed out after {duration + 60}s"
            logging.error(msg)
            return {"success": False, "error": msg, "status": "Timeout"}
        except Exception as exc:
            logging.exception("Profile error")
            return {"success": False, "error": str(exc), "status": "Error"}


class ControllerRx:
    """On-demand sweep/record orchestrator — owns sync-wait infra and recipes.

    Created when the user clicks Start (or from CLI). Takes a MEPBus reference
    for all MQTT communication, plus a ControllerTuner (shared with ControllerTx
    when both exist, since there is only one physical tuner). Owns transient
    session state: sweep config, recorder state, stop flag, synchronous wait.
    """

    def __init__(self, bus: MEPBus, tuner_ctrl: "ControllerTuner" = None):
        self.bus = bus
        self.host_platform = HostPlatform()
        self.tuner_ctrl = tuner_ctrl if tuner_ctrl is not None else ControllerTuner(bus)
        self.recorder = Recorder(bus, self)
        self.telemetry_logger = self.recorder.telemetry_logger

        self.conjugate_policy: str = CONJUGATE_POLICY_DEFAULT

        # ---- Stop flag for sweeps ----
        self._stop_flag = threading.Event()

        # ---- Synchronous wait infrastructure (for sweep orchestration) ----
        self._status = {t: None for t in _SYNC_STATUS_TOPICS}
        self._status_lock = threading.Lock()
        self._status_events = {t: threading.Event() for t in _SYNC_STATUS_TOPICS}

        # ---- Register sync-wait listeners on bus ----
        self._sync_cbs = {}

        for topic in _SYNC_STATUS_TOPICS:
            def _make_status_cb(t):
                def _cb(data):
                    with self._status_lock:
                        self._status[t] = data
                    self._status_events[t].set()
                return _cb
            self._sync_cbs[topic] = _make_status_cb(topic)
            self.bus.on_status(topic, self._sync_cbs[topic])

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

    # ---- Read-only proxies onto the shared tuner state ----

    @property
    def tuner(self) -> Optional[str]:
        return self.tuner_ctrl.tuner

    @property
    def adc_if_mhz(self) -> Optional[float]:
        return self.tuner_ctrl.adc_if_mhz

    @property
    def injection(self) -> Optional[str]:
        return self.tuner_ctrl.injection

    def close(self):
        """Remove sync-wait listeners from bus and stop recorder (best-effort)."""
        for topic, cb in self._sync_cbs.items():
            self.bus.remove_listener(topic, cb)
        self._sync_cbs.clear()
        self.telemetry_logger.stop()
        if self.recorder._recorder_running:
            try:
                self.recorder.stop_recorder()
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    #  Synchronous wait helpers (used during sweep orchestration)          #
    # ------------------------------------------------------------------ #

    def prepare_status_wait(self, topic: str):
        """Clear a status wait before publishing a command that triggers it."""
        if topic not in self._status_events:
            raise ValueError(f"Unknown sync status topic: {topic!r}")
        self._status_events[topic].clear()
        with self._status_lock:
            self._status[topic] = None

    def wait_for_status(self, topic: str, timeout_s: float = 2.0, pre_armed: bool = False):
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

    # ------------------------------------------------------------------ #
    #  Conjugate policy                                                    #
    # ------------------------------------------------------------------ #

    def _normalized_conjugate_policy(self) -> str:
        """Return a valid conjugate policy from current controller state."""
        policy = str(self.conjugate_policy or "").strip().lower()
        if policy in CONJUGATE_POLICY_OPTIONS:
            return policy
        return CONJUGATE_POLICY_DEFAULT

    def _resolve_apply_conjugate(self) -> bool:
        """Resolve effective packet.apply_conjugate from policy + tuner state."""
        policy = self._normalized_conjugate_policy()
        injection_mode = (self.injection or "").lower()
        if policy == "auto":
            return (self.tuner is not None and injection_mode == "high")
        if policy == "force_on":
            return True
        if policy == "force_off":
            return False
        return False

    def get_conjugate_state(self) -> dict:
        """Expose conjugate policy + effective state for GUI/CLI consumers."""
        policy = self._normalized_conjugate_policy()
        return {
            "policy": policy,
            "policy_options": list(CONJUGATE_POLICY_OPTIONS),
            "tuner": self.tuner,
            "injection": self.injection,
            "apply_conjugate": self._resolve_apply_conjugate(),
        }

    # ------------------------------------------------------------------ #
    #  Recorder recipe (sweep orchestration)                               #
    # ------------------------------------------------------------------ #

    def profile_holoscan(
        self,
        freq_hz: float,
        trace: str = "cuda,nvtx,osrt",
        duration: int = 60,
        output_path: str = "/data/captures/holoscan_profile",
        force_overwrite: bool = True,
        cudabacktrace: str = "all",
        flush_on_cudaprofilerstop: bool = False,
    ) -> dict:
        """Tune and arm RFSoC, then run the recorder-owned Holoscan profile."""
        if not self._require_mqtt("profile holoscan"):
            return {"success": False, "error": "MQTT not connected", "status": "Error"}
        if self.recorder.channel not in RECORDER_CHANNEL_PORTS:
            return {
                "success": False,
                "error": f"Invalid channel: {self.recorder.channel}",
                "status": "Failed",
            }

        config = self.recorder.build_config(
            apply_conjugate=self.get_conjugate_state()["apply_conjugate"]
        )
        armed = False
        try:
            self.recorder.prepare_holoscan_profile()
            logging.info("Tuning and arming RFSoC for profiling at %.2f MHz...", freq_hz / 1e6)
            if not self.tune_and_arm(freq_hz):
                return {
                    "success": False,
                    "error": "RFSoC tune/arm failed; aborting profile",
                    "status": "Failed",
                }
            armed = True
            time.sleep(1)
            return self.recorder.run_holoscan_profile(
                config=config,
                trace=trace,
                duration=duration,
                output_path=output_path,
                force_overwrite=force_overwrite,
                cudabacktrace=cudabacktrace,
                flush_on_cudaprofilerstop=flush_on_cudaprofilerstop,
            )
        finally:
            if armed:
                try:
                    logging.info("Stopping RFSoC UDP stream after profiling...")
                    self.bus.rfsoc_reset()
                except Exception as exc:
                    logging.warning("Could not reset RFSoC after profiling: %s", exc)

    # ------------------------------------------------------------------ #
    #  Tune and Arm recipe (from "Tune and Capture" flowchart)             #
    # ------------------------------------------------------------------ #

    def tune_and_arm(self, f_hz: float) -> bool:
        """Full tune + capture-arm sequence for one frequency step."""
        if not self._require_mqtt("tune and arm"):
            return False

        f_mhz = f_hz / 1e6

        self.bus.rfsoc_reset()

        # Set channel immediately after reset, before any frequency writes.
        # freq_IF and freq_metadata write to whichever channel is currently
        # active in the FPGA — if channel is set after freq commands the
        # metadata ends up on the old channel's registers.
        self.bus.publish_command(RFSOC_CMD_TOPIC, {"task_name": "set", "arguments": f"channel {self.recorder.channel}"})
        time.sleep(0.05)

        injection_mode = (self.injection or "").lower()

        if self.tuner is None:
            if self.injection is not None:
                logging.debug("Ignoring injection=%r because tuner is None", self.injection)
            logging.info(f"[TUNER_NO] RFSoC NCO → {GREEN}{f_mhz:.2f} MHz{RESET}")
            self.bus.publish_command(RFSOC_CMD_TOPIC, {"task_name": "set", "arguments": f"freq_IF {f_mhz}"})
            time.sleep(0.1)
        else:
            if self.adc_if_mhz is None:
                raise ValueError("adc_if_mhz is required when a tuner is specified")

            self.bus.publish_command(RFSOC_CMD_TOPIC, {"task_name": "set", "arguments": f"freq_IF {self.adc_if_mhz}"})
            time.sleep(0.1)

            # Cold-start guard: only blocks when the tuner isn't yet online
            # (e.g. first capture after launch). Warm captures pass straight
            # through because _tuner_ready() is already True.
            lo_mhz = self.tuner_ctrl.apply_lo(f_mhz)
            if lo_mhz is None:
                logging.error(f"Tuner '{self.tuner}' did not come online — aborting capture")
                return False

            logging.info(
                f"[TUNER_YES/{injection_mode or 'low'}-side] RF → {GREEN}{f_mhz:.2f} MHz{RESET}  "
                f"LO={lo_mhz:.2f} MHz  IF={self.adc_if_mhz:.2f} MHz"
            )

        # Common tail: metadata → capture → TLM
        # (channel was already set right after reset above)
        self.bus.publish_command(RFSOC_CMD_TOPIC, {"task_name": "set", "arguments": f"freq_metadata {f_hz}"})
        self.bus.publish_command(RFSOC_CMD_TOPIC, {"task_name": "capture_next_pps"})

        # Ignore status packets from the reset/configuration sequence; this
        # command's postcondition is an active RFSoC capture.
        tlm = self.bus.get_tlm(timeout_s=2.0, expected_rx_state="active")

        if not tlm or tlm.get("state_RX") != "active":
            logging.error(f"RFSoC capture failed or inactive: {MEPBus._tlm_to_str(tlm)}")
            return False
        logging.info(f"Armed — {MEPBus._tlm_to_str(tlm)}")
        return True

    # ------------------------------------------------------------------ #
    #  Scan recipes (from "Start Scan" flowchart)                          #
    # ------------------------------------------------------------------ #

    def start_single(self, f_hz: float, dwell_s: float = None):
        """Single-frequency capture with optional dwell-based auto-stop."""
        if not self._require_mqtt("run single capture"):
            return False

        self.recorder.write_capture_settings(f_hz=f_hz, sweep=False)

        sample_rate_changed = (self.recorder.sample_rate_mhz != self.recorder._active_sample_rate)
        channel_changed = (self.recorder.channel != self.recorder._active_channel)

        if self.recorder._recorder_running and (sample_rate_changed or channel_changed):
            logging.info(
                "Sample rate or channel changed — restarting recorder "
                f"(sr: {self.recorder._active_sample_rate}→{self.recorder.sample_rate_mhz}, "
                f"ch: {self.recorder._active_channel}→{self.recorder.channel})"
            )
            self.recorder.stop_recorder()
            if not self.tune_and_arm(f_hz):
                return False
            if not self.recorder.start_recorder():
                return False
        else:
            if not self.tune_and_arm(f_hz):
                return False
            if not self.recorder._recorder_running:
                if not self.recorder.start_recorder():
                    return False

        if dwell_s is not None and dwell_s > 0:
            self._dwell(dwell_s)
            self.recorder.stop_recorder()
        return True

    def start_sweep(self, freqs_hz, dwell_s: float):
        """Sweep: start recorder once, tune_and_arm + dwell per step."""
        if not self._require_mqtt("run sweep"):
            return False

        logging.info(f"Sweep: {len(freqs_hz)} steps, dwell={dwell_s}s")

        if not self.recorder.start_recorder():
            return False
        wrote_settings = False

        try:
            for f_hz in freqs_hz:
                if self._stop_flag.is_set():
                    logging.info("Sweep interrupted by stop flag")
                    break

                if not wrote_settings:
                    self.recorder.write_capture_settings(f_hz=f_hz, sweep=True)
                    wrote_settings = True

                if not self.tune_and_arm(f_hz):
                    return False
                self._dwell(dwell_s)
        finally:
            self.recorder.stop_recorder()
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
            tlm = self.bus.get_tlm(timeout_s=1.5)
            logging.debug(MEPBus._tlm_to_str(tlm))
            time.sleep(1)

    def request_stop(self):
        """Signal the current sweep or dwell to exit early."""
        logging.info("Stop requested")
        self._stop_flag.set()

    def stop(self):
        """Full stop: break any running sweep/dwell, disable the recorder, and
        reset the RFSoC. Safe to call even if nothing is currently running.
        """
        self.request_stop()
        self.recorder.stop_recorder()
        self.bus.rfsoc_reset()


# ===== TX CONTROLLER ===== #

class ControllerTx:
    """DAC function-generator (transmit) orchestrator.

    Owns the TX start/stop policy and lives independently of RX capture: a single
    instance is created once at app startup and never recreated, so transmit is
    available with or without an RX session and RX reconfiguration can never
    disturb an active transmit. TX state physically lives in the RFSoC's FPGA
    registers, fully parallel to the RX capture path.

    Takes a ControllerTuner (shared with ControllerRx when both exist, since
    there is only one physical tuner/oscillator). Whichever side last calls
    start/tune_and_arm with a tuner selected is what the LO is set to.
    """

    def __init__(self, bus: MEPBus, tuner_ctrl: "ControllerTuner" = None):
        self.bus = bus
        self.tuner_ctrl = tuner_ctrl if tuner_ctrl is not None else ControllerTuner(bus)

    # ---- Read-only proxies onto the shared tuner state ----

    @property
    def tuner(self) -> Optional[str]:
        return self.tuner_ctrl.tuner

    @property
    def adc_if_mhz(self) -> Optional[float]:
        return self.tuner_ctrl.adc_if_mhz

    @property
    def injection(self) -> Optional[str]:
        return self.tuner_ctrl.injection

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

    def start(self, channel: str, center_freq_mhz: float,
              offset_freq_mhz: float, amplitude_bins: int) -> bool:
        """Apply staged TX settings and begin (or update) transmission.

        If a tuner is selected, sets the shared LO from center_freq_mhz first
        (same formula/init/readiness path RX uses). Then sets the TX parameters
        (center freq, amplitude, offset freq) and calls the explicit tx_start
        task, which the firmware now handles atomically.
        """
        if not self._require_mqtt("start TX"):
            return False
        try:
            if self.tuner is not None:
                if self.adc_if_mhz is None:
                    raise ValueError("adc_if_mhz is required when a tuner is specified")
                lo_mhz = self.tuner_ctrl.apply_lo(center_freq_mhz)
                if lo_mhz is None:
                    logging.error(f"Tuner '{self.tuner}' did not come online — aborting TX start")
                    return False
                logging.info(
                    "TX tuner LO set: %.3f MHz (IF=%.3f MHz, injection=%s)",
                    lo_mhz, self.adc_if_mhz, self.injection,
                )
            self.bus.rfsoc_set_tx_center_freq(center_freq_mhz)
            self.bus.rfsoc_set_tx_amplitude(amplitude_bins)
            self.bus.rfsoc_set_tx_offset_freq(offset_freq_mhz)
            self.bus.rfsoc_set_tx_channel(channel)
            self.bus.rfsoc_tx_start()
            logging.info(
                "TX start/update: channel=%s center=%.3f MHz offset=%.3f MHz amplitude=%d bins",
                channel, center_freq_mhz, offset_freq_mhz, amplitude_bins,
            )
            return True
        except ValueError as e:
            logging.error(f"TX start failed: {e}")
            return False

    def stop(self) -> bool:
        """Disable all TX output (authoritative hardware-off).

        Publishes the explicit tx_stop command to the firmware.
        Idempotent and safe to call at any time, including shutdown.
        """
        if not self._require_mqtt("stop TX"):
            return False
        self.bus.rfsoc_tx_stop()
        logging.info("TX stop: sent to firmware")
        return True


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
                        help="Override injection side (default: from TUNERS table)")
    parser.add_argument("--log-level",  "-l",  type=str,   default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
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

    # === Build controller === #
    bus = MEPBus()
    tuner_ctrl = ControllerTuner(bus)
    tuner_ctrl.configure(tuner=args.tuner, adc_if_mhz=args.adc_if_mhz, injection=args.injection)
    capture = ControllerRx(bus, tuner_ctrl)

    capture.recorder.configure_capture(
        channel=args.channel,
        sample_rate_mhz=args.sample_rate_mhz,
        capture_name=args.capture_name,
    )

    # === Wait for RFSoC firmware === #
    if not bus.wait_for_firmware_ready(max_wait_s=30):
        logging.error("RFSoC firmware not ready — aborting")
        capture.close()
        bus.disconnect()
        exit(1)

    # === Run === #
    freqs_hz = get_frequency_list(args.freq_start, args.freq_end, args.step)
    is_sweep = not math.isnan(args.freq_end)

    try:
        if is_sweep:
            capture.start_sweep(freqs_hz, dwell_s=args.dwell)
        else:
            capture.start_single(freqs_hz[0])
    finally:
        logging.info("Stopping recorder")
        capture.recorder.stop_recorder()
        capture.close()
        bus.disconnect()
