"""
Migration tool to transfer existing runtime state (predictions.db and telegram_memory.json)
from the public git repository to private persistent S3-compatible storage.
"""

import logging
import os
import sys

import storage_sync

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)


def migrate() -> bool:
    print("=== State Storage Migration ===")
    client, bucket_name = storage_sync.get_s3_client()

    if not client or not bucket_name:
        print("Error: S3 credentials/bucket not configured.")
        print(
            "Please set S3_BUCKET, AWS_ACCESS_KEY_ID, and "
            "AWS_SECRET_ACCESS_KEY before running migration."
        )
        return False

    files_to_migrate = ["predictions.db", "telegram_memory.json"]
    existing_files = [
        f for f in files_to_migrate if os.path.exists(f)
    ]

    if not existing_files:
        print(
            "No local predictions.db or telegram_memory.json found "
            "to migrate."
        )
        return True

    print(
        f"Migrating local files to s3://{bucket_name}/: "
        f"{', '.join(existing_files)}"
    )

    success = storage_sync.upload_state_files(
        existing_files,
        client=client,
        bucket_name=bucket_name,
    )

    if success:
        print(
            "Migration successful! Existing state files uploaded "
            "to private persistent S3 storage."
        )
        print(
            "You may now remove predictions.db and telegram_memory.json "
            "from git tracking using 'git rm --cached'."
        )
        return True
    else:
        print("Migration encountered errors during file upload.")
        return False


if __name__ == "__main__":
    if not migrate():
        sys.exit(1)
