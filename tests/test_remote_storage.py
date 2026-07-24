"""Integration tests for RemoteStorageService against moto (mocked S3)."""
from __future__ import annotations

import json
from dataclasses import dataclass

import boto3
import pytest

moto = pytest.importorskip("moto")
from moto import mock_aws  # noqa: E402

from services.remote_storage import RemoteStorageService, upload_part_count  # noqa: E402

_BUCKET = "vol-test-123"
_REGION = "us-east-1"
# moto only intercepts the default AWS endpoint; a custom endpoint_url would make
# boto3 hit the real network. Leave it None so the mocked backend is used.
_ENDPOINT = None


@dataclass
class FakeProfile:
    volume_id: str = _BUCKET
    access_key_id: str = "user_test"
    region: str = _REGION
    endpoint: str = _ENDPOINT


def _make_bucket():
    client = boto3.client("s3", region_name=_REGION,
                          aws_access_key_id="user_test", aws_secret_access_key="rps_test")
    client.create_bucket(Bucket=_BUCKET)
    return client


@mock_aws
def test_probe_and_usage():
    _make_bucket()
    svc = RemoteStorageService(FakeProfile(), "rps_test")
    svc.probe()  # should not raise
    used, count = svc.bucket_usage("")
    assert used == 0 and count == 0


@mock_aws
def test_upload_list_head_download_delete(tmp_path):
    client = _make_bucket()
    svc = RemoteStorageService(FakeProfile(), "rps_test")

    # Seed a JSON workflow and a model file.
    client.put_object(Bucket=_BUCKET, Key="workflows/wf.json",
                      Body=json.dumps({"nodes": []}).encode())

    local = tmp_path / "model.safetensors"
    local.write_bytes(b"x" * 2048)
    progress = []
    svc.upload_file(str(local), "models/checkpoints/model.safetensors",
                    progress_cb=lambda n: progress.append(n))

    # Listing strips the prefix into relpath.
    objs = list(svc.list_objects("models/"))
    rels = {o.relpath for o in objs}
    assert "checkpoints/model.safetensors" in rels
    assert sum(progress) == 2048

    # head + download.
    assert svc.head_object("models/checkpoints/model.safetensors") is not None
    assert svc.head_object("models/does-not-exist.bin") is None
    data = svc.download_json("workflows/wf.json")
    assert data == {"nodes": []}

    used, count = svc.bucket_usage("")
    assert count == 2 and used == 2048 + len(json.dumps({"nodes": []}))

    # delete one key at a time.
    svc.delete_object("models/checkpoints/model.safetensors")
    assert svc.head_object("models/checkpoints/model.safetensors") is None


@mock_aws
def test_bucket_usage_respects_prefix_and_progress():
    client = _make_bucket()
    client.put_object(Bucket=_BUCKET, Key="models/a.bin", Body=b"aaa")
    client.put_object(Bucket=_BUCKET, Key="models/b.bin", Body=b"bb")
    client.put_object(Bucket=_BUCKET, Key="other/c.bin", Body=b"c")
    svc = RemoteStorageService(FakeProfile(), "rps_test")

    progress: list[tuple[int, str]] = []
    used, count = svc.bucket_usage(
        "models/", progress_cb=lambda n, msg: progress.append((n, msg)),
    )
    assert used == 5 and count == 2
    assert progress
    assert progress[0][0] == 1

    used_all, count_all = svc.bucket_usage("")
    assert count_all == 3 and used_all == 6


@mock_aws
def test_upload_multipart_splits_into_parts(tmp_path, monkeypatch):
    import services.remote_storage as rs

    part = 5 * 1024 * 1024  # moto enforces the 5 MB minimum on non-final parts
    monkeypatch.setattr(rs, "_MULTIPART_THRESHOLD", part)
    monkeypatch.setattr(rs, "_MULTIPART_PART_SIZE", part)

    client = _make_bucket()
    svc = RemoteStorageService(FakeProfile(), "rps_test")

    payload = b"z" * (part + 1000)  # -> two parts: 5 MB + 1000 B
    local = tmp_path / "big.bin"
    local.write_bytes(payload)

    progress: list[int] = []
    svc.upload_file(str(local), "models/big.bin", progress_cb=progress.append)

    assert len(progress) == 2
    assert sum(progress) == len(payload)
    body = client.get_object(Bucket=_BUCKET, Key="models/big.bin")["Body"].read()
    assert body == payload


@mock_aws
def test_upload_part_retries_on_524(tmp_path, monkeypatch):
    import services.remote_storage as rs
    from botocore.exceptions import ClientError

    monkeypatch.setattr(rs, "_MULTIPART_THRESHOLD", 1024)
    monkeypatch.setattr(rs, "_MULTIPART_PART_SIZE", 1 * 1024 * 1024)  # one part
    monkeypatch.setattr(rs, "_RETRY_BASE_DELAY", 0.0)  # no real backoff sleeps

    _make_bucket()
    svc = RemoteStorageService(FakeProfile(), "rps_test")

    real_upload_part = svc._client.upload_part
    calls = {"n": 0}

    def flaky_upload_part(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ClientError(
                {"Error": {"Code": "524", "Message": "origin timeout"},
                 "ResponseMetadata": {"HTTPStatusCode": 524}},
                "UploadPart",
            )
        return real_upload_part(**kwargs)

    monkeypatch.setattr(svc._client, "upload_part", flaky_upload_part)

    payload = b"w" * 2048
    local = tmp_path / "flaky.bin"
    local.write_bytes(payload)

    svc.upload_file(str(local), "models/flaky.bin")

    assert calls["n"] == 2  # failed once, retried, succeeded
    body = svc.head_object("models/flaky.bin")
    assert body is not None


def test_upload_part_count():
    part = 16 * 1024 * 1024
    assert upload_part_count(0) == 1
    assert upload_part_count(1024) == 1
    assert upload_part_count(part - 1) == 1
    assert upload_part_count(part) == 1
    assert upload_part_count(part + 1) == 2
    assert upload_part_count(part * 3 + 1000) == 4


@mock_aws
def test_list_skips_directory_markers():
    client = _make_bucket()
    svc = RemoteStorageService(FakeProfile(), "rps_test")
    client.put_object(Bucket=_BUCKET, Key="models/loras/", Body=b"")  # dir marker
    client.put_object(Bucket=_BUCKET, Key="models/loras/a.safetensors", Body=b"data")
    rels = {o.relpath for o in svc.list_objects("models/")}
    assert rels == {"loras/a.safetensors"}
