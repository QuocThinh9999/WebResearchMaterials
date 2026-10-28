from __future__ import annotations

from types import SimpleNamespace
import threading
from contextlib import contextmanager
from uuid import uuid4

import httpx
import pymupdf
import pytest

from app.models import TaiLieu
from app.services.ai.client import AIConfigurationError, AIResponseError, OllamaGateway
from app.services.ai.hard_rules import evaluate_hard_rules, publisher_matches
from app.services.ai.intent_parser import analyze_ai_request
from app.services.ai.pdf_cache import CachedPdf, cached_pdf
from app.services.ai.pdf_reader import PdfPage, create_chunks
from app.services.ai.semantic_check import Evidence, SemanticDecision, check_constructs


class NoModelGateway:
    model = "test-semantic-v1"
    call_count = 0

    def structured_output(self, *args):
        raise AssertionError("Điều kiện này không được gọi Ollama")

    def embeddings(self, *_):
        raise AssertionError("Điều kiện này không được tạo embedding")


class EvidenceGateway(NoModelGateway):
    def structured_output(self, output_type, instructions, payload):
        self.call_count += 1
        active = next((item for item in payload["excerpts"] if item["category"] == "active"), None)
        if not active:
            return SemanticDecision(status="insufficient", confidence=0, evidence=[], reason="Chưa đủ bằng chứng.")
        return SemanticDecision(
            status="matched", confidence=95,
            evidence=[Evidence(page=active["page"], section=active["section"], text=active["text"][:60])],
            reason="Khái niệm nằm trong mô hình nghiên cứu.",
        )


@pytest.mark.parametrize("user_text,expected", [
    ("Không lấy từ các nhà xuất bản MDPI", ("MDPI",)),
    ("Không lấy bài của MDPI", ("MDPI",)),
    ("Exclude MDPI", ("MDPI",)),
    ("Exclude papers published by MDPI", ("MDPI",)),
])
def test_publisher_rule_skips_ollama(user_text, expected):
    parsed = analyze_ai_request(user_text, NoModelGateway())
    assert tuple(parsed.excluded_publishers) == expected
    assert not parsed.needs_semantic_pdf_check


@pytest.mark.parametrize("user_text", [
    "Trong mô hình phải có Customer Satisfaction",
    "Mô hình nghiên cứu phải chứa Customer Satisfaction",
    "Customer Satisfaction must be included in the research model",
    "The conceptual model must contain Customer Satisfaction",
    "The research model must include Customer Satisfaction",
])
def test_model_rule_is_equivalent_in_vietnamese_and_english(user_text):
    parsed = analyze_ai_request(user_text, NoModelGateway())
    assert parsed.required_constructs_in_model == ["Customer Satisfaction"]
    assert parsed.needs_semantic_pdf_check


def _document(publisher):
    return TaiLieu(tieu_de="Paper", tac_gia=[], tu_khoa=[], nha_xuat_ban=publisher, ma_ngoai={}, du_lieu_nguon={})


def test_publisher_aliases_and_metadata_only_exclusion():
    intent = analyze_ai_request("Exclude MDPI", NoModelGateway())
    assert publisher_matches("MDPI AG", "MDPI")
    assert publisher_matches("Multidisciplinary Digital Publishing Institute", "MDPI")
    assert not publisher_matches("Elsevier", "MDPI")
    assert evaluate_hard_rules(_document("MDPI AG"), intent).violations
    assert not evaluate_hard_rules(_document("Elsevier"), intent).violations


def _cached_text(text):
    pages = [PdfPage(1, text)]
    return CachedPdf(pages, create_chunks(pages), "test-" + str(abs(hash(text))), False, 0, 0)


@pytest.mark.parametrize("publisher,text,user_text,expected", [
    ("MDPI", "Conceptual Framework\nCustomer Satisfaction is our dependent variable.", "Exclude MDPI", "khong_phu_hop"),
    ("MDPI AG", "Conceptual Framework\nCustomer Satisfaction is our dependent variable.", "Không lấy bài của MDPI", "khong_phu_hop"),
    ("Elsevier", "Introduction\nMDPI papers discuss banking.", "Exclude MDPI", "phu_hop"),
    ("Elsevier", "Conceptual Framework\nCustomer Satisfaction is a dependent variable in our model.", "Trong mô hình phải có Customer Satisfaction", "phu_hop"),
    ("Elsevier", "Hypotheses\nH1: Customer Satisfaction predicts loyalty.", "Customer Satisfaction must be included in the research model", "phu_hop"),
    ("Elsevier", "Literature Review\nPrior work studies Customer Satisfaction.", "Trong mô hình phải có Customer Satisfaction", "khong_phu_hop"),
    ("Elsevier", "References\nSmith (2020). Customer Satisfaction in banks.", "Trong mô hình phải có Customer Satisfaction", "khong_phu_hop"),
    ("Elsevier", "Methods\nWe measure service quality only.", "Trong mô hình phải có Customer Satisfaction", "khong_phu_hop"),
    ("Elsevier", "Hypotheses\nConsumer satisfaction predicts loyalty in H1.", "Trong mô hình phải có Customer Satisfaction", "phu_hop"),
    ("MDPI", "Hypotheses\nCustomer Satisfaction predicts loyalty.", "Không lấy từ các nhà xuất bản MDPI; Trong mô hình phải có Customer Satisfaction", "khong_phu_hop"),
])
def test_manually_labelled_cases(publisher, text, user_text, expected, tmp_path):
    intent = analyze_ai_request(user_text, NoModelGateway())
    rules = evaluate_hard_rules(_document(publisher), intent)
    if rules.violations:
        actual = "khong_phu_hop"
    elif not intent.required_constructs_in_model:
        actual = "phu_hop"
    else:
        decision = check_constructs(_cached_text(text), intent.required_constructs_in_model, tmp_path, EvidenceGateway())
        actual = {"matched": "phu_hop", "unmatched": "khong_phu_hop", "insufficient": "chua_danh_gia"}[decision.status]
    assert actual == expected


def test_unreadable_pdf_is_unassessed(tmp_path):
    with pytest.raises(Exception):
        cached_pdf(b"not a pdf", tmp_path)


def test_pdf_extraction_and_semantic_result_are_cached(monkeypatch, tmp_path):
    import app.services.ai.pdf_cache as module

    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((50, 50), "Conceptual Framework\nCustomer Satisfaction is our dependent variable.")
    content = document.tobytes()
    document.close()
    original = module.extract_pdf_pages
    reads = []

    def counted(value):
        reads.append(1)
        return original(value)

    monkeypatch.setattr(module, "extract_pdf_pages", counted)
    first = cached_pdf(content, tmp_path)
    second = cached_pdf(content, tmp_path)
    assert not first.hit and second.hit and len(reads) == 1
    gateway = EvidenceGateway()
    first_result = check_constructs(first, ["Customer Satisfaction"], tmp_path, gateway)
    second_result = check_constructs(second, ["Customer Satisfaction"], tmp_path, gateway)
    assert first_result == second_result and gateway.call_count == 1
    modified = cached_pdf(content + b"\n", tmp_path)
    assert not modified.hit and len(reads) == 2
    monkeypatch.setattr(module, "PDF_READER_VERSION", "pymupdf-text-v2")
    changed = cached_pdf(content, tmp_path)
    assert not changed.hit and len(reads) == 3


def test_model_evidence_must_quote_an_active_section(tmp_path):
    class InventingGateway(EvidenceGateway):
        def structured_output(self, *_):
            self.call_count += 1
            return SemanticDecision(status="matched", confidence=99, evidence=[Evidence(page=1, section="Hypotheses", text="Invented statement")], reason="Đạt")

    pdf = _cached_text("Hypotheses\nCustomer Satisfaction predicts loyalty.")
    result = check_constructs(pdf, ["Customer Satisfaction"], tmp_path, InventingGateway())
    assert result.status == "insufficient"


def test_verified_model_result_gets_consistent_score_and_vietnamese_reason(tmp_path):
    class LowConfidenceGateway(EvidenceGateway):
        def structured_output(self, output_type, instructions, payload):
            result = super().structured_output(output_type, instructions, payload)
            return result.model_copy(update={"confidence": 0, "reason": "English reason"})

    pdf = _cached_text("Hypotheses\nWe predict that Customer Satisfaction increases loyalty in this study.")
    result = check_constructs(pdf, ["Customer Satisfaction"], tmp_path, LowConfidenceGateway())
    assert result.status == "matched"
    assert result.confidence >= 90
    assert "bằng chứng" in result.reason.casefold()


def test_parallel_lookup_claims_each_job_once(monkeypatch):
    import app.services.ai.worker as worker

    job_ids = [uuid4() for _ in range(7)]
    lock = threading.Lock()
    processed = []

    class FakeSession:
        @contextmanager
        def begin(self):
            yield object()

    def claim(db, retries, lookup_id):
        with lock:
            return SimpleNamespace(id=job_ids.pop(0)) if job_ids else None

    def process(job_id, lookup_id, settings):
        with lock:
            processed.append(job_id)

    monkeypatch.setattr(worker, "SessionLocal", FakeSession())
    monkeypatch.setattr(worker, "get_settings", lambda: SimpleNamespace(ai_max_retries=3, ai_lookup_workers=2))
    monkeypatch.setattr(worker, "recover_stale_ai_lookup_jobs", lambda *_: set())
    monkeypatch.setattr(worker, "update_ai_lookup_progress", lambda *_: None)
    monkeypatch.setattr(worker, "claim_next_ai_lookup_job", claim)
    monkeypatch.setattr(worker, "_process_ai_lookup_job_in_thread", process)
    assert worker.run_ai_lookup_batch(uuid4()) == 7
    assert len(set(processed)) == 7


def test_ollama_timeout_and_offline_have_safe_errors():
    settings = SimpleNamespace(ollama_base_url="http://127.0.0.1:11435", ollama_model="test", ollama_embedding_model="test", ollama_timeout_seconds=1)
    for exception, expected in [
        (httpx.ConnectError("offline"), AIConfigurationError),
        (httpx.ReadTimeout("timeout"), AIResponseError),
    ]:
        client = httpx.Client(transport=httpx.MockTransport(lambda _: (_ for _ in ()).throw(exception)))
        with pytest.raises(expected):
            OllamaGateway(settings, client).structured_output(SemanticDecision, "Check", "test")


def test_invalid_ai_output_is_not_shown_as_raw_json(monkeypatch):
    import app.routers.ai_search as route

    messages = []
    monkeypatch.setattr(route, "require_csrf", lambda *_: None)
    monkeypatch.setattr(route, "analyze_ai_request", lambda *_: (_ for _ in ()).throw(ValueError('{"internal":"raw JSON"}')))
    monkeypatch.setattr(route, "flash", lambda request, message, kind=None: messages.append(message))
    response = route.start_ai_lookup(
        SimpleNamespace(), SimpleNamespace(), uuid4(), [uuid4()], "Yêu cầu", "token", SimpleNamespace(id=uuid4()),
    )
    assert response.status_code == 303
    assert "raw JSON" not in messages[0]

