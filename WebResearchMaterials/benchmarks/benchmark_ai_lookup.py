"""Đo luồng tra cứu trên PDF tổng hợp, không truy cập cơ sở dữ liệu thật."""

from __future__ import annotations

import json
import math
import statistics
import sys
import tempfile
import time
from pathlib import Path

import pymupdf

from app.models import TaiLieu
from app.services.ai.client import OllamaGateway
from app.services.ai.evaluator import evaluate_document
from app.services.ai.hard_rules import evaluate_hard_rules
from app.services.ai.intent_parser import analyze_ai_request
from app.services.ai.pdf_reader import create_chunks, extract_pdf_pages, select_chunks_for_conditions
from app.services.ai.pdf_cache import cached_pdf
from app.services.ai.semantic_check import cached_decision, check_constructs


class MeasuredGateway(OllamaGateway):
    def __init__(self):
        from types import SimpleNamespace

        super().__init__(SimpleNamespace(
            ollama_base_url="http://127.0.0.1:11435",
            ollama_model="qwen3:4b",
            ollama_embedding_model="qwen3-embedding:0.6b",
            ollama_timeout_seconds=300,
        ))
        self.calls = []

    def _post(self, path, payload):
        start = time.perf_counter()
        response = super()._post(path, payload)
        self.calls.append({
            "endpoint": path,
            "seconds": round(time.perf_counter() - start, 3),
            **{key: response.get(key) for key in (
                "load_duration", "prompt_eval_duration", "eval_duration",
                "prompt_eval_count", "eval_count",
            ) if key in response},
            "message": (response.get("message") or {}).get("content", "")[:2000],
        })
        return response


def sample_pdf() -> bytes:
    document = pymupdf.open()
    for index in range(6):
        page = document.new_page()
        section = "Conceptual Framework" if index == 3 else "Methods" if index == 4 else "Introduction"
        detail = (
            "Customer Satisfaction is a dependent variable in our research model. "
            "Hypothesis H2 tests its relationship with service quality."
            if index == 3 else "This study investigates banking service quality. " * 15
        )
        page.insert_textbox(pymupdf.Rect(50, 50, 540, 760), f"{section}\n\n{detail}", fontsize=11)
    content = document.tobytes()
    document.close()
    return content


def run_case(gateway, request, publisher, content, mode, cache_dir):
    timings = {}
    start = time.perf_counter()
    phase = start
    intent = analyze_ai_request(request, gateway)
    timings["parse_s"] = time.perf_counter() - phase
    document = TaiLieu(tieu_de="Banking study", tac_gia=[], tu_khoa=[], nha_xuat_ban=publisher, ma_ngoai={}, du_lieu_nguon={})
    rules = evaluate_hard_rules(document, intent)
    if rules.violations:
        label = "khong_phu_hop"
    elif mode == "after" and intent.input_language and not intent.needs_semantic_pdf_check:
        label = "phu_hop" if not rules.unknown else "chua_danh_gia"
    elif mode == "after" and intent.required_constructs_in_model:
        phase = time.perf_counter()
        decision = cached_decision(content, intent.required_constructs_in_model, cache_dir, gateway.model)
        pdf = None if decision else cached_pdf(content, cache_dir)
        timings["cache_hit"] = pdf is None or pdf.hit
        timings["pdf_s"] = pdf.read_seconds if pdf else 0
        timings["chunk_s"] = pdf.chunk_seconds if pdf else 0
        timings["cache_access_s"] = time.perf_counter() - phase
        phase = time.perf_counter()
        if decision is None:
            decision = check_constructs(pdf, intent.required_constructs_in_model, cache_dir, gateway)
        timings["decision"] = decision.model_dump()
        timings["llm_s"] = time.perf_counter() - phase if gateway.call_count else 0
        timings["embedding_s"] = 0
        label = {"matched": "phu_hop", "unmatched": "khong_phu_hop", "insufficient": "chua_danh_gia"}[decision.status]
    else:
        phase = time.perf_counter()
        pages = extract_pdf_pages(content)
        timings["pdf_s"] = time.perf_counter() - phase
        phase = time.perf_counter()
        chunks = create_chunks(pages)
        timings["chunk_s"] = time.perf_counter() - phase
        phase = time.perf_counter()
        selected = select_chunks_for_conditions(intent.dieu_kien_nguyen_tu, chunks, gateway, 4)
        timings["embedding_s"] = time.perf_counter() - phase
        phase = time.perf_counter()
        result = evaluate_document(request, intent, document, selected, gateway, 70)
        timings["llm_s"] = time.perf_counter() - phase
        label = result.nhan
    timings["total_s"] = time.perf_counter() - start
    timings["label"] = label
    timings["ollama_calls"] = len(gateway.calls)
    timings["calls"] = gateway.calls
    return timings


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "after"
    run_count = int(sys.argv[2]) if len(sys.argv) > 2 else 2
    content = sample_pdf()
    cases = [
        ("Không lấy từ các nhà xuất bản MDPI", "MDPI AG"),
        ("Trong mô hình phải có Customer Satisfaction", "Elsevier"),
    ]
    results = []
    with tempfile.TemporaryDirectory(prefix="ai-benchmark-") as temporary:
        for request, publisher in cases:
            for run in range(run_count):
                gateway = MeasuredGateway()
                result = run_case(gateway, request, publisher, content, mode, Path(temporary))
                gateway.close()
                result.update({"request": request, "run": "cold" if run == 0 else "warm"})
                results.append(result)
                if run_count <= 2:
                    print(json.dumps(result, ensure_ascii=False), flush=True)
    totals = [item["total_s"] for item in results]
    report = {"cases": results, "median_s": statistics.median(totals), "p95_s": sorted(totals)[math.ceil(0.95 * len(totals)) - 1], "documents_per_minute": round(60 * len(results) / sum(totals), 2)}
    path = Path("logs") / f"ai_{mode}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "cases"}, ensure_ascii=False), flush=True)
    print(f"Báo cáo: {path}")


if __name__ == "__main__":
    main()
