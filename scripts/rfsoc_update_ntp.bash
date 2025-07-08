#!/bin/bash

# Safely quote all passed arguments
REMOTE_ARGS=$(printf '%q ' "$@")

ssh -t root@192.168.20.100 "ntpdate -s 192.168.20.1 && systemctl restart systemd-timesyncd && echo 'NTP update completed. Current system time:' && date" 