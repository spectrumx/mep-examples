#!/bin/bash

# ===== SCREEN SESSION: Jetson Power GUI ===== #
SESSION5="jetsonpowergui"
CMD5="sudo DISPLAY=$DISPLAY python /usr/share/jetsonpowergui/__main__.py"

# Clean up any previous screen session
screen -S $SESSION5 -X quit 2>/dev/null

# Ensure the script directory exists
mkdir -p /tmp/mep_screens

# Write the screen startup script
cat > /tmp/mep_screens/$SESSION5.sh <<EOF
#!/usr/bin/env bash -l

# Export X11 auth to root
xauth extract - \$DISPLAY | sudo xauth merge -

echo "Running: $CMD5"
$CMD5

exec bash
EOF

# Make the script executable
chmod +x /tmp/mep_screens/$SESSION5.sh

# Launch the screen session
screen -dmS $SESSION5 bash /tmp/mep_screens/$SESSION5.sh
echo "... screen -xS $SESSION5"
