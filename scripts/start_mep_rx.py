#!/opt/radiohound/python313/bin/python
"""
start_mep_rx.py

Automate tuning and starting the RFSOC I/Q stream.
Supports frequency sweep capture.

- If a tuner is specified: performs tuner-controlled RF sweep.
- If tuner is None: performs IF sweep only using RFSoC local NCO.

Author: nicholas.rainville@colorado.edu, updated by john.marino@colorado.edu
"""

# ===== IMPORTS ===== #
import argparse
import time
import logging
import os
import math
from datetime import datetime
import src.mep_rfsoc as mep_rfsoc

# ===== COMMON CONFIG ===== #
LOG_DIR = os.path.join(os.path.expanduser("~"), "log", "spectrumx")
GREEN = "\033[92m"
RESET = "\033[0m"

# ===== HELPER FUNCTIONS ===== #
def stop_start_recorder(channel="A", rate=10):
    logging.info("Restarting recorder")
    os.system('/opt/mep-examples/scripts/stop_rec.py')
    time.sleep(2)
    os.system(f'/opt/mep-examples/scripts/start_rec.py -c {channel} -r {rate}')
    time.sleep(1)
    os.system(f'/opt/mep-examples/scripts/start_rec.py -c {channel} -r {rate}')
    time.sleep(1)

def force_restart_recorder(last_restart, interval_s, channel, rate):
    if time.time() - last_restart >= interval_s:
        logging.info("Restart interval reached. Restarting recorder.")
        stop_start_recorder(channel, rate)
        return time.time()
    return last_restart

def get_frequency_list(start_mhz, end_mhz, step_mhz):
    start_hz = int(start_mhz * 1e6)
    step_hz = int(step_mhz * 1e6)
    if math.isnan(end_mhz):
        return [start_hz]
    end_hz = int(end_mhz * 1e6)
    return range(start_hz, end_hz, step_hz)

def wait_for_rfsoc(rfsoc, timeout_s=10):
    for _ in range(timeout_s):
        tlm = rfsoc.get_tlm()
        if tlm is not None:
            return True
        time.sleep(1)
    return False

def tlm_to_str(tlm):
    if tlm is None:
        return ""
    return (
        f"RX State: {tlm['state']} "
        f"f_c (tagged): {float(tlm['f_c_hz'])/1e6:.2f} MHz "
        f"f_if (ADC): {float(tlm['f_if_hz'])/1e6:.2f} MHz "
        f"f_s: {float(tlm['f_s'])/1e6:.2f} MHz "
        f"PPS Count: {tlm['pps_count']} "
        f"Channel(s): {tlm['channels']}"
    )

def wait_dwell_period(rfsoc, dwell_s):
    start = time.time()
    while (time.time() - start) < dwell_s:
        tlm = rfsoc.get_tlm()
        logging.debug(tlm_to_str(tlm))
        time.sleep(1)

def get_tuner_object(tuner_type, adc_if):
    tuner_object = None

    if tuner_type is not None:
        if adc_if is None:
            logging.error("You must specify --adc_if when using a tuner.")
            exit(1)

        logging.info(f"Tuner selected: {tuner_type} with ADC IF = {adc_if:.2f} MHz")

        if tuner_type == "TEST":
            import src.mep_tuner_test as tuner_mod
            tuner_object = tuner_mod.MEPTunerTest(adc_if)
            
        elif tuner_type == "VALON":
            import src.mep_tuner_valon2 as tuner_mod
            tuner_object = tuner_mod.MEPTunerValon(adc_if)
            
        elif tuner_type == "LMX2820":
            import src.mep_tuner_lmx2820 as tuner_mod
            tuner_object = tuner_mod.MEPTunerLMX2820(adc_if)
            
        else:
            raise ValueError(f"Unknown tuner type: {tuner_type}")

    return tuner_object

        
def run_sweep(freqs_hz, rfsoc, args, tuner=None):
    # Determine Sweep Mode: Sweep IF without a tuner, or sweep RF with a tuner, must specify fixed IF
    mode = "IF" if tuner is None else "RF"
    logging.info(f"{mode} sweep starting...")

    # Start Recorder
    last_restart_time = time.time()
    stop_start_recorder(channel=args.channel)

    # Loop through Frequencies
    for f_hz in freqs_hz:
    	# Convert to Hz
        f_mhz = f_hz / 1e6
        
        # Reset RFSoC
        rfsoc.reset()

	# Set RF or IF frequency and metadata
        if mode == "IF":
            logging.info(f"Setting RFSoC NCO to {GREEN}{f_mhz:.2f} MHz{RESET}")
            rfsoc.set_freq_IF(f_mhz)
            time.sleep(0.1) # Wait for lock
            rfsoc.set_freq_metadata(f_hz / 1e3)  # Tag in kHz
        elif mode == "RF":
            logging.info(f"Tuning to {GREEN}{f_mhz:.2f} MHz{RESET}")
            tuner.set_freq(f_mhz)
            time.sleep(0.1) # Wait for lock
            rfsoc.set_freq_metadata(f_hz / 1e3)

        # Capture
        rfsoc.capture_next_pps()
        tlm = rfsoc.get_tlm()
        tlm = rfsoc.get_tlm()
        if not tlm or tlm.get('state') != 'active':
            logging.error("RFSoC capture failed or telemetry missing.")
            continue
        logging.info(f"RFSoC state: {tlm['state']}")
        wait_dwell_period(rfsoc, args.dwell)
        
        # Force recorder restart every args.restart_interval seconds
        last_restart_time = force_restart_recorder(
            last_restart_time,
            args.restart_interval,
            args.channel,
            rate=10
        )

# ---------------------- Script Entry ----------------------

if __name__ == "__main__":
    # === Parse Arguments === #
    parser = argparse.ArgumentParser(description='RFSoC capture with optional tuner or IF sweep')
    parser.add_argument('--freq_start', '-f1', type=float, default=7000, help='Start frequency in MHz')
    parser.add_argument('--freq_end', '-f2', type=float, default=float('nan'), help='End frequency in MHz')
    parser.add_argument('--channel', '-c', type=str, default="A", help='Channel: A or A B')
    parser.add_argument('--step', '-s', type=float, default=10, help='Step size in MHz')
    parser.add_argument('--dwell', '-d', type=float, default=60, help='Dwell time in seconds')
    parser.add_argument('--tuner', '-t', type=lambda x: x.upper() if x.lower() != "none" else None,
                        choices=["LMX2820", "VALON", "TEST", "None"], default=None,
                        help='Tuner type [LMX2820, VALON, TEST, None]')
    parser.add_argument('--adc_if', type=float, help='ADC IF in MHz (required if tuner is used)')
    parser.add_argument('--log-level', '-l', type=str, default='INFO',
                        choices=['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'], help='Logging level')
    parser.add_argument('--skip_ntp', action='store_true', help='Skip NTP sync')
    parser.add_argument('--restart_interval', type=int, default=300, help='Recorder restart interval in seconds')
    args = parser.parse_args()

    # === Setup Logging === #
    os.makedirs(LOG_DIR, exist_ok=True)
    timestamp = datetime.now().isoformat().replace(':', '-').replace('.', '-')
    log_path = os.path.join(LOG_DIR, f"capture_sweep_{timestamp}.log")
    logging.basicConfig(level=args.log_level, format='%(asctime)s - %(levelname)s - %(message)s',
                        filename=log_path, datefmt='%Y-%m-%dT%H:%M:%S')
    logging.getLogger().addHandler(logging.StreamHandler())

    # === Update NTP if Needed === #
    if not args.skip_ntp:
        logging.info("Updating NTP on RFSoC")
        os.system(os.path.join(os.getcwd(), "rfsoc_update_ntp.bash"))

    # === Connect to RFSoC === #
    rfsoc = mep_rfsoc.MEPRFSoC()
    if not wait_for_rfsoc(rfsoc):
        logging.error("RFSoC not responding.")
        exit(1)

    # === Tuner Setup === #
    tuner = get_tuner_object(args.tuner, args.adc_if)

    # === Frequency Sweep === #
    freqs_hz = get_frequency_list(args.freq_start, args.freq_end, args.step)
    run_sweep(freqs_hz, rfsoc, args, tuner=tuner)

    # === Shutdown === #
    logging.info("Stopping recorder")
    os.system('/opt/mep-examples/scripts/stop_rec.py')

