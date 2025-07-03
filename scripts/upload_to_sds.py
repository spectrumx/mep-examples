#!/usr/bin/env python
#
# Example script to upload a single channel Digital-RF capture to SDS.
#
# It will perform in separate steps:
#   1. Recursive upload of all files in the directory passed.
#   2. Create a single channel capture of type Digital-RF in SDS.
# Run upload_to_sds.py --help for usage instructions.
#

import argparse
import logging
import os
import pathlib
import sys
from pathlib import PurePosixPath

from spectrumx.client import Client
from spectrumx.errors import CaptureError, NetworkError, Result, SDSError, ServiceError
from spectrumx.models.captures import CaptureType
from spectrumx.models.files import File

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(
        prog="upload_to_sds",
        description="Upload directory of files to the SDS",
        epilog="Example:\n  python upload_to_sds.py <local_dir> <reference dir> --create-capture --channel <channel name>\nThis uploads data and creates a capture.",
    )
    parser.add_argument(
        "data_dir", type=pathlib.Path, help="Path to data directory to upload"
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
        logger.error(
            f"Data directory '{data_dir}' does not exist or is not a directory!"
        )
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
    # this will raise an AuthError if the token is not set or invalid

    upload_results: list[Result[File]] = sds.upload(
        local_path=data_dir,
        sds_path=args.reference_name,
        verbose=True,  # shows a progress bar during upload
    )

    if not upload_results:
        logger.error("Upload results are not available.")
        return None

    successful_files: list[File] = [res() for res in upload_results if res]
    failed_results: list[Result[File]] = [res for res in upload_results if not res]
    logger.info(f"Uploaded {len(successful_files)} files.")
    logger.info(f"{len(failed_results)} files failed.")

    # TODO: Handle failed uploads here as needed.
    # calling() a failed Result will raise the wrapped exception.

    # You can iterate over the successfully uploaded file instances:
    for file_instance in successful_files:
        logger.debug(f"\t{file_instance.uuid} to '{file_instance.path}'")
        break

    if sds.dry_run:
        logger.info("Turn off dry-run to actually upload files!")
        return None

    if not args.create_capture:
        logger.info("Capture creation skipped.")
        return None

    if not args.channel:
        logger.error("--channel is required when --create-capture is used")
        sys.exit(1)

    logger.info("Creating capture...")

    sds_dir = PurePosixPath(args.reference_name)
    channel = args.channel
    try:
        logger.info(f"Creating capture at {sds_dir} with channel {channel}")
        capture = sds.captures.create(
            top_level_dir=PurePosixPath(sds_dir),
            channel=channel,
            capture_type=CaptureType.DigitalRF,
        )
        logger.info(f"Created capture with UUID {capture.uuid}")
    except CaptureError as e:
        logger.error(f"Error creating capture at {sds_dir=} with {channel=}: {e}")
        # TODO: handle specific capture errors if needed
        raise
    except (NetworkError, ServiceError, SDSError) as e:
        logger.error(f"SDS might be down or unreachable: {e}")
        # TODO: handle network or service errors
        raise


if __name__ == "__main__":
    # Usability hint for users
    logger.info(
        "Tip: Run 'python upload_to_sds.py --help' to see all available options.\n"
    )
    main()
