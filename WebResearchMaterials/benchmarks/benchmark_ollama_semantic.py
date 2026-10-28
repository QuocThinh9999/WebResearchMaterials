"""Kiểm tra một ca ngữ cảnh còn mơ hồ bằng Ollama đã cài."""

from pathlib import Path
from tempfile import TemporaryDirectory

from benchmarks.benchmark_ai_lookup import MeasuredGateway
from app.services.ai.pdf_cache import CachedPdf
from app.services.ai.pdf_reader import PdfPage, create_chunks
from app.services.ai.semantic_check import check_constructs


def main():
    pages = [PdfPage(3, "Hypotheses\nWe predict that Customer Satisfaction increases customer loyalty in this study.")]
    pdf = CachedPdf(pages, create_chunks(pages), "semantic-live-benchmark", False, 0, 0)
    gateway = MeasuredGateway()
    with TemporaryDirectory(prefix="semantic-live-") as directory:
        result = check_constructs(pdf, ["Customer Satisfaction"], Path(directory), gateway)
    print(result.model_dump_json(), flush=True)
    print(gateway.calls, flush=True)
    gateway.close()


if __name__ == "__main__":
    main()
