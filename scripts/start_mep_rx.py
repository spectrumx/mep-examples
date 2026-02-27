#!/opt/radiohound/python313/bin/python
"""
start_mep_rx_v2.py

Automate tuning and starting the RFSoC I/Q stream.
Supports single-frequency capture and frequency sweep.

All control is done via direct MQTT publishes — no subprocess calls,
no hardware-driver imports (tuner_*, mep_rfsoc).

Author: john.marino@colorado.edu
"""

# ===== IMPORTS ===== #
import argparse
import time
import logging
import json
import os
import math
from datetime import datetime
import threading
from typing import Optional
import paho.mqtt.client as mqtt_lib

# ===== CONFIG ===== #
LOG_DIR     = os.path.join(os.path.expanduser("~"), "log", "spectrumx")
MQTT_BROKER = "localhost"
MQTT_PORT   = 1883

RFSOC_CMD_TOPIC      = "rfsoc/command"
RFSOC_STATUS_TOPIC   = "rfsoc/status"
RECORDER_CMD_TOPIC   = "recorder/command"
RECORDER_STATUS_TOPIC = "recorder/status"
TUNER_CMD_TOPIC      = "tuner_control/command"
TUNER_STATUS_TOPIC   = "tuner_control/status"

STATUS_TOPICS = (RFSOC_STATUS_TOPIC, RECORDER_STATUS_TOPIC, TUNER_STATUS_TOPIC)

RECORDER_CHANNEL_PORTS = {"A": 60134, "B": 60133, "C": 60132, "D": 60131}

# Which side does each physical tuner inject from?
# Both available tuners are high-side: LO = RF + IF
TUNER_INJECTION_SIDE = {
    "VALON":   "high",
    "LMX2820": "high",
    "TEST":    "high",
}

GREEN = "\033[92m"
RESET = "\033[0m"


# ===== MEP CONTROLLER ===== #

class MEPController:
    """
    Orchestrates RFSoC, recorder, and tuner via MQTT.

    Recipes (from system flowcharts):
      tune_and_arm(f_hz)         — full tune+capture sequence (one step)
      start_recorder()           — configure and enable DigitalRF recorder
      stop_recorder()            — disable recorder
      run_sweep(...)             — start recorder, loop tune_and_arm, dwell
      run_single(f_hz)           — single freq; restarts recorder only if
                                   sample_rate or channel changed
    """

    def __init__(
        self,
        channel:      str,
        sample_rate:  int,
        tuner:        str   = None,   # "VALON", "LMX2820", "TEST", "auto", or None
        adc_if:       float = None,   # Fixed RFSoC IF in MHz (required with tuner)
        injection:    str   = None,   # "high"/"low" — overrides TUNER_INJECTION_SIDE
        capture_name: str   = None,   # Save under {capture_name}/sr.../ch...; None → ringbuffer
        broker:       str   = MQTT_BROKER,
        port:         int   = MQTT_PORT,
    ):
        self.channel      = channel.upper()
        self.sample_rate  = sample_rate
        self.tuner        = tuner
        self.adc_if       = adc_if
        self.capture_name = capture_name

        # Injection side: only meaningful when a tuner is present
        if tuner is None:
            self.injection = None
        elif injection is not None:
            self.injection = injection                          # explicit CLI override
        elif tuner.lower() == "auto":
            self.injection = "high"                            # auto-detect: both known tuners are high-side
        elif tuner.upper() in TUNER_INJECTION_SIDE:
            self.injection = TUNER_INJECTION_SIDE[tuner.upper()]
        else:
            raise ValueError(f"Tuner {tuner!r} not in TUNER_INJECTION_SIDE — add it")

        # "What changed" state — tracks what the recorder was last configured with
        self._active_channel     = None
        self._active_sample_rate = None
        self._recorder_running   = False

        # Stop flag — set by request_stop() to interrupt _dwell() and run_sweep()
        self._stop_flag = threading.Event()

        # ---- MQTT setup ----
        self._tlm       = None
        self._tlm_lock  = threading.Lock()
        self._tlm_event = threading.Event()

        # Per-topic status storage for feedback from recorder and tuner services
        self._status       = {t: None  for t in STATUS_TOPICS}
        self._status_lock  = threading.Lock()
        self._status_events = {t: threading.Event() for t in STATUS_TOPICS}

        self._client = mqtt_lib.Client(
            callback_api_version=mqtt_lib.CallbackAPIVersion.VERSION1,
            client_id=f"mep_rx_v2_{int(time.time())}",
        )
        self._client.on_connect    = self._on_connect
        self._client.on_message    = self._on_message
        self._client.on_disconnect = self._on_disconnect

        logging.info(f"Connecting to MQTT broker {broker}:{port}")
        self._client.connect(broker, port, keepalive=60)
        self._client.loop_start()
        time.sleep(0.5)

    # ------------------------------------------------------------------ #
    #  MQTT internals                                                      #
    # ------------------------------------------------------------------ #

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            logging.info("MQTT connected")
            for topic in STATUS_TOPICS:
                client.subscribe(topic)
                logging.debug(f"Subscribed to {topic}")
        else:
            logging.error(f"MQTT connect failed: rc={rc}")

    def _on_message(self, client, userdata, msg):
        try:
            data = json.loads(msg.payload.decode())
        except Exception as e:
            logging.warning(f"Failed to parse message on {msg.topic}: {e}")
            return

        if msg.topic == RFSOC_STATUS_TOPIC:
            with self._tlm_lock:
                self._tlm = data
            self._tlm_event.set()

        if msg.topic in STATUS_TOPICS:
            with self._status_lock:
                self._status[msg.topic] = data
            self._status_events[msg.topic].set()
            logging.debug(f"[{msg.topic}] {data}")

    def _on_disconnect(self, client, userdata, rc):
        if rc != 0:
            logging.warning(f"MQTT unexpectedly disconnected: rc={rc}")

    def _publish(self, topic: str, payload: dict, sleep_s: float = 0.1):
        self._client.publish(topic, json.dumps(payload))
        if sleep_s:
            time.sleep(sleep_s)

    def _get_tlm(self, timeout_s: float = 2.0):
        """Request and return the latest RFSoC telemetry, or None on timeout."""
        with self._tlm_lock:
            self._tlm = None
        self._tlm_event.clear()
        self._client.publish(RFSOC_CMD_TOPIC, json.dumps({"task_name": "get", "arguments": ["tlm"]}))
        if self._tlm_event.wait(timeout=timeout_s):
            with self._tlm_lock:
                return self._tlm
        return None

    def _wait_for_status(self, topic: str, timeout_s: float = 2.0):
        """Wait for a status message on the given topic, return payload or None.

        Clears the event before waiting so that the next message arriving after
        this call is captured, not a stale one from before.
        """
        if topic not in self._status_events:
            raise ValueError(f"Unknown status topic: {topic!r}")
        self._status_events[topic].clear()
        with self._status_lock:
            self._status[topic] = None
        if self._status_events[topic].wait(timeout=timeout_s):
            with self._status_lock:
                return self._status[topic]
        logging.warning(f"No status response from {topic} within {timeout_s}s — service may not be running")
        return None

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

    def disconnect(self):
        self._client.loop_stop()
        self._client.disconnect()

    # ------------------------------------------------------------------ #
    #  Startup                                                             #
    # ------------------------------------------------------------------ #

    def wait_for_firmware_ready(self, max_wait_s: int = 30) -> bool:
        """Poll rfsoc/status until f_s is a valid non-NaN positive number."""
        logging.info("Waiting for RFSoC firmware to be ready...")
        deadline = time.time() + max_wait_s
        while time.time() < deadline:
            tlm = self._get_tlm(timeout_s=2.0)
            if tlm is not None:
                f_s = tlm.get("f_s")
                if isinstance(f_s, (int, float)) and f_s == f_s and f_s > 0:
                    logging.info(f"RFSoC firmware ready: f_s={f_s/1e6:.2f} MHz")
                    return True
            logging.debug("RFSoC not ready yet, waiting...")
            time.sleep(1)
        logging.error(f"RFSoC firmware not ready after {max_wait_s}s")
        return False

    # ------------------------------------------------------------------ #
    #  Recorder recipe                                                     #
    # ------------------------------------------------------------------ #

    def start_recorder(self, freq_idx_offset: float = 0.0):
        """Configure and enable the DigitalRF recorder.

        Uses self.channel, self.sample_rate, and self.injection.
        Always stops any active recording first.
        """
        if self.channel not in RECORDER_CHANNEL_PORTS:
            raise ValueError(f"channel must be one of {list(RECORDER_CHANNEL_PORTS.keys())}")

        dst_port    = RECORDER_CHANNEL_PORTS[self.channel]
        config_name = f"sr{self.sample_rate}MHz"

        logging.info(
            f"Starting recorder: channel={self.channel}, config={config_name}, "
            f"port={dst_port}"
        )

        self._publish(RECORDER_CMD_TOPIC, {"task_name": "disable"})

        self._publish(RECORDER_CMD_TOPIC, {
            "task_name": "config.load",
            "arguments": {"name": config_name},
            "response_topic": "recorder/config/response",
        })

        self._publish(RECORDER_CMD_TOPIC, {
            "task_name": "config.set",
            "arguments": {"key": "packet.freq_idx_offset", "value": str(freq_idx_offset)},
            "response_topic": "recorder/config/response",
        })

        if self.capture_name:
            channel_dir = f"{self.capture_name}/{config_name}/ch{self.channel}"
        else:
            channel_dir = f"ringbuffer/{config_name}/ch{self.channel}"

        self._publish(RECORDER_CMD_TOPIC, {
            "task_name": "config.set",
            "arguments": {"key": "drf_sink.channel_dir", "value": channel_dir},
            "response_topic": "recorder/config/response",
        })

        self._publish(RECORDER_CMD_TOPIC, {
            "task_name": "config.set",
            "arguments": {"key": "basic_network.dst_port", "value": str(dst_port)},
            "response_topic": "recorder/config/response",
        })

        self._publish(RECORDER_CMD_TOPIC, {"task_name": "enable"})
        status = self._wait_for_status(RECORDER_STATUS_TOPIC, timeout_s=3.0)
        if status is not None:
            logging.info(f"Recorder enabled — status: {status}")
        else:
            logging.warning("Recorder enable sent but no status response received")

        self._active_channel     = self.channel
        self._active_sample_rate = self.sample_rate
        self._recorder_running   = True

    def stop_recorder(self):
        """Disable the DigitalRF recorder."""
        logging.info("Stopping recorder")
        self._publish(RECORDER_CMD_TOPIC, {"task_name": "disable"})
        self._recorder_running = False

    # ------------------------------------------------------------------ #
    #  Tune and Arm recipe  (from "Tune and Capture" flowchart)           #
    # ------------------------------------------------------------------ #

    def tune_and_arm(self, f_hz: float) -> bool:
        """
        Full tune + capture-arm sequence for one frequency step.

        TUNER_NO:   Reset → set NCO to f_hz → metadata → channel → capture
        TUNER_YES:  Reset → set fixed IF → init tuner (auto or forced) →
                    compute LO (low/high side) → set LO → PLL check →
                    metadata → channel → capture → TLM
        """
        f_mhz = f_hz / 1e6

        # Reset RFSoC
        self._publish(RFSOC_CMD_TOPIC, {"task_name": "reset"})

        if self.tuner is None:
            # ---- TUNER_NO: NCO sweeps to target frequency ----
            logging.info(f"[TUNER_NO] RFSoC NCO → {GREEN}{f_mhz:.2f} MHz{RESET}")
            self._publish(RFSOC_CMD_TOPIC, {"task_name": "set", "arguments": f"freq_IF {f_mhz}"})
            time.sleep(0.1)

        else:
            # ---- TUNER_YES: RFSoC IF is fixed, external tuner sweeps ----
            if self.adc_if is None:
                raise ValueError("adc_if is required when a tuner is specified")

            resolved_tuner = self.tuner.upper()

            # Set fixed IF
            self._publish(RFSOC_CMD_TOPIC, {"task_name": "set", "arguments": f"freq_IF {self.adc_if}"})
            time.sleep(0.1)

            # Init tuner: auto-select or force a specific type
            if self.tuner.lower() == "auto":
                logging.debug("Tuner: auto-select (not forcing tuner type)")
                self._publish(TUNER_CMD_TOPIC, {"task_name": "init_tuner", "arguments": {}})
                init_status = self._wait_for_status(TUNER_STATUS_TOPIC, timeout_s=2.0)
                resolved_tuner = self._extract_tuner_name(init_status) or "AUTO"
                logging.info(f"Auto tuner resolved to: {resolved_tuner}")
            else:
                self._publish(TUNER_CMD_TOPIC, {
                    "task_name": "init_tuner",
                    "arguments": {"force_tuner": self.tuner},
                })
            time.sleep(0.1)

            # LO calculation depends on injection side
            if self.injection == "high":
                lo_mhz = f_mhz + self.adc_if   # LO > RF
            else:
                lo_mhz = f_mhz - self.adc_if   # LO < RF

            logging.info(
                f"[TUNER_YES/{self.injection}-side] RF → {GREEN}{f_mhz:.2f} MHz{RESET}  "
                f"LO={lo_mhz:.2f} MHz  IF={self.adc_if:.2f} MHz"
            )

            # Spectrum flip: conjugate required when LO is above RF (high-side injection)
            # to correct the mirrored spectrum that results from downconversion.
            apply_conjugate = (self.injection == "high")
            self._publish(RECORDER_CMD_TOPIC, {
                "task_name": "config.set",
                "arguments": {"key": "packet.apply_conjugate", "value": str(apply_conjugate).lower()},
            })

            # Set tuner LO frequency
            self._publish(TUNER_CMD_TOPIC, {"task_name": "set_freq", "arguments": {"freq_mhz": lo_mhz}})
            time.sleep(0.1)

            # PLL lock check: Valon supports get_lock_status; other tuners do not
            if resolved_tuner == "VALON":
                self._publish(TUNER_CMD_TOPIC, {"task_name": "get_lock_status", "arguments": {}})
                lock_status = self._wait_for_status(TUNER_STATUS_TOPIC, timeout_s=2.0)
                if lock_status is not None:
                    logging.info(f"Tuner lock status: {lock_status}")
                else:
                    logging.warning("No tuner lock status response received")

        # Common tail: metadata → channel → capture → TLM
        self._publish(RFSOC_CMD_TOPIC, {"task_name": "set", "arguments": f"freq_metadata {f_hz}"})
        self._publish(RFSOC_CMD_TOPIC, {"task_name": "set", "arguments": f"channel {self.channel}"})
        self._publish(RFSOC_CMD_TOPIC, {"task_name": "capture_next_pps"})

        tlm = self._get_tlm()
        if not tlm or tlm.get("state") != "active":
            logging.error(f"RFSoC capture failed or inactive: {self._tlm_to_str(tlm)}")
            return False
        logging.info(f"Armed — {self._tlm_to_str(tlm)}")
        return True

    # ------------------------------------------------------------------ #
    #  Scan recipes  (from "Start Scan" flowchart)                        #
    # ------------------------------------------------------------------ #

    def run_single(self, f_hz: float):
        """
        Single-frequency capture with 'what changed' recorder logic.

        Sample rate or channel changed → stop recorder, tune_and_arm, restart recorder
        Frequency only (or first run)  → tune_and_arm; start recorder if not running
        """
        sample_rate_changed = (self.sample_rate != self._active_sample_rate)
        channel_changed     = (self.channel     != self._active_channel)

        if self._recorder_running and (sample_rate_changed or channel_changed):
            logging.info(
                "Sample rate or channel changed — restarting recorder "
                f"(sr: {self._active_sample_rate}→{self.sample_rate}, "
                f"ch: {self._active_channel}→{self.channel})"
            )
            self.stop_recorder()
            self.tune_and_arm(f_hz)
            self.start_recorder()
        else:
            self.tune_and_arm(f_hz)
            if not self._recorder_running:
                self.start_recorder()

    def run_sweep(self, freqs_hz, dwell_s: float, restart_interval: int = None):
        """
        Sweep recipe: start recorder once, tune_and_arm + dwell per step.
        Optionally force-restarts the recorder every restart_interval seconds.
        """
        logging.info(
            f"Sweep: {len(freqs_hz)} steps, dwell={dwell_s}s, "
            f"restart_interval={restart_interval}s"
        )

        self.start_recorder()
        last_restart = time.time()

        for f_hz in freqs_hz:
            if self._stop_flag.is_set():
                logging.info("Sweep interrupted by stop flag")
                break

            if restart_interval is not None and time.time() - last_restart >= restart_interval:
                logging.info("Restart interval reached — restarting recorder")
                self.start_recorder()
                last_restart = time.time()

            self.tune_and_arm(f_hz)
            self._dwell(dwell_s)

    # ------------------------------------------------------------------ #
    #  Utilities                                                           #
    # ------------------------------------------------------------------ #

    def _dwell(self, dwell_s: float):
        """Sleep for dwell_s, logging TLM each second. Exits early if _stop_flag is set."""
        start = time.time()
        while (time.time() - start) < dwell_s:
            if self._stop_flag.is_set():
                logging.info("Dwell interrupted by stop flag")
                return
            tlm = self._get_tlm(timeout_s=1.5)
            logging.debug(self._tlm_to_str(tlm))
            time.sleep(1)

    def request_stop(self):
        """Signal the current sweep or dwell to exit early."""
        logging.info("Stop requested")
        self._stop_flag.set()

    @staticmethod
    def _tlm_to_str(tlm) -> str:
        if tlm is None:
            return "<no tlm>"
        return (
            f"state={tlm.get('state')} "
            f"f_c={float(tlm.get('f_c_hz', 0))/1e6:.2f} MHz "
            f"f_if={float(tlm.get('f_if_hz', 0))/1e6:.2f} MHz "
            f"f_s={float(tlm.get('f_s', 0))/1e6:.2f} MHz "
            f"pps={tlm.get('pps_count')} "
            f"ch={tlm.get('channels')}"
        )


# ===== HELPERS ===== #

def get_frequency_list(start_mhz: float, end_mhz: float, step_mhz: float):
    start_hz = int(start_mhz * 1e6)
    step_hz  = int(step_mhz  * 1e6)
    if math.isnan(end_mhz):
        return [start_hz]
    end_hz = int(end_mhz * 1e6)
    return range(start_hz, end_hz + step_hz, step_hz)


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
    parser.add_argument("--sample-rate","-r",  type=int,   default=None,
                        help="Recording sample rate in MHz (default: step size)")
    parser.add_argument("--step",       "-s",  type=float, default=10,
                        help="Sweep step size in MHz")
    parser.add_argument("--dwell",      "-d",  type=float, default=60,
                        help="Dwell time per step in seconds")
    parser.add_argument("--tuner",      "-t",  type=tuner_type_arg, default=None,
                        help="Tuner: VALON, LMX2820, TEST, auto, or None")
    parser.add_argument("--adc_if",            type=float, default=None,
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
    parser.add_argument("--capture_name",       type=str,   default=None,
                        help="Save data under captures/{name}/... (default: ringbuffer)")
    args = parser.parse_args()
    args.channel = args.channel.upper()

    if args.tuner is not None and args.adc_if is None:
        parser.error("--adc_if is required when --tuner is set")

    if args.sample_rate is None:
        args.sample_rate = int(args.step)

    # === Logging === #
    os.makedirs(LOG_DIR, exist_ok=True)
    timestamp = datetime.now().isoformat().replace(":", "-").replace(".", "-")
    log_path  = os.path.join(LOG_DIR, f"capture_sweep_{timestamp}.log")
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
        os.system(os.path.join(os.getcwd(), "rfsoc_update_ntp.bash"))

    # === Build controller === #
    mep = MEPController(
        channel      = args.channel,
        sample_rate  = args.sample_rate,
        tuner        = args.tuner,
        adc_if       = args.adc_if,
        injection    = args.injection,
        capture_name = args.capture_name,
    )

    # === Wait for RFSoC firmware === #
    if not mep.wait_for_firmware_ready(max_wait_s=30):
        logging.error("RFSoC firmware not ready — aborting")
        mep.disconnect()
        exit(1)

    # === Run === #
    freqs_hz = get_frequency_list(args.freq_start, args.freq_end, args.step)
    is_sweep = not math.isnan(args.freq_end)

    try:
        if is_sweep:
            mep.run_sweep(freqs_hz, dwell_s=args.dwell, restart_interval=args.restart_interval)
        else:
            mep.run_single(freqs_hz[0])
    finally:
        logging.info("Stopping recorder")
        mep.stop_recorder()
        mep.disconnect()
