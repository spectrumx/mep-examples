#!/bin/bash

# === NOTES === #
# A script to start other scripts needed to collect data for the VLA experiment, each in separate screen sessions:
#
#    1) Starts the RF SOC configuration: start_rfsoc_rx.bash
#    2) Starts the Sweep and Recorder: start_mep_rx.py
#    3) Starts the Jetson power/temp monitorGUI: "sudo python /usr/share/jetsonpowergui/__main__.py"
#
#    Re-running this script will kill previously started screen sessions
#        Can force with killall screen

cat << 'EOF'
#
#    1) Important, the time sync requires a One-Time ssh key setup for each laptop/pc <--> MEP pair:
#        1) ON YOUR LAPTOP run ssh-copy-id mep@<<the mep's IP address>>, if that works, done. If not,
#        2) ON YOUR LAPTOP run ssh-keygen -t rsa, then
#        3) ON YOUR LAPTOP re-run ssh-copy-id mep@<<the mep's IP address>>
#
#    2) ON YOUR LAPTOP,  run /opt/mep-examples/scripts/system_time_sync.bash 192.168.33.1 (needs Linux, Mac, or WSL on Windows)
#    3) ON THE MEP, open *THIS* script and modify configuation, then run: /opt/mep-examples/experiments/run_sweep.bash
#
EOF
# --------------- USER SETTINGS --------------- #
# ===== COMMON ===== #
TUNER="LMX2820"           # Options: VALON, LMX2820, TEST, None
ADC_IF=1090               # Fixed IF of the RFSoC in MHz, only required when a Tuner is being used, ignored otherwise
FREQ_START=7000           # MHz for IF sweep, or RF sweep start if tuner present
FREQ_END=8500             # MHz for IF sweep, or RF sweep end if tuner present
STEP=10                   # Sweep frequency step size in MHz
DWELL=10                  # Dwell time in seconds: Time to remain at each frequency step
CHANNEL="A"               # Channel String, "A" or "A B"
REC_RESTART_INTERVAL=300  # Force the DigitalRF recorder to restart every N seconds
WORKDIR="/opt/mep-examples/scripts" # Place where the other scripts are located

# --------------- SCREEN SESSIONS --------------- #
# ===== REPORT ===== #
mkdir -p /tmp/mep_screens
echo "Starting Screen Sessions"

# ===== SCREEN SESSION: rfsoc_rx ===== #
SESSION1="rfsoc_rx"
CMD1="./start_rfsoc_rx.bash -c $CHANNEL -r"

screen -S $SESSION1 -X quit 2>/dev/null
cat > /tmp/mep_screens/$SESSION1.sh <<EOF
#!/usr/bin/env bash -l
cd "$WORKDIR"
echo "Running: $CMD1"
$CMD1
exec bash
EOF

chmod +x /tmp/mep_screens/$SESSION1.sh
screen -dmS $SESSION1 bash /tmp/mep_screens/$SESSION1.sh
echo "... screen -xS $SESSION1"

# ===== SCREEN SESSION: mep_rx ===== #
SESSION2="mep_rx"
CMD2="./start_mep_rx.py -f1 $FREQ_START -f2 $FREQ_END -s $STEP -d $DWELL -t $TUNER --adc_if $ADC_IF --restart_interval $REC_RESTART_INTERVAL"

screen -S $SESSION2 -X quit 2>/dev/null
cat > /tmp/mep_screens/$SESSION2.sh <<EOF
#!/usr/bin/env bash -l
cd "$WORKDIR"
echo "Running: $CMD2"
$CMD2
exec bash
EOF

chmod +x /tmp/mep_screens/$SESSION2.sh
screen -dmS $SESSION2 bash /tmp/mep_screens/$SESSION2.sh
echo "... screen -xS $SESSION2"

# ===== SCREEN SESSION: Jetson Power GUI ===== #
SESSION5="jetsonpowergui"
CMD5="sudo python /usr/share/jetsonpowergui/__main__.py"

screen -S $SESSION5 -X quit 2>/dev/null
cat > /tmp/mep_screens/$SESSION5.sh <<EOF
#!/usr/bin/env bash -l
echo "Running: $CMD5"
$CMD5
exec bash
EOF

chmod +x /tmp/mep_screens/$SESSION5.sh
screen -dmS $SESSION5 bash /tmp/mep_screens/$SESSION5.sh
echo "... screen -xS $SESSION5"

