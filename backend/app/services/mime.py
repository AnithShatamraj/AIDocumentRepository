"""Upload validation: format allow-list + magic-byte sniffing (don't trust extensions)."""
from __future__ import annotations

import os

# extension -> canonical mime
ALLOWED = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "doc": "application/msword",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "ppt": "application/vnd.ms-powerpoint",
    "odt": "application/vnd.oasis.opendocument.text",
    "odp": "application/vnd.oasis.opendocument.presentation",
    "rtf": "application/rtf",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "csv": "text/csv",
    "txt": "text/plain",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "tif": "image/tiff",
    "tiff": "image/tiff",
    "bmp": "image/bmp",
    "gif": "image/gif",
    "webp": "image/webp",
}

_ZIP_CONTAINERS = {"docx", "pptx", "xlsx", "odt", "odp"}
_OLE_CONTAINERS = {"doc", "ppt"}  # legacy binary Office (OLE2 compound file)


class UploadValidationError(ValueError):
    pass


def ext_of(filename: str) -> str:
    return os.path.splitext(filename)[1].lower().lstrip(".")


def sniff(data: bytes) -> str:
    """Return a coarse detected family from magic bytes."""
    if data[:5] == b"%PDF-":
        return "pdf"
    if data[:4] == b"PK\x03\x04":
        return "zip"  # ooxml / opendocument container
    if data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return "ole"  # legacy doc/ppt/xls
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:4] in (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+"):
        return "tiff"
    if data[:2] == b"BM":
        return "bmp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:5] == b"{\\rtf":
        return "rtf"
    return "text"


# Extensions that must carry a specific binary signature.
_EXPECTED_FAMILY = {
    "png": "png", "jpg": "jpeg", "jpeg": "jpeg", "tif": "tiff", "tiff": "tiff",
    "bmp": "bmp", "gif": "gif", "webp": "webp", "rtf": "rtf",
}
_LABEL = {"png": "PNG", "jpeg": "JPEG", "tiff": "TIFF", "bmp": "BMP", "gif": "GIF",
          "webp": "WebP", "rtf": "RTF"}


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
    if ext in _ZIP_CONTAINERS and family != "zip":
        raise UploadValidationError(f"File content is not a valid .{ext}")
    if ext in _OLE_CONTAINERS and family != "ole":
        raise UploadValidationError(f"File content is not a valid .{ext}")
    want = _EXPECTED_FAMILY.get(ext)
    if want and family != want:
        raise UploadValidationError(f"File content is not a valid {_LABEL[want]}")
    return ext, ALLOWED[ext]
