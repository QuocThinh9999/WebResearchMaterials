from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy.dialects import postgresql

from app.models import TaiLieu
from app.repositories.ai_evaluations import claim_next_job
from app.services.ai.client import AIResponseError, OllamaGateway
from app.services.ai.evaluator import evaluate_document
from app.services.ai.hard_rules import evaluate_hard_rules
from app.services.ai.intent_parser import AIConditionConflict, analyze_ai_request, combine_conditions
from app.services.ai.pdf_downloader import PdfUnavailableError, UnsafePdfUrl, download_pdf, validate_public_https_url
from app.services.ai.pdf_reader import PdfPage, create_chunks, extract_pdf_pages, select_top_chunks
from app.services.ai.schemas import KetQuaDanhGiaAI, NhomTuDongNghia, PhanTichYeuCauAI
from app.services.providers import SearchCriteria
from app.services.providers.scopus import build_scopus_query


def intent(**changes) -> PhanTichYeuCauAI:
    data = {
        "chu_de_bat_buoc": ["customer satisfaction", "digital banking"],
        "chu_de_tuy_chon": [],
        "tu_khoa_tim_kiem_tieng_anh": ["customer satisfaction", "digital banking"],
        "tu_khoa_loai_tru": [],
        "tieu_de_loai_tru": [],
        "doi_loai_tru": [],
        "tac_gia_bat_buoc": [],
        "tac_gia_loai_tru": [],
        "nha_xuat_ban_bat_buoc": [],
        "nha_xuat_ban_loai_tru": [],
        "nam_tu": 2025,
        "nam_den": 2026,
        "loai_tai_lieu": None,
        "ngon_ngu": None,
        "quartile": [],
        "chi_truy_cap_mo": False,
        "dien_giai_ngan_gon": "Hai khái niệm bắt buộc.",
        "ngon_ngu_yeu_cau": "vi",
        "ngon_ngu_tai_lieu_cho_phep": [],
        "khai_niem_goc_tieng_viet": ["sự hài lòng khách hàng", "ngân hàng số"],
        "khai_niem_tim_kiem_tieng_anh": ["customer satisfaction", "digital banking"],
        "nhom_tu_dong_nghia": [
            NhomTuDongNghia(
                khai_niem="customer satisfaction",
                tu_khoa_tieng_anh=["customer satisfaction", "consumer satisfaction"],
            ),
            NhomTuDongNghia(
                khai_niem="digital banking",
                tu_khoa_tieng_anh=["digital banking", "online banking"],
            ),
        ],
        "cau_truy_van_scopus": "",
        "cau_truy_van_wos": "",
    }
    data.update(changes)
    return PhanTichYeuCauAI.model_validate(data)


class FakeGateway:
    model = "mo-hinh-kiem-thu"
    embedding_model = "embedding-kiem-thu"

    def __init__(self, structured=None):
        self.structured = structured

    def structured_output(self, output_type, instructions, user_input):
        assert instructions
        return self.structured

    def structured_output_with_pdf(self, output_type, instructions, prompt, pdf_content):
        return self.structured

    def embeddings(self, texts):
        vectors = {"truy van": [1.0, 0.0], "khong lien quan": [0.0, 1.0], "rat lien quan": [0.9, 0.1]}
        return [vectors[text] for text in texts]


def test_agent_parses_vietnamese_request_with_strict_result():
    expected = intent()
    result = analyze_ai_request("Tìm bài về sự hài lòng trong ngân hàng số", FakeGateway(expected))
    assert result.ngon_ngu_yeu_cau == "vi"
    assert result.ngon_ngu_tai_lieu_cho_phep == []
    assert result.nhom_tu_khoa_bat_buoc[1] == ["digital banking", "online banking"]


def test_agent_builds_or_inside_groups_and_and_between_groups():
    manual = SearchCriteria.model_construct(sources=["scopus"], keyword_groups=[])
    criteria = combine_conditions(manual, intent())
    query = build_scopus_query(criteria)
    assert 'TITLE-ABS-KEY("customer satisfaction") OR TITLE-ABS-KEY("consumer satisfaction")' in query
    assert ') AND (TITLE-ABS-KEY("digital banking") OR TITLE-ABS-KEY("online banking")' in query


def test_conflicting_years_are_rejected_before_search():
    manual = SearchCriteria.model_construct(sources=["scopus"], keyword_groups=[], year_from=2020, year_to=2023)
    with pytest.raises(AIConditionConflict):
        combine_conditions(manual, intent())


def test_hard_rules_exclude_year_publisher_and_title_without_ai_call():
    document = TaiLieu(
        tieu_de="B: A forbidden title",
        tac_gia=[],
        tu_khoa=[],
        nam_xuat_ban=2024,
        nha_xuat_ban="Publisher A",
        truy_cap_mo=True,
    )
    result = evaluate_hard_rules(
        document,
        intent(tieu_de_loai_tru=["B: A forbidden title"], nha_xuat_ban_loai_tru=["Publisher A"]),
    )
    assert len(result.violations) == 3


def test_language_is_not_restricted_unless_user_requests_it():
    document = TaiLieu(tieu_de="Paper", tac_gia=[], tu_khoa=[], ngon_ngu="en", truy_cap_mo=False)
    assert not evaluate_hard_rules(document, intent()).violations
    restricted = evaluate_hard_rules(
        document,
        intent(ngon_ngu="vi", ngon_ngu_tai_lieu_cho_phep=["vi"]),
    )
    assert any("ngôn ngữ" in item.casefold() for item in restricted.violations)


def test_private_and_plain_http_pdf_urls_are_blocked():
    with pytest.raises(UnsafePdfUrl):
        validate_public_https_url("http://example.test/paper.pdf", lambda _: ["93.184.216.34"])
    with pytest.raises(UnsafePdfUrl):
        validate_public_https_url("https://example.test/paper.pdf", lambda _: ["127.0.0.1"])


def test_pdf_download_revalidates_redirect_and_rejects_html():
    def redirect_handler(_: httpx.Request):
        return httpx.Response(302, headers={"location": "https://internal.test/paper.pdf"})

    client = httpx.Client(transport=httpx.MockTransport(redirect_handler))
    with pytest.raises(UnsafePdfUrl):
        download_pdf(
            "https://public.test/paper.pdf",
            max_bytes=1000,
            resolver=lambda host: ["127.0.0.1"] if host == "internal.test" else ["93.184.216.34"],
            client=client,
        )

    html_client = httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html>"))
    )
    with pytest.raises(PdfUnavailableError):
        download_pdf(
            "https://public.test/paper.pdf",
            max_bytes=1000,
            resolver=lambda _: ["93.184.216.34"],
            client=html_client,
        )


def test_pdf_download_checks_stream_size_and_signature():
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF-" + b"x" * 20)
        )
    )
    with pytest.raises(PdfUnavailableError):
        download_pdf(
            "https://public.test/paper.pdf",
            max_bytes=10,
            resolver=lambda _: ["93.184.216.34"],
            client=client,
        )


def test_chunks_keep_page_and_embedding_selects_most_relevant():
    chunks = create_chunks([PdfPage(1, "khong lien quan"), PdfPage(7, "rat lien quan")], chunk_size=100, overlap=10)
    selected = select_top_chunks("truy van", chunks, FakeGateway(), 1)
    assert selected[0].trang == 7


def test_extracts_text_and_page_number_from_pdf():
    import pymupdf

    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), "Digital banking satisfaction")
    content = document.tobytes()
    document.close()
    pages = extract_pdf_pages(content)
    assert pages[0].page_number == 1
    assert "Digital banking satisfaction" in pages[0].text


def test_relevance_threshold_is_applied_by_python():
    ai_result = KetQuaDanhGiaAI(
        nhan="phu_hop",
        diem_phu_hop=69,
        ly_do="Chưa đủ mức phù hợp.",
        dieu_kien_dat=[],
        dieu_kien_khong_dat=[],
        dieu_kien_khong_xac_dinh=[],
        bang_chung=[],
    )
    document = TaiLieu(tieu_de="Paper", tac_gia=[], tu_khoa=[], truy_cap_mo=False)
    result = evaluate_document("Yêu cầu", intent(), document, [], FakeGateway(ai_result), 70)
    assert result.nhan == "khong_phu_hop"


def test_malformed_structured_output_is_rejected():
    def malformed_handler(request: httpx.Request):
        assert request.url.path == "/api/chat"
        assert b'"stream":false' in request.content
        assert b'"format"' in request.content
        return httpx.Response(200, json={"message": {"content": '{"nhan": "phu_hop"}'}})

    client = httpx.Client(
        transport=httpx.MockTransport(malformed_handler)
    )
    settings = SimpleNamespace(
        ollama_base_url="http://127.0.0.1:11434",
        ollama_model="test-model",
        ollama_embedding_model="test-embedding",
        ollama_timeout_seconds=2,
    )
    gateway = OllamaGateway(settings, client)
    with pytest.raises(AIResponseError):
        gateway.structured_output(PhanTichYeuCauAI, "Hướng dẫn", "Yêu cầu")


def test_ollama_embeddings_use_local_api():
    def embedding_handler(request: httpx.Request):
        assert request.url.path == "/api/embed"
        assert b'"test-embedding"' in request.content
        return httpx.Response(200, json={"embeddings": [[1.0, 0.0], [0.0, 1.0]]})

    settings = SimpleNamespace(
        ollama_base_url="http://127.0.0.1:11434",
        ollama_model="test-model",
        ollama_embedding_model="test-embedding",
        ollama_timeout_seconds=2,
    )
    gateway = OllamaGateway(
        settings,
        httpx.Client(transport=httpx.MockTransport(embedding_handler)),
    )
    assert gateway.embeddings(["một", "hai"]) == [[1.0, 0.0], [0.0, 1.0]]


def test_ai_migration_is_idempotent_by_design():
    sql = open("sql/nang_cap_tim_kiem_ai.sql", encoding="utf-8").read().casefold()
    assert "add column if not exists" in sql
    assert "create table if not exists danh_gia_ai_tai_lieu" in sql
    assert "create index if not exists" in sql


def test_worker_claim_uses_postgresql_skip_locked():
    class CapturingSession:
        statement = None

        def scalar(self, statement):
            self.statement = statement
            return None

    session = CapturingSession()
    assert claim_next_job(session, 3) is None
    compiled = str(session.statement.compile(dialect=postgresql.dialect())).upper()
    assert "FOR UPDATE SKIP LOCKED" in compiled
