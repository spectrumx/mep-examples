#!/bin/bash

# Safely quote all passed arguments
REMOTE_ARGS=$(printf '%q ' "$@")

ssh -t root@192.168.20.100 "source /usr/local/share/pynq-venv/bin/activate && \
BOARD=RFSoC4x2 XILINX_XRT=/usr python /opt/git/rfsoc_qsfp_10g/boards/RFSoC4x2/rfsoc_qsfp_offload/scripts/start_capture_rx.py $REMOTE_ARGS"


