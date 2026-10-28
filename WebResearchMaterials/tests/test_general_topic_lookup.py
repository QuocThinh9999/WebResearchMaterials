from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.models import TaiLieu
from app.services.ai.intent_parser import analyze_ai_request
from app.services.ai.pdf_cache import CachedPdf
from app.services.ai.pdf_reader import PdfPage, create_chunks
from app.services.ai.topic_relevance import check_general_topic, metadata_mention, metadata_topic_evidence


class NoOllama:
    def structured_output(self, *_):
        raise AssertionError("Không gọi Ollama cho yêu cầu chủ đề đơn giản")


def _document(title: str, abstract: str = "") -> TaiLieu:
    return TaiLieu(tieu_de=title, tom_tat=abstract, tac_gia=[], tu_khoa=[], ma_ngoai={}, du_lieu_nguon={})


def _pdf(text: str) -> CachedPdf:
    pages = [PdfPage(1, text)]
    return CachedPdf(pages, create_chunks(pages), "general-topic-test", False, 0, 0)


@pytest.mark.parametrize("user_text", [
    "Phải liên quan đến Malaysia",
    "Phải luên quan đến Malaysia",
    "must be related to Malaysia",
    "Malaysia must be included",
])
def test_any_simple_topic_uses_fast_parser(user_text):
    parsed = analyze_ai_request(user_text, NoOllama())
    assert parsed.required_topics == ["Malaysia"]
    assert parsed.required_constructs_in_model == []
    assert parsed.needs_semantic_pdf_check
    assert parsed.nam_tu is None and parsed.nam_den is None
    assert parsed.loai_tai_lieu is None
    assert parsed.dieu_kien_nguyen_tu[0].loai == "bat_buoc"


@pytest.mark.parametrize("user_text", [
    "Phải có liên quan đến Malaysia",
    "Liên quan đến Malaysia",
    "Liên quan Malaysia",
    "Tìm bài về Malaysia",
    "Bài viết phải có liên quan đến Malaysia",
    "Tôi muốn bài viết liên quan đến Malaysia",
])
def test_flexible_positive_phrases_skip_ollama(user_text):
    parsed = analyze_ai_request(user_text, NoOllama())
    assert parsed.required_topics == ["Malaysia"]
    assert parsed.excluded_topics == []
    assert parsed.dieu_kien_nguyen_tu[0].loai == "bat_buoc"


def test_required_term_phrase_skips_ollama():
    parsed = analyze_ai_request("Phải có Malaysia", NoOllama())
    assert parsed.required_topics == []
    assert parsed.required_terms == ["Malaysia"]
    assert parsed.dieu_kien_nguyen_tu[0].loai == "bat_buoc"


@pytest.mark.parametrize("user_text", [
    "Không liên quan đến Malaysia",
    "Không liên quan Malaysia",
    "Không có liên quan đến Malaysia",
    "not related to Malaysia",
    "Chỉ lấy bài không liên quan đến Malaysia",
])
def test_flexible_negative_phrases_skip_ollama(user_text):
    parsed = analyze_ai_request(user_text, NoOllama())
    assert parsed.required_topics == []
    assert parsed.excluded_topics == ["Malaysia"]
    assert parsed.needs_semantic_pdf_check
    assert parsed.dieu_kien_nguyen_tu[0].loai == "loai_tru"


def test_absence_phrase_skips_ollama_and_requires_no_mention():
    parsed = analyze_ai_request("Không có Malaysia", NoOllama())
    assert parsed.excluded_topics == []
    assert parsed.tu_khoa_loai_tru == ["Malaysia"]
    assert parsed.dieu_kien_nguyen_tu[0].loai == "loai_tru"


def test_bank_topic_alias_keeps_specialized_relevance():
    intent = analyze_ai_request("Phải liên quan đến lĩnh vực ngân hàng", NoOllama())
    assert intent.required_topics == ["banking"]


def test_negative_bank_alias_checks_pdf_without_model():
    intent = analyze_ai_request("Không liên quan đến ngân hàng", NoOllama())
    assert intent.excluded_topics == ["banking"]
    assert check_general_topic(_pdf("Article\nAbstract\nWe surveyed bank employees in Malaysia."), "banking").status == "matched"
    assert check_general_topic(_pdf("Article\nAbstract\nWe surveyed teachers in Malaysia."), "banking").status == "unmatched"


def test_generic_topic_in_title_or_abstract_is_relevant():
    assert metadata_topic_evidence(_document("Leadership in Malaysia"), "Malaysia")
    assert metadata_topic_evidence(_document("Customer satisfaction among Malaysian employees"), "Malaysia")
    assert metadata_topic_evidence(_document("Leadership study", "This study surveyed workers in Malaysia."), "Malaysia")
    assert metadata_topic_evidence(_document("Customer satisfaction and service quality"), "customer satisfaction")
    assert metadata_topic_evidence(_document("Study in Vietnam"), "Vi\u1ec7t Nam")
    assert not metadata_topic_evidence(_document("Leadership in Indonesia"), "Malaysia")
    assert not metadata_topic_evidence(_document("Leadership", "Prior studies in Malaysia examined this issue."), "Malaysia")


def test_required_term_accepts_metadata_mention():
    assert metadata_mention(_document("Leadership in Malaysia"), "Malaysia")
    assert not metadata_mention(_document("Leadership in Indonesia"), "Malaysia")


@pytest.mark.parametrize("text,expected", [
    ("Article\nAbstract\nWe surveyed bank employees in Malaysia.\nKeywords: leadership", "matched"),
    ("Article\nMethods\nOur data were collected in Malaysia from 250 employees.", "matched"),
    ("Article\nAbstract\nThis study concerns Indonesia.\nReferences\nPrior study in Malaysia.", "unmatched"),
    ("Article\nIntroduction\nPrior studies in Malaysia were cited. Our sample is from Indonesia.", "unmatched"),
    ("Article\nIntroduction\nMalaysia was mentioned once as an example.", "insufficient"),
])
def test_generic_topic_checks_own_pdf_study_without_model(text, expected):
    decision = check_general_topic(_pdf(text), "Malaysia")
    assert decision.status == expected
    if expected == "matched":
        assert decision.evidence and decision.confidence >= 85


@pytest.mark.parametrize("pdf_text,expected", [
    ("Article\nAbstract\nWe surveyed employees in Malaysia.", "khong_phu_hop"),
    ("Article\nAbstract\nWe surveyed employees in Indonesia.", "phu_hop"),
    ("Article\nIntroduction\nMalaysia is mentioned as an example.", "chua_danh_gia"),
])
def test_negative_topic_worker_does_not_call_ollama(pdf_text, expected, tmp_path, monkeypatch):
    import app.services.ai.worker as worker

    parsed = analyze_ai_request("Không liên quan đến Malaysia", NoOllama())
    result = SimpleNamespace()
    document = _document("A research paper")
    document.id_file_pdf = uuid4()
    context = SimpleNamespace(result=result, lookup=SimpleNamespace(dieu_kien_ai=parsed.model_dump(mode="json")), document=document)
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.7\n")
    monkeypatch.setattr(worker, "_load_lookup_context", lambda *_: context)
    monkeypatch.setattr(worker, "resolve_stored_path", lambda *_: pdf)
    monkeypatch.setattr(worker, "cached_pdf", lambda *_: _pdf(pdf_text))
    db = MagicMock()
    db.get.return_value = SimpleNamespace()
    worker.process_ai_lookup_job(db, uuid4(), NoOllama(), SimpleNamespace(upload_path=tmp_path))
    assert result.nhan == expected
    assert result.chi_tiet_dieu_kien["so_lan_goi_ollama"] == 0


@pytest.mark.parametrize("pdf_text,expected", [
    ("Article\nIntroduction\nPrior studies in Malaysia examined this issue.", "khong_phu_hop"),
    ("Article\nIntroduction\nPrior studies in Indonesia examined this issue.", "phu_hop"),
])
def test_absence_worker_checks_full_pdf_without_model(pdf_text, expected, tmp_path, monkeypatch):
    import app.services.ai.worker as worker

    parsed = analyze_ai_request("Không có Malaysia", NoOllama())
    result = SimpleNamespace()
    document = _document("A research paper")
    document.id_file_pdf = uuid4()
    context = SimpleNamespace(result=result, lookup=SimpleNamespace(dieu_kien_ai=parsed.model_dump(mode="json")), document=document)
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.7\n")
    monkeypatch.setattr(worker, "_load_lookup_context", lambda *_: context)
    monkeypatch.setattr(worker, "resolve_stored_path", lambda *_: pdf)
    monkeypatch.setattr(worker, "cached_pdf", lambda *_: _pdf(pdf_text))
    db = MagicMock()
    db.get.return_value = SimpleNamespace()
    worker.process_ai_lookup_job(db, uuid4(), NoOllama(), SimpleNamespace(upload_path=tmp_path))
    assert result.nhan == expected
    assert result.chi_tiet_dieu_kien["so_lan_goi_ollama"] == 0


@pytest.mark.parametrize("pdf_text,expected", [
    ("Article\nIntroduction\nPrior studies in Malaysia examined this issue.", "phu_hop"),
    ("Article\nIntroduction\nPrior studies in Indonesia examined this issue.", "khong_phu_hop"),
])
def test_required_term_worker_checks_full_pdf_without_model(pdf_text, expected, tmp_path, monkeypatch):
    import app.services.ai.worker as worker

    parsed = analyze_ai_request("Phải có Malaysia", NoOllama())
    result = SimpleNamespace()
    document = _document("A research paper")
    document.id_file_pdf = uuid4()
    context = SimpleNamespace(result=result, lookup=SimpleNamespace(dieu_kien_ai=parsed.model_dump(mode="json")), document=document)
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.7\n")
    monkeypatch.setattr(worker, "_load_lookup_context", lambda *_: context)
    monkeypatch.setattr(worker, "resolve_stored_path", lambda *_: pdf)
    monkeypatch.setattr(worker, "cached_pdf", lambda *_: _pdf(pdf_text))
    db = MagicMock()
    db.get.return_value = SimpleNamespace()
    worker.process_ai_lookup_job(db, uuid4(), NoOllama(), SimpleNamespace(upload_path=tmp_path))
    assert result.nhan == expected
    assert result.chi_tiet_dieu_kien["so_lan_goi_ollama"] == 0

