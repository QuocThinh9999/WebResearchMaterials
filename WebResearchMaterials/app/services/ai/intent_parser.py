from __future__ import annotations

import re
import unicodedata

from app.services.ai.client import AIGateway, OllamaGateway
from app.services.ai.prompts import HUONG_DAN_CHUAN_HOA
from app.services.ai.schemas import DieuKienNguyenTu, PhanTichYeuCauAI, YeuCauChuanHoa
from app.services.providers import SearchCriteria


class AIConditionConflict(ValueError):
    pass


def analyze_ai_request(request_text: str, gateway: AIGateway | None = None) -> PhanTichYeuCauAI:
    request_text = request_text.strip()
    if not request_text:
        raise ValueError("Yêu cầu AI không được để trống.")
    fast = _parse_simple_request(request_text)
    if fast is not None:
        return fast
    owned_gateway = gateway is None
    gateway = gateway or OllamaGateway(timeout_seconds=120)
    try:
        for attempt in range(2):
            instruction = HUONG_DAN_CHUAN_HOA
            if attempt:
                instruction += "\nKiểm tra lại từng loại điều kiện. Chỉ dùng no_pub khi người dùng nêu rõ nhà xuất bản; chỉ dùng no_author khi nêu tên tác giả. Không đảo chiều điều kiện khẳng định và phủ định."
            normalized = gateway.structured_output(YeuCauChuanHoa, instruction, request_text)
            if isinstance(normalized, PhanTichYeuCauAI):
                return _remove_unrequested_filters(request_text, normalized)
            parsed = _parse_normalized_request(request_text, normalized)
            if parsed is not None:
                return parsed
    finally:
        if owned_gateway:
            gateway.close()
    # Câu ngoài bộ điều kiện gọn vẫn được Ollama đánh giá theo nguyên văn trong nền.
    return _empty_intent().model_copy(update={
        "dien_giai_ngan_gon": request_text,
        "needs_semantic_pdf_check": True,
        "dieu_kien_nguyen_tu": [DieuKienNguyenTu(
            noi_dung=request_text, loai="bat_buoc", trong_so=5,
            truy_van_tim_bang_chung=[request_text],
        )],
    })


def _empty_intent() -> PhanTichYeuCauAI:
    return PhanTichYeuCauAI(
        chu_de_bat_buoc=[], chu_de_tuy_chon=[], tu_khoa_tim_kiem_tieng_anh=[],
        tu_khoa_loai_tru=[], tieu_de_loai_tru=[], doi_loai_tru=[],
        tac_gia_bat_buoc=[], tac_gia_loai_tru=[],
        nha_xuat_ban_bat_buoc=[], nha_xuat_ban_loai_tru=[],
        nam_tu=None, nam_den=None, loai_tai_lieu=None, ngon_ngu=None,
        quartile=[], chi_truy_cap_mo=False, dien_giai_ngan_gon="",
        ngon_ngu_yeu_cau="vi", ngon_ngu_tai_lieu_cho_phep=[],
        khai_niem_goc_tieng_viet=[], khai_niem_tim_kiem_tieng_anh=[],
        nhom_tu_dong_nghia=[], cau_truy_van_scopus="", cau_truy_van_wos="",
    )


def _plain(value: str) -> str:
    folded = unicodedata.normalize("NFD", value.casefold().replace("đ", "d"))
    return "".join(char for char in folded if not unicodedata.combining(char))


def _origin_clause(request_text: str, country: str) -> str | None:
    original = _plain(request_text)
    country_plain = _plain(country)
    aliases = [country_plain]
    if country_plain in {"vietnam", "viet nam"}:
        aliases = ["viet nam", "vietnam"]
    positions = [original.find(alias) for alias in aliases if alias in original]
    if not positions:
        return None
    before_country = original[:min(positions)]
    return re.split(r"\b(?:va|and)\b|[;,.]", before_country)[-1]


def _parse_normalized_request(request_text: str, normalized: YeuCauChuanHoa) -> PhanTichYeuCauAI | None:
    required_countries = [item.v.strip() for item in normalized.c if item.k == "origin" and item.v.strip()]
    excluded_countries = [item.v.strip() for item in normalized.c if item.k == "no_origin" and item.v.strip()]
    if required_countries or excluded_countries:
        if not re.search(r"\b(?:cua|tac gia|don vi|affiliation|author|from)\b", _plain(request_text)):
            return None
        for country in required_countries:
            clause = _origin_clause(request_text, country)
            if clause is None or re.search(r"\b(?:khong|chang|loai tru|exclude|not|without)\b", clause):
                return None
        for country in excluded_countries:
            clause = _origin_clause(request_text, country)
            if clause is None or not re.search(r"\b(?:khong|chang|loai tru|exclude|not|without)\b", clause):
                return None
    if set(map(_plain, required_countries)) & set(map(_plain, excluded_countries)):
        return None
    clauses = YeuCauChuanHoa(c=[item for item in normalized.c if item.k not in {"origin", "no_origin"}])
    normalized_text = _normalized_condition_text(clauses) if clauses.c else ""
    parsed = _parse_simple_request(normalized_text) if normalized_text else _empty_intent() if required_countries or excluded_countries else None
    if parsed is None or not _normalization_preserves_filters(request_text, parsed):
        return None
    if not required_countries and not excluded_countries:
        return parsed
    conditions = [*parsed.dieu_kien_nguyen_tu, *(
        DieuKienNguyenTu(
            noi_dung=f"Bài phải có tác giả hoặc đơn vị công tác ở {country}.",
            loai="bat_buoc", trong_so=5,
            truy_van_tim_bang_chung=[country],
        ) for country in required_countries
    ), *(
        DieuKienNguyenTu(
            noi_dung=f"Loại bài có tác giả hoặc đơn vị công tác ở {country}.",
            loai="loai_tru", trong_so=5,
            truy_van_tim_bang_chung=[country],
        ) for country in excluded_countries
    )]
    return parsed.model_copy(update={
        "fast_rule_parsed": True,
        "required_affiliation_countries": required_countries,
        "excluded_affiliation_countries": excluded_countries,
        "needs_semantic_pdf_check": True,
        "dieu_kien_nguyen_tu": conditions,
        "dien_giai_ngan_gon": "; ".join(item.noi_dung for item in conditions),
    })


def _normalized_condition_text(normalized: YeuCauChuanHoa) -> str:
    clauses: list[str] = []
    prefixes = {
        "topic": "Liên quan đến", "no_topic": "Không liên quan đến",
        "has": "Phải có", "no_has": "Không có", "model": "Trong mô hình phải có",
        "from": "Từ năm", "to": "Đến năm", "q": "", "pub": "nhà xuất bản",
        "no_pub": "loại trừ nhà xuất bản", "author": "tác giả",
        "no_author": "loại trừ tác giả",
    }
    for condition in normalized.c:
        value = condition.v.strip()
        if condition.k in {"other", "origin", "no_origin"}:
            return ""
        if condition.k == "oa":
            clauses.append("truy cập mở")
        elif condition.k == "lang":
            if value.casefold() in {"en", "english", "tiếng anh"}:
                clauses.append("tiếng Anh")
            elif value.casefold() in {"vi", "vietnamese", "tiếng việt"}:
                clauses.append("tiếng Việt")
            else:
                return ""
        elif condition.k == "type":
            kind = value.casefold()
            if "review" in kind or "tổng quan" in kind:
                clauses.append("bài tổng quan")
            elif "conference" in kind:
                clauses.append("conference paper")
            elif "article" in kind or "nghiên cứu" in kind:
                clauses.append("bài báo nghiên cứu")
            else:
                return ""
        elif condition.k in prefixes and value:
            clauses.append(f"{prefixes[condition.k]} {value}".strip())
        else:
            return ""
    return "; ".join(clauses)


def _normalization_preserves_filters(request_text: str, intent: PhanTichYeuCauAI) -> bool:
    cleaned = _remove_unrequested_filters(request_text, intent)
    fields = ("nam_tu", "nam_den", "quartile", "loai_tai_lieu", "chi_truy_cap_mo", "ngon_ngu")
    if any(getattr(cleaned, field) != getattr(intent, field) for field in fields):
        return False
    original = request_text.casefold()
    if (intent.nha_xuat_ban_bat_buoc or intent.nha_xuat_ban_loai_tru or intent.excluded_publishers) and not re.search(r"nhà\s+xuất\s+bản|publisher|published\s+by|\b(?:MDPI|Elsevier|Springer|Wiley|Hindawi|Frontiers)\b", request_text, re.IGNORECASE):
        return False
    if (intent.tac_gia_bat_buoc or intent.tac_gia_loai_tru) and not re.search(r"tác\s+giả|\bauthor\b", original):
        return False
    if intent.required_constructs_in_model and not re.search(r"\bmodel\b|mô\s+hình|giả\s+thuyết|\bhypothes", original):
        return False
    names = (
        intent.tac_gia_bat_buoc + intent.tac_gia_loai_tru
        + intent.nha_xuat_ban_bat_buoc + intent.nha_xuat_ban_loai_tru
    )
    return all(name.casefold() in original for name in names)


_PUBLISHER = re.compile(
    r"(?:không\s+lấy(?:\s+bài)?(?:\s+từ\s+các\s+nhà\s+xuất\s+bản|\s+của)?|"
    r"exclude(?:\s+papers\s+published\s+by)?)\s+"
    r"(MDPI(?:\s+AG)?|Multidisciplinary\s+Digital\s+Publishing\s+Institute)\b",
    re.IGNORECASE,
)
_MODEL_VI = re.compile(
    r"(?:trong\s+mô\s+hình(?:\s+nghiên\s+cứu)?\s+(?:phải|cần)\s+(?:có|chứa)|"
    r"mô\s+hình\s+nghiên\s+cứu\s+(?:phải|cần)\s+(?:có|chứa))\s+"
    r"(?P<concept>[\w][\w\s-]{1,100}?)(?=\s*(?:[;,.\n]|\s+và\s+không|$))",
    re.IGNORECASE,
)
_PUBLISHER_EXCLUDED = re.compile(
    r"(?:(?:không\s+(?:lấy|chọn)\s+(?:bài\s+)?(?:của|từ)\s+|exclude\s+)"
    r"(?P<known>MDPI|Elsevier|Springer|Wiley|Hindawi|Frontiers|Sage|Taylor\s*(?:&|and)\s*Francis)\b|"
    r"(?:loại\s+trừ\s+|không\s+(?:thuộc|từ)\s+|exclude\s+)"
    r"(?:nhà\s+xuất\s+bản|publisher)\s+(?P<name>[\w][\w .&-]{1,80}?))"
    r"(?=\s*(?:[;,\n.!?]|\s+và\s+|$))",
    re.IGNORECASE,
)
_PUBLISHER_REQUIRED = re.compile(
    r"(?:nhà\s+xuất\s+bản|published\s+by|publisher)\s+"
    r"(?P<name>[\w][\w .&-]{1,80}?)(?=\s*(?:[;,\n.!?]|\s+và\s+|$))",
    re.IGNORECASE,
)
_YEAR_RANGE = re.compile(
    r"(?:từ\s+năm|năm\s+từ|(?:trong\s+)?giai\s+đoạn|between)\s*"
    r"(?P<first>(?:19|20)\d{2})\s*(?:đến|tới|[-–]|and)\s*(?:năm\s+)?(?P<last>(?:19|20)\d{2})",
    re.IGNORECASE,
)
_YEAR_FROM = re.compile(
    r"(?:(?:được\s+)?xuất\s+bản\s+)?(?:từ\s+năm|kể\s+từ\s+năm|sau\s+năm|since|after)\s*(?P<year>(?:19|20)\d{2})",
    re.IGNORECASE,
)
_YEAR_TO = re.compile(
    r"(?:(?:được\s+)?xuất\s+bản\s+)?(?:đến\s+năm|trước\s+năm|tới\s+năm|before|until)\s*(?P<year>(?:19|20)\d{2})",
    re.IGNORECASE,
)
_YEAR_EXACT = re.compile(r"(?:năm|xuất\s+bản\s+năm|published\s+in)\s*(?P<year>(?:19|20)\d{2})", re.IGNORECASE)
_QUARTILES = re.compile(
    r"(?:quartile\s+|hạng\s+|thuộc\s+)?\bQ[1-4]\b(?:\s*(?:,|hoặc|or)\s*\bQ[1-4]\b)*",
    re.IGNORECASE,
)
_LANGUAGE = re.compile(
    r"(?:bằng\s+|viết\s+bằng\s+|ngôn\s+ngữ\s+)?(?P<language>tiếng\s+Anh|tiếng\s+Việt|English|Vietnamese)\b",
    re.IGNORECASE,
)
_OPEN_ACCESS = re.compile(r"\b(?:truy\s+cập\s+mở|open[ -]access)\b", re.IGNORECASE)
_DOCUMENT_TYPE = re.compile(
    r"(?:loại\s+tài\s+liệu\s+|dạng\s+|document\s+type\s+)?"
    r"(?P<type>bài\s+báo\s+nghiên\s+cứu|bài\s+tổng\s+quan|research\s+article|review\s+article|conference\s+paper)\b",
    re.IGNORECASE,
)
_AUTHOR_EXCLUDED = re.compile(
    r"(?:không\s+(?:lấy|chọn)\s+(?:bài\s+)?(?:của\s+)?tác\s+giả|loại\s+trừ\s+tác\s+giả|"
    r"exclude\s+author)\s+(?P<name>[\w][\w .'-]{1,80}?)(?=\s*(?:[;,\n.!?]|\s+và\s+|$))",
    re.IGNORECASE,
)
_AUTHOR_REQUIRED = re.compile(
    r"(?:của\s+tác\s+giả|tác\s+giả|author)\s+"
    r"(?P<name>[\w][\w .'-]{1,80}?)(?=\s*(?:[;,\n.!?]|\s+và\s+|$))",
    re.IGNORECASE,
)
_MODEL_VI_AFTER = re.compile(
    r"(?:phải|cần)\s+(?:có|chứa)\s+(?P<concept>[\w][\w\s-]{1,100}?)\s+"
    r"trong\s+mô\s+hình(?:\s+nghiên\s+cứu)?\b",
    re.IGNORECASE,
)
_MODEL_EN = re.compile(
    r"(?P<concept>[A-Za-z][A-Za-z\s-]{1,100}?)\s+must\s+(?:be\s+included|appear)\s+in\s+"
    r"(?:the\s+)?(?:research|conceptual)\s+model\b|"
    r"(?:the\s+)?(?:research|conceptual)\s+model\s+must\s+(?:contain|include)\s+"
    r"(?P<concept_after>[A-Za-z][A-Za-z\s-]{1,100}?)(?=\s*(?:[;,.\n]|$))",
    re.IGNORECASE,
)
_PASSIVE_MODIFIER = re.compile(
    r"(?:không\s+chấp\s+nhận\s+bài\s+chỉ\s+nhắc\s+đến|"
    r"must\s+be\s+included\s+in\s+the\s+conceptual\s+model,\s+hypotheses,\s+or\s+analysis)"
    r"[^;\n]*",
    re.IGNORECASE,
)
_RELATED_TOPIC = re.compile(
    r"(?:\b(?:phải|cần|nên)\s+)?(?:có\s+)?(?:liên|luên)\s+quan(?:\s+(?:đến|tới|với))?\s+(?P<vi>[^;,.!?\n]{1,100})|"
    r"\b(?:phải|cần)\s+(?:có|chứa|bao\s+gồm|nhắc\s+đến|đề\s+cập\s+đến)\s+(?P<vi_has>[^;,.!?\n]{1,100})|"
    r"\b(?:có|chứa)\s+(?P<vi_keyword>từ\s+khóa\s+[^;,.!?\n]{1,100})|"
    r"\b(?:tìm|chọn|lọc)\s+(?:bài(?:\s+viết|\s+nghiên\s+cứu)?|tài\s+liệu)\s+về\s+(?P<vi_about>[^;,.!?\n]{1,100})|"
    r"must\s+be\s+related\s+to\s+(?P<en>[^;,.!?\n]{1,100})|"
    r"(?P<included>[A-Za-z][A-Za-z\s-]{1,100}?)\s+must\s+be\s+included\b",
    re.IGNORECASE,
)
_EXCLUDED_TOPIC = re.compile(
    r"\b(?:không|chẳng)\s+(?:được\s+)?(?:có\s+)?(?:liên|luên)\s+quan(?:\s+(?:đến|tới|với))?\s+"
    r"(?P<vi_related>[^;,.!?\n]{1,100})|"
    r"\b(?:không\s+(?:lấy|chọn)|loại\s+trừ)\s+(?:bài(?:\s+viết|\s+nghiên\s+cứu)?|tài\s+liệu)\s+"
    r"(?:về|liên\s+quan\s+đến)\s+(?P<vi_about>[^;,.!?\n]{1,100})|"
    r"\b(?:không|chẳng)\s+(?:được\s+)?(?:có|chứa|bao\s+gồm|nhắc\s+đến|đề\s+cập\s+đến)\s+(?P<vi_absent>[^;,.!?\n]{1,100})|"
    r"\b(?:must\s+not\s+be|not)\s+related\s+to\s+(?P<en_related>[^;,.!?\n]{1,100})",
    re.IGNORECASE,
)
_REQUEST_SUBJECT = re.compile(
    r"(?:(?:hãy|vui\s+lòng|tôi\s+muốn|mình\s+muốn|chỉ\s+lấy|tìm|chọn|lọc)\s+)*"
    r"(?:(?:các|những|một)\s+)?(?:bài(?:\s+báo|\s+viết|\s+nghiên\s+cứu)?|tài\s+liệu|nghiên\s+cứu)(?:\s+này)?",
    re.IGNORECASE,
)


def _normalize_topic(value: str) -> str:
    topic = re.sub(r"^(?:a|an|the)\s+", "", value.strip(" *\"'“”‘’"), flags=re.IGNORECASE).strip()
    if re.fullmatch(r"(?:(?:lĩnh\s+vực|ngành)\s+)?ngân\s+hàng|bank|banks|banking", topic, re.IGNORECASE):
        return "banking"
    return topic


def _normalize_term(value: str) -> str:
    return re.sub(r"^(?:từ\s+khóa|cụm\s+từ|nội\s+dung)\s+", "", _normalize_topic(value), flags=re.IGNORECASE)


def _extract_metadata_rules(value: str) -> tuple[str, dict[str, object], list[DieuKienNguyenTu]]:
    remaining = value
    rules: dict[str, object] = {
        "nha_xuat_ban_loai_tru": [], "nha_xuat_ban_bat_buoc": [],
        "tac_gia_loai_tru": [], "tac_gia_bat_buoc": [],
        "nam_tu": None, "nam_den": None, "quartile": [],
        "ngon_ngu": None, "loai_tai_lieu": None, "chi_truy_cap_mo": False,
    }
    conditions: list[DieuKienNguyenTu] = []

    def consume(pattern: re.Pattern[str], apply: object) -> None:
        nonlocal remaining

        def replace(match: re.Match[str]) -> str:
            note = apply(match)
            if note:
                conditions.append(DieuKienNguyenTu(noi_dung=note, loai="metadata_cung", trong_so=5))
            return " "

        remaining = pattern.sub(replace, remaining)

    def add_publisher(match: re.Match[str], key: str) -> str:
        name = match.groupdict().get("name") or match.groupdict().get("known") or "MDPI"
        name = name.strip()
        rules[key].append(name)
        return f"{'Loại nhà xuất bản' if key.endswith('loai_tru') else 'Nhà xuất bản bắt buộc'} {name}."

    consume(_AUTHOR_EXCLUDED, lambda match: _add_named_rule(rules, "tac_gia_loai_tru", match, "Loại tác giả"))
    consume(_AUTHOR_REQUIRED, lambda match: _add_named_rule(rules, "tac_gia_bat_buoc", match, "Tác giả bắt buộc"))
    consume(_PUBLISHER, lambda match: add_publisher(match, "nha_xuat_ban_loai_tru"))
    consume(_PUBLISHER_EXCLUDED, lambda match: add_publisher(match, "nha_xuat_ban_loai_tru"))
    consume(_PUBLISHER_REQUIRED, lambda match: add_publisher(match, "nha_xuat_ban_bat_buoc"))

    def year_range(match: re.Match[str]) -> str:
        first, last = int(match.group("first")), int(match.group("last"))
        rules["nam_tu"], rules["nam_den"] = first, last
        return f"Năm xuất bản từ {first} đến {last}."

    consume(_YEAR_RANGE, year_range)

    def year_bound(match: re.Match[str], field: str) -> str:
        year = int(match.group("year"))
        phrase = match.group().casefold()
        if re.search(r"\b(?:sau|after)\b", phrase):
            year += 1
        elif re.search(r"\b(?:trước|before)\b", phrase):
            year -= 1
        current = rules[field]
        rules[field] = (max if field == "nam_tu" else min)(current, year) if current is not None else year
        return f"Năm xuất bản {'từ' if field == 'nam_tu' else 'đến'} {year}."

    consume(_YEAR_FROM, lambda match: year_bound(match, "nam_tu"))
    consume(_YEAR_TO, lambda match: year_bound(match, "nam_den"))

    def year_exact(match: re.Match[str]) -> str:
        year = int(match.group("year"))
        rules["nam_tu"] = rules["nam_den"] = year
        return f"Năm xuất bản {year}."

    consume(_YEAR_EXACT, year_exact)

    def quartiles(match: re.Match[str]) -> str:
        values = re.findall(r"\bQ[1-4]\b", match.group(), flags=re.IGNORECASE)
        rules["quartile"].extend(value.upper() for value in values)
        return "Quartile thuộc " + ", ".join(value.upper() for value in values) + "."

    consume(_QUARTILES, quartiles)

    def language(match: re.Match[str]) -> str:
        code = "vi" if re.search(r"việt|vietnamese", match.group("language"), re.IGNORECASE) else "en"
        rules["ngon_ngu"] = code
        return f"Ngôn ngữ tài liệu: {'tiếng Việt' if code == 'vi' else 'tiếng Anh'}."

    consume(_LANGUAGE, language)
    consume(_OPEN_ACCESS, lambda match: _set_open_access(rules))

    def document_type(match: re.Match[str]) -> str:
        value = match.group("type").casefold()
        selected = "Review" if "review" in value or "tổng quan" in value else "Conference Paper" if "conference" in value else "Article"
        rules["loai_tai_lieu"] = selected
        return f"Loại tài liệu {selected}."

    consume(_DOCUMENT_TYPE, document_type)
    for key in ("nha_xuat_ban_loai_tru", "nha_xuat_ban_bat_buoc", "tac_gia_loai_tru", "tac_gia_bat_buoc", "quartile"):
        rules[key] = list(dict.fromkeys(rules[key]))
    return remaining, rules, conditions


def _add_named_rule(rules: dict[str, object], key: str, match: re.Match[str], label: str) -> str:
    name = match.group("name").strip()
    rules[key].append(name)
    return f"{label} {name}."


def _set_open_access(rules: dict[str, object]) -> str:
    rules["chi_truy_cap_mo"] = True
    return "Chỉ lấy tài liệu truy cập mở."


def _parse_simple_request(value: str) -> PhanTichYeuCauAI | None:
    remaining, metadata, metadata_conditions = _extract_metadata_rules(value)
    remaining = re.sub(
        r"\s+(?:và|and)\s+(?=(?:phải|cần|không|chẳng|liên\s+quan|must|not\s+related)\b)",
        "; ", remaining, flags=re.IGNORECASE,
    )
    remaining = re.sub(r"\b(?:và|and)\s*(?=[;,.!?\n]|$)", " ", remaining, flags=re.IGNORECASE)
    constructs: list[str] = []
    topics: list[str] = []
    required_terms: list[str] = []
    excluded_topics: list[str] = []
    excluded_terms: list[str] = []
    for pattern in (_MODEL_VI, _MODEL_VI_AFTER, _MODEL_EN):
        for match in pattern.finditer(remaining):
            concept = (match.groupdict().get("concept") or match.groupdict().get("concept_after") or "").strip()
            if concept:
                constructs.append(concept)
                remaining = remaining.replace(match.group(), " ", 1)
    for match in _EXCLUDED_TOPIC.finditer(remaining):
        topic = _normalize_topic(match.group("vi_related") or match.group("vi_about") or match.group("vi_absent") or match.group("en_related"))
        if topic and not re.search(r"\b(?:và|and|nhưng|but)\b|\b(?:19|20)\d{2}\b|\btrong\s+mô\s+hình\b", topic, re.IGNORECASE):
            if match.group("vi_absent"):
                excluded_terms.append(_normalize_term(topic))
            else:
                excluded_topics.append(topic)
            remaining = remaining.replace(match.group(), " ", 1)
    for match in _RELATED_TOPIC.finditer(remaining):
        if constructs and match.group("included"):
            continue
        topic = _normalize_topic(next(part for part in match.groups() if part is not None))
        if topic and not re.search(r"\b(?:và|and|nhưng|but|trong)\b|\b(?:19|20)\d{2}\b", topic, re.IGNORECASE):
            if match.group("vi_has") or match.group("vi_keyword"):
                required_terms.append(_normalize_term(topic))
            else:
                topics.append(topic)
            remaining = remaining.replace(match.group(), " ", 1)
    semantic_groups = (constructs, topics, required_terms, excluded_topics, excluded_terms)
    if (not metadata_conditions and not any(semantic_groups)) or (constructs and sum(bool(group) for group in semantic_groups) > 1):
        return None
    remaining = _PASSIVE_MODIFIER.sub(" ", remaining)
    remaining = re.sub(r"\b(?:và|and)\b\s*$", " ", remaining, flags=re.IGNORECASE)
    if _REQUEST_SUBJECT.fullmatch(remaining.strip(" \t,.;:*!?")):
        remaining = ""
    if constructs:
        remaining = re.sub(r"\b(?:hypotheses?,?\s+or\s+analysis)\b", " ", remaining, flags=re.IGNORECASE)
    remaining = re.sub(r"\b(?:và|and)\b|[\s,.;:*!?]+", "", remaining, flags=re.IGNORECASE)
    if remaining:
        return None
    lowered = value.casefold()
    language = "vi" if re.search(r"[à-ỹ]|\b(?:không|trong|mô hình|liên quan|ngân hàng)\b", lowered) else "en"
    constructs = list(dict.fromkeys(constructs))
    required_terms = list(dict.fromkeys(required_terms))
    excluded_topics = list(dict.fromkeys(excluded_topics))
    excluded_terms = list(dict.fromkeys(excluded_terms))
    conditions = metadata_conditions + [
        DieuKienNguyenTu(
            noi_dung=f"{concept} phải tham gia mô hình nghiên cứu.",
            loai="khai_niem_trong_mo_hinh", trong_so=5,
            truy_van_tim_bang_chung=[concept],
        ) for concept in constructs
    ] + [
        DieuKienNguyenTu(
            noi_dung=f"Nghiên cứu phải liên quan đến {'ngân hàng' if topic == 'banking' else topic}.",
            loai="bat_buoc", trong_so=5,
            truy_van_tim_bang_chung=["bank", "banks", "banking", "financial institution"] if topic == "banking" else [topic],
        ) for topic in dict.fromkeys(topics)
    ] + [
        DieuKienNguyenTu(
            noi_dung=f"Tài liệu phải có {term}.", loai="bat_buoc", trong_so=5,
            truy_van_tim_bang_chung=[term],
        ) for term in required_terms
    ] + [
        DieuKienNguyenTu(
            noi_dung=f"Nghiên cứu không được liên quan đến {'ngân hàng' if topic == 'banking' else topic}.",
            loai="loai_tru", trong_so=5,
            truy_van_tim_bang_chung=[topic],
        ) for topic in excluded_topics
    ] + [
        DieuKienNguyenTu(
            noi_dung=f"Tài liệu không được chứa {term}.", loai="loai_tru", trong_so=5,
            truy_van_tim_bang_chung=[term],
        ) for term in excluded_terms
    ]
    return PhanTichYeuCauAI(
        fast_rule_parsed=True,
        chu_de_bat_buoc=topics, chu_de_tuy_chon=[], tu_khoa_tim_kiem_tieng_anh=topics + required_terms,
        tu_khoa_loai_tru=excluded_terms, tieu_de_loai_tru=[], doi_loai_tru=[],
        tac_gia_bat_buoc=metadata["tac_gia_bat_buoc"], tac_gia_loai_tru=metadata["tac_gia_loai_tru"],
        nha_xuat_ban_bat_buoc=metadata["nha_xuat_ban_bat_buoc"],
        nha_xuat_ban_loai_tru=metadata["nha_xuat_ban_loai_tru"], nam_tu=metadata["nam_tu"], nam_den=metadata["nam_den"],
        loai_tai_lieu=metadata["loai_tai_lieu"], ngon_ngu=metadata["ngon_ngu"],
        quartile=metadata["quartile"], chi_truy_cap_mo=metadata["chi_truy_cap_mo"],
        dien_giai_ngan_gon="; ".join(item.noi_dung for item in conditions),
        ngon_ngu_yeu_cau=language, ngon_ngu_tai_lieu_cho_phep=[],
        khai_niem_goc_tieng_viet=[], khai_niem_tim_kiem_tieng_anh=constructs,
        nhom_tu_dong_nghia=[], cau_truy_van_scopus="", cau_truy_van_wos="",
        dieu_kien_nguyen_tu=conditions, excluded_publishers=metadata["nha_xuat_ban_loai_tru"],
        required_constructs_in_model=constructs, input_language=language,
        required_topics=topics, required_terms=required_terms, excluded_topics=excluded_topics,
        needs_semantic_pdf_check=bool(constructs or topics or required_terms or excluded_topics or excluded_terms),
    )


def _remove_unrequested_filters(request_text: str, intent: PhanTichYeuCauAI) -> PhanTichYeuCauAI:
    """Không biến giá trị mặc định hoặc suy đoán của model thành điều kiện cứng."""
    text = request_text.casefold()
    has_year = bool(re.search(r"\b(?:19|20)\d{2}\b", text))
    has_quartile = bool(re.search(r"\bq[1-4]\b|quartile", text))
    has_type = bool(re.search(r"\b(?:article|review|conference|document type)\b|loại tài liệu|bài báo|bài tổng quan", text))
    has_open = bool(re.search(r"open[ -]access|truy cập mở|\boa\b", text))
    has_language = bool(re.search(r"\b(?:english|vietnamese|language)\b|tiếng anh|tiếng việt|ngôn ngữ", text))
    updates = {
        "nam_tu": intent.nam_tu if has_year and intent.nam_tu not in (0, None) else None,
        "nam_den": intent.nam_den if has_year and intent.nam_den not in (0, None) else None,
        "quartile": intent.quartile if has_quartile else [],
        "loai_tai_lieu": intent.loai_tai_lieu if has_type and (intent.loai_tai_lieu or "").casefold() not in {"", "all", "any"} else None,
        "chi_truy_cap_mo": intent.chi_truy_cap_mo if has_open else False,
        "ngon_ngu": intent.ngon_ngu if has_language and intent.ngon_ngu else None,
        "ngon_ngu_tai_lieu_cho_phep": intent.ngon_ngu_tai_lieu_cho_phep if has_language else [],
    }
    cleaned = intent.model_copy(update=updates)
    model_context = bool(re.search(r"\b(?:model|hypothes(?:is|es))\b|mô hình|giả thuyết", text))
    original = [item for item in intent.dieu_kien_nguyen_tu if item.loai != "metadata_cung"]
    conditions: list[DieuKienNguyenTu] = []
    for item in original:
        if item.loai == "khai_niem_trong_mo_hinh" and not model_context:
            concept = item.truy_van_tim_bang_chung[0] if item.truy_van_tim_bang_chung else item.noi_dung
            item = item.model_copy(update={"noi_dung": f"Nghiên cứu phải liên quan đến {concept}.", "loai": "bat_buoc"})
        conditions.append(item)
    generated = build_atomic_conditions(cleaned)
    if not conditions:
        conditions = generated
        if model_context:
            conditions = [
                item.model_copy(update={"loai": "khai_niem_trong_mo_hinh"}) if item.loai == "bat_buoc" else item
                for item in conditions
            ]
    else:
        conditions.extend(item for item in generated if item.loai == "metadata_cung")
    return cleaned.model_copy(update={"dieu_kien_nguyen_tu": conditions})


def build_atomic_conditions(intent: PhanTichYeuCauAI) -> list[DieuKienNguyenTu]:
    conditions: list[DieuKienNguyenTu] = []
    for concept in intent.chu_de_bat_buoc:
        conditions.append(
            DieuKienNguyenTu(
                noi_dung=f"Nghiên cứu phải liên quan đến {concept}.",
                loai="bat_buoc",
                trong_so=3,
                truy_van_tim_bang_chung=[concept],
            )
        )
    for item in intent.tu_khoa_loai_tru:
        conditions.append(DieuKienNguyenTu(noi_dung=f"Loại trừ nội dung {item}.", loai="loai_tru", trong_so=5))
    metadata_parts: list[str] = []
    if intent.nam_tu is not None or intent.nam_den is not None:
        metadata_parts.append(f"Năm xuất bản từ {intent.nam_tu or 'không giới hạn'} đến {intent.nam_den or 'không giới hạn'}")
    if intent.quartile:
        metadata_parts.append(f"Quartile thuộc {', '.join(intent.quartile)}")
    if intent.loai_tai_lieu:
        metadata_parts.append(f"Loại tài liệu {intent.loai_tai_lieu}")
    for item in metadata_parts:
        conditions.append(DieuKienNguyenTu(noi_dung=item, loai="metadata_cung", trong_so=4))
    return conditions


def _same_text(left: str, right: str) -> bool:
    return left.strip().casefold() == right.strip().casefold()


def combine_conditions(manual: SearchCriteria, intent: PhanTichYeuCauAI) -> SearchCriteria:
    year_from_values = [value for value in (manual.year_from, intent.nam_tu) if value is not None]
    year_to_values = [value for value in (manual.year_to, intent.nam_den) if value is not None]
    year_from = max(year_from_values) if year_from_values else None
    year_to = min(year_to_values) if year_to_values else None
    if year_from and year_to and year_from > year_to:
        raise AIConditionConflict(
            f"Khoảng năm trên biểu mẫu mâu thuẫn với yêu cầu AI: không có năm nào nằm trong cả hai khoảng."
        )

    document_type = manual.document_type or intent.loai_tai_lieu
    if manual.document_type and intent.loai_tai_lieu and not _same_text(manual.document_type, intent.loai_tai_lieu):
        raise AIConditionConflict(
            f"Loại tài liệu trên biểu mẫu ({manual.document_type}) mâu thuẫn với yêu cầu AI ({intent.loai_tai_lieu})."
        )

    allowed_languages = {value.casefold() for value in intent.ngon_ngu_tai_lieu_cho_phep}
    ai_language = intent.ngon_ngu
    if manual.language and allowed_languages and manual.language.casefold() not in allowed_languages:
        raise AIConditionConflict(
            f"Ngôn ngữ trên biểu mẫu ({manual.language}) không thuộc ngôn ngữ tài liệu mà yêu cầu AI cho phép."
        )
    if manual.language and ai_language and not _same_text(manual.language, ai_language):
        raise AIConditionConflict(
            f"Ngôn ngữ trên biểu mẫu ({manual.language}) mâu thuẫn với yêu cầu AI ({ai_language})."
        )
    language = manual.language or ai_language

    ai_quartiles = list(dict.fromkeys(intent.quartile))
    if manual.quartiles and ai_quartiles:
        quartiles = [value for value in manual.quartiles if value in ai_quartiles]
        if not quartiles:
            raise AIConditionConflict("Quartile trên biểu mẫu không giao nhau với quartile trong yêu cầu AI.")
    else:
        quartiles = manual.quartiles or ai_quartiles

    author = manual.author or (intent.tac_gia_bat_buoc[0] if intent.tac_gia_bat_buoc else None)
    groups = [group for group in intent.nhom_tu_khoa_bat_buoc if group]
    if not groups and intent.tu_khoa_tim_kiem_tieng_anh:
        groups = [list(dict.fromkeys(intent.tu_khoa_tim_kiem_tieng_anh))]

    combined = manual.model_copy(
        update={
            "year_from": year_from,
            "year_to": year_to,
            "document_type": document_type,
            "language": language,
            "quartiles": quartiles,
            "open_access": manual.open_access or intent.chi_truy_cap_mo,
            "author": author,
            "keyword_groups": groups,
        }
    )
    return SearchCriteria.model_validate(combined.model_dump())
