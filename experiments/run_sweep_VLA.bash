#!/bin/bash

# === NOTES === #
# A script to start other scripts needed to collect data for the VLA experiment, each in separate screen sessions
#
#    Sets the Jetson time to the remote computer's time (Or else jetson time is 1970 which causes problems) [jetson_time]
#    Starts the RF SOC configuration: "./start_rfsoc_rx.bash -c A B -r" [rfsoc_rx]
#    Starts the Tuner Sweep: "python3 start_mep_rx.py -f1 7125 -s 10 -d 6000 -t VALON" [mep_rx]
#    Starts the DigitalRF recording: "python3 start_rec.py -c A -r 1" [recorder_digitalrf]
#    (disabled) Starts gnuradio: "gnuradio-companion" [gnuradio]
#    Starts the jetson power/temp monitor: "sudo python /usr/share/jetsonpowergui/__main__.py" [jetsonpowergui]
#
#    Re-running this script will kill previously started screen sessions
#        Can force with killall screen
#
#    Important, the time sync requires a One-Time ssh key setup for each laptop/pc <--> MEP pair:
#        1) Try  ssh-copy-id mep@<<the mep's IP address>>, if that works, done. If not, 
#        2) Run this on the remote laptop/pc: ssh-keygen -t rsa, then
#        3) re-run ssh-copy-id mep@<<the mep's IP address>>

# ===== COMMON ===== #
TUNER="VALON" #VALON, LMX2820, or TEST
WORKDIR="/opt/mep-examples/scripts"

# ===== REPORT ===== #
mkdir -p /tmp/mep_screens
echo "Starting Screen Sessions"

# ===== SCREEN SESSION: Jetson Time ===== #
SESSION0="jetson_time"
CMD0="./system_time_sync.bash"

screen -S $SESSION0 -X quit 2>/dev/null
cat > /tmp/mep_screens/$SESSION0.sh <<EOF
#!/usr/bin/env bash -l
cd "$WORKDIR"
echo "Running: $CMD0"
$CMD1
exec bash
EOF

chmod +x /tmp/mep_screens/$SESSION0.sh
screen -dmS $SESSION0 bash /tmp/mep_screens/$SESSION0.sh
echo "... screen -xS $SESSION0"

# ===== SCREEN SESSION: rfsoc_rx ===== #
SESSION1="rfsoc_rx"
CMD1="./start_rfsoc_rx.bash -c A B -r"

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
CMD2="./start_mep_rx.py -f1 7000 -f2 8500 -s 10 -d 10 -t $TUNER"

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

# ===== SCREEN SESSION: recorder_digitalrf ===== #
SESSION3="recorder_digitalrf"
CMD3="./start_rec.py -c A -r 10"

screen -S $SESSION3 -X quit 2>/dev/null
cat > /tmp/mep_screens/$SESSION3.sh <<EOF
#!/usr/bin/env bash -l
cd "$WORKDIR"
echo "Running: $CMD3"
$CMD3
exec bash
EOF

chmod +x /tmp/mep_screens/$SESSION3.sh
screen -dmS $SESSION3 bash /tmp/mep_screens/$SESSION3.sh
echo "... screen -xS $SESSION3"

# ===== SCREEN SESSION: gnuradio ===== #
#SESSION4="gnuradio"
#CMD4="gnuradio-companion"
#
#screen -S $SESSION4 -X quit 2>/dev/null
#cat > /tmp/mep_screens/$SESSION4.sh <<EOF
##!/usr/bin/env bash -l
#source /opt/radioconda/etc/profile.d/conda.sh
#conda activate base
#cd "$WORKDIR"
#echo "Running: $CMD4"
#$CMD4
#exec bash
#EOF
#
#chmod +x /tmp/mep_screens/$SESSION4.sh
#screen -dmS $SESSION4 bash /tmp/mep_screens/$SESSION4.sh
#echo "... screen -xS $SESSION4"

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

