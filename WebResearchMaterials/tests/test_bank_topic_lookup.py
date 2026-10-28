from __future__ import annotations

from app.models import TaiLieu
from app.services.ai.intent_parser import analyze_ai_request
from app.services.ai.pdf_cache import CachedPdf
from app.services.ai.pdf_reader import PdfPage, create_chunks
from app.services.ai.schemas import DieuKienNguyenTu, PhanTichYeuCauAI
from app.services.ai.topic_relevance import check_bank_topic, metadata_bank_evidence


class NoOllama:
    model = "kiem-thu"
    call_count = 0

    def structured_output(self, *_):
        raise AssertionError("Không được gọi Ollama cho bằng chứng đã rõ")


def _document(title, abstract=""):
    return TaiLieu(tieu_de=title, tom_tat=abstract, tac_gia=[], tu_khoa=[], ma_ngoai={}, du_lieu_nguon={})


def _pdf(text):
    pages = [PdfPage(1, text)]
    return CachedPdf(pages, create_chunks(pages), "test-bank-" + str(abs(hash(text))), False, 0, 0)


def test_bank_request_is_topic_not_research_model_in_both_languages():
    english = analyze_ai_request("must be related to a bank", NoOllama())
    vietnamese = analyze_ai_request("Phải liên quan đến ngân hàng", NoOllama())
    for intent in (english, vietnamese):
        assert intent.required_topics == ["banking"]
        assert intent.required_constructs_in_model == []
        assert intent.nam_tu is None and intent.nam_den is None
        assert intent.loai_tai_lieu is None and intent.quartile == []
        assert intent.chi_truy_cap_mo is False
        assert intent.dieu_kien_nguyen_tu[0].loai == "bat_buoc"
    assert english.input_language == "en"
    assert vietnamese.input_language == "vi"


def test_unrequested_filters_from_model_are_removed():
    bad = PhanTichYeuCauAI(
        chu_de_bat_buoc=["bank"], chu_de_tuy_chon=[], tu_khoa_tim_kiem_tieng_anh=["bank"],
        tu_khoa_loai_tru=[], tieu_de_loai_tru=[], doi_loai_tru=[], tac_gia_bat_buoc=[],
        tac_gia_loai_tru=[], nha_xuat_ban_bat_buoc=[], nha_xuat_ban_loai_tru=[],
        nam_tu=0, nam_den=0, loai_tai_lieu="all", ngon_ngu="", quartile=["Q1", "Q2", "Q3", "Q4"],
        chi_truy_cap_mo=True, dien_giai_ngan_gon="", ngon_ngu_yeu_cau="en",
        ngon_ngu_tai_lieu_cho_phep=["en", "vi"], khai_niem_goc_tieng_viet=[],
        khai_niem_tim_kiem_tieng_anh=["bank"], nhom_tu_dong_nghia=[],
        cau_truy_van_scopus="", cau_truy_van_wos="",
        dieu_kien_nguyen_tu=[DieuKienNguyenTu(noi_dung="bank trong mô hình", loai="khai_niem_trong_mo_hinh", truy_van_tim_bang_chung=["bank"])],
    )

    class FakeGateway:
        def structured_output(self, *_):
            return bad

    intent = analyze_ai_request("Find papers about bank employees", FakeGateway())
    assert (intent.nam_tu, intent.nam_den, intent.loai_tai_lieu, intent.quartile, intent.chi_truy_cap_mo) == (None, None, None, [], False)
    assert intent.dieu_kien_nguyen_tu[0].loai == "bat_buoc"
    assert not any(item.loai == "metadata_cung" for item in intent.dieu_kien_nguyen_tu)


def test_bank_title_is_relevant_but_world_bank_and_affiliation_are_not():
    assert metadata_bank_evidence(_document("Leadership Styles: Evidence from Rural Banks"))
    assert not metadata_bank_evidence(_document("World Bank statistics and real estate"))
    assert not metadata_bank_evidence(_document("Leadership and job performance", "Authors work at University of Banking"))


def test_bank_abstract_is_relevant_without_ollama(tmp_path):
    pdf = _pdf("Article title\nAbstract\nWe surveyed 345 employees at five commercial banks in Vietnam.\nKeywords: leadership")
    decision = check_bank_topic(b"%PDF-synthetic", pdf, tmp_path, NoOllama())
    assert decision.status == "matched"
    assert decision.evidence[0].page == 1
    assert decision.confidence >= 90


def test_affiliation_and_references_do_not_make_bank_topic(tmp_path):
    cases = [
        "University of Banking\nAbstract\nThis study surveys cosmetics industry workers.\nReferences\nSmith: bank employees.",
        "Article\nAbstract\nThis study examines real estate workers.\nIntroduction\nThe World Bank reports market data.\nReferences\nBanking studies.",
        "Introduction\nPrior research in different industries includes Niazi, et al. [6] in banks, while this study examines engineering firms.",
        "Results\nA study by Mozammel and Haan (2016) using bank employees found similar results. Our sample is university teachers.",
    ]
    for text in cases:
        decision = check_bank_topic(b"%PDF-" + text.encode(), _pdf(text), tmp_path, NoOllama())
        assert decision.status == "unmatched"
