#!/bin/bash

# === NOTES === #
# A script to start other scripts needed to collect data for the VLA experiment, each in separate screen sessions:
#
#    1) Starts the RFSoC with ADC capture held in reset: start_rfsoc_rx.bash
#    2) Starts the Tuning, Sweeping, and Recording activities: start_mep_rx.py
#    3) Starts the Jetson power/temp monitorGUI: "sudo python /usr/share/jetsonpowergui/__main__.py"
#    4) (Removed) Starts GNURadio: "gnuradio-companion"
#    5) Watches the Ringbuffer Directory for DigitalRF file changes: "drf watch /data/ringbuffer"
#
#    Re-running this script will kill previously started screen sessions
#        Can force with killall screen

cat << 'EOF'
#
#    # ==== TIME SYNCING ==== #
#    # When powered on without internet, the MEP thinks it is 1970. You must sync the time!
#    1) Important, the time sync requires a One-Time ssh key setup for each laptop/pc <--> MEP pair:
#        1) ON YOUR LAPTOP run ssh-copy-id mep@<<the mep's IP address>>, if that works, done. If not,
#        2) ON YOUR LAPTOP run ssh-keygen -t rsa, then
#        3) ON YOUR LAPTOP re-run ssh-copy-id mep@<<the mep's IP address>>
#    2) ON YOUR LAPTOP,  run /opt/mep-examples/scripts/system_time_sync.bash 192.168.33.1 (needs Linux, Mac, or WSL on Windows)
#    
#    # ==== INSTRUCTIONS ==== #
#    ON THE MEP, open *THIS* script and modify configuation, then run: /opt/mep-examples/experiments/run_sweep.bash
#
#    # ==== TIPS ==== #
#    If you are in the MEP's GUI DESKTOP environment and want to open all existing screen sessions in separate terminal windows, run:
#      screen -ls | awk '/Detached/ {print $1}' | while read session; do gnome-terminal -- bash -c "screen -r $session; exec bash"; done
#
#    If you want to kill all screen seessions, run (this doesn't cleanly stop any scripts, it just kills the sessions):
#      killall screen
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

# --------------- HELPER FUNCTIONS --------------- #
# Function to start a named screen session running an interactive login shell
start_screen_session() {
    # Setup variables
    local SESSION_NAME="$1"
    local COMMENT="${2:-}"

    # Report
    echo "... $SESSION_NAME"
    echo "... ... $COMMENT"
      
    # Check if the session exists before trying to kill it
    if screen -list | grep -q "\.${SESSION_NAME}[[:space:]]"; then
        echo "... ... Existing session found: killing and restarting"
        screen -S "$SESSION_NAME" -X quit
    fi

    # Start a new named screen session in detached mode (-dmS), using a login shell (-l)
    screen -dmS "$SESSION_NAME" bash -l

    # Print how to attach to it
    echo "... ... Join session with: screen -xS $SESSION_NAME"
}

# Function to send (stuff) a command into a running screen session
send_command_to_session() {
    local SESSION_NAME="$1"
    local CMD="$2"
    screen -S "$SESSION_NAME" -X stuff "$CMD"$'\n'
}

# --------------- SCREEN SESSIONS --------------- #
echo "Starting Screen Sessions"

# ===== SCREEN SESSION: rfsoc_rx ===== #
COMMENT1="Starting the RFSoC with ADC capture held in reset"
CMD1="/opt/mep-examples/scripts/start_rfsoc_rx.bash -c $CHANNEL -r"
start_screen_session "rfsoc_rx" "$COMMENT1"
send_command_to_session "rfsoc_rx" "$CMD1"

# ===== SCREEN SESSION: mep_rx ===== #
COMMENT2="Starting the Tuning, Sweeping, and Recording activities"
CMD2="/opt/mep-examples/scripts/start_mep_rx.py -f1 $FREQ_START -f2 $FREQ_END -s $STEP -d $DWELL -t $TUNER --adc_if $ADC_IF --restart_interval $REC_RESTART_INTERVAL"
start_screen_session "mep_rx" "$COMMENT2"
send_command_to_session "mep_rx" "$CMD2"

# ===== SCREEN SESSION: Jetson Power GUI ===== #
COMMENT3="Starting the Jetson Power GUI for monitoring temperatures and voltages" #Works with X11 forwarding over SSH
CMD3="sudo python /usr/share/jetsonpowergui/__main__.py"
start_screen_session "jetsonpowergui" "$COMMENT3"
send_command_to_session "jetsonpowergui" "$CMD3"

# ===== SCREEN SESSION: gnuradio ===== #
# COMMENT4="Starting GNURadio"  # Uncomment if enabling
# CMD4="source /opt/radioconda/etc/profile.d/conda.sh && conda activate base && /opt/mep-examples/scripts/gnuradio-companion"
# start_screen_session "gnuradio" "$COMMENT4"
# send_command_to_session "gnuradio" "$CMD4"

# ===== SCREEN SESSION: drf_watch ===== #
COMMENT5="Watching the Ringbuffer Directory for DigitalRF file changes"
start_screen_session "drf_watch" "$COMMENT5"
send_command_to_session "drf_watch" "source /opt/radioconda/etc/profile.d/conda.sh"
send_command_to_session "drf_watch" "conda activate base"
send_command_to_session "drf_watch" "drf watch /data/ringbuffer"

