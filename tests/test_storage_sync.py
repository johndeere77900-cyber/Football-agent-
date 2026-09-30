"""
Unit tests for storage_sync.py and migrate_state.py.
"""

import os
import pytest
import storage_sync
import migrate_state


def test_get_s3_client_unconfigured(monkeypatch):
    monkeypatch.delenv("S3_BUCKET", raising=False)
    monkeypatch.delenv("STATE_S3_BUCKET", raising=False)
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)

    client, bucket = storage_sync.get_s3_client()
    assert client is None
    assert bucket is None


def test_get_s3_client_configured(monkeypatch):
    monkeypatch.setenv("S3_BUCKET", "test-bucket")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test-secret")
    monkeypatch.setenv("AWS_REGION", "us-west-2")

    client, bucket = storage_sync.get_s3_client()
    assert client is not None
    assert bucket == "test-bucket"


def test_download_state_files_fallback_when_unconfigured(monkeypatch):
    monkeypatch.delenv("S3_BUCKET", raising=False)
    monkeypatch.delenv("STATE_S3_BUCKET", raising=False)

    success = storage_sync.download_state_files(
        files=["predictions.db", "telegram_memory.json"]
    )
    assert success is False


def test_download_state_files_with_mock_client():
    downloaded = []

    class MockClient:
        def download_file(self, bucket, filename, filepath):
            downloaded.append((bucket, filename, filepath))

    client = MockClient()
    success = storage_sync.download_state_files(
        files=["predictions.db"],
        client=client,
        bucket_name="my-bucket",
    )

    assert success is True
    assert downloaded == [("my-bucket", "predictions.db", "predictions.db")]


def test_upload_state_files_fallback_when_unconfigured(monkeypatch):
    monkeypatch.delenv("S3_BUCKET", raising=False)
    monkeypatch.delenv("STATE_S3_BUCKET", raising=False)

    success = storage_sync.upload_state_files(
        files=["predictions.db", "telegram_memory.json"]
    )
    assert success is False


def test_upload_state_files_skips_missing_local_files():
    uploaded = []

    class MockClient:
        def upload_file(self, filepath, bucket, filename):
            uploaded.append((filepath, bucket, filename))

    client = MockClient()
    success = storage_sync.upload_state_files(
        files=["nonexistent_file_xyz.db"],
        client=client,
        bucket_name="my-bucket",
    )

    assert success is True
    assert uploaded == []


def test_upload_state_files_with_mock_client(tmp_path):
    local_file = tmp_path / "test_state.db"
    local_file.write_text("dummy database content")

    uploaded = []

    class MockClient:
        def upload_file(self, filepath, bucket, filename):
            uploaded.append((filepath, bucket, filename))

    client = MockClient()
    success = storage_sync.upload_state_files(
        files=[str(local_file)],
        client=client,
        bucket_name="my-bucket",
    )

    assert success is True
    assert uploaded == [(str(local_file), "my-bucket", "test_state.db")]


def test_migrate_state_unconfigured(monkeypatch):
    monkeypatch.delenv("S3_BUCKET", raising=False)
    monkeypatch.delenv("STATE_S3_BUCKET", raising=False)

    res = migrate_state.migrate()
    assert res is False


def test_migrate_state_no_local_files(monkeypatch):
    monkeypatch.setenv("S3_BUCKET", "test-bucket")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")

    # Mock storage_sync.upload_state_files
    monkeypatch.setattr(
        storage_sync,
        "upload_state_files",
        lambda files, client=None, bucket_name=None: True,
    )

    # Monkeypatch os.path.exists to simulate no files
    monkeypatch.setattr(os.path, "exists", lambda path: False)

    res = migrate_state.migrate()
    assert res is True
