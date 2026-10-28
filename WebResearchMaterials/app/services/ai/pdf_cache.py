"""Cache PDF theo nội dung và phiên bản pipeline, không lưu lặp trong PostgreSQL."""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from app.services.ai.pdf_reader import PdfPage, create_chunks, extract_pdf_pages
from app.services.ai.schemas import DoanPdf


PDF_READER_VERSION = "pymupdf-text-v1"
CHUNK_VERSION = "page-1800-250-v1"


@dataclass(slots=True)
class CachedPdf:
    pages: list[PdfPage]
    chunks: list[DoanPdf]
    key: str
    hit: bool
    read_seconds: float
    chunk_seconds: float


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def pdf_cache_key(content: bytes) -> str:
    digest = hashlib.sha256(content).hexdigest()
    return hashlib.sha256(f"{digest}:{PDF_READER_VERSION}:{CHUNK_VERSION}".encode()).hexdigest()


def cached_pdf(content: bytes, cache_dir: Path) -> CachedPdf:
    key = pdf_cache_key(content)
    path = cache_dir / f"{key}.json"
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("key") == key:
                return CachedPdf(
                    [PdfPage(**item) for item in data["pages"]],
                    [DoanPdf.model_validate(item) for item in data["chunks"]],
                    key, True, 0.0, 0.0,
                )
        except (OSError, ValueError, KeyError, TypeError):
            pass
    start = time.perf_counter()
    pages = extract_pdf_pages(content)
    read_seconds = time.perf_counter() - start
    start = time.perf_counter()
    chunks = create_chunks(pages)
    chunk_seconds = time.perf_counter() - start
    _write_json(path, {
        "key": key,
        "pages": [{"page_number": item.page_number, "text": item.text} for item in pages],
        "chunks": [item.model_dump() for item in chunks],
    })
    return CachedPdf(pages, chunks, key, False, read_seconds, chunk_seconds)


def cached_phrase_locations(pdf: CachedPdf, phrases: list[str], cache_dir: Path) -> dict[str, list[int]]:
    normalized = sorted({phrase.strip().casefold() for phrase in phrases if phrase.strip()})
    phrase_key = hashlib.sha256(json.dumps(normalized, ensure_ascii=False).encode()).hexdigest()
    path = cache_dir / f"{pdf.key}-{phrase_key}-phrases.json"
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if set(data) == set(normalized):
                return data
        except (OSError, ValueError, TypeError):
            pass
    result = {
        phrase: [page.page_number for page in pdf.pages if phrase in page.text.casefold()]
        for phrase in normalized
    }
    _write_json(path, result)
    return result
