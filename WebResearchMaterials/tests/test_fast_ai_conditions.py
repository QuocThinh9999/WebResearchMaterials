from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.models import TaiLieu
from app.services.ai.hard_rules import evaluate_hard_rules
from app.services.ai.intent_parser import analyze_ai_request
from app.services.ai.pdf_cache import CachedPdf
from app.services.ai.pdf_reader import PdfPage, create_chunks
from app.services.ai.worker import _set_lookup_result


class NoOllama:
    def structured_output(self, *_):
        raise AssertionError("Điều kiện rõ nghĩa không cần gọi Ollama")


@pytest.mark.parametrize("request_text,expected", [
    ("Phải có liên quan tới Malaysia", "topic"),
    ("Chỉ lấy bài có liên quan đến Malaysia", "topic"),
    ("Không lấy bài về Malaysia", "excluded_topic"),
    ("Không được đề cập đến Malaysia", "excluded_term"),
    ("Bài phải chứa từ khóa Malaysia", "required_term"),
])
def test_common_phrases_skip_ollama(request_text, expected):
    parsed = analyze_ai_request(request_text, NoOllama())
    values = {
        "topic": parsed.required_topics,
        "excluded_topic": parsed.excluded_topics,
        "excluded_term": parsed.tu_khoa_loai_tru,
        "required_term": parsed.required_terms,
    }
    assert values[expected] == ["Malaysia"]


@pytest.mark.parametrize("request_text,field,expected", [
    ("Từ năm 2020 đến 2024", "years", (2020, 2024)),
    ("Xuất bản sau năm 2020 và trước năm 2025", "years", (2021, 2024)),
    ("Q1 hoặc Q2", "quartile", ["Q1", "Q2"]),
    ("Chỉ lấy bài tiếng Anh và truy cập mở", "language_open", ("en", True)),
    ("Bài tổng quan của tác giả John Smith", "author", ["John Smith"]),
    ("Không lấy bài của Elsevier", "publisher", ["Elsevier"]),
    ("Không lấy bài của tác giả John Smith", "excluded_author", ["John Smith"]),
    ("Loại trừ nhà xuất bản Academic Press", "publisher", ["Academic Press"]),
])
def test_metadata_conditions_skip_ollama(request_text, field, expected):
    parsed = analyze_ai_request(request_text, NoOllama())
    values = {
        "years": (parsed.nam_tu, parsed.nam_den),
        "quartile": parsed.quartile,
        "language_open": (parsed.ngon_ngu, parsed.chi_truy_cap_mo),
        "author": parsed.tac_gia_bat_buoc,
        "publisher": parsed.nha_xuat_ban_loai_tru,
        "excluded_author": parsed.tac_gia_loai_tru,
    }
    assert values[field] == expected
    assert not parsed.needs_semantic_pdf_check
    assert parsed.dieu_kien_nguyen_tu


def test_metadata_and_topic_conditions_skip_ollama():
    parsed = analyze_ai_request(
        "Chỉ lấy bài có liên quan đến Malaysia, xuất bản sau năm 2020 và thuộc Q1",
        NoOllama(),
    )
    assert parsed.required_topics == ["Malaysia"]
    assert parsed.nam_tu == 2021
    assert parsed.quartile == ["Q1"]
    assert parsed.needs_semantic_pdf_check
    assert len(parsed.dieu_kien_nguyen_tu) == 3


@pytest.mark.parametrize("pdf_text,expected", [
    ("Article\nAbstract\nWe surveyed employees in Malaysia.", "phu_hop"),
    ("Article\nAbstract\nWe surveyed employees in Malaysia and Indonesia.", "khong_phu_hop"),
])
def test_multiple_content_conditions_use_pdf_without_ollama(pdf_text, expected, tmp_path, monkeypatch):
    import app.services.ai.worker as worker

    parsed = analyze_ai_request("Phải liên quan đến Malaysia và không có Indonesia", NoOllama())
    document = TaiLieu(tieu_de="A study", tac_gia=[], tu_khoa=[], ma_ngoai={}, du_lieu_nguon={})
    document.id_file_pdf = uuid4()
    result = SimpleNamespace()
    context = SimpleNamespace(result=result, document=document,
                              lookup=SimpleNamespace(dieu_kien_ai=parsed.model_dump(mode="json")))
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-1.7\n")
    pages = [PdfPage(1, pdf_text)]
    cached = CachedPdf(pages, create_chunks(pages), "compound-test", False, 0, 0)
    monkeypatch.setattr(worker, "_load_lookup_context", lambda *_: context)
    monkeypatch.setattr(worker, "resolve_stored_path", lambda *_: pdf_path)
    monkeypatch.setattr(worker, "cached_pdf", lambda *_: cached)
    db = MagicMock()
    db.get.return_value = SimpleNamespace()
    worker.process_ai_lookup_job(db, uuid4(), NoOllama(), SimpleNamespace(upload_path=tmp_path))
    assert result.nhan == expected
    assert result.chi_tiet_dieu_kien["so_lan_goi_ollama"] == 0


def test_language_aliases_and_missing_metadata_are_not_false_positive():
    parsed = analyze_ai_request("Chỉ lấy bài tiếng Anh và truy cập mở", NoOllama())
    document = TaiLieu(tieu_de="Paper", tac_gia=[], tu_khoa=[], ngon_ngu="English", truy_cap_mo=True)
    assert not evaluate_hard_rules(document, parsed).violations
    document.ngon_ngu = "French"
    assert evaluate_hard_rules(document, parsed).violations

    result = SimpleNamespace()
    _set_lookup_result(
        result, status="hoan_thanh", label="phu_hop", score=95, reason="Có chủ đề.",
        details={"dieu_kien_cung": {"khong_xac_dinh": ["Không xác định được nhà xuất bản."]}},
    )
    assert result.nhan == "chua_danh_gia"
    assert result.diem_phu_hop is None
