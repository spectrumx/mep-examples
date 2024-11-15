set -E

trap "trap - SIGTERM && kill -- -$$" SIGINT SIGTERM EXIT

eval "$(command conda 'shell.bash' 'hook' 2> /dev/null)"
conda activate base

drf mirror --force_polling --link cp /data/ringbuffer/mep /data/recordings/mep &
drf ringbuffer --force_polling -c 1 /data/ringbuffer &
find /data/ringbuffer -type f -name "tmp.rf*.h5" -exec rm "{}" \;

sleep 2

/opt/holohub/build/applications/mimo_radar_pipeline/cpp/mimo_radar_pipeline $@ &

sleep infinity
