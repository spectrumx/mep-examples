#!/bin/bash

# === NOTES === #
# A script to start other scripts needed to collect data for the VLA experiment, each in separate screen sessions
#
#    Starts the RF SOC configuration: "./start_rfsoc_rx.bash -c A B -r" [rfsoc_rx]
#    Starts the Tuner Sweep: "python3 start_mep_rx.py -f1 7125 -s 10 -d 6000 -t VALON" [mep_rx]
#    Starts the DigitalRF recording: "python3 start_rec.py -c A -r 1" [recorder_digitalrf]
#    (disabled) Starts gnuradio: "gnuradio-companion" [gnuradio]
#    Starts the jetson power/temp monitor: "sudo python /usr/share/jetsonpowergui/__main__.py" [jetsonpowergui]
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
#    2) ON YOUR LAPTOP run /opt/mep-examples/scripts/system_time_sync.bash 192.168.33.1 (needs Linux, Mac, or WSL on Windows)
#    3) Run *this* script ON THE MEP /opt/mep-examples/experiments/run_sweep_VLA_<<TUNER>>.bash: Either Valon or LMX version
EOF

# ===== COMMON ===== #
TUNER="LMX2820"           # Options: VALON, LMX2820, TEST, None
ADC_IF=1090               # Fixed IF of the RFSoC in MHz, only required when a Tuner is being used.
FREQ_START=7000           # MHz for IF sweep, or RF sweep start if tuner present
FREQ_END=8500             # MHz for IF sweep, or RF sweep end if tuner present
STEP=10                   # MHz
DWELL=5                   # Seconds
CHANNEL="A"               # Channel String, "A" or "A B"
WORKDIR="/opt/mep-examples/scripts"

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

if [[ "$TUNER" == "None" ]]; then
    CMD2="./start_mep_rx.py -f1 $FREQ_START -f2 $FREQ_END -s $STEP -d $DWELL --restart_interval 99999"
else
    CMD2="./start_mep_rx.py -f1 $FREQ_START -f2 $FREQ_END -s $STEP -d $DWELL -t $TUNER --adc_if $ADC_IF --restart_interval 99999"
fi

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

# Dont need this anymore, it is handled in start_mep_rx now.
# ===== SCREEN SESSION: recorder_digitalrf ===== #
#SESSION3="recorder_digitalrf"
#CMD3="./start_rec.py -c A -r $STEP"
#
#screen -S $SESSION3 -X quit 2>/dev/null
#cat > /tmp/mep_screens/$SESSION3.sh <<EOF
##!/usr/bin/env bash -l
#cd "$WORKDIR"
#echo "Running: $CMD3"
#$CMD3
#exec bash
#EOF

#chmod +x /tmp/mep_screens/$SESSION3.sh
#screen -dmS $SESSION3 bash /tmp/mep_screens/$SESSION3.sh
#echo "... screen -xS $SESSION3"

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

