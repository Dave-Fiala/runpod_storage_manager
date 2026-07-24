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
import os
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterator, Optional

import boto3
from botocore.config import Config
from botocore.exceptions import (
    ClientError,
    ConnectionClosedError,
    EndpointConnectionError,
    ReadTimeoutError,
)

logger = logging.getLogger(__name__)

_MB = 1024 * 1024

# Multipart upload tuning for RunPod's S3 gateway.
#
# RunPod fronts its S3 API with Cloudflare, whose ~100s origin-response window
# surfaces as HTTP 524 on UploadPart when a single part takes too long to commit
# on the backend. Large parts + high concurrency make that far more likely, so we
# keep parts small and upload them one at a time, retrying transient failures
# (524 and the other Cloudflare 52x codes, plus 5xx / connection drops) per part
# rather than restarting the whole transfer.
_MULTIPART_THRESHOLD = 16 * _MB  # switch to multipart above this size
_MULTIPART_PART_SIZE = 16 * _MB  # per-part size; well under RunPod's 500 MB cap
_MAX_PART_ATTEMPTS = 8           # attempts per part / put / complete
_RETRY_BASE_DELAY = 2.0          # seconds; exponential backoff base
_RETRY_MAX_DELAY = 60.0          # seconds; backoff ceiling

# HTTP status codes worth retrying. 520-527 are Cloudflare's edge codes (524 is
# the origin timeout we actually hit); 500/502/503/504 are ordinary S3 hiccups.
_RETRYABLE_STATUS = frozenset({500, 502, 503, 504, 520, 521, 522, 523, 524, 525, 526, 527})


def upload_part_count(size_bytes: int) -> int:
    """Return the number of S3 upload steps (PutObject or UploadPart) for a file.

    Kept in sync with ``RemoteStorageService.upload_file`` so the sync engine can
    pre-compute total progress steps before any bytes move."""
    if size_bytes < _MULTIPART_THRESHOLD:
        return 1
    return max(1, (size_bytes + _MULTIPART_PART_SIZE - 1) // _MULTIPART_PART_SIZE)


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
        """Upload a file to ``key``.

        Small files go via a single ``PutObject``; larger files use explicit
        multipart with small parts uploaded one at a time and retried per part.
        ``progress_cb`` receives the number of bytes transferred *since the last
        call* (incremental)."""
        size = os.path.getsize(local_path)
        if size < _MULTIPART_THRESHOLD:
            self._upload_single(local_path, key, size, progress_cb, cancel_token)
        else:
            self._upload_multipart(local_path, key, progress_cb, cancel_token)

    def _upload_single(
        self,
        local_path: str,
        key: str,
        size: int,
        progress_cb: Optional[Callable[[int], None]],
        cancel_token,
    ) -> None:
        self._check_cancelled(cancel_token, key)
        with open(local_path, "rb") as fh:
            body = fh.read()
        self._retry(
            f"PutObject {key}",
            lambda: self._client.put_object(Bucket=self._bucket, Key=key, Body=body),
        )
        if progress_cb is not None:
            progress_cb(size)

    def _upload_multipart(
        self,
        local_path: str,
        key: str,
        progress_cb: Optional[Callable[[int], None]],
        cancel_token,
    ) -> None:
        self._check_cancelled(cancel_token, key)
        mpu = self._retry(
            f"CreateMultipartUpload {key}",
            lambda: self._client.create_multipart_upload(Bucket=self._bucket, Key=key),
        )
        upload_id = mpu["UploadId"]
        parts: list[dict] = []
        try:
            with open(local_path, "rb") as fh:
                part_number = 1
                while True:
                    self._check_cancelled(cancel_token, key)
                    chunk = fh.read(_MULTIPART_PART_SIZE)
                    if not chunk:
                        break
                    resp = self._retry(
                        f"UploadPart {part_number} of {key}",
                        lambda c=chunk, p=part_number: self._client.upload_part(
                            Bucket=self._bucket,
                            Key=key,
                            PartNumber=p,
                            UploadId=upload_id,
                            Body=c,
                        ),
                    )
                    parts.append({"ETag": resp["ETag"], "PartNumber": part_number})
                    if progress_cb is not None:
                        progress_cb(len(chunk))
                    part_number += 1
            self._retry(
                f"CompleteMultipartUpload {key}",
                lambda: self._client.complete_multipart_upload(
                    Bucket=self._bucket,
                    Key=key,
                    UploadId=upload_id,
                    MultipartUpload={"Parts": parts},
                ),
            )
        except BaseException:
            # Always abort on any failure or cancellation: in-flight parts live in
            # a hidden .s3compat_uploads/ dir and otherwise silently consume the
            # volume's fixed capacity until aborted.
            try:
                self._client.abort_multipart_upload(
                    Bucket=self._bucket, Key=key, UploadId=upload_id
                )
            except Exception:
                logger.warning("Failed to abort multipart upload for %s", key)
            raise

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
    def _check_cancelled(cancel_token, key: str) -> None:
        if cancel_token is not None and cancel_token.cancelled:
            raise TransferCancelled(f"Cancelled during upload of {key}")

    @staticmethod
    def _http_status(exc: ClientError) -> Optional[int]:
        try:
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if status is not None:
                return int(status)
        except (AttributeError, TypeError, ValueError):
            pass
        # Some gateways only populate Error.Code with the numeric status.
        try:
            return int(exc.response.get("Error", {}).get("Code", ""))
        except (AttributeError, TypeError, ValueError):
            return None

    def _retry(self, op_desc: str, fn: Callable):
        """Run ``fn`` with backoff on transient RunPod/Cloudflare failures.

        Auth errors are never retried; non-retryable client errors surface
        immediately. Used for each multipart part so one slow part (524) does
        not restart the whole upload."""
        delay = _RETRY_BASE_DELAY
        for attempt in range(1, _MAX_PART_ATTEMPTS + 1):
            try:
                return fn()
            except ClientError as exc:
                if self._is_auth_error(exc):
                    raise self._auth_or_generic(exc) from exc
                status = self._http_status(exc)
                if attempt >= _MAX_PART_ATTEMPTS or status not in _RETRYABLE_STATUS:
                    raise RemoteStorageError(f"{op_desc} failed: {exc}") from exc
                logger.warning(
                    "%s: HTTP %s (attempt %d/%d), retrying in %.0fs",
                    op_desc, status, attempt, _MAX_PART_ATTEMPTS, delay,
                )
            except (EndpointConnectionError, ConnectionClosedError, ReadTimeoutError) as exc:
                if attempt >= _MAX_PART_ATTEMPTS:
                    raise RemoteStorageError(f"{op_desc} failed: {exc}") from exc
                logger.warning(
                    "%s: %s (attempt %d/%d), retrying in %.0fs",
                    op_desc, type(exc).__name__, attempt, _MAX_PART_ATTEMPTS, delay,
                )
            time.sleep(delay)
            delay = min(delay * 2, _RETRY_MAX_DELAY)

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
