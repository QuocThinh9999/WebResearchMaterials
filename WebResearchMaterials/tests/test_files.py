import pytest

from app.services.files import InvalidUpload, detected_image_mime, validate_file


def test_accept_valid_pdf():
    assert validate_file("bai-bao.pdf", "application/pdf", b"%PDF-1.7\n...", "pdf", 1024) == "application/pdf"


def test_reject_pdf_with_wrong_signature():
    with pytest.raises(InvalidUpload):
        validate_file("bai-bao.pdf", "application/pdf", b"khong-phai-pdf", "pdf", 1024)


def test_image_signatures():
    assert detected_image_mime(b"\x89PNG\r\n\x1a\nabc") == "image/png"
    assert detected_image_mime(b"\xff\xd8\xffabc") == "image/jpeg"
    with pytest.raises(InvalidUpload):
        validate_file("anh.png", "image/png", b"GIF89a...", "image", 1024)

