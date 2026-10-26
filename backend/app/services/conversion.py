"""Turn Office documents and images into a PDF so one pipeline reads everything.

Only permissively licensed, enterprise-safe pieces are used:
  - Office files (Word, PowerPoint, OpenDocument, RTF) go to Gotenberg (MIT), an HTTP
    wrapper around LibreOffice (MPL-2.0), run as its own service.
  - Images are wrapped into a PDF in-process with Pillow (HPND); a multi-page TIFF
    becomes a multi-page PDF.

The original file is never replaced. The PDF is a *rendition*: it is what gets parsed
(layout, boxes, OCR) and shown in the viewer, so a click on an extracted value
highlights the right spot on a page the user can see.
"""
from __future__ import annotations

import io

import httpx

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

OFFICE = {"docx", "doc", "pptx", "ppt", "odt", "odp", "rtf"}
IMAGES = {"png", "jpg", "jpeg", "tif", "tiff", "bmp", "gif", "webp"}
CONVERTIBLE = OFFICE | IMAGES

_MAX_IMAGE_FRAMES = 200
_DEFAULT_DPI = 150


class ConversionError(RuntimeError):
    """The file could not be turned into a PDF (corrupt, too large, converter down)."""


def needs_conversion(ext: str) -> bool:
    return ext in CONVERTIBLE


def to_pdf(data: bytes, ext: str, filename: str = "") -> bytes:
    """Return the PDF rendition of `data`, or raise ConversionError."""
    if ext in OFFICE:
        return _office_to_pdf(data, ext, filename)
    if ext in IMAGES:
        return _image_to_pdf(data)
    raise ConversionError(f"No converter for .{ext}")


# ------------------------------------------------------------------- office
def _office_to_pdf(data: bytes, ext: str, filename: str) -> bytes:
    url = settings.gotenberg_url.rstrip("/") + "/forms/libreoffice/convert"
    # LibreOffice picks its import filter from the extension, so it must be right.
    name = f"document.{ext}"
    try:
        resp = httpx.post(
            url, files={"files": (name, data)},
            data={"nativePdfFormats": "false"},
            timeout=settings.conversion_timeout_seconds)
    except httpx.HTTPError as e:
        raise ConversionError(f"Document converter is unreachable: {e}") from e
    if resp.status_code != 200:
        raise ConversionError(
            f"Document converter rejected the file (HTTP {resp.status_code}): {resp.text[:200]}")
    pdf = resp.content
    if not pdf.startswith(b"%PDF-"):
        raise ConversionError("Document converter did not return a PDF")
    log.info("office_converted", ext=ext, source_bytes=len(data), pdf_bytes=len(pdf), name=filename)
    return pdf


# ------------------------------------------------------------------- images
def _image_to_pdf(data: bytes) -> bytes:
    from PIL import Image, ImageOps, UnidentifiedImageError

    Image.MAX_IMAGE_PIXELS = settings.max_image_pixels  # decompression-bomb guard
    try:
        src = Image.open(io.BytesIO(data))
        frames = getattr(src, "n_frames", 1)
        if frames > _MAX_IMAGE_FRAMES:
            raise ConversionError(f"Image has {frames} pages; the limit is {_MAX_IMAGE_FRAMES}")
        pages = []
        for i in range(frames):
            src.seek(i)
            pages.append(_page(src.copy(), ImageOps))
    except ConversionError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as e:
        raise ConversionError(f"Image could not be read: {e}") from e

    buf = io.BytesIO()
    first, rest = pages[0][0], [p[0] for p in pages[1:]]
    first.save(buf, format="PDF", save_all=bool(rest), append_images=rest,
               resolution=pages[0][1], quality=95)
    return buf.getvalue()


def _page(im, ImageOps) -> tuple[object, float]:
    """One frame as a PDF-safe RGB/greyscale image, plus the resolution to place it at."""
    from PIL import Image

    dpi = im.info.get("dpi")
    res = float(dpi[0]) if dpi and 72 <= dpi[0] <= 1200 else float(_DEFAULT_DPI)
    im = ImageOps.exif_transpose(im)  # phone photos: honour the rotation tag
    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        rgba = im.convert("RGBA")
        flat = Image.new("RGB", rgba.size, "white")  # transparency on a white page
        flat.paste(rgba, mask=rgba.getchannel("A"))
        return flat, res
    if im.mode in ("L", "RGB"):
        return im, res
    if im.mode == "1":
        return im.convert("L"), res
    if im.mode.startswith("I;16") or im.mode == "I":
        return im.point(lambda v: v / 256).convert("L"), res  # 16-bit scans -> 8-bit
    return im.convert("RGB"), res
