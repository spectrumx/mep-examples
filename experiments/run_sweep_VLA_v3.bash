#!/bin/bash

# ===== COMMON ===== #
WORKDIR="/opt/mep-examples/scripts"
mkdir -p /tmp/mep_screens
echo "Starting Screen Sessions"

# ===== SCREEN SESSION: rfsoc_rx ===== #
SESSION1="rfsoc_rx"
CMD1="./start_rfsoc_rx.bash -c A B -r"

screen -S $SESSION1 -X quit 2>/dev/null
cat > /tmp/mep_screens/$SESSION1.sh <<EOF
#!/usr/bin/env bash -l
source /opt/radioconda/etc/profile.d/conda.sh
conda activate base
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
CMD2="python3 start_mep_rx.py -f1 7125 -s 10 -d 6000 -t VALON"

screen -S $SESSION2 -X quit 2>/dev/null
cat > /tmp/mep_screens/$SESSION2.sh <<EOF
#!/usr/bin/env bash -l
source /opt/radioconda/etc/profile.d/conda.sh
conda activate base
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
CMD3="python3 start_rec.py -c A -r 1"

screen -S $SESSION3 -X quit 2>/dev/null
cat > /tmp/mep_screens/$SESSION3.sh <<EOF
#!/usr/bin/env bash -l
source /opt/radioconda/etc/profile.d/conda.sh
conda activate base
cd "$WORKDIR"
echo "Running: $CMD3"
$CMD3
exec bash
EOF

chmod +x /tmp/mep_screens/$SESSION3.sh
screen -dmS $SESSION3 bash /tmp/mep_screens/$SESSION3.sh
echo "... screen -xS $SESSION3"

# ===== SCREEN SESSION: gnuradio ===== #
SESSION4="gnuradio"
CMD4="gnuradio-companion"

screen -S $SESSION4 -X quit 2>/dev/null
cat > /tmp/mep_screens/$SESSION4.sh <<EOF
#!/usr/bin/env bash -l
source /opt/radioconda/etc/profile.d/conda.sh
conda activate base
cd "$WORKDIR"
echo "Running: $CMD4"
$CMD4
exec bash
EOF

chmod +x /tmp/mep_screens/$SESSION4.sh
screen -dmS $SESSION4 bash /tmp/mep_screens/$SESSION4.sh
echo "... screen -xS $SESSION4"

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

