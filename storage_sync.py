"""
Persistent state synchronization for football and basketball prediction agent.

Separates runtime database and bot state files (predictions.db, telegram_memory.json)
from the public git source repository by syncing with secure private S3-compatible storage.

Supported Environment Variables:
- S3_BUCKET / STATE_S3_BUCKET: Bucket name (e.g. "my-sports-agent-state")
- AWS_ACCESS_KEY_ID: S3 access key
- AWS_SECRET_ACCESS_KEY: S3 secret key
- AWS_REGION: S3 region (default: "us-east-1")
- S3_ENDPOINT_URL: Optional endpoint for R2, MinIO, or custom S3 provider
"""

import logging
import os
import sys
from typing import Any, List, Optional, Tuple

logger = logging.getLogger("storage_sync")


def get_s3_client(
    bucket: Optional[str] = None,
    access_key: Optional[str] = None,
    secret_key: Optional[str] = None,
    region: Optional[str] = None,
    endpoint_url: Optional[str] = None,
) -> Tuple[Optional[Any], Optional[str]]:
    """
    Construct boto3 S3 client if S3 credentials are configured.
    Returns (s3_client, bucket_name) or (None, None) if unconfigured.
    """
    bucket_name = (
        bucket
        or os.environ.get("S3_BUCKET")
        or os.environ.get("STATE_S3_BUCKET")
    )
    aws_access_key = access_key or os.environ.get("AWS_ACCESS_KEY_ID")
    aws_secret_key = secret_key or os.environ.get("AWS_SECRET_ACCESS_KEY")
    aws_region = region or os.environ.get("AWS_REGION", "us-east-1")
    s3_endpoint = endpoint_url or os.environ.get("S3_ENDPOINT_URL")

    if not bucket_name or not aws_access_key or not aws_secret_key:
        return None, None

    try:
        import boto3
        from botocore.config import Config

        s3_config = Config(
            region_name=aws_region,
            signature_version="s3v4",
        )
        client_kwargs = {
            "service_name": "s3",
            "aws_access_key_id": aws_access_key,
            "aws_secret_access_key": aws_secret_key,
            "config": s3_config,
        }
        if s3_endpoint:
            client_kwargs["endpoint_url"] = s3_endpoint

        client = boto3.client(**client_kwargs)
        return client, bucket_name
    except Exception as exc:
        logger.warning(f"Failed to initialize S3 client: {exc}")
        return None, None


def download_state_files(
    files: Optional[List[str]] = None,
    client: Any = None,
    bucket_name: Optional[str] = None,
) -> bool:
    """
    Download state files from private S3 bucket if configured.
    If S3 is unconfigured, safely falls back to local disk files.
    """
    if files is None:
        files = ["predictions.db", "telegram_memory.json"]

    if client is None or bucket_name is None:
        client, bucket_name = get_s3_client()

    if not client or not bucket_name:
        logger.info(
            "S3 storage unconfigured; using local disk state files."
        )
        return False

    success = True
    for filepath in files:
        if not filepath:
            continue
        filename = os.path.basename(filepath)
        try:
            client.download_file(bucket_name, filename, filepath)
            logger.info(
                f"Downloaded {filename} from s3://{bucket_name}/{filename}"
            )
        except Exception as exc:
            logger.info(
                f"Could not download {filename} from S3 ({exc}); using local file if present."
            )
            success = False

    return success


def upload_state_files(
    files: Optional[List[str]] = None,
    client: Any = None,
    bucket_name: Optional[str] = None,
) -> bool:
    """
    Upload state files to private S3 bucket if configured and present locally.
    If S3 is unconfigured, safely logs and completes without error.
    """
    if files is None:
        files = ["predictions.db", "telegram_memory.json"]

    if client is None or bucket_name is None:
        client, bucket_name = get_s3_client()

    if not client or not bucket_name:
        logger.info(
            "S3 storage unconfigured; skipping remote state upload."
        )
        return False

    success = True
    for filepath in files:
        if not filepath or not os.path.exists(filepath):
            logger.info(
                f"Local state file {filepath} not found; skipping upload."
            )
            continue

        filename = os.path.basename(filepath)
        try:
            client.upload_file(filepath, bucket_name, filename)
            logger.info(
                f"Uploaded {filename} to s3://{bucket_name}/{filename}"
            )
        except Exception as exc:
            logger.error(
                f"Failed to upload {filename} to S3: {exc}"
            )
            success = False

    return success


def main():
    """CLI helper for workflow steps: python3 storage_sync.py [download|upload]"""
    if len(sys.argv) < 2:
        print("Usage: python3 storage_sync.py [download|upload]")
        sys.exit(1)

    cmd = sys.argv[1].lower()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    if cmd == "download":
        download_state_files()
    elif cmd == "upload":
        upload_state_files()
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
