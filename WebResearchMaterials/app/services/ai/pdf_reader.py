from __future__ import annotations

import math
import re
from dataclasses import dataclass

from app.services.ai.client import AIGateway
from app.services.ai.schemas import DieuKienNguyenTu, DoanPdf


class PdfReadError(RuntimeError):
    pass


@dataclass(slots=True)
class PdfPage:
    page_number: int
    text: str


def extract_pdf_pages(content: bytes) -> list[PdfPage]:
    try:
        import pymupdf
    except ImportError as exc:
        raise PdfReadError("Chưa cài PyMuPDF để đọc nội dung PDF.") from exc
    try:
        document = pymupdf.open(stream=content, filetype="pdf")
        pages = []
        for index, page in enumerate(document):
            raw = page.get_text("text") or ""
            paragraphs = [re.sub(r"[ \t]+", " ", value).strip() for value in re.split(r"\n\s*\n", raw)]
            pages.append(PdfPage(index + 1, "\n\n".join(value for value in paragraphs if value)))
        document.close()
        return pages
    except Exception as exc:
        raise PdfReadError("Không thể đọc tệp PDF hợp lệ.") from exc


def create_chunks(pages: list[PdfPage], chunk_size: int = 1800, overlap: int = 250) -> list[DoanPdf]:
    if chunk_size <= 0 or overlap < 0 or overlap >= chunk_size:
        raise ValueError("Kích thước và phần chồng lấn của đoạn PDF không hợp lệ.")
    result: list[DoanPdf] = []
    step = chunk_size - overlap
    for page in pages:
        text = page.text.strip()
        for start in range(0, len(text), step):
            chunk = text[start : start + chunk_size].strip()
            if chunk:
                result.append(DoanPdf(trang=page.page_number, noi_dung=chunk))
            if start + chunk_size >= len(text):
                break
    return result


def _cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        return -1
    denominator = math.sqrt(sum(value * value for value in left)) * math.sqrt(
        sum(value * value for value in right)
    )
    return sum(a * b for a, b in zip(left, right, strict=True)) / denominator if denominator else -1


def select_top_chunks(
    query: str,
    chunks: list[DoanPdf],
    gateway: AIGateway,
    top_n: int,
) -> list[DoanPdf]:
    if not chunks or top_n <= 0:
        return []
    vectors = gateway.embeddings([query, *(chunk.noi_dung for chunk in chunks)])
    if len(vectors) != len(chunks) + 1:
        raise PdfReadError("Không nhận đủ embedding để chọn nội dung PDF.")
    ranked = sorted(
        enumerate(chunks),
        key=lambda item: (_cosine(vectors[0], vectors[item[0] + 1]), -item[0]),
        reverse=True,
    )
    return [chunk for _, chunk in ranked[:top_n]]


_PRIORITY_HEADINGS = (
    "literature review",
    "theoretical background",
    "hypotheses",
    "conceptual framework",
    "research model",
    "methodology",
    "measures",
    "data analysis",
    "results",
    "discussion",
)


def select_chunks_for_conditions(
    conditions: list[DieuKienNguyenTu],
    chunks: list[DoanPdf],
    gateway: AIGateway,
    top_n_per_condition: int,
) -> list[DoanPdf]:
    if not chunks or top_n_per_condition <= 0:
        return []
    queries = [
        "\n".join([condition.noi_dung, *condition.truy_van_tim_bang_chung]).strip()
        for condition in conditions
    ]
    queries = [value for value in queries if value]
    if not queries:
        return []
    vectors = gateway.embeddings([*queries, *(chunk.noi_dung for chunk in chunks)])
    if len(vectors) != len(queries) + len(chunks):
        raise PdfReadError("Không nhận đủ embedding để chọn nội dung PDF theo từng điều kiện.")
    chunk_vectors = vectors[len(queries) :]
    selected_indexes: set[int] = set()
    for query_index, query_vector in enumerate(vectors[: len(queries)]):
        ranked = sorted(
            range(len(chunks)),
            key=lambda index: (
                _cosine(query_vector, chunk_vectors[index])
                + (0.08 if any(heading in chunks[index].noi_dung.casefold() for heading in _PRIORITY_HEADINGS) else 0),
                -index,
            ),
            reverse=True,
        )
        for index in ranked[:top_n_per_condition]:
            selected_indexes.add(index)
            for neighbor in (index - 1, index + 1):
                if 0 <= neighbor < len(chunks) and abs(chunks[neighbor].trang - chunks[index].trang) <= 1:
                    selected_indexes.add(neighbor)
    return [chunks[index] for index in sorted(selected_indexes)]
