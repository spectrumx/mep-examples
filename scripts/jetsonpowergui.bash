#!/bin/bash

# Allow root to access your X11 display
xauth extract - "$DISPLAY" | sudo xauth merge -

# Run the Jetson Power GUI as root with X11 forwarding
sudo DISPLAY=$DISPLAY python /usr/share/jetsonpowergui/__main__.py