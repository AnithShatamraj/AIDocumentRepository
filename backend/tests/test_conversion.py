"""Format support: which files are accepted, and the in-process image -> PDF path.

Office conversion goes through the Gotenberg service and is exercised end to end
against the running stack, not here.
"""
from __future__ import annotations

import io

import pytest
from PIL import Image

from app.services import conversion, mime

MB = 1024 * 1024


def _img(fmt: str, size=(300, 200), mode="RGB", **kw) -> bytes:
    buf = io.BytesIO()
    Image.new(mode, size, "white").save(buf, fmt, **kw)
    return buf.getvalue()


@pytest.mark.parametrize("name,fmt", [
    ("a.png", "PNG"), ("a.jpg", "JPEG"), ("a.jpeg", "JPEG"), ("a.tif", "TIFF"), ("a.tiff", "TIFF"),
    ("a.bmp", "BMP"), ("a.gif", "GIF"), ("a.webp", "WEBP"),
])
def test_images_are_accepted_by_content(name, fmt):
    ext, _ = mime.validate(name, _img(fmt), 10 * MB)
    assert ext == name.split(".")[-1]
    assert conversion.needs_conversion(ext)


def test_an_image_with_the_wrong_extension_is_refused():
    with pytest.raises(mime.UploadValidationError):
        mime.validate("a.gif", _img("PNG"), 10 * MB)


def test_legacy_office_needs_the_ole_signature():
    ole = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 64
    assert mime.validate("old.doc", ole, MB)[0] == "doc"
    assert mime.validate("old.ppt", ole, MB)[0] == "ppt"
    with pytest.raises(mime.UploadValidationError):
        mime.validate("old.doc", b"PK\x03\x04zip", MB)
    with pytest.raises(mime.UploadValidationError):
        mime.validate("new.docx", ole, MB)


def test_rtf_and_opendocument():
    assert mime.validate("a.rtf", rb"{\rtf1\ansi hello}", MB)[0] == "rtf"
    assert mime.validate("a.odt", b"PK\x03\x04rest", MB)[0] == "odt"
    with pytest.raises(mime.UploadValidationError):
        mime.validate("a.rtf", b"plain text", MB)


def test_pdf_xlsx_csv_txt_are_not_converted():
    assert not any(conversion.needs_conversion(e) for e in ("pdf", "xlsx", "csv", "txt"))


def test_image_becomes_a_pdf():
    pdf = conversion.to_pdf(_img("PNG"), "png")
    assert pdf.startswith(b"%PDF-") and b"%%EOF" in pdf[-1024:]


def test_multipage_tiff_keeps_every_page():
    frames = [Image.new("RGB", (200, 100), c) for c in ("white", "grey", "black")]
    buf = io.BytesIO()
    frames[0].save(buf, "TIFF", save_all=True, append_images=frames[1:])
    import pypdfium2 as pdfium

    assert len(pdfium.PdfDocument(conversion.to_pdf(buf.getvalue(), "tiff"))) == 3


@pytest.mark.parametrize("mode", ["RGBA", "LA", "P", "1", "L", "CMYK", "I;16"])
def test_unusual_colour_modes_convert(mode):
    im = Image.new(mode, (50, 50))
    buf = io.BytesIO()
    im.save(buf, "TIFF" if mode in ("CMYK", "I;16") else "PNG")
    ext = "tiff" if mode in ("CMYK", "I;16") else "png"
    assert conversion.to_pdf(buf.getvalue(), ext).startswith(b"%PDF-")


def test_a_corrupt_image_is_a_conversion_error():
    with pytest.raises(conversion.ConversionError):
        conversion.to_pdf(b"definitely not a png", "png")


def test_oversized_images_are_refused(monkeypatch):
    monkeypatch.setattr(conversion.settings, "max_image_pixels", 1000)
    with pytest.raises(conversion.ConversionError):
        conversion.to_pdf(_img("PNG", size=(500, 500)), "png")
