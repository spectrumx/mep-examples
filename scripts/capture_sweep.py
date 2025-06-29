#!/usr/bin/env python3

"""
capture_sweep.py

Automate a frequency sweep capture. Control the tuner through USB/SPI
and RFSoC through ZeroMQ.

Author: nicholas.rainville@colorado.edu
"""

import argparse
import time
import logging
import mep_tuner_test
import mep_rfsoc
import os

LOG_DIR = os.path.join(os.path.expanduser("~"),"log","spectrumx")

GREEN = "\033[92m"
BLUE = "\033[94m"
RED = "\033[91m"
RESET = "\033[0m"

def main(args):
    """
    Main function for the frequency sweep control. 
    Args:
        args (argparse.Namespace): Command-line arguments. Expected to contain:
            - command (list): A list of command strings to send.

    Raises:
        KeyboardInterrupt: If the user interrupts the program (Ctrl+C).
    """

    # Configure logging
    os.makedirs(LOG_DIR, exist_ok=True)
    time_str = time.strftime("%Y%m%d_%H%M%S", time.localtime())
    log_filename = f"capture_sweep_{time_str}.log"
    log_filepath = os.path.join(LOG_DIR, log_filename)

    logging.basicConfig(
        level=logging.DEBUG,
        format='%(asctime)s - %(levelname)s - %(message)s',
        filename=log_filepath
    )

    # Add console handler to also log to terminal
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    console_handler.setFormatter(formatter)
    logging.getLogger().addHandler(console_handler)

    # Inputs 
    f_c_start_hz = int(args.freq_start * 1e6)
    f_c_end_hz = int(args.freq_end * 1e6)
    f_step_hz = int(args.step * 1e6)
    dwell_s = args.dwell

    # Constants
    f_if_hz = 1090e6 # Hz

    # Connect to tuner
    if (args.tuner == "TEST"):
        tuner = mep_tuner_test.MEPTunerTest()
    if (args.tuner == "LMX2820"):
        logging.error("LMX2820 tuner not yet implemented")
        return
    if (args.tuner == "VALON"):
        logging.error("Valon tuner not yet implemented")
        return

    # Connect to RFSoC ZMQ
    rfsoc = mep_rfsoc.MEPRFSoC()
    
    for f_c_hz in range(f_c_start_hz, f_c_end_hz, f_step_hz):
        logging.info(f"Tuning to {GREEN}{f_c_hz}{RESET}")
        # Place RFSoC Capture in Reset
        rfsoc.reset()

        # Tune to starting frequency
        f_lo_hz = f_c_hz - f_if_hz
        f_lo_mhz = f_lo_hz / 1e6
        tuner.set_freq(f_lo_mhz)
        time.sleep(.1) # Wait for tuner to lock?

        # Start capture on pps edge
        rfsoc.set_freq_metadata(f_c_hz)
        rfsoc.capture_next_pps()
        time.sleep(args.dwell)
    
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Send command to the RFSoC')
    parser.add_argument('--freq_start', '-f1', type=float, default=7000, help='Start frequency in MHz (default: 7000 MHz)')
    parser.add_argument('--freq_end', '-f2', type=float, default=7100, help='Start frequency in MHz (default: 7100 MHz)')
    parser.add_argument('--step', '-s', type=float, default=10, help='Step size in MHz (default: 10 MHz)')
    parser.add_argument('--dwell', '-d', type=float, default=60, help='Dwell time in seconds (default: 60 s)')
    parser.add_argument( '--tuner', '-t', type=lambda x: x.upper(),
                        choices=["LMX2820", "VALON", "TEST"], default="TEST",
                        help='Select tuner [LMX2820, VALON, TEST] (default: TEST)'
)
    args = parser.parse_args()
    main(args)