from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from app.models import TaiLieu
from app.services.ai.schemas import PhanTichYeuCauAI
from app.services.providers import normalize_doi, normalize_title


PROHIBITED_PUBLISHERS = {
    "frontiers": ("frontiers", "frontiers media", "frontiers media sa"),
    "mdpi": ("mdpi", "mdpi ag", "multidisciplinary digital publishing institute"),
    "hindawi": ("hindawi", "hindawi publishing", "hindawi limited"),
}


@dataclass(slots=True)
class HardRuleResult:
    violations: list[str] = field(default_factory=list)
    passed: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)


def _contains(source: str | None, expected: str) -> bool:
    return bool(source and expected.strip().casefold() in source.casefold())


def _language_matches(actual: str, requested: str) -> bool:
    aliases = {
        "en": {"en", "eng", "english", "tiếng anh"},
        "vi": {"vi", "vie", "vietnamese", "tiếng việt"},
    }
    actual_name, requested_name = actual.strip().casefold(), requested.strip().casefold()
    for names in aliases.values():
        if actual_name in names or requested_name in names:
            return actual_name in names and requested_name in names
    return _contains(actual_name, requested_name) or _contains(requested_name, actual_name)


def _normalized_publisher(value: str | None) -> str:
    plain = unicodedata.normalize("NFKD", value or "").casefold()
    return re.sub(r"[^a-z0-9]+", " ", plain).strip()


def publisher_matches(actual: str | None, requested: str) -> bool:
    actual_name = _normalized_publisher(actual)
    requested_name = _normalized_publisher(requested)
    if not actual_name or not requested_name:
        return False
    for variants in PROHIBITED_PUBLISHERS.values():
        normalized = {_normalized_publisher(item) for item in variants}
        if requested_name in normalized and actual_name in normalized:
            return True
    return actual_name == requested_name


def prohibited_publisher(document: TaiLieu, pdf_front_matter: str | None = None) -> str | None:
    values = [_normalized_publisher(document.nha_xuat_ban)]
    if pdf_front_matter:
        values.append(_normalized_publisher(pdf_front_matter))
    for display_name, variants in PROHIBITED_PUBLISHERS.items():
        for value in values:
            if any(re.search(rf"\b{re.escape(_normalized_publisher(variant))}\b", value) for variant in variants):
                return display_name.title() if display_name != "mdpi" else "MDPI"
    return None


def evaluate_hard_rules(document: TaiLieu, intent: PhanTichYeuCauAI) -> HardRuleResult:
    result = HardRuleResult()
    blocked_publisher = prohibited_publisher(document)
    if blocked_publisher:
        result.violations.append(f"Nhà xuất bản bị loại trừ mặc định: {blocked_publisher}.")
    if intent.nam_tu is not None:
        if document.nam_xuat_ban is None:
            result.unknown.append("Không xác định được năm xuất bản tối thiểu.")
        elif document.nam_xuat_ban < intent.nam_tu:
            result.violations.append(f"Năm xuất bản {document.nam_xuat_ban} nhỏ hơn {intent.nam_tu}.")
        else:
            result.passed.append("Đạt điều kiện năm bắt đầu.")
    if intent.nam_den is not None:
        if document.nam_xuat_ban is None:
            result.unknown.append("Không xác định được năm xuất bản tối đa.")
        elif document.nam_xuat_ban > intent.nam_den:
            result.violations.append(f"Năm xuất bản {document.nam_xuat_ban} lớn hơn {intent.nam_den}.")
        else:
            result.passed.append("Đạt điều kiện năm kết thúc.")

    normalized_title = normalize_title(document.tieu_de)
    for excluded in intent.tieu_de_loai_tru:
        normalized_excluded = normalize_title(excluded)
        if normalized_excluded and (
            normalized_title == normalized_excluded
            or (len(normalized_excluded) >= 8 and normalized_excluded in normalized_title)
        ):
            result.violations.append(f"Tiêu đề nằm trong danh sách loại trừ: {excluded}.")

    document_doi = normalize_doi(document.doi)
    excluded_dois = {normalize_doi(value) for value in intent.doi_loai_tru}
    if document_doi and document_doi in excluded_dois:
        result.violations.append(f"DOI nằm trong danh sách loại trừ: {document_doi}.")

    for publisher in intent.nha_xuat_ban_loai_tru:
        if publisher_matches(document.nha_xuat_ban, publisher):
            result.violations.append(f"Nhà xuất bản bị loại trừ: {publisher}.")
        elif document.nha_xuat_ban:
            result.passed.append(f"Nhà xuất bản không thuộc danh sách loại trừ: {publisher}.")
        else:
            result.unknown.append("Không xác định được nhà xuất bản để đối chiếu điều kiện loại trừ.")
    if intent.nha_xuat_ban_bat_buoc:
        if not document.nha_xuat_ban:
            result.unknown.append("Không xác định được nhà xuất bản bắt buộc.")
        elif not any(_contains(document.nha_xuat_ban, value) for value in intent.nha_xuat_ban_bat_buoc):
            result.violations.append("Nhà xuất bản không thuộc danh sách bắt buộc.")
        else:
            result.passed.append("Đạt điều kiện nhà xuất bản bắt buộc.")

    if intent.loai_tai_lieu:
        if not document.loai_tai_lieu:
            result.unknown.append("Không xác định được loại tài liệu.")
        elif not _contains(document.loai_tai_lieu, intent.loai_tai_lieu):
            result.violations.append(f"Loại tài liệu không phải {intent.loai_tai_lieu}.")
        else:
            result.passed.append("Đạt điều kiện loại tài liệu.")
    if intent.quartile:
        if not document.quartile:
            result.unknown.append("Không xác định được quartile.")
        elif document.quartile.upper() not in intent.quartile:
            result.violations.append(f"Quartile {document.quartile} không được chấp nhận.")
        else:
            result.passed.append("Đạt điều kiện quartile.")
    allowed_languages = list(intent.ngon_ngu_tai_lieu_cho_phep)
    if intent.ngon_ngu:
        allowed_languages.append(intent.ngon_ngu)
    allowed_languages = list(dict.fromkeys(value.strip() for value in allowed_languages if value.strip()))
    if allowed_languages:
        if not document.ngon_ngu:
            result.unknown.append("Không xác định được ngôn ngữ tài liệu.")
        elif not any(_language_matches(document.ngon_ngu, value) for value in allowed_languages):
            result.violations.append("Ngôn ngữ tài liệu không thuộc danh sách cho phép.")
        else:
            result.passed.append("Đạt điều kiện ngôn ngữ.")
    if intent.chi_truy_cap_mo:
        if not document.truy_cap_mo:
            result.violations.append("Tài liệu không được xác định là truy cập mở.")
        else:
            result.passed.append("Tài liệu có truy cập mở.")

    metadata_text = " ".join([document.tieu_de, document.tom_tat or "", *(document.tu_khoa or [])])
    for keyword in intent.tu_khoa_loai_tru:
        if _contains(metadata_text, keyword):
            result.violations.append(f"Metadata chứa từ khóa bị loại trừ: {keyword}.")

    authors = " ".join(
        str(item.get("name") or item.get("display_name") or item) if isinstance(item, dict) else str(item)
        for item in (document.tac_gia or [])
    )
    for author in intent.tac_gia_loai_tru:
        if _contains(authors, author):
            result.violations.append(f"Tác giả bị loại trừ: {author}.")
    if intent.tac_gia_bat_buoc:
        if not authors:
            result.unknown.append("Không xác định được tác giả bắt buộc.")
        elif not all(_contains(authors, author) for author in intent.tac_gia_bat_buoc):
            result.violations.append("Không có đủ tác giả bắt buộc.")
        else:
            result.passed.append("Đạt điều kiện tác giả bắt buộc.")
    return result
