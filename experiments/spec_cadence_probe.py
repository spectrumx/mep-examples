#!/usr/bin/env python3
"""Standalone SPEC frame cadence probe.

Subscribes directly to the MEP spectrum topic and, for every frame, reports:

  arr_dt   - gap between *arrivals* at this subscriber, on a monotonic clock
             (transit + producer cadence as seen by a consumer; no GUI involved)
  send_dt  - gap between successive producer 'timestamp' values
             (the device's own emit rhythm, independent of the network)
  lag      - arr_clock - send_clock, i.e. how far arrival trails the producer
             timestamp; only the *variation* is meaningful unless the producer
             and this host share a synced clock
  batch    - the payload 'batch' field, to see whether it is a per-frame counter

Reading the result:
  * send_dt steady (~scan_time) but arr_dt lumpy  -> burst introduced in transit
    (broker / network / paho), not the framing code.
  * send_dt itself lumpy                          -> the recording/framing side is
    emitting unevenly; that is the producer's domain.
  * batch increments 0,1,2,...                    -> usable sequence number; gaps
    in it reveal dropped/reordered frames.

Run while the recorder is going:
    python spec_cadence_probe.py --host localhost --count 200
"""

import argparse
import csv
import json
import time
from datetime import datetime

import paho.mqtt.client as mqtt

TOPIC = "radiohound/clients/data/#"


def parse_iso(ts: str):
    """Parse the producer ISO-8601 timestamp to epoch seconds (float)."""
    if not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts).timestamp()
    except ValueError:
        return None


def main():
    ap = argparse.ArgumentParser(description="Probe SPEC frame arrival vs send cadence.")
    ap.add_argument("--host", default="localhost", help="MQTT broker host (default: localhost)")
    ap.add_argument("--port", type=int, default=1883, help="MQTT broker port (default: 1883)")
    ap.add_argument("--topic", default=TOPIC, help="Topic filter to subscribe to")
    ap.add_argument("--count", type=int, default=0, help="Stop after N frames (0 = run forever)")
    ap.add_argument("--file", default=None, help="Optional CSV file to log per-frame rows to")
    args = ap.parse_args()

    csv_file = None
    csv_writer = None
    if args.file:
        csv_file = open(args.file, "w", newline="")
        csv_writer = csv.writer(csv_file)
        csv_writer.writerow(["n", "arr_dt_ms", "send_dt_ms", "lag_ms", "batch", "timestamp"])
        print(f"Logging CSV to {args.file}")

    state = {
        "n": 0,
        "prev_arr": None,    # monotonic seconds of previous arrival
        "prev_send": None,   # epoch seconds of previous producer timestamp
        "prev_batch": None,
        "arr_dts": [],
        "send_dts": [],
    }

    print(f"{'#':>5} {'arr_dt':>9} {'send_dt':>9} {'lag':>9} {'batch':>7}  timestamp")
    print("-" * 70)

    def on_connect(client, userdata, flags, rc, *_):
        client.subscribe(args.topic)

    def on_message(client, userdata, msg):
        arr = time.monotonic()
        try:
            d = json.loads(msg.payload)
        except (ValueError, UnicodeDecodeError):
            return

        send = parse_iso(d.get("timestamp"))
        batch = d.get("batch")

        arr_dt = (arr - state["prev_arr"]) * 1000.0 if state["prev_arr"] is not None else None
        send_dt = (send - state["prev_send"]) * 1000.0 if (send is not None and state["prev_send"] is not None) else None
        lag = (arr - send) * 1000.0 if send is not None else None

        state["n"] += 1
        if arr_dt is not None:
            state["arr_dts"].append(arr_dt)
        if send_dt is not None:
            state["send_dts"].append(send_dt)

        def fmt(v):
            return f"{v:9.1f}" if v is not None else f"{'-':>9}"

        print(f"{state['n']:>5} {fmt(arr_dt)} {fmt(send_dt)} {fmt(lag)} {str(batch):>7}  {d.get('timestamp')}")

        if csv_writer is not None:
            csv_writer.writerow([
                state["n"],
                f"{arr_dt:.3f}" if arr_dt is not None else "",
                f"{send_dt:.3f}" if send_dt is not None else "",
                f"{lag:.3f}" if lag is not None else "",
                batch,
                d.get("timestamp"),
            ])
            csv_file.flush()

        state["prev_arr"] = arr
        state["prev_send"] = send
        state["prev_batch"] = batch

        if args.count and state["n"] >= args.count:
            client.disconnect()

    client = mqtt.Client()
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(args.host, args.port, keepalive=60)

    try:
        client.loop_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if csv_file is not None:
            csv_file.close()

    # Summary
    def stats(xs):
        if not xs:
            return "no data"
        xs_sorted = sorted(xs)
        n = len(xs_sorted)
        mean = sum(xs_sorted) / n
        median = xs_sorted[n // 2]
        return f"n={n} min={xs_sorted[0]:.1f} median={median:.1f} mean={mean:.1f} max={xs_sorted[-1]:.1f} ms"

    print("-" * 70)
    print(f"arrival cadence : {stats(state['arr_dts'])}")
    print(f"send cadence    : {stats(state['send_dts'])}")


if __name__ == "__main__":
    main()
