"""Đánh giá yêu cầu tài liệu liên quan đến ngân hàng, không nhầm với biến trong mô hình."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from pathlib import Path

from app.models import TaiLieu
from app.services.ai.client import AIGateway
from app.services.ai.pdf_cache import CachedPdf, _write_json, pdf_cache_key
from app.services.ai.semantic_check import Evidence, SemanticDecision


TOPIC_PROMPT_VERSION = "bank-topic-v2"
_BANK = re.compile(r"\b(?:banks?|banking|financial institutions?|credit institutions?)\b|ngân\s+hàng", re.IGNORECASE)
_STRONG = re.compile(r"\b(?:banks|banking|financial institutions?|credit institutions?)\b|ngân\s+hàng", re.IGNORECASE)
_STUDY = re.compile(r"\b(?:study|research|survey|sample|data|employees?|respondents?|participants?|sector|industry|context|conducted|collected)\b", re.IGNORECASE)
_INCIDENTAL = re.compile(r"\b(?:world bank|state bank|university of banking|banking university)\b|\b(?:left|right)-bank\b", re.IGNORECASE)
_PRIOR_STUDY = re.compile(
    r"\b(?:a study by|another study by|prior studies|previous studies|et al\.?)\b|"
    r"\b\d{4}\)\s+study\b|\[\d+\]\s+in\s+banks?\b",
    re.IGNORECASE,
)
_REFERENCES = re.compile(r"(?im)^\s*(?:\d+\.?\s*)?(?:references|bibliography|tài liệu tham khảo)\s*$")
_ABSTRACT = re.compile(r"\babstract\b|\btóm tắt\b", re.IGNORECASE)
_ABSTRACT_END = re.compile(r"\b(?:keywords?|introduction|1\.\s*introduction|references|bibliography|từ khóa|giới thiệu|tài liệu tham khảo)\b", re.IGNORECASE)
_AFFILIATION = re.compile(r"\b(?:university|universities|institute|institution|faculty|department|school|college|academy|hospital|research center|trường đại học|viện|khoa|đơn vị công tác)\b", re.IGNORECASE)


def _excerpt(text: str, match: re.Match[str], radius: int = 160) -> str:
    return " ".join(text[max(0, match.start() - radius):match.end() + radius].split())


def check_author_affiliation_country(pdf: CachedPdf, country: str) -> SemanticDecision:
    """Chỉ xét phần thông tin tác giả trước tóm tắt, không lấy nơi nghiên cứu trong nội dung."""
    country_pattern = (
        re.compile(r"\b(?:Viet\s*Nam|Vietnam|Việt\s*Nam)\b", re.IGNORECASE)
        if country.casefold() in {"vietnam", "viet nam", "việt nam"}
        else _topic_pattern(country)
    )
    has_affiliation = False
    for page in pdf.pages[:2]:
        abstract = _ABSTRACT.search(page.text)
        front = page.text[:abstract.start()] if abstract else page.text[:4000]
        lines = front.splitlines()
        for index, line in enumerate(lines):
            if _AFFILIATION.search(line):
                has_affiliation = True
            match = country_pattern.search(line)
            if not match:
                continue
            context = " ".join(lines[max(0, index - 2):index + 2])
            if _AFFILIATION.search(context):
                evidence = Evidence(page=page.page_number, section="Thông tin tác giả", text=" ".join(context.split()))
                return SemanticDecision(status="matched", confidence=100, evidence=[evidence], reason=f"Đơn vị công tác của tác giả ở {country}.")
        if abstract:
            break
    if has_affiliation:
        return SemanticDecision(status="unmatched", confidence=90, evidence=[], reason=f"Không thấy {country} trong thông tin đơn vị công tác của tác giả.")
    return SemanticDecision(status="insufficient", confidence=0, evidence=[], reason="PDF không nêu rõ đơn vị công tác hoặc quốc gia của tác giả.")


def metadata_bank_evidence(document: TaiLieu) -> tuple[str, str] | None:
    title = document.tieu_de or ""
    if _STRONG.search(title) and not _INCIDENTAL.search(title):
        return "Tiêu đề", title
    abstract = document.tom_tat or ""
    for match in _BANK.finditer(abstract):
        excerpt = _excerpt(abstract, match)
        if _STUDY.search(excerpt) and not _INCIDENTAL.search(excerpt):
            return "Tóm tắt", excerpt
    return None


def _abstract_evidence(pdf: CachedPdf) -> Evidence | None:
    for page in pdf.pages[:2]:
        start = _ABSTRACT.search(page.text)
        if not start:
            continue
        following = page.text[start.end():start.end() + 3500]
        end = _ABSTRACT_END.search(following)
        abstract = following[:end.start()] if end else following
        for match in _BANK.finditer(abstract):
            excerpt = _excerpt(abstract, match)
            if _STUDY.search(excerpt) and not _INCIDENTAL.search(excerpt):
                return Evidence(page=page.page_number, section="Abstract", text=excerpt)
    return None


def _candidates(pdf: CachedPdf) -> list[dict]:
    result: list[dict] = []
    in_references = False
    for page in pdf.pages:
        body = page.text
        reference_heading = _REFERENCES.search(body)
        if reference_heading:
            body = body[:reference_heading.start()]
            in_references = True
        elif in_references:
            continue
        if page.page_number == 1:
            abstract = _ABSTRACT.search(body)
            if abstract:
                body = body[abstract.start():]
        for match in _BANK.finditer(body):
            excerpt = _excerpt(body, match)
            if _INCIDENTAL.search(excerpt) or _PRIOR_STUDY.search(excerpt):
                continue
            result.append({"page": page.page_number, "section": "Nội dung bài", "text": excerpt})
    return result[:8]


def _cache_path(content: bytes, cache_dir: Path, model: str) -> Path:
    key = hashlib.sha256(json.dumps([pdf_cache_key(content), TOPIC_PROMPT_VERSION, model]).encode()).hexdigest()
    return cache_dir / f"{key}-bank-topic.json"


def cached_bank_decision(content: bytes, cache_dir: Path, model: str) -> SemanticDecision | None:
    path = _cache_path(content, cache_dir, model)
    if path.is_file():
        try:
            return SemanticDecision.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    return None


def check_bank_topic(content: bytes, pdf: CachedPdf, cache_dir: Path, gateway: AIGateway) -> SemanticDecision:
    path = _cache_path(content, cache_dir, gateway.model)
    direct = _abstract_evidence(pdf)
    if direct:
        decision = SemanticDecision(status="matched", confidence=92, evidence=[direct], reason="Tóm tắt nêu rõ nghiên cứu được thực hiện trong bối cảnh ngân hàng.")
    else:
        candidates = _candidates(pdf)
        if not candidates:
            decision = SemanticDecision(status="unmatched", confidence=100, evidence=[], reason="Không tìm thấy bằng chứng nghiên cứu liên quan đến ngân hàng trong nội dung chính.")
        else:
            instruction = (
                "Decide if this paper's OWN study concerns banks or banking as its setting, participants, "
                "data or main subject. Author affiliations, World Bank statistics, references and examples "
                "of prior studies do not qualify. Use only the supplied excerpts. For matched, quote exact "
                "evidence with the supplied page and section. If uncertain, use insufficient. "
                "Return compact JSON and a Vietnamese reason of at most 15 words."
            )
            response = gateway.structured_output(SemanticDecision, instruction, {"topic": "banking", "excerpts": candidates})
            if response.status == "matched":
                valid = any(
                    item.page == source["page"] and item.text.strip()
                    and item.text.strip().casefold() in source["text"].casefold()
                    for item in response.evidence for source in candidates
                )
                decision = (
                    response.model_copy(update={"confidence": max(90, response.confidence), "reason": "Nội dung chính cho thấy bài nghiên cứu liên quan đến ngân hàng."})
                    if valid else SemanticDecision(status="insufficient", confidence=0, evidence=[], reason="Bằng chứng do mô hình trả về không khớp nội dung PDF.")
                )
            elif response.status == "unmatched":
                decision = response.model_copy(update={"confidence": 100, "reason": "Ngân hàng chỉ được nhắc ngoài chủ đề nghiên cứu chính."})
            else:
                decision = response.model_copy(update={"confidence": 0, "reason": "Chưa đủ bằng chứng để xác định bài nghiên cứu liên quan đến ngân hàng."})
    _write_json(path, decision.model_dump())
    return decision


_OWN_STUDY = re.compile(
    r"\b(?:we|our|this study|the present study|surveyed|sampled|respondents?|participants?|"
    r"data (?:were |was )?(?:collected|obtained|gathered|from)|conducted in|study was conducted)\b",
    re.IGNORECASE,
)


def _topic_pattern(topic: str) -> re.Pattern[str]:
    topic = topic.strip()
    if topic == "banking":
        return _BANK
    terms = [topic]
    plain = "".join(char for char in unicodedata.normalize("NFD", topic.replace("\u0111", "d").replace("\u0110", "D")) if not unicodedata.combining(char))
    if plain != topic:
        terms.extend([plain, re.sub(r"\s+", "", plain)])
    if topic.casefold().endswith("ia"):
        terms.append(topic + "n")
    alternatives = [re.escape(term).replace(r"\ ", r"\s+") for term in terms if term]
    return re.compile(r"(?<!\w)(?:" + "|".join(alternatives) + r")(?!\w)", re.IGNORECASE)


def metadata_topic_evidence(document: TaiLieu, topic: str) -> tuple[str, str] | None:
    if topic == "banking":
        return metadata_bank_evidence(document)
    pattern = _topic_pattern(topic)
    title = document.tieu_de or ""
    if pattern.search(title):
        return "Tiêu đề", title
    abstract = document.tom_tat or ""
    match = pattern.search(abstract)
    if match and not _PRIOR_STUDY.search(_excerpt(abstract, match)):
        return "Tóm tắt", _excerpt(abstract, match)
    return None


def check_general_topic(pdf: CachedPdf, topic: str) -> SemanticDecision:
    pattern = _topic_pattern(topic)
    for page in pdf.pages[:2]:
        start = _ABSTRACT.search(page.text)
        if not start:
            continue
        following = page.text[start.end():start.end() + 3500]
        end = _ABSTRACT_END.search(following)
        abstract = following[:end.start()] if end else following
        for match in pattern.finditer(abstract):
            excerpt = _excerpt(abstract, match)
            if topic == "banking" and _INCIDENTAL.search(excerpt):
                continue
            if not _PRIOR_STUDY.search(excerpt):
                evidence = Evidence(page=page.page_number, section="Abstract", text=excerpt)
                return SemanticDecision(status="matched", confidence=92, evidence=[evidence], reason=f"Tóm tắt cho thấy nghiên cứu liên quan đến {topic}.")

    candidates: list[Evidence] = []
    in_references = False
    for page in pdf.pages:
        body = page.text
        reference_heading = _REFERENCES.search(body)
        if reference_heading:
            body = body[:reference_heading.start()]
            in_references = True
        elif in_references:
            continue
        if page.page_number == 1:
            abstract = _ABSTRACT.search(body)
            if abstract:
                body = body[abstract.start():]
        for match in pattern.finditer(body):
            excerpt = _excerpt(body, match)
            if _PRIOR_STUDY.search(excerpt) or (topic == "banking" and _INCIDENTAL.search(excerpt)):
                continue
            candidates.append(Evidence(page=page.page_number, section="Nội dung bài", text=excerpt))
            if _OWN_STUDY.search(excerpt):
                return SemanticDecision(status="matched", confidence=88, evidence=[candidates[-1]], reason=f"Nội dung nghiên cứu liên quan đến {topic}.")
    if candidates:
        return SemanticDecision(status="insufficient", confidence=0, evidence=candidates[:1], reason=f"Bài có nhắc đến {topic} nhưng chưa rõ vai trò trong nghiên cứu.")
    return SemanticDecision(status="unmatched", confidence=100, evidence=[], reason=f"Không thấy bằng chứng nghiên cứu liên quan đến {topic}.")


def find_topic_mention(pdf: CachedPdf, topic: str) -> Evidence | None:
    pattern = _topic_pattern(topic)
    for page in pdf.pages:
        match = pattern.search(page.text)
        if match:
            return Evidence(page=page.page_number, section="Nội dung PDF", text=_excerpt(page.text, match))
    return None


def metadata_mention(document: TaiLieu, term: str) -> tuple[str, str] | None:
    pattern = _topic_pattern(term)
    for source, text in (("Tiêu đề", document.tieu_de or ""), ("Tóm tắt", document.tom_tat or ""),
                         ("Từ khóa", " ".join(document.tu_khoa or []))):
        if pattern.search(text):
            return source, text
    return None
