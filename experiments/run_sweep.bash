#!/bin/bash

cat << 'EOF'
╔═══════════════════════════════════════════════════════════════════════════╗
║ ░░░░░░░░░░░░░░░░░░▒▒▓▓████ MEP DATA COLLECTION ████▓▓▒▒░░░░░░░░░░░░░░░░░░ ║
╠═══════════════════════════════════════════════════════════════════════════╣
║                              ▶ DESCRIPTION ◀                              ║
╠═══════════════════════════════════════════════════════════════════════════╣
║ A unified script to sweep the MEP across frequencies and record data:     ║
║ Each opens in a separate screen session:                                  ║
║   • RFSoC ADC Capture :     /opt/mep-examples/script/start_rfsoc_rx.bash  ║
║   • Sweep/Tune/Record :     /opt/mep-examples/script/start_mep_rx.py      ║
║   • Jetson Monitor GUI:     /usr/share/jetsonpowergui/__main__.py"        ║
║   • DigitalRF Watcher :     drf watch /data/ringbuffer                    ║
║   • (Removed) GNURadio:     gnuradio-companion                            ║
╠═══════════════════════════════════════════════════════════════════════════╣
║                         ▶ TIME SYNCING REQUIRED ◀                         ║
╠═══════════════════════════════════════════════════════════════════════════╣
║    MEP boots to 1970 without internet. Time must be synced manually!      ║
║                                                                           ║
║ 1) On YOUR LAPTOP, run:                                                   ║
║    1A) $ ssh-copy-id mep@192.168.33.1 | If that works, skip to Step 2:    ║
║    1B) $ ssh-keygen -t rsa            |                                   ║
║    1C) $ ssh-copy-id mep@192.168.33.1 |                                   ║
║                                                                           ║
║ 2) ON YOUR LAPTOP, run: (Requires Linux, macOS, or WSL on Windows)        ║
║     $ /opt/mep-examples/scripts/system_time_sync.bash 192.168.33.1        ║
╠═══════════════════════════════════════════════════════════════════════════╣
║                             ▶ INSTRUCTIONS ◀                              ║
╠═══════════════════════════════════════════════════════════════════════════╣
║ ON THE MEP, open *THIS* script and modify the user settings. The run it:  ║
║     $ /opt/mep-examples/experiments/run_sweep.bash                        ║
╠═══════════════════════════════════════════════════════════════════════════╣
║                                 ▶ TIPS ◀                                  ║
╠═══════════════════════════════════════════════════════════════════════════╣
║ • From the MEP's GUI DESKTOP, to open all screen sessions in new windows: ║
║     $ screen -ls | awk '/Detached/ {print $1}' | while read session; do gnome-terminal -- bash -c "screen -r $session; exec bash"; done
║                                                                           ║
║ • To kill all screen sessions (forceful — does NOT stop scripts):         ║
║     $ killall screen                                                      ║
╚═══════════════════════════════════════════════════════════════════════════╝

EOF

# --------------- USER SETTINGS --------------- #
TUNER="None"          # Options: VALON, LMX2820, TEST, None
ADC_IF=1090               # Fixed IF of the RFSoC in MHz, only required when a Tuner is being used, ignored otherwise
FREQ_START=7000           # MHz for IF sweep, or RF sweep start if tuner present
FREQ_END=8500             # MHz for IF sweep, or RF sweep end if tuner present
STEP=10                   # Sweep frequency step size in MHz
DWELL=5                   # Dwell time in seconds: Time to remain at each frequency step
CHANNEL="A"               # Channel String, "A" or "A B"
REC_RESTART_INTERVAL=9999999  # Force the DigitalRF recorder to restart every N seconds


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

print_user_settings() {
cat << EOF
╔═══════════════════════════════════════════════════════════════════════════╗
║                            ▶ USER SETTINGS ◀                              ║
╠═══════════════════════════════════════════════════════════════════════════╣
  TUNER:                ${TUNER:-<unset>}         # Options: VALON, LMX2820, TEST, None     
  ADC_IF:               ${ADC_IF:-<unset>} MHz        # RFSoC fixed IF (if tuner used)    
  FREQ_START:           ${FREQ_START:-<unset>} MHz        # Start of sweep                    
  FREQ_END:             ${FREQ_END:-<unset>} MHz        # End of sweep                      
  STEP:                 ${STEP:-<unset>} MHz          # Frequency step size               
  DWELL:                ${DWELL:-<unset>} sec          # Time per frequency step           
  CHANNEL:              ${CHANNEL:-<unset>}               # Channels to record (e.g., A B)    
  REC_RESTART_INTERVAL: ${REC_RESTART_INTERVAL:-<unset>} sec         # Recorder restart interval  
╚═══════════════════════════════════════════════════════════════════════════╝
EOF
}


# --------------- SCREEN SESSIONS --------------- #
print_user_settings
echo "Starting Screen Sessions"

# ===== SCREEN SESSION: rfsoc_rx ===== #
SESSION_NAME_1="rfsoc_rx"
COMMENT1="Starting the RFSoC with ADC capture held in reset"
CMD1="cd /opt/mep-examples/scripts && ./start_rfsoc_rx.bash -c $CHANNEL -r"
start_screen_session "$SESSION_NAME_1" "$COMMENT1"
send_command_to_session "$SESSION_NAME_1" "$CMD1"

# ===== SCREEN SESSION: mep_rx ===== #
SESSION_NAME_2="mep_rx"
COMMENT2="Starting the Tuning, Sweeping, and Recording activities"
CMD2="cd /opt/mep-examples/scripts && ./start_mep_rx.py -f1 $FREQ_START -f2 $FREQ_END -s $STEP -d $DWELL -t $TUNER --adc_if $ADC_IF --restart_interval $REC_RESTART_INTERVAL"
start_screen_session "$SESSION_NAME_2" "$COMMENT2"
send_command_to_session "$SESSION_NAME_2" "$CMD2"

# ===== SCREEN SESSION: Jetson Power GUI ===== #
SESSION_NAME_3="jetsonpowergui"
COMMENT3="Starting the Jetson Power GUI for monitoring temperatures and voltages" #Works with X11 forwarding over SSH
CMD3="sudo python /usr/share/jetsonpowergui/__main__.py"
start_screen_session "$SESSION_NAME_3" "$COMMENT3"
send_command_to_session "$SESSION_NAME_3" "$CMD3"

# ===== SCREEN SESSION: gnuradio ===== #
# SESSION_NAME_4="gnuradio"
# COMMENT4="Starting GNURadio"  # Uncomment if enabling
# CMD4="source /opt/radioconda/etc/profile.d/conda.sh && conda activate base && /opt/mep-examples/scripts/gnuradio-companion"
# start_screen_session "$SESSION_NAME_4" "$COMMENT4"
# send_command_to_session "$SESSION_NAME_4" "$CMD4"

# ===== SCREEN SESSION: drf_watch ===== #
SESSION_NAME_5="drf_watch"
COMMENT5="Watching the Ringbuffer Directory for DigitalRF file changes"
start_screen_session "$SESSION_NAME_5" "$COMMENT5"
send_command_to_session "$SESSION_NAME_5" "source /opt/radioconda/etc/profile.d/conda.sh"
send_command_to_session "$SESSION_NAME_5" "conda activate base"
send_command_to_session "$SESSION_NAME_5" "drf watch /data/ringbuffer"

