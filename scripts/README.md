## Usage examples for MEP scripts

Capturing an I/Q stream on the MEP requires multiple scripts working simultaneously. An 
example capture of a frequency sweep  would first start with initializing the RFSoC FPGA 
receive bitstream with:

```
./start_rfsoc_rx.bash -c A B -r
```

The -c option sets the active input channels, A and B. The -r option holds the UDP stream 
in reset until the tuner script sets the external tuner frequency. This can be done using
the start_mep_rx.py script from within the base conda environment on the Jetson:

```
conda activate
./start_mep_rx.py -f1 7010 -f2 7050 -s 10 -d 5 -t LMX2820
```

This example will automate the 7.01GHz to 7.05GHz frequency sweep with a step size of 10MHz,
a dwell time of 5 s, and it will use the LMX2820 tuner.

### start_rfsoc_rx.bash

This script configures and initalizes the FPGA bitstream on the RFSoC. Since it 
is a bash wrapper that uses ssh to execute start_capture_rx.py on the RFSoC, any 
flags passed to it will be forwarded to start_capture_rx.py

For example: 

```
./start_rfsoc_rx.bash -h

usage: start_capture_rx.py [-h] [-f FREQ] [-c [{A,B,C,D} ...]] [-r] [-i]
                           [--log-level {DEBUG,INFO,WARNING,ERROR,CRITICAL}]

Tune RFSoC and stream data over QSFP

options:
  -h, --help            show this help message and exit
  -f FREQ, --freq FREQ  Intermediate frequency (MHz.) Also center frequency if external tuner is not used. (default: 1090)
  -c [{A,B,C,D} ...], --channels [{A,B,C,D} ...]
                        Channels to capture (default: A)
  -r, --reset           Hold capture in reset on start (default: False)
  -i, --internal_clock  Disable external clock and use internal VCO (default: False)
  --log-level {DEBUG,INFO,WARNING,ERROR,CRITICAL}, -l {DEBUG,INFO,WARNING,ERROR,CRITICAL}
                        Set logging level (default: INFO) (default: INFO)
```

### start_mep_rx.py

This script automates the tuning and capture process for the I/Q stream from the RFSoC. It 
configures the external tuner to f_lo and the RFSoC to f_if. 

```
usage: start_mep_rx.py [-h] [--freq_start FREQ_START] [--freq_end FREQ_END] [--step STEP] [--dwell DWELL]
                       [--tuner {LMX2820,VALON,TEST}] [--log-level {DEBUG,INFO,WARNING,ERROR,CRITICAL}]

Send command to the RFSoC

options:
  -h, --help            show this help message and exit
  --freq_start FREQ_START, -f1 FREQ_START
                        Center frequency in MHz, if FREQ_END is also set this is the starting frequency
                        (default: 7000 MHz)
  --freq_end FREQ_END, -f2 FREQ_END
                        End frequency in MHz (default: NaN)
  --step STEP, -s STEP  Step size in MHz (default: 10 MHz)
  --dwell DWELL, -d DWELL
                        Dwell time in seconds (default: 60 s)
  --tuner {LMX2820,VALON,TEST}, -t {LMX2820,VALON,TEST}
                        Select tuner [LMX2820, VALON, TEST] (default: TEST)
  --log-level {DEBUG,INFO,WARNING,ERROR,CRITICAL}, -l {DEBUG,INFO,WARNING,ERROR,CRITICAL}
                        Set logging level (default: INFO)
```