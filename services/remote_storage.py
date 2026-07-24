"""
remote_storage.py

boto3-based access to a RunPod network volume via its S3-compatible API. The
volume ID is the bucket. Constructed from the active Connection Manager profile
(endpoint, region, access key) plus the secret from the CredentialsStore.

RunPod is a *partial* S3 clone — this service bakes in the important caveats:
- every request carries ``endpoint_url`` + ``region``;
- standard-mode retries with a high attempt count (502s mid-transfer are routine);
- a long read timeout (CompleteMultipartUpload is slow);
- listing is resilient to the "same next token twice" cold-listing error;
- deletes are one key at a time (no bulk ``DeleteObjects``).

Qt-free and safe to call from a worker thread.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterator, Optional

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config
from botocore.exceptions import ClientError, EndpointConnectionError

logger = logging.getLogger(__name__)

_MB = 1024 * 1024


class RemoteStorageError(Exception):
    pass


class RemoteAuthError(RemoteStorageError):
    pass


class TransferCancelled(RemoteStorageError):
    pass


@dataclass
class ObjectStat:
    key: str  # full S3 key
    relpath: str  # key with the query prefix stripped
    size_bytes: int
    mtime: float


class RemoteStorageService:
    def __init__(self, profile, secret: str) -> None:
        self._bucket = profile.volume_id
        try:
            self._client = boto3.client(
                "s3",
                aws_access_key_id=profile.access_key_id,
                aws_secret_access_key=secret,
                region_name=profile.region,
                endpoint_url=profile.endpoint,
                config=Config(
                    read_timeout=7200,
                    retries={"max_attempts": 10, "mode": "standard"},
                ),
            )
        except Exception as exc:  # noqa: BLE001
            raise RemoteStorageError(f"Failed to construct S3 client: {exc}") from exc

    @property
    def bucket(self) -> str:
        return self._bucket

    # --------------------------------------------------------------- validate
    def probe(self) -> None:
        """Cheapest way to validate credentials + volume together."""
        try:
            self._client.head_bucket(Bucket=self._bucket)
        except ClientError as exc:
            raise self._auth_or_generic(exc) from exc
        except EndpointConnectionError as exc:
            raise RemoteStorageError(f"Cannot reach endpoint: {exc}") from exc

    # ------------------------------------------------------------------- list
    def list_objects(self, prefix: str = "", attempts: int = 5) -> Iterator[ObjectStat]:
        """Paginated listing, resilient to cold-listing pagination errors.

        Directory-marker objects (size 0, key ends with '/') are skipped so the
        caller only sees real files.
        """
        for attempt in range(attempts):
            try:
                yield from self._list_once(prefix)
                return
            except ClientError as exc:
                if self._is_auth_error(exc):
                    raise self._auth_or_generic(exc) from exc
                if attempt == attempts - 1:
                    raise RemoteStorageError(str(exc)) from exc
                time.sleep(10 * (attempt + 1))
            except Exception as exc:  # noqa: BLE001 - includes the repeated-token error
                if attempt == attempts - 1:
                    raise RemoteStorageError(str(exc)) from exc
                time.sleep(10 * (attempt + 1))

    def _list_once(self, prefix: str) -> Iterator[ObjectStat]:
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                size = obj.get("Size", 0)
                if size == 0 and key.endswith("/"):
                    continue  # directory marker
                relpath = key[len(prefix):] if prefix and key.startswith(prefix) else key
                last_mod = obj.get("LastModified")
                mtime = last_mod.timestamp() if isinstance(last_mod, datetime) else 0.0
                yield ObjectStat(key=key, relpath=relpath, size_bytes=size, mtime=mtime)

    # --------------------------------------------------------------- metadata
    def head_object(self, key: str) -> Optional[dict]:
        try:
            return self._client.head_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return None
            raise self._auth_or_generic(exc) from exc

    def download_json(self, key: str) -> dict:
        try:
            resp = self._client.get_object(Bucket=self._bucket, Key=key)
            return json.loads(resp["Body"].read().decode("utf-8"))
        except ClientError as exc:
            raise self._auth_or_generic(exc) from exc

    # ----------------------------------------------------------------- upload
    def upload_file(
        self,
        local_path: str,
        key: str,
        progress_cb: Optional[Callable[[int], None]] = None,
        cancel_token=None,
    ) -> None:
        """Upload with automatic multipart. ``progress_cb`` receives the number
        of bytes transferred *since the last call* (incremental)."""
        transfer = TransferConfig(
            multipart_threshold=64 * _MB,
            multipart_chunksize=128 * _MB,  # comfortably under RunPod's 500 MB part cap
            max_concurrency=4,
            use_threads=True,
        )

        def _callback(bytes_amount: int) -> None:
            if cancel_token is not None and cancel_token.cancelled:
                raise TransferCancelled(f"Cancelled during upload of {key}")
            if progress_cb is not None:
                progress_cb(bytes_amount)

        try:
            self._client.upload_file(local_path, self._bucket, key,
                                     Config=transfer, Callback=_callback)
        except TransferCancelled:
            raise
        except ClientError as exc:
            raise self._auth_or_generic(exc) from exc

    # ----------------------------------------------------------------- delete
    def delete_object(self, key: str) -> None:
        try:
            self._client.delete_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            raise self._auth_or_generic(exc) from exc

    # ------------------------------------------------------------------ usage
    def bucket_usage(
        self,
        prefix: str = "",
        progress_cb: Optional[Callable[[int, str], None]] = None,
        progress_interval: int = 100,
    ) -> tuple[int, int]:
        used = 0
        count = 0
        for obj in self.list_objects(prefix):
            used += obj.size_bytes
            count += 1
            if progress_cb and (count == 1 or count % progress_interval == 0):
                progress_cb(count, f"Reading usage ({count:,} objects)…")
        return used, count

    def capacity_bytes(self) -> Optional[int]:
        """RunPod does not expose provisioned volume capacity over the S3 API."""
        return None

    # -------------------------------------------------------------- internals
    @staticmethod
    def _is_auth_error(exc: ClientError) -> bool:
        code = exc.response.get("Error", {}).get("Code", "")
        return code in ("403", "AccessDenied", "SignatureDoesNotMatch", "InvalidAccessKeyId")

    def _auth_or_generic(self, exc: ClientError) -> RemoteStorageError:
        if self._is_auth_error(exc):
            return RemoteAuthError(
                "S3 authentication failed — the access key/secret is wrong, "
                "the volume ID is invalid, or the system clock is skewed."
            )
        return RemoteStorageError(str(exc))
