"""Chọn bằng chứng PDF ngắn và kiểm tra điều kiện mô hình nghiên cứu."""

from __future__ import annotations

import re
import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.services.ai.client import AIGateway
from app.services.ai.pdf_cache import CachedPdf, _write_json, cached_phrase_locations, pdf_cache_key


SEMANTIC_PROMPT_VERSION = "model-context-v2"


_HEADINGS = {
    "literature review": "passive", "related work": "passive", "references": "passive",
    "bibliography": "passive", "introduction": "passive",
    "conceptual framework": "active", "research model": "active",
    "hypotheses": "active", "hypothesis development": "active",
    "methodology": "active", "methods": "active", "measures": "active",
    "variables": "active", "results": "active", "analysis": "active",
    "mô hình nghiên cứu": "active", "giả thuyết": "active",
    "phương pháp": "active", "kết quả": "active",
    "tổng quan tài liệu": "passive", "tài liệu tham khảo": "passive",
}
_SYNONYMS = {
    "customer satisfaction": ["customer satisfaction", "consumer satisfaction", "sự hài lòng của khách hàng", "sự hài lòng khách hàng"],
}


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    page: int = Field(ge=1)
    section: str
    text: str


class SemanticDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["matched", "unmatched", "insufficient"]
    confidence: int = Field(ge=0, le=100)
    evidence: list[Evidence]
    reason: str


def _section_lines(text: str, previous: str) -> list[tuple[str, str]]:
    section = previous
    result = []
    for line in text.splitlines():
        heading = re.sub(r"^\d+(?:\.\d+)*\s*", "", line.strip()).rstrip(":.").casefold()
        if heading in _HEADINGS:
            section = heading
        result.append((line, section))
    return result


def candidate_evidence(pdf: CachedPdf, construct: str, cache_dir: Path, limit: int = 6) -> tuple[list[dict], bool]:
    synonyms = _SYNONYMS.get(construct.casefold(), [construct])
    locations = cached_phrase_locations(pdf, synonyms, cache_dir)
    if not any(locations.values()):
        return [], False
    candidates = []
    section = ""
    for page in pdf.pages:
        lines = _section_lines(page.text, section)
        if lines:
            section = lines[-1][1]
        for index, (line, line_section) in enumerate(lines):
            if not any(phrase.casefold() in line.casefold() for phrase in synonyms):
                continue
            excerpt = " ".join(item[0].strip() for item in lines[max(0, index - 2):min(len(lines), index + 3)])[:850]
            category = _HEADINGS.get(line_section, "unknown")
            candidates.append({"page": page.page_number, "section": line_section or "Không rõ", "text": excerpt, "category": category})
    candidates.sort(key=lambda item: {"active": 0, "unknown": 1, "passive": 2}[item["category"]])
    return candidates[:limit], True


def _explicit_model_evidence(construct: str, candidates: list[dict]) -> dict | None:
    synonyms = _SYNONYMS.get(construct.casefold(), [construct])
    for item in candidates:
        if item["category"] != "active":
            continue
        text = item["text"]
        for phrase in synonyms:
            escaped = re.escape(phrase)
            variable = rf"\b{escaped}\s+is\s+(?:a|an|the)\s+(?:dependent|independent|mediating|moderating|control)\s+variable\s+in\s+(?:our|this)\s+(?:research|conceptual)\s+model\b"
            hypothesis = rf"\bH\d+[a-z]?\s*[:.]\s*{escaped}\s+(?:predicts|affects|influences|impacts|mediates|moderates)\b"
            if re.search(variable, text, re.IGNORECASE) or ("hypothes" in item["section"] and re.search(hypothesis, text, re.IGNORECASE)):
                return item
    return None


def _result_path(pdf_key: str, constructs: list[str], cache_dir: Path, model: str) -> Path:
    cache_key = hashlib.sha256(json.dumps(
        [pdf_key, SEMANTIC_PROMPT_VERSION, model, [item.casefold() for item in constructs]],
        ensure_ascii=False,
    ).encode()).hexdigest()
    return cache_dir / f"{cache_key}-semantic.json"


def cached_decision(content: bytes, constructs: list[str], cache_dir: Path, model: str) -> SemanticDecision | None:
    result_path = _result_path(pdf_cache_key(content), constructs, cache_dir, model)
    if result_path.is_file():
        try:
            return SemanticDecision.model_validate_json(result_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    return None


def check_constructs(pdf: CachedPdf, constructs: list[str], cache_dir: Path, gateway: AIGateway) -> SemanticDecision:
    result_path = _result_path(pdf.key, constructs, cache_dir, gateway.model)
    if result_path.is_file():
        try:
            return SemanticDecision.model_validate_json(result_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    all_candidates = []
    explicit = []
    for construct in constructs:
        candidates, found = candidate_evidence(pdf, construct, cache_dir)
        if not found:
            decision = SemanticDecision(status="unmatched", confidence=100, evidence=[], reason=f"Không tìm thấy {construct} hoặc từ đồng nghĩa trong PDF.")
            _write_json(result_path, decision.model_dump())
            return decision
        if candidates and all(item["category"] == "passive" for item in candidates):
            decision = SemanticDecision(status="unmatched", confidence=100, evidence=[], reason=f"{construct} chỉ xuất hiện trong phần tổng quan hoặc tài liệu tham khảo.")
            _write_json(result_path, decision.model_dump())
            return decision
        direct = _explicit_model_evidence(construct, candidates)
        if direct:
            explicit.append(Evidence(page=direct["page"], section=direct["section"], text=direct["text"]))
        all_candidates.extend({"construct": construct, **item} for item in candidates)
    if len(explicit) == len(constructs):
        decision = SemanticDecision(
            status="matched", confidence=95, evidence=explicit,
            reason="Các khái niệm được nêu rõ là biến trong mô hình hoặc giả thuyết của nghiên cứu.",
        )
        _write_json(result_path, decision.model_dump())
        return decision
    prompt = {
        "required_constructs": constructs,
        "excerpts": all_candidates[:8],
    }
    instruction = (
        "The required_constructs are target names from the user, not a claim by the paper. "
        "Mark matched if every target is used as a variable in this paper's own model, hypotheses, methods or analysis. "
        "A dependent variable in our research model qualifies. Literature review and references do not. "
        "Quote exact evidence from excerpts with page and section. If uncertain, status=insufficient. "
        "Return compact JSON. Vietnamese reason: at most 15 words."
    )
    decision = gateway.structured_output(SemanticDecision, instruction, prompt)
    if decision.status == "matched":
        valid = all(
            any(
                item.page == source["page"]
                and item.text.strip()
                and item.text.strip().casefold() in source["text"].casefold()
                and source["category"] == "active"
                for item in decision.evidence for source in all_candidates
                if source["construct"] == construct
            ) for construct in constructs
        )
        if not valid:
            return SemanticDecision(status="insufficient", confidence=0, evidence=[], reason="Bằng chứng do mô hình trả về không khớp đoạn PDF ở phần nghiên cứu.")
        decision = decision.model_copy(update={
            "confidence": max(90, decision.confidence),
            "reason": "Có bằng chứng trực tiếp cho khái niệm trong mô hình, giả thuyết hoặc phân tích của bài.",
        })
    elif decision.status == "unmatched":
        decision = decision.model_copy(update={"confidence": 100, "reason": "Không có bằng chứng khái niệm tham gia mô hình hoặc phân tích của bài."})
    else:
        decision = decision.model_copy(update={"confidence": 0, "reason": "Chưa đủ bằng chứng để xác định vai trò khái niệm trong bài."})
    _write_json(result_path, decision.model_dump())
    return decision
