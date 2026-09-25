"""Authorized OpenWebUI upload resolution for the Parqet trading tool.

The public tool argument is deliberately an opaque OpenWebUI file UUID.  A
caller can never supply a server path: the resolver first proves that the UUID
is attached to the current tool invocation, then asks OpenWebUI for the file
record and enforces its normal file-access policy.  Only a readable local copy
inside OpenWebUI's upload directory reaches the importer.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


ALLOWED_CSV_CONTENT_TYPES = {
    "text/csv",
    "application/csv",
    "application/vnd.ms-excel",
}


class UploadResolutionError(ValueError):
    """A safe, user-facing failure to resolve an OpenWebUI uploaded file."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class AuthorizedUpload:
    """A local file that OpenWebUI has authorized for this invocation."""

    file_id: str
    filename: str
    content_type: str | None
    path: Path


def _canonical_file_id(value: str) -> str:
    """Accept only canonical UUIDs, never paths, URLs, or arbitrary strings."""
    if not isinstance(value, str):
        raise UploadResolutionError("INVALID_UPLOAD_REFERENCE", "uploaded_file_id must be an OpenWebUI file UUID")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise UploadResolutionError("INVALID_UPLOAD_REFERENCE", "uploaded_file_id must be an OpenWebUI file UUID") from exc
    if str(parsed) != value.lower():
        raise UploadResolutionError("INVALID_UPLOAD_REFERENCE", "uploaded_file_id must be a canonical OpenWebUI file UUID")
    return str(parsed)


def _attachment_ids(attachments: Iterable[Any] | None) -> set[str]:
    """Extract only IDs from OpenWebUI's injected ``__files__`` metadata."""
    ids: set[str] = set()
    for attachment in attachments or ():
        if not isinstance(attachment, dict):
            continue
        if attachment.get("type", "file") != "file":
            continue
        candidates: list[Any] = [attachment.get("id")]
        nested = attachment.get("file")
        if isinstance(nested, dict):
            candidates.append(nested.get("id"))
        for candidate in candidates:
            if not isinstance(candidate, str):
                continue
            try:
                ids.add(_canonical_file_id(candidate))
            except UploadResolutionError:
                continue
    return ids


def require_attached_upload(uploaded_file_id: str, attachments: Iterable[Any] | None) -> str:
    """Return an attached UUID or reject the request before touching storage."""
    file_id = _canonical_file_id(uploaded_file_id)
    if file_id not in _attachment_ids(attachments):
        raise UploadResolutionError(
            "UPLOAD_NOT_ATTACHED",
            "uploaded_file_id is not attached to this OpenWebUI tool invocation",
        )
    return file_id


def validate_csv_upload_path(
    *,
    path: str | Path,
    upload_dir: str | Path,
    filename: str,
    content_type: str | None,
) -> Path:
    """Validate a runtime-resolved local upload without exposing its path.

    ``Storage.get_file`` may materialize object storage downloads beneath
    ``UPLOAD_DIR``.  Resolving both paths rejects traversal and symlinks that
    leave this controlled directory.
    """
    if Path(filename).suffix.casefold() != ".csv":
        raise UploadResolutionError("UNSUPPORTED_FILE_TYPE", "only .csv uploads are accepted")
    if content_type and content_type.casefold() not in ALLOWED_CSV_CONTENT_TYPES:
        raise UploadResolutionError("UNSUPPORTED_FILE_TYPE", "uploaded file is not declared as CSV")

    try:
        upload_root = Path(upload_dir).resolve(strict=True)
        candidate = Path(path).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise UploadResolutionError("UPLOAD_UNAVAILABLE", "uploaded file is no longer available") from exc
    try:
        candidate.relative_to(upload_root)
    except ValueError as exc:
        raise UploadResolutionError("UPLOAD_OUT_OF_SCOPE", "uploaded file resolved outside OpenWebUI upload storage") from exc
    if not candidate.is_file() or not os.access(candidate, os.R_OK):
        raise UploadResolutionError("UPLOAD_UNAVAILABLE", "uploaded file is not readable")

    try:
        with candidate.open("rb") as handle:
            sample = handle.read(16 * 1024)
        text = sample.decode("utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise UploadResolutionError("INVALID_CSV", "uploaded CSV must be readable UTF-8 text") from exc
    header = text.splitlines()[0] if text.splitlines() else ""
    if ";" not in header or not {"type", "identifier"}.issubset(
        {part.strip().casefold() for part in header.split(";")}
    ):
        raise UploadResolutionError("INVALID_CSV", "uploaded file is not a supported Parqet CSV")
    return candidate


async def resolve_openwebui_upload(
    uploaded_file_id: str,
    *,
    attachments: Iterable[Any] | None,
    user: dict[str, Any] | None,
) -> AuthorizedUpload:
    """Resolve an attached, authorized OpenWebUI CSV to a trusted local path.

    Imports are intentionally local to this function: repository tests do not
    need an OpenWebUI installation, while the deployed tool uses the runtime's
    own file records, storage backend, and access-control implementation.
    """
    file_id = require_attached_upload(uploaded_file_id, attachments)
    user_id = (user or {}).get("id")
    if not isinstance(user_id, str) or not user_id:
        raise UploadResolutionError("UNAUTHORIZED_UPLOAD", "OpenWebUI user context is required")

    try:
        from open_webui.config import UPLOAD_DIR
        from open_webui.models.files import Files
        from open_webui.models.users import Users
        from open_webui.storage.provider import Storage
        from open_webui.utils.access_control.files import has_access_to_file
    except ImportError as exc:  # pragma: no cover - deployment configuration error
        raise UploadResolutionError("UPLOAD_RESOLVER_UNAVAILABLE", "OpenWebUI upload services are unavailable") from exc

    file_record = await Files.get_file_by_id(file_id)
    if file_record is None:
        raise UploadResolutionError("UPLOAD_NOT_FOUND", "uploaded file was not found")

    # The attachment check limits the invocation scope.  This second check
    # preserves OpenWebUI's ownership/shared-resource permission semantics.
    if file_record.user_id != user_id and (user or {}).get("role") != "admin":
        runtime_user = await Users.get_user_by_id(user_id)
        if runtime_user is None or not await has_access_to_file(file_id, "read", runtime_user):
            raise UploadResolutionError("UNAUTHORIZED_UPLOAD", "you do not have access to this uploaded file")

    if not file_record.path:
        raise UploadResolutionError("UPLOAD_UNAVAILABLE", "uploaded file has no storage path")
    try:
        materialized = await asyncio.to_thread(Storage.get_file, file_record.path)
    except Exception as exc:
        raise UploadResolutionError("UPLOAD_UNAVAILABLE", "unable to read uploaded file from OpenWebUI storage") from exc

    meta = file_record.meta if isinstance(file_record.meta, dict) else {}
    content_type = meta.get("content_type") if isinstance(meta.get("content_type"), str) else None
    path = validate_csv_upload_path(
        path=materialized,
        upload_dir=UPLOAD_DIR,
        filename=file_record.filename,
        content_type=content_type,
    )
    return AuthorizedUpload(file_id=file_id, filename=file_record.filename, content_type=content_type, path=path)
