"""Lớp tương thích; dịch vụ tải PDF dùng chung nằm tại ``app.services.pdf_downloader``."""

from app.services.pdf_downloader import (
    DownloadedPdf,
    PdfDownloadError,
    PdfUnavailableError,
    Resolver,
    UnsafePdfUrl,
    download_pdf,
    validate_public_https_url,
)

__all__ = [
    "DownloadedPdf",
    "PdfDownloadError",
    "PdfUnavailableError",
    "Resolver",
    "UnsafePdfUrl",
    "download_pdf",
    "validate_public_https_url",
]
