#!/usr/bin/env python

import argparse
import logging
import pathlib
import sys
import os
from pathlib import PurePosixPath

from spectrumx.client import Client
from spectrumx.errors import NetworkError, SDSError, ServiceError
from spectrumx.models.captures import CaptureType

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def create_capture(sds_client: Client, sds_dir: str, channel: str) -> bool:
    """
    Create a capture in the SDS.

    Args:
        sds_client: Authenticated SDS client
        sds_dir: SDS directory containing the file
        channel: Channel to use for the capture

    Returns:
        bool: True if capture was created successfully, False otherwise
    """
    try:
        logger.info(f"Creating capture at {sds_dir} with channel {channel}")
        capture = sds_client.captures.create(
            top_level_dir=PurePosixPath(sds_dir),
            channel=channel,
            capture_type=CaptureType.DigitalRF,
        )
        logger.info(f"Created capture with UUID {capture.uuid}")
        return True
    except (NetworkError, ServiceError, SDSError) as e:
        logger.error(f"Error creating capture at {sds_dir} with channel {channel}: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(
        prog="upload_to_sds",
        description="Upload directory of files to the SDS",
        epilog="Example:\n  python upload_to_sds.py <local_dir> <reference dir> --create-capture --channel <channel name>\nThis uploads data and creates a capture."
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
        "--channel",
        help="Channel to use for creating capture after upload",
    )
    parser.add_argument(
        "--create-capture",
        action="store_true",
        help="Create a capture after uploading files",
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

    upload_results = sds.upload(
        local_path=data_dir,
        sds_path=args.reference_name,
        verbose=True,
    )

    if (not upload_results):
        logger.error("Upload results are not available.")
        return None
        
    success_results = [success for success in upload_results if success]
    failed_results = [success for success in upload_results if not success]
    logger.info(f"Uploaded {len(success_results)} assets.")
    logger.info(f"{len(failed_results)} assets failed.")
    # TODO: Handle failed uploads here. You may want to inspect the upload results and take action if any uploads failed.


    if sds.dry_run:
        logger.info("Turn off dry-run to actually upload files!")
    else:
        if args.create_capture:
            if not args.channel:
                logger.error("--channel is required when --create-capture is used")
                sys.exit(1)
            logger.info("Upload completed successfully. Creating capture...")
            success = create_capture(
                sds_client=sds,
                sds_dir=args.reference_name,
                channel=args.channel
            )
            if success:
                logger.info("Capture created successfully!")
            else:
                logger.error("Failed to create capture")
                sys.exit(1)


if __name__ == "__main__":
    # Usability hint for users
    logger.info("Tip: Run 'python upload_to_sds.py --help' to see all available options.\n")
    main()
