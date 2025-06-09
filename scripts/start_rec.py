#!/usr/bin/env python3

import argparse
import json
import subprocess
import time


def main():
    parser = argparse.ArgumentParser(
        prog="start_rec",
        description="Enable and configure recording for the SpectrumX Mobile Experiment Platform (MEP)",
    )
    parser.add_argument(
        "-f", "--center_frequency", type=float, help="Channel center frequency, in MHz"
    )
    parser.add_argument(
        "-c",
        "--channel",
        help="RFSoC channel to tune (e.g. 'A', 'B', 'C', or 'D')",
    )
    parser.add_argument(
        "-r",
        "--sample_rate",
        type=int,
        help="Output recording sample rate to use, in MHz",
        default=1,
    )
    args = parser.parse_args()

    if args.channel not in ("A", "B", "C", "D"):
        raise ValueError("Channel must be 'A', 'B', 'C', or 'D'!")
    if args.channel == "A":
        dst_port = 60134
    elif args.channel == "B":
        dst_port = 60133

    valid_srs = (1, 2, 4, 8, 10, 16, 20, 64)
    if args.sample_rate not in valid_srs:
        raise ValueError(f"Sample rate must be one of: {valid_srs} MHz")

    config_name = f"sr{args.sample_rate}MHz"

    payload = {
        "task_name": "disable",
    }
    subprocess.run(
        [
            "mosquitto_pub",
            "-t",
            "recorder/command",
            "-m",
            json.dumps(payload),
        ]
    )
    time.sleep(0.1)

    payload = {
        "task_name": "config.load",
        "arguments": {
            "name": f"{config_name}",
        },
        "response_topic": "recorder/config/response",
    }
    subprocess.run(
        [
            "mosquitto_pub",
            "-t",
            "recorder/command",
            "-m",
            json.dumps(payload),
        ]
    )
    time.sleep(0.1)

    sub_listen = subprocess.Popen(
        "mosquitto_sub -t recorder/config/response -t recorder/status -C 3 -W 1 | jq --color-output",
        shell=True,
    )

    payload = {
        "task_name": "config.set",
        "arguments": {
            "key": "basic_network.dst_port",
            "value": f"{dst_port}",
        },
        "response_topic": "recorder/config/response",
    }
    subprocess.run(
        [
            "mosquitto_pub",
            "-t",
            "recorder/command",
            "-m",
            json.dumps(payload),
        ]
    )

    payload = {
        "task_name": "enable",
    }
    subprocess.run(
        [
            "mosquitto_pub",
            "-t",
            "recorder/command",
            "-m",
            json.dumps(payload),
        ]
    )

    sub_listen.wait()

    print("Done")


if __name__ == "__main__":
    main()
