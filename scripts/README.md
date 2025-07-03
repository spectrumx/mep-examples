## Usage examples for MEP scripts

Capturing an I/Q stream on the MEP requires multiple scripts working simultaneously. An 
example capture of a frequency sweep  would first start with initializing the RFSoC FPGA 
receive bitstream with:

```
./start_rfsoc_rx.bash -c A B -r
```

The -c option sets the active input channels, A and B. The -r option holds the UDP stream 
in reset until the tuner script sets the external tuner frequency. This can be done using
the start_mep_rx.py script:

```
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
                        Start frequency in MHz (default: 7000 MHz)
  --freq_end FREQ_END, -f2 FREQ_END
                        Start frequency in MHz (default: 7100 MHz)
  --step STEP, -s STEP  Step size in MHz (default: 10 MHz)
  --dwell DWELL, -d DWELL
                        Dwell time in seconds (default: 60 s)
  --tuner {LMX2820,VALON,TEST}, -t {LMX2820,VALON,TEST}
                        Select tuner [LMX2820, VALON, TEST] (default: TEST)
  --log-level {DEBUG,INFO,WARNING,ERROR,CRITICAL}, -l {DEBUG,INFO,WARNING,ERROR,CRITICAL}
                        Set logging level (default: INFO)
```

### upload_to_sds.py

This script uploads directory of files to the SDS and create capture from these files.

```
usage: upload_to_sds [-h] [-n] [--dotenv DOTENV] [--channel CHANNEL]
                     [--create-capture]
                     data_dir reference_name

positional arguments:
  data_dir           Path to data directory to upload
  reference_name     Reference name (virtual directory) to upload to. For example: digitalrf/your-data-folder-name

options:
  -h, --help         show this help message and exit
  -n, --dry-run      
  --dotenv DOTENV    Path to .env file containing SDS_SECRET_TOKEN
  --channel CHANNEL  Channel to use for creating capture after upload
  --create-capture   Create a capture after uploading files (--channel required if using --create-capture)

Example: python upload_to_sds.py <local_dir> <reference dir> --create-capture
--channel <channel name> This uploads data and creates a capture.
```

### upload_multichannel_to_sds.py

```
Use this script to upload captures if you have SDK v0.1.11 and if your data directory contains data from multiple channels. You can specify the list of channel names for which you would like to create capture using the --channels option (eg: --channels ch1 ch2 ch3).

usage: upload_multichannel_to_sds [-h] [-n] [--dotenv DOTENV] --channels
                                  CHANNELS [CHANNELS ...]
                                  data_dir reference_name

Upload a multi-channel DigitalRF capture to the SDS

positional arguments:
  data_dir              Path to data directory to upload
  reference_name        Reference name (virtual directory) to upload to

options:
  -h, --help            show this help message and exit
  -n, --dry-run
  --dotenv DOTENV       Path to .env file containing SDS_SECRET_TOKEN
  --channels CHANNELS [CHANNELS ...]
                        List of channels to upload (space separated)

Example: python upload_multichannel_to_sds.py <local_dir> <reference dir>
--channels ch1 ch2 ch3 This uploads a multi-channel capture.
```
