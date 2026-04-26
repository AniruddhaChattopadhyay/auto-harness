"""
core/storage.py — MinIO object-storage client wrapper.

All functions are synchronous; the MinIO Python SDK is sync-only.
FastAPI routes should call these via ``asyncio.to_thread(put_object, ...)``
to avoid blocking the event loop. Worker code can call them directly.
"""

from __future__ import annotations

import io
from typing import Iterator

from minio import Minio
from minio.error import S3Error

from core.config import get_settings

_client: Minio | None = None


def _get_client() -> Minio:
    global _client
    if _client is None:
        s = get_settings()
        _client = Minio(
            endpoint=s.minio_endpoint,
            access_key=s.minio_access_key,
            secret_key=s.minio_secret_key,
            secure=s.minio_use_ssl,
        )
    return _client


def _bucket() -> str:
    return get_settings().minio_bucket


def ensure_bucket() -> None:
    """Create the configured bucket if it does not already exist. Idempotent."""
    client = _get_client()
    bucket = _bucket()
    if not client.bucket_exists(bucket):
        client.make_bucket(bucket)


# ── Core operations ────────────────────────────────────────────────────────────


def put_object(
    uri: str,
    data: bytes | str,
    content_type: str = "application/octet-stream",
) -> None:
    """
    Upload *data* to *uri* within the configured bucket.

    ``uri`` is a path-within-bucket, e.g. ``"experiments/abc/agents/v0.py"``.
    Strings are encoded as UTF-8.
    """
    if isinstance(data, str):
        raw = data.encode("utf-8")
    else:
        raw = data

    client = _get_client()
    client.put_object(
        bucket_name=_bucket(),
        object_name=uri,
        data=io.BytesIO(raw),
        length=len(raw),
        content_type=content_type,
    )


def get_object(uri: str) -> bytes:
    """Download and return the raw bytes for *uri*."""
    client = _get_client()
    response = client.get_object(bucket_name=_bucket(), object_name=uri)
    try:
        return response.read()
    finally:
        response.close()
        response.release_conn()


def get_object_text(uri: str) -> str:
    """Download *uri* and decode it as UTF-8 text."""
    return get_object(uri).decode("utf-8")


def object_exists(uri: str) -> bool:
    """Return True if the object at *uri* exists."""
    client = _get_client()
    try:
        client.stat_object(bucket_name=_bucket(), object_name=uri)
        return True
    except S3Error as exc:
        if exc.code == "NoSuchKey":
            return False
        raise


def list_prefix(prefix: str) -> Iterator[str]:
    """
    Yield all object keys beneath *prefix*.

    Equivalent to ``aws s3 ls s3://<bucket>/<prefix> --recursive``.
    """
    client = _get_client()
    objects = client.list_objects(
        bucket_name=_bucket(),
        prefix=prefix,
        recursive=True,
    )
    for obj in objects:
        yield obj.object_name  # type: ignore[union-attr]


def delete_prefix(prefix: str) -> None:
    """
    Delete all objects whose key begins with *prefix*.

    Used for cascade-deleting an experiment's workspace from MinIO after the
    Postgres rows have already been removed.
    """
    client = _get_client()
    keys = list(list_prefix(prefix))
    if not keys:
        return

    from minio.deleteobjects import DeleteObject

    delete_objects = [DeleteObject(k) for k in keys]
    errors = list(
        client.remove_objects(bucket_name=_bucket(), delete_object_list=delete_objects)
    )
    if errors:
        msgs = "; ".join(str(e) for e in errors)
        raise RuntimeError(f"MinIO delete_prefix errors: {msgs}")
