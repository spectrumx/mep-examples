#!/usr/bin/env python3

import argparse
import json
import subprocess


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

    config_name = f"sr{args.sample_rate}MHz"

    sub_status = subprocess.Popen(
        [
            "mosquitto_sub",
            "-t",
            "recorder/status",
        ]
    )
    sub_config = subprocess.Popen(
        [
            "mosquitto_sub",
            "-t",
            "recorder/config/response",
        ]
    )

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

    sub_config.terminate()
    sub_config.wait()
    sub_status.terminate()
    sub_status.wait()


if __name__ == "__main__":
    main()
