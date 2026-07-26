"""Upload validation: format allow-list + magic-byte sniffing (don't trust extensions)."""
from __future__ import annotations

import os

# extension -> canonical mime
ALLOWED = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "csv": "text/csv",
    "txt": "text/plain",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "tif": "image/tiff",
    "tiff": "image/tiff",
}

_ZIP_OOXML = {"docx", "pptx", "xlsx"}


class UploadValidationError(ValueError):
    pass


def ext_of(filename: str) -> str:
    return os.path.splitext(filename)[1].lower().lstrip(".")


def sniff(data: bytes) -> str:
    """Return a coarse detected family from magic bytes."""
    if data[:5] == b"%PDF-":
        return "pdf"
    if data[:4] == b"PK\x03\x04":
        return "zip"  # ooxml container
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:2] in (b"II", b"MM"):
        return "tiff"
    return "text"


def validate(filename: str, data: bytes, max_bytes: int) -> tuple[str, str]:
    """Return (extension, mime) or raise UploadValidationError."""
    if len(data) == 0:
        raise UploadValidationError("Empty file")
    if len(data) > max_bytes:
        raise UploadValidationError(f"File exceeds max size of {max_bytes // (1024 * 1024)} MB")
    ext = ext_of(filename)
    if ext not in ALLOWED:
        raise UploadValidationError(f"Unsupported file type: .{ext}")

    family = sniff(data)
    # Cross-check magic bytes against the claimed extension.
    if ext == "pdf" and family != "pdf":
        raise UploadValidationError("File content is not a valid PDF")
    if ext == "pdf" and b"%%EOF" not in data[-2048:]:
        # A structurally complete PDF ends with an %%EOF trailer; its absence
        # almost always means a truncated/interrupted upload.
        raise UploadValidationError(
            "PDF appears truncated (missing %%EOF trailer) — re-upload the complete file")
    if ext in _ZIP_OOXML and family != "zip":
        raise UploadValidationError(f"File content is not a valid .{ext}")
    if ext == "png" and family != "png":
        raise UploadValidationError("File content is not a valid PNG")
    if ext in ("jpg", "jpeg") and family != "jpeg":
        raise UploadValidationError("File content is not a valid JPEG")
    return ext, ALLOWED[ext]
