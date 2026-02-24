#!/usr/bin/env python3

import argparse
import json
import subprocess


def main():
    parser = argparse.ArgumentParser(
        prog="stop_rec",
        description="Stop recording for the SpectrumX Mobile Experiment Platform (MEP)",
    )
    args = parser.parse_args()

    sub_listen = subprocess.Popen(
        "mosquitto_sub -t recorder/status -C 2 -W 1 | jq --color-output",
        shell=True,
    )

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

    sub_listen.wait()

    print("Done")


if __name__ == "__main__":
    main()
