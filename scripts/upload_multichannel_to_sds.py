#!/usr/bin/env python

import argparse
import logging
import pathlib
import sys
import os
from pathlib import PurePosixPath

from spectrumx.client import Client
from spectrumx.errors import NetworkError, SDSError, ServiceError

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def main():
    parser = argparse.ArgumentParser(
        prog="upload_multichannel_to_sds",
        description="Upload a multi-channel DigitalRF capture to the SDS",
        epilog="Example:\n  python upload_multichannel_to_sds.py <local_dir> <reference dir> --channels ch1 ch2 ch3\nThis uploads a multi-channel capture."
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
    parser.add_argument(
        "--channels",
        nargs='+',
        required=True,
        help="List of channels to upload (space separated)",
    )
    args = parser.parse_args()

    data_dir = args.data_dir.resolve()
    if not data_dir.exists() or not data_dir.is_dir():
        logger.error(f"Data directory '{data_dir}' does not exist or is not a directory!")
        sys.exit(1)
    if not os.access(data_dir, os.R_OK):
        logger.error(f"Data directory '{data_dir}' is not readable!")
        sys.exit(1)

    sds = Client(
        host="sds.crc.nd.edu",
        env_file=args.dotenv,
    )
    sds.dry_run = args.dry_run
    sds.authenticate()

    try:
        upload_results = sds.upload_multichannel_drf_capture(
            local_path=data_dir,
            sds_path=args.reference_name,
            channels=args.channels,
            verbose=True,
        )
    except (NetworkError, ServiceError, SDSError) as e:
        logger.error(f"Error uploading multi-channel capture: {e}")
        sys.exit(1)

    if not upload_results:
        logger.error("Upload results are not available.")
        return None

    success_results = [success for success in upload_results if success]
    failed_results = [success for success in upload_results if not success]
    logger.info(f"Uploaded {len(success_results)} assets.")
    logger.info(f"{len(failed_results)} assets failed.")
    # TODO: Handle failed uploads here. You may want to inspect the upload results and take action if any uploads failed.

    if sds.dry_run:
        logger.info("Turn off dry-run to actually upload files!")

if __name__ == "__main__":
    # Usability hint for users
    logger.info("Tip: Run 'python upload_multichannel_to_sds.py --help' to see all available options.\n")
    main() 