from __future__ import annotations

import json
from types import SimpleNamespace

import httpx

from app.services.ai.client import OllamaGateway
from app.services.ai.evaluator import _evaluation_prompt
from app.services.ai.intent_parser import _parse_simple_request, analyze_ai_request
from app.services.ai.schemas import DieuKienRutGon, KetQuaDanhGiaAI, PhanTichYeuCauAI, YeuCauChuanHoa
from app.services.ai.pdf_cache import CachedPdf
from app.services.ai.pdf_reader import PdfPage
from app.services.ai.topic_relevance import check_author_affiliation_country
from app.models import TaiLieu


class CompactGateway:
    def __init__(self, conditions: list[tuple[str, str]]):
        self.conditions = conditions
        self.calls: list[type] = []

    def structured_output(self, output_type, *_):
        self.calls.append(output_type)
        return YeuCauChuanHoa(c=[DieuKienRutGon(k=kind, v=value) for kind, value in self.conditions])


def test_paraphrase_uses_one_short_ollama_response():
    gateway = CompactGateway([("topic", "customer satisfaction"), ("topic", "digital banking")])
    parsed = analyze_ai_request("Tìm bài về sự hài lòng và ngân hàng số", gateway)
    assert parsed.required_topics == ["customer satisfaction", "digital banking"]
    assert parsed.fast_rule_parsed
    assert gateway.calls == [YeuCauChuanHoa]


def test_ollama_interprets_author_country_without_large_schema():
    gateway = CompactGateway([("topic", "banking"), ("no_origin", "Vietnam")])
    parsed = analyze_ai_request(
        "Các bài nghiên cứu phải có ngân hàng và không phải là bài của Việt Nam", gateway
    )
    assert parsed.required_topics == ["banking"]
    assert parsed.excluded_affiliation_countries == ["Vietnam"]
    assert gateway.calls == [YeuCauChuanHoa]


def test_positive_author_country_is_required_not_excluded():
    gateway = CompactGateway([("origin", "Vietnam")])
    parsed = analyze_ai_request("Bài viết có tác giả là người Việt Nam", gateway)
    assert parsed.required_affiliation_countries == ["Vietnam"]
    assert parsed.excluded_affiliation_countries == []
    assert parsed.dieu_kien_nguyen_tu[0].loai == "bat_buoc"
    assert gateway.calls == [YeuCauChuanHoa]


def test_reversed_author_country_is_rejected_and_retried():
    class CorrectingGateway(CompactGateway):
        def structured_output(self, output_type, *_):
            self.calls.append(output_type)
            kind = "no_origin" if len(self.calls) == 1 else "origin"
            return YeuCauChuanHoa(c=[DieuKienRutGon(k=kind, v="Vietnam")])

    gateway = CorrectingGateway([])
    parsed = analyze_ai_request("Bài viết có tác giả là người Việt Nam", gateway)
    assert parsed.required_affiliation_countries == ["Vietnam"]
    assert parsed.excluded_affiliation_countries == []
    assert gateway.calls == [YeuCauChuanHoa, YeuCauChuanHoa]


def test_wrong_publisher_classification_gets_compact_retry():
    class CorrectingGateway(CompactGateway):
        def structured_output(self, output_type, *_):
            self.calls.append(output_type)
            if len(self.calls) == 1:
                return YeuCauChuanHoa(c=[
                    DieuKienRutGon(k="topic", v="banking"),
                    DieuKienRutGon(k="no_pub", v="Vietnam"),
                ])
            return YeuCauChuanHoa(c=[
                DieuKienRutGon(k="topic", v="banking"),
                DieuKienRutGon(k="no_origin", v="Vietnam"),
            ])

    gateway = CorrectingGateway([])
    parsed = analyze_ai_request("Các bài nghiên cứu phải có ngân hàng và không phải là bài của Việt Nam", gateway)
    assert parsed.excluded_affiliation_countries == ["Vietnam"]
    assert gateway.calls == [YeuCauChuanHoa, YeuCauChuanHoa]


def test_author_country_uses_front_matter_not_study_location():
    def pdf(text):
        return CachedPdf([PdfPage(1, text)], [], "test", False, 0, 0)

    vietnam_author = pdf("University of Economics, Hanoi, Vietnam\nAbstract\nBanking study in Malaysia")
    foreign_author = pdf("University of Malaya, Malaysia\nAbstract\nBanking study in Vietnam")
    assert check_author_affiliation_country(vietnam_author, "Vietnam").status == "matched"
    assert check_author_affiliation_country(foreign_author, "Vietnam").status == "unmatched"


def test_normalization_does_not_add_unrequested_year():
    class Gateway(CompactGateway):
        def structured_output(self, output_type, *_):
            self.calls.append(output_type)
            if output_type is YeuCauChuanHoa:
                return YeuCauChuanHoa(c=[DieuKienRutGon(k="topic", v="Malaysia"), DieuKienRutGon(k="from", v="2020")])
            from app.services.ai.intent_parser import _parse_simple_request
            return _parse_simple_request("Liên quan đến Malaysia")

    gateway = Gateway([])
    parsed = analyze_ai_request("Hãy ưu tiên các nghiên cứu đặt tại Malaysia", gateway)
    assert parsed.nam_tu is None
    assert len(gateway.calls) == 2


def test_compact_request_has_small_output_and_context_limits():
    settings = SimpleNamespace(
        ollama_base_url="http://127.0.0.1:11434", ollama_model="qwen3:4b",
        ollama_embedding_model="qwen3-embedding:0.6b", ollama_timeout_seconds=900,
    )
    gateway = OllamaGateway(settings, httpx.Client())
    payload = gateway._structured_payload(YeuCauChuanHoa, "Chuẩn hóa", "Yêu cầu")
    assert payload["options"]["num_predict"] == 160
    assert payload["options"]["num_ctx"] == 1024
    assert gateway._structured_payload(PhanTichYeuCauAI, "Phân tích", "Yêu cầu")["options"]["num_predict"] == 1024
    assert gateway._structured_payload(KetQuaDanhGiaAI, "Đánh giá", "Yêu cầu")["options"]["num_predict"] == 1024
    assert gateway._timeout == 900
    gateway._client.close()


def test_pdf_prompt_omits_empty_analysis_fields():
    intent = _parse_simple_request("Liên quan đến Malaysia; Từ năm 2020")
    document = TaiLieu(tieu_de="Paper", tac_gia=[], tu_khoa=[], ma_ngoai={}, du_lieu_nguon={})
    prompt = json.loads(_evaluation_prompt("Yêu cầu", intent, document, []))
    analyzed = prompt["dieu_kien_da_phan_tich"]
    assert analyzed["required_topics"] == ["Malaysia"]
    assert analyzed["nam_tu"] == 2020
    assert "chu_de_tuy_chon" not in analyzed
    assert "parse_duration_ms" not in analyzed
