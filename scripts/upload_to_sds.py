#!/usr/bin/env python

import argparse
import pathlib

from spectrumx.client import Client

def main():
    parser = argparse.ArgumentParser(
        prog="upload_to_sds",
        description="Upload directory of files to the SDS",
    )
    parser.add_argument(
        "data_dir",
        type=pathlib.Path,
        help="Path to data directory to upload"
    )
    parser.add_argument(
        "reference_name",
        help="Reference name (virtual directory) to upload to",
    )
    parser.add_argument(
        "-n",
        "--dry-run",
        dest="dry_run",
        action="store_true",
    )
    parser.add_argument(
        "--dotenv",
        type=pathlib.Path,
        default=pathlib.Path(".env"),
        help="Path to .env file containing SDS_SECRET_TOKEN",
    )
    args = parser.parse_args()

    data_dir = args.data_dir.resolve()
    if not data_dir.exists():
        msg = f"Data directory '{data_dir}' does not exist!"
        raise ValueError(msg)

    sds = Client(
        host="sds.crc.nd.edu",
        env_file=args.dotenv,
    )
    sds.dry_run = args.dry_run

    sds.authenticate()
    sds.upload(
        local_path=data_dir,
        sds_path=args.reference_name,
        verbose=True,
    )

    if sds.dry_run:
        print("Turn off dry-run to actually upload files!")

if __name__ == "__main__":
    main()
