#!/opt/radiohound/python313/bin/python
"""
start_mep_rx.py

Automate tuning and starting the RFSOC I/Q stream.
Supports frequency sweep capture. Control the tuner
through USB/SPI and RFSoC through ZeroMQ.

Author: nicholas.rainville@colorado.edu
"""

import argparse
import time
import logging
import src.mep_tuner_test as mep_tuner_test
import src.mep_rfsoc as mep_rfsoc
import src.mep_tuner_valon as mep_tuner_valon
import os
import math
from datetime import datetime

LOG_DIR = os.path.join(os.path.expanduser("~"),"log","spectrumx")
ADC_IF = 1090      # ADC intermediate frequency (MHz)

GREEN = "\033[92m"
BLUE = "\033[94m"
RED = "\033[91m"
RESET = "\033[0m"

def main(args):
    """
    Main function for the frequency sweep control.
    Args:
        args (argparse.Namespace): Command-line arguments. Expected to contain:
    """

    # Configure logging
    os.makedirs(LOG_DIR, exist_ok=True)
    time_str = datetime.now().isoformat().replace(':', '-').replace('.', '-')
    log_filename = f"capture_sweep_{time_str}.log"
    log_filepath = os.path.join(LOG_DIR, log_filename)

    logging.basicConfig(
        level=args.log_level,
        format='%(asctime)s - %(levelname)s - %(message)s',
        filename=log_filepath,
        datefmt='%Y-%m-%dT%H:%M:%S'
    )

    # Add console handler to also log to terminal
    console_handler = logging.StreamHandler()
    console_handler.setLevel(args.log_level)
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s', datefmt='%Y-%m-%dT%H:%M:%S')
    console_handler.setFormatter(formatter)
    logging.getLogger().addHandler(console_handler)

    # Inputs
    f_c_start_hz = int(args.freq_start * 1e6)
    f_step_hz = int(args.step * 1e6)

    # Constants
    f_if_hz = 1090e6 # Hz

    # Connect to tuner
    if (args.tuner == "TEST"):
        tuner = mep_tuner_test.MEPTunerTest(ADC_IF)
    if (args.tuner == "LMX2820"):
        import src.mep_tuner_lmx2820 as mep_tuner_lmx2820
        tuner = mep_tuner_lmx2820.MEPTunerLMX2820(ADC_IF)
    if (args.tuner == "VALON"):
        tuner = mep_tuner_valon.MEPTunerValon(ADC_IF)
    
    # Update NTP
    logging.info("Updating NTP on RFSoC")
    os.system(os.path.join(os.getcwd(), "rfsoc_update_ntp.bash"))

    # Connect to RFSoC ZMQ
    rfsoc = mep_rfsoc.MEPRFSoC()

    # Wait for RFSoC
    rfsoc_timeout_s = 10
    rfsoc_wait_count = 0
    tlm = rfsoc.get_tlm()
    while(tlm is None and rfsoc_wait_count < rfsoc_timeout_s):
        logging.debug("Waiting for RFSoC")
        time.sleep(1)
        tlm = rfsoc.get_tlm()
        rfsoc_wait_count += 1

    if (rfsoc_wait_count >= rfsoc_timeout_s):
        logging.error("Failed to connect to RFSoC")
        return

    # Generate frequency list
    if math.isnan(args.freq_end):
        freqs_hz = [f_c_start_hz]
    else:
        f_c_end_hz = int(args.freq_end * 1e6)
        freqs_hz = range(f_c_start_hz, f_c_end_hz, f_step_hz)

    # Loop over frequency range
    for f_c_hz in freqs_hz:
        logging.info(f"Tuning to {GREEN}{f_c_hz}{RESET}")
        # Place RFSoC Capture in Reset
        rfsoc.reset()

        # Tune to starting frequency
        f_c_mhz = f_c_hz / 1e6
        tuner.set_freq(f_c_mhz)
        time.sleep(.1) # Wait for tuner to lock?

        # Start capture on pps edge
        rfsoc.set_freq_metadata(f_c_hz)
        rfsoc.capture_next_pps()
        tlm = rfsoc.get_tlm()
        if(tlm is not None):
            if(tlm['state']) != 'active':
                logging.error(f"Failed to start RFSoC capture")
                continue
            logging.info(f"RFSoC state {tlm['state']}")

        # Wait for dwell time
        time_loop_start = time.time()
        logging.info(f"Waiting for {args.dwell} s")
        while((time_loop_start - time.time() + args.dwell) > 0):
            tlm = rfsoc.get_tlm()
            logging.debug(f"{tlm_to_str(tlm)} ")
            time.sleep(1)

def tlm_to_str(tlm):
    if (tlm is None):
        return ""
    tlm_str =  f"RX State: {tlm['state']} "
    tlm_str += f"f_c: {float(tlm['f_c_hz'])/1e6} MHz "
    tlm_str += f"f_if: {float(tlm['f_if_hz'])/1e6} MHz "
    tlm_str += f"f_s: {float(tlm['f_s'])/1e6} MHz "
    tlm_str += f"PPS Count: {tlm['pps_count']} "
    tlm_str += f"Channel(s): {tlm['channels']}"
    return tlm_str


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Send command to the RFSoC')
    parser.add_argument('--freq_start', '-f1', type=float, default=7000, help='Center frequency in MHz, if FREQ_END is also set this is the starting frequency (default: 7000 MHz)')
    parser.add_argument('--freq_end', '-f2', type=float, default=float('nan'), help='End frequency in MHz (default: NaN)')
    parser.add_argument('--step', '-s', type=float, default=10, help='Step size in MHz (default: 10 MHz)')
    parser.add_argument('--dwell', '-d', type=float, default=60, help='Dwell time in seconds (default: 60 s)')
    parser.add_argument( '--tuner', '-t', type=lambda x: x.upper(),
                        choices=["LMX2820", "VALON", "TEST"], default="TEST",
                        help='Select tuner [LMX2820, VALON, TEST] (default: TEST)')
    parser.add_argument('--log-level', '-l', type=str, default='INFO', choices=['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'],
                        help='Set logging level (default: INFO)')

    args = parser.parse_args()
    main(args)
