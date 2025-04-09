#!/bin/bash

# NOTE: you should probably have a ramdisk mounted to /data/ringbuffer, e.g. add
#
#   tmpfs /data/ringbuffer tmpfs nodev,nosuid,noexec,size=1G 0 0
#
# to /etc/fstab

set -E

trap "trap - SIGTERM && kill -- -$$" SIGINT SIGTERM EXIT

eval "$(command conda 'shell.bash' 'hook' 2> /dev/null)"
conda activate base

while true; do
    read -p "# Enter the sample rate in MHz (1, 10, or, 20): " sr
    case $sr in
        1 )
            echo "1 MHz"
            break
        ;;
        10 )
            echo "10 MHz"
            break
        ;;
        20 )
            echo "20 MHz"
            break
        ;;
        * )
            echo "Answer either '1', '10', or '20'!"
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
            drf mirror mv "/data/ringbuffer/mep/sr${sr}MHz" "/data/recordings/${exp}/sr${sr}MHz" &
            break
        ;;
    esac
done

drf ringbuffer --size 500MB /data/ringbuffer &
find /data/ringbuffer -type f -name "tmp.rf*.h5" -exec rm "{}" \;
sleep 2

/opt/holohub/build/applications/sdr_mep_recorder/sdr_mep_recorder "sr${sr}MHz.yaml" &

sleep infinity
