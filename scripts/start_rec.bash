#!/bin/bash

# NOTE: you should probably have a ramdisk mounted to /data/ringbuffer, e.g. add
#
#   tmpfs /data/ringbuffer tmpfs nodev,nosuid,noexec,size=1G 0 0
#
# to /etc/fstab

set -E

export HOLOSCAN_EXECUTOR_LOG_LEVEL=WARN
export HOLOSCAN_LOG_LEVEL=INFO
export HOLOSCAN_LOG_FORMAT=DEFAULT

trap "trap - SIGTERM && kill -SIGTERM -$$" SIGINT SIGTERM EXIT

eval "$(command conda 'shell.bash' 'hook' 2> /dev/null)"

while true; do
    read -p "# Enter the sample rate in MHz (1, 10, 16, 20, or 64): " sr
    case $sr in
        1 )
            echo "1 MHz"
            break
        ;;
        10 )
            echo "10 MHz"
            break
        ;;
        16 )
            echo "16 MHz"
            break
        ;;
        20 )
            echo "20 MHz"
            break
        ;;
        64 )
            echo "64 MHz"
            break
        ;;
        * )
            echo "Answer either '1', '10', '16', '20', or '64'!"
        ;;
    esac
done

while true; do
    read -p "# Enter a short name for the experiment: " exp
    case $exp in
        . )
            echo "skipping recording"
            break
        ;;
        *\ * )
            echo "Please enter a string without white space"
        ;;
        * )
            echo "Experiment name is '$exp'"
            conda run -n base --no-capture-output drf mirror --link cp "/data/ringbuffer/mep/sr${sr}MHz" "/data/recordings/${exp}/sr${sr}MHz" &
            break
        ;;
    esac
done

conda run -n base --no-capture-output drf ringbuffer -c 1 /data/ringbuffer &
find /data/ringbuffer -type f -name "tmp.rf*.h5" -exec rm "{}" \;
sleep 2

mkdir -p /data/ringbuffer/mep
cd /data/ringbuffer/mep

export PYTHONPATH=/opt/nvidia/holoscan/python/lib:/opt/holohub/build/python/lib
python /opt/holohub/applications/sdr_mep_recorder/sdr_mep_recorder.py "/opt/holohub/applications/sdr_mep_recorder/configs/sr${sr}MHz.yaml" &

sleep infinity
