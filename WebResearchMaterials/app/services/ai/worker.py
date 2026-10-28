from __future__ import annotations

import datetime as dt
import logging
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass

import httpx
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database import SessionLocal
from app.models import (
    CongViecTaiPdf,
    DanhGiaAiTaiLieu,
    DeTaiTaiLieu,
    KetQuaTraCuuAi,
    LanTimKiem,
    LanTraCuuAi,
    TaiLieu,
    TepTin,
)
from app.repositories.ai_evaluations import (
    claim_next_ai_lookup_job,
    claim_next_job,
    recover_stale_ai_lookup_jobs,
    update_ai_lookup_progress,
    update_search_progress,
)
from app.repositories.pdf_jobs import (
    claim_next_pdf_job,
    recover_stale_pdf_jobs,
    update_pdf_search_progress,
)
from app.services.ai.client import AIGateway, OllamaGateway
from app.services.ai.evaluator import evaluate_document, evaluate_document_with_vision
from app.services.ai.hard_rules import evaluate_hard_rules, prohibited_publisher
from app.services.ai.intent_parser import build_atomic_conditions
from app.services.ai.pdf_reader import create_chunks, extract_pdf_pages, select_chunks_for_conditions, select_top_chunks
from app.services.ai.pdf_cache import cached_pdf
from app.services.ai.semantic_check import cached_decision, check_constructs
from app.services.ai.topic_relevance import cached_bank_decision, check_author_affiliation_country, check_bank_topic, check_general_topic, find_topic_mention, metadata_bank_evidence, metadata_mention, metadata_topic_evidence
from app.services.ai.prompts import PHIEN_BAN_PROMPT_DANH_GIA
from app.services.ai.schemas import PhanTichYeuCauAI
from app.services.files import resolve_stored_path
from app.services.pdf_cancel import PdfSearchCancelled, bind_pdf_search, pdf_search_cancelled
from app.services.pdf_downloader import HostRequestLimiter, PdfConnectivityError
from app.services.pdf_worker import process_pdf_job
from app.services.pdf_browser import PdfBrowserAccessRequired


_PDF_BATCH_LOCK = threading.Lock()
_PDF_SEARCH_LOCKS: dict[uuid.UUID, threading.Lock] = {}
_PDF_LIMITER_LOCK = threading.Lock()
_PDF_PROCESS_SLOTS: dict[int, threading.BoundedSemaphore] = {}
_PDF_HOST_LIMITERS: dict[int, HostRequestLimiter] = {}
logger = logging.getLogger(__name__)


@dataclass(slots=True)
class JobContext:
    evaluation: DanhGiaAiTaiLieu
    search: LanTimKiem
    document: TaiLieu


def _load_context(db: Session, job_id: uuid.UUID) -> JobContext:
    row = db.execute(
        select(DanhGiaAiTaiLieu, LanTimKiem, TaiLieu)
        .join(LanTimKiem, LanTimKiem.id == DanhGiaAiTaiLieu.id_lan_tim_kiem)
        .join(DeTaiTaiLieu, DeTaiTaiLieu.id == DanhGiaAiTaiLieu.id_de_tai_tai_lieu)
        .join(TaiLieu, TaiLieu.id == DeTaiTaiLieu.id_tai_lieu)
        .where(DanhGiaAiTaiLieu.id == job_id)
    ).one_or_none()
    if not row:
        raise ValueError("Không tìm thấy công việc AI.")
    return JobContext(row[0], row[1], row[2])


def _set_result(
    evaluation: DanhGiaAiTaiLieu,
    *,
    status: str,
    label: str,
    score: float | None = None,
    reason: str | None = None,
    evidence: list[dict] | None = None,
    details: dict | None = None,
    model: str | None = None,
    error: str | None = None,
) -> None:
    evaluation.trang_thai_xu_ly = status
    evaluation.nhan = label
    evaluation.diem_phu_hop = score
    evaluation.ly_do = reason
    evaluation.bang_chung = evidence or []
    evaluation.chi_tiet_dieu_kien = details or {}
    evaluation.mo_hinh = model
    evaluation.phien_ban_prompt = PHIEN_BAN_PROMPT_DANH_GIA if model else None
    evaluation.loi_xu_ly = error
    evaluation.ngay_cap_nhat = dt.datetime.now(dt.UTC)


def _pdf_content(db: Session, context: JobContext, settings: Settings) -> bytes | None:
    document = context.document
    if document.id_file_pdf:
        record = db.get(TepTin, document.id_file_pdf)
        if record:
            return resolve_stored_path(record, settings).read_bytes()
    return None


def process_claimed_job(
    db: Session,
    job_id: uuid.UUID,
    gateway: AIGateway,
    settings: Settings,
) -> None:
    context = _load_context(db, job_id)
    intent = PhanTichYeuCauAI.model_validate(context.search.dieu_kien_ai)
    hard_rules = evaluate_hard_rules(context.document, intent)
    details = {
        "dat": hard_rules.passed,
        "vi_pham": hard_rules.violations,
        "khong_xac_dinh": hard_rules.unknown,
    }
    if hard_rules.violations:
        _set_result(
            context.evaluation,
            status="hoan_thanh",
            label="khong_phu_hop",
            score=0,
            reason="Tài liệu vi phạm điều kiện loại trừ hoặc điều kiện cứng.",
            details=details,
        )
        return

    if "banking" in intent.required_topics and (bank_evidence := metadata_bank_evidence(context.document)):
        _set_lookup_result(
            context.result, status="hoan_thanh", label="phu_hop", score=95,
            reason=f"{bank_evidence[0]} cho thấy bài nghiên cứu liên quan đến ngân hàng.",
            evidence=[{"trang": None, "nguon": bank_evidence[0], "noi_dung": bank_evidence[1], "dieu_kien": "Liên quan đến ngân hàng", "dien_giai_tieng_viet": "Bằng chứng trong metadata."}],
            details={"dieu_kien_cung": hard_details, "phuong_phap": "quy_tac", "so_lan_goi_ollama": 0, "thoi_gian_xu_ly_ms": round((time.perf_counter() - started) * 1000, 2)},
        )
        return

    try:
        content = _pdf_content(db, context, settings)
    except (FileNotFoundError, ValueError) as exc:
        _set_result(
            context.evaluation,
            status="khong_co_pdf",
            label="chua_danh_gia",
            reason="Không thể lấy bản PDF công khai hợp lệ để hệ thống AI đọc nội dung.",
            details=details,
            error=str(exc),
        )
        return
    if not content:
        _set_result(
            context.evaluation,
            status="khong_co_pdf",
            label="chua_danh_gia",
            reason="Tài liệu không có bản PDF truy cập mở hoặc tệp PDF đã lưu.",
            details=details,
        )
        return

    pages = extract_pdf_pages(content)
    chunks = create_chunks(pages)
    if chunks:
        query = "\n".join(
            [
                context.search.yeu_cau_ai or "",
                *intent.khai_niem_tim_kiem_tieng_anh,
                *intent.chu_de_bat_buoc,
            ]
        )
        selected = select_top_chunks(query, chunks, gateway, settings.ai_top_chunks)
        result = evaluate_document(
            context.search.yeu_cau_ai or "",
            intent,
            context.document,
            selected,
            gateway,
            settings.ai_relevance_threshold,
        )
    elif settings.ai_allow_pdf_vision:
        result = evaluate_document_with_vision(
            context.search.yeu_cau_ai or "",
            intent,
            context.document,
            content,
            gateway,
            settings.ai_relevance_threshold,
        )
    else:
        _set_result(
            context.evaluation,
            status="loi",
            label="chua_danh_gia",
            reason="PDF không có lớp văn bản và chế độ đọc PDF bằng thị giác đang tắt.",
            details=details,
        )
        return
    details.update(
        {
            "dieu_kien_dat_ai": result.dieu_kien_dat,
            "dieu_kien_khong_dat_ai": result.dieu_kien_khong_dat,
            "dieu_kien_khong_xac_dinh_ai": result.dieu_kien_khong_xac_dinh,
        }
    )
    _set_result(
        context.evaluation,
        status="hoan_thanh",
        label=result.nhan,
        score=result.diem_phu_hop,
        reason=result.ly_do,
        evidence=[item.model_dump(mode="json") for item in result.bang_chung],
        details=details,
        model=gateway.model,
    )


def _recover_stale_jobs(db: Session) -> None:
    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=30)
    db.execute(
        update(DanhGiaAiTaiLieu)
        .where(
            DanhGiaAiTaiLieu.trang_thai_xu_ly == "dang_xu_ly",
            DanhGiaAiTaiLieu.ngay_cap_nhat < cutoff,
        )
        .values(trang_thai_xu_ly="cho_xu_ly", loi_xu_ly="Tự động phục hồi sau khi worker gián đoạn.")
    )


@dataclass(slots=True)
class LookupJobContext:
    result: KetQuaTraCuuAi
    lookup: LanTraCuuAi
    document: TaiLieu


def _load_lookup_context(db: Session, job_id: uuid.UUID) -> LookupJobContext:
    row = db.execute(
        select(KetQuaTraCuuAi, LanTraCuuAi, TaiLieu)
        .join(LanTraCuuAi, LanTraCuuAi.id == KetQuaTraCuuAi.id_lan_tra_cuu_ai)
        .join(DeTaiTaiLieu, DeTaiTaiLieu.id == KetQuaTraCuuAi.id_de_tai_tai_lieu)
        .join(TaiLieu, TaiLieu.id == DeTaiTaiLieu.id_tai_lieu)
        .where(KetQuaTraCuuAi.id == job_id)
    ).one_or_none()
    if not row:
        raise ValueError("Không tìm thấy công việc tra cứu bằng AI.")
    return LookupJobContext(row[0], row[1], row[2])


def _set_lookup_result(
    result_row: KetQuaTraCuuAi,
    *,
    status: str,
    label: str,
    score: float | None = None,
    reason: str | None = None,
    evidence: list[dict] | None = None,
    details: dict | None = None,
    model: str | None = None,
    error: str | None = None,
) -> None:
    unknown_metadata = (details or {}).get("dieu_kien_cung", {}).get("khong_xac_dinh", [])
    if status == "hoan_thanh" and label == "phu_hop" and unknown_metadata:
        label, score = "chua_danh_gia", None
        reason = "Thiếu metadata để xác minh điều kiện: " + " ".join(unknown_metadata)
    result_row.trang_thai_xu_ly = status
    result_row.nhan = label
    result_row.diem_phu_hop = score
    result_row.ly_do = reason
    result_row.bang_chung = evidence or []
    result_row.chi_tiet_dieu_kien = details or {}
    result_row.mo_hinh = model
    result_row.phien_ban_prompt = PHIEN_BAN_PROMPT_DANH_GIA if model else None
    result_row.loi_xu_ly = error
    result_row.ngay_cap_nhat = dt.datetime.now(dt.UTC)


def process_ai_lookup_job(
    db: Session,
    job_id: uuid.UUID,
    gateway: AIGateway,
    settings: Settings,
) -> None:
    started = time.perf_counter()
    context = _load_lookup_context(db, job_id)
    intent = PhanTichYeuCauAI.model_validate(context.lookup.dieu_kien_ai)
    if not intent.dieu_kien_nguyen_tu:
        intent = intent.model_copy(update={"dieu_kien_nguyen_tu": build_atomic_conditions(intent)})
    simple_condition_count = sum(len(group) for group in (
        intent.required_topics, intent.required_terms, intent.excluded_topics, intent.tu_khoa_loai_tru,
        intent.required_affiliation_countries,
        intent.excluded_affiliation_countries,
    ))
    hard_rules = evaluate_hard_rules(context.document, intent)
    hard_details = {
        "dat": hard_rules.passed,
        "vi_pham": hard_rules.violations,
        "khong_xac_dinh": hard_rules.unknown,
    }
    if hard_rules.violations:
        _set_lookup_result(
            context.result,
            status="hoan_thanh",
            label="khong_phu_hop",
            score=0,
            reason="Tài liệu vi phạm điều kiện cứng: " + " ".join(hard_rules.violations),
            details={"dieu_kien_cung": hard_details, "ket_qua_tung_dieu_kien": []},
        )
        return

    if simple_condition_count == 1 and len(intent.required_terms) == 1 and (required_mention := metadata_mention(context.document, intent.required_terms[0])):
        _set_lookup_result(
            context.result, status="hoan_thanh", label="phu_hop", score=95,
            reason=f"{required_mention[0]} chứa nội dung bắt buộc: {intent.required_terms[0]}.",
            evidence=[{"trang": None, "nguon": required_mention[0], "noi_dung": required_mention[1]}],
            details={"dieu_kien_cung": hard_details, "phuong_phap": "quy_tac", "so_lan_goi_ollama": 0},
        )
        return

    if simple_condition_count == 1 and len(intent.excluded_topics) == 1 and (excluded_evidence := metadata_topic_evidence(context.document, intent.excluded_topics[0])):
        _set_lookup_result(
            context.result, status="hoan_thanh", label="khong_phu_hop", score=0,
            reason=f"{excluded_evidence[0]} cho thấy bài nghiên cứu liên quan đến chủ đề đã loại trừ.",
            evidence=[{"trang": None, "nguon": excluded_evidence[0], "noi_dung": excluded_evidence[1]}],
            details={"dieu_kien_cung": hard_details, "phuong_phap": "quy_tac", "so_lan_goi_ollama": 0},
        )
        return

    if intent.input_language and not intent.needs_semantic_pdf_check:
        unknown = bool(hard_rules.unknown)
        _set_lookup_result(
            context.result,
            status="hoan_thanh",
            label="chua_danh_gia" if unknown else "phu_hop",
            score=None if unknown else 100,
            reason="Thiếu metadata để xác minh điều kiện." if unknown else "Đạt toàn bộ điều kiện metadata.",
            details={"dieu_kien_cung": hard_details, "phuong_phap": "quy_tac", "thoi_gian_xu_ly_ms": round((time.perf_counter() - started) * 1000, 2)},
        )
        return

    if not context.document.id_file_pdf:
        _set_lookup_result(
            context.result,
            status="khong_co_pdf",
            label="chua_danh_gia",
            reason="Tài liệu không có PDF đã lưu nên được bỏ qua.",
            details={"dieu_kien_cung": hard_details},
        )
        return
    record = db.get(TepTin, context.document.id_file_pdf)
    if not record:
        _set_lookup_result(
            context.result,
            status="khong_co_pdf",
            label="chua_danh_gia",
            reason="Bản ghi PDF không còn tồn tại nên tài liệu được bỏ qua.",
            details={"dieu_kien_cung": hard_details},
        )
        return
    try:
        content = resolve_stored_path(record, settings).read_bytes()
    except OSError as exc:
        _set_lookup_result(
            context.result,
            status="khong_co_pdf",
            label="chua_danh_gia",
            reason="Tệp PDF vật lý không còn tồn tại nên tài liệu được bỏ qua.",
            details={"dieu_kien_cung": hard_details},
            error=str(exc),
        )
        return
    if not content.startswith(b"%PDF-"):
        _set_lookup_result(
            context.result,
            status="khong_co_pdf",
            label="chua_danh_gia",
            reason="Tệp đã lưu không có chữ ký PDF hợp lệ nên tài liệu được bỏ qua.",
            details={"dieu_kien_cung": hard_details},
        )
        return

    # Giải phóng transaction đọc trước khi chờ PDF và Ollama.
    db.expire_on_commit = False
    db.commit()
    cache_dir = settings.upload_path / ".ai-cache"

    if intent.fast_rule_parsed and (simple_condition_count > 1 or intent.required_affiliation_countries or intent.excluded_affiliation_countries) and not intent.required_constructs_in_model:
        pdf = cached_pdf(content, cache_dir)
        if not pdf.chunks:
            _set_lookup_result(
                context.result, status="loi", label="chua_danh_gia",
                reason="PDF không có lớp văn bản đủ để kiểm tra các điều kiện nội dung.",
                details={"dieu_kien_cung": hard_details, "cache_hit": pdf.hit},
            )
            return
        failed: list[str] = []
        unknown: list[str] = []
        evidence: list[dict] = []
        for topic in intent.required_topics:
            decision = check_general_topic(pdf, topic)
            if decision.status == "unmatched":
                failed.append(f"Không liên quan đến {topic}.")
            elif decision.status == "insufficient":
                unknown.append(f"Chưa đủ bằng chứng về {topic}.")
            evidence.extend({"trang": item.page, "phan_muc": item.section, "noi_dung": item.text}
                            for item in decision.evidence)
        for term in intent.required_terms:
            mention = metadata_mention(context.document, term) or find_topic_mention(pdf, term)
            if not mention:
                failed.append(f"Không có nội dung bắt buộc: {term}.")
            elif not isinstance(mention, tuple):
                evidence.append({"trang": mention.page, "phan_muc": mention.section, "noi_dung": mention.text})
        for topic in intent.excluded_topics:
            decision = check_general_topic(pdf, topic)
            if decision.status == "matched":
                failed.append(f"Có liên quan đến chủ đề bị loại trừ: {topic}.")
            elif decision.status == "insufficient":
                unknown.append(f"Chưa đủ bằng chứng loại trừ {topic}.")
            evidence.extend({"trang": item.page, "phan_muc": item.section, "noi_dung": item.text}
                            for item in decision.evidence)
        for term in intent.tu_khoa_loai_tru:
            mention = find_topic_mention(pdf, term)
            if mention:
                failed.append(f"PDF chứa nội dung bị loại trừ: {term}.")
                evidence.append({"trang": mention.page, "phan_muc": mention.section, "noi_dung": mention.text})
        for country in intent.required_affiliation_countries:
            decision = check_author_affiliation_country(pdf, country)
            if decision.status == "unmatched":
                failed.append(f"Không thấy tác giả hoặc đơn vị công tác ở {country}.")
            elif decision.status == "insufficient":
                unknown.append(f"Chưa đủ thông tin xác minh tác giả hoặc đơn vị công tác ở {country}.")
            evidence.extend({"trang": item.page, "phan_muc": item.section, "noi_dung": item.text}
                            for item in decision.evidence)
        for country in intent.excluded_affiliation_countries:
            decision = check_author_affiliation_country(pdf, country)
            if decision.status == "matched":
                failed.append(f"Tác giả hoặc đơn vị công tác ở {country}.")
            elif decision.status == "insufficient":
                unknown.append(f"Chưa đủ thông tin về quốc gia tác giả hoặc đơn vị công tác để loại {country}.")
            evidence.extend({"trang": item.page, "phan_muc": item.section, "noi_dung": item.text}
                            for item in decision.evidence)
        label = "khong_phu_hop" if failed else "chua_danh_gia" if unknown else "phu_hop"
        reason = " ".join(failed or unknown) if label != "phu_hop" else "Đạt tất cả điều kiện nội dung."
        _set_lookup_result(
            context.result, status="hoan_thanh", label=label,
            score=0 if failed else None if unknown else 95, reason=reason, evidence=evidence,
            details={"dieu_kien_cung": hard_details, "phuong_phap": "quy_tac", "so_lan_goi_ollama": 0,
                     "cache_hit": pdf.hit, "thoi_gian_xu_ly_ms": round((time.perf_counter() - started) * 1000, 2)},
        )
        return

    if len(intent.required_terms) == 1 and not intent.required_topics and not intent.required_constructs_in_model:
        term = intent.required_terms[0]
        pdf = cached_pdf(content, cache_dir)
        if not pdf.chunks:
            _set_lookup_result(
                context.result, status="loi", label="chua_danh_gia",
                reason="PDF không có lớp văn bản đủ để kiểm tra nội dung bắt buộc.",
                details={"dieu_kien_cung": hard_details, "cache_hit": pdf.hit},
            )
            return
        mention = find_topic_mention(pdf, term)
        reason = f"PDF có nội dung bắt buộc: {term}." if mention else f"Không tìm thấy nội dung bắt buộc: {term}."
        evidence = [{"trang": mention.page, "phan_muc": mention.section, "noi_dung": mention.text}] if mention else []
        _set_lookup_result(
            context.result, status="hoan_thanh", label="phu_hop" if mention else "khong_phu_hop",
            score=100 if mention else 0, reason=reason, evidence=evidence,
            details={"dieu_kien_cung": hard_details, "phuong_phap": "quy_tac", "so_lan_goi_ollama": 0,
                     "cache_hit": pdf.hit, "thoi_gian_xu_ly_ms": round((time.perf_counter() - started) * 1000, 2)},
        )
        return

    if len(intent.excluded_topics) == 1 and not intent.required_topics and not intent.required_constructs_in_model:
        topic = intent.excluded_topics[0]
        pdf = cached_pdf(content, cache_dir)
        if not pdf.chunks:
            _set_lookup_result(
                context.result, status="loi", label="chua_danh_gia",
                reason="PDF không có lớp văn bản đủ để xác minh chủ đề loại trừ.",
                details={"dieu_kien_cung": hard_details, "cache_hit": pdf.hit},
            )
            return
        decision = check_general_topic(pdf, topic)
        label = {"matched": "khong_phu_hop", "unmatched": "phu_hop", "insufficient": "chua_danh_gia"}[decision.status]
        reason = (
            f"Bài nghiên cứu liên quan đến chủ đề đã loại trừ: {topic}." if label == "khong_phu_hop"
            else f"Không thấy bài nghiên cứu liên quan đến chủ đề đã loại trừ: {topic}." if label == "phu_hop"
            else f"Chưa đủ bằng chứng để xác định bài có liên quan đến {topic}."
        )
        evidence = [
            {"trang": item.page, "phan_muc": item.section, "noi_dung": item.text,
             "dieu_kien": f"Không liên quan đến {topic}", "dien_giai_tieng_viet": reason}
            for item in decision.evidence
        ]
        _set_lookup_result(
            context.result, status="hoan_thanh", label=label,
            score=100 if label == "phu_hop" else 0 if label == "khong_phu_hop" else None,
            reason=reason, evidence=evidence,
            details={"dieu_kien_cung": hard_details, "phuong_phap": "quy_tac", "so_lan_goi_ollama": 0,
                     "cache_hit": pdf.hit, "thoi_gian_xu_ly_ms": round((time.perf_counter() - started) * 1000, 2)},
        )
        return

    if intent.input_language and len(intent.tu_khoa_loai_tru) == 1 and not intent.excluded_topics and not intent.required_topics and not intent.required_constructs_in_model:
        term = intent.tu_khoa_loai_tru[0]
        pdf = cached_pdf(content, cache_dir)
        if not pdf.chunks:
            _set_lookup_result(
                context.result, status="loi", label="chua_danh_gia",
                reason="PDF không có lớp văn bản đủ để kiểm tra nội dung loại trừ.",
                details={"dieu_kien_cung": hard_details, "cache_hit": pdf.hit},
            )
            return
        mention = find_topic_mention(pdf, term)
        reason = f"PDF có nội dung bị loại trừ: {term}." if mention else f"Không tìm thấy nội dung bị loại trừ: {term}."
        evidence = [{"trang": mention.page, "phan_muc": mention.section, "noi_dung": mention.text}] if mention else []
        _set_lookup_result(
            context.result, status="hoan_thanh", label="khong_phu_hop" if mention else "phu_hop",
            score=0 if mention else 100, reason=reason, evidence=evidence,
            details={"dieu_kien_cung": hard_details, "phuong_phap": "quy_tac", "so_lan_goi_ollama": 0,
                     "cache_hit": pdf.hit, "thoi_gian_xu_ly_ms": round((time.perf_counter() - started) * 1000, 2)},
        )
        return

    if len(intent.required_topics) == 1 and intent.required_topics[0] != "banking" and not intent.required_constructs_in_model:
        topic = intent.required_topics[0]
        pdf = cached_pdf(content, cache_dir)
        if not pdf.chunks:
            _set_lookup_result(
                context.result, status="loi", label="chua_danh_gia",
                reason="PDF không có lớp văn bản đủ để xác minh chủ đề.",
                details={"dieu_kien_cung": hard_details, "cache_hit": pdf.hit},
            )
            return
        decision = check_general_topic(pdf, topic)
        label = {"matched": "phu_hop", "unmatched": "khong_phu_hop", "insufficient": "chua_danh_gia"}[decision.status]
        evidence = [
            {"trang": item.page, "phan_muc": item.section, "noi_dung": item.text,
             "dieu_kien": f"Liên quan đến {topic}", "dien_giai_tieng_viet": decision.reason}
            for item in decision.evidence
        ]
        _set_lookup_result(
            context.result, status="hoan_thanh", label=label,
            score=decision.confidence if label == "phu_hop" else 0 if label == "khong_phu_hop" else None,
            reason=decision.reason, evidence=evidence,
            details={
                "dieu_kien_cung": hard_details,
                "ket_qua_tung_dieu_kien": [{
                    "noi_dung": f"Nghiên cứu phải liên quan đến {topic}.",
                    "loai": "bat_buoc", "trong_so": 5,
                    "trang_thai": "dat" if label == "phu_hop" else "khong_dat" if label == "khong_phu_hop" else "khong_xac_dinh",
                    "diem": decision.confidence if label == "phu_hop" else 0,
                    "vai_tro_khai_niem": "Chủ đề hoặc bối cảnh nghiên cứu" if label == "phu_hop" else None,
                    "phan_muc_pdf": [item.section for item in decision.evidence],
                    "bang_chung": evidence, "giai_thich_tieng_viet": decision.reason,
                }],
                "phuong_phap": "quy_tac", "so_lan_goi_ollama": 0,
                "cache_hit": pdf.hit, "doc_pdf_ms": round(pdf.read_seconds * 1000, 2),
                "tach_doan_ms": round(pdf.chunk_seconds * 1000, 2),
                "thoi_gian_xu_ly_ms": round((time.perf_counter() - started) * 1000, 2),
            },
        )
        return
    if "banking" in intent.required_topics:
        decision = cached_bank_decision(content, cache_dir, gateway.model)
        pdf = None if decision else cached_pdf(content, cache_dir)
        if pdf is not None and not pdf.chunks:
            _set_lookup_result(
                context.result, status="loi", label="chua_danh_gia",
                reason="PDF không có lớp văn bản đủ để xác minh chủ đề ngân hàng.",
                details={"dieu_kien_cung": hard_details, "cache_hit": pdf.hit},
            )
            return
        calls_before = getattr(gateway, "call_count", 0)
        model_started = time.perf_counter()
        if decision is None:
            decision = check_bank_topic(content, pdf, cache_dir, gateway)
        model_seconds = time.perf_counter() - model_started
        model_calls = getattr(gateway, "call_count", 0) - calls_before
        label = {"matched": "phu_hop", "unmatched": "khong_phu_hop", "insufficient": "chua_danh_gia"}[decision.status]
        evidence = [
            {"trang": item.page, "phan_muc": item.section, "noi_dung": item.text, "dieu_kien": "Liên quan đến ngân hàng", "dien_giai_tieng_viet": decision.reason}
            for item in decision.evidence
        ]
        _set_lookup_result(
            context.result, status="hoan_thanh", label=label,
            score=decision.confidence if label == "phu_hop" else 0 if label == "khong_phu_hop" else None,
            reason=decision.reason, evidence=evidence,
            details={
                "dieu_kien_cung": hard_details,
                "ket_qua_tung_dieu_kien": [{
                    "noi_dung": "Nghiên cứu phải liên quan đến ngân hàng.", "loai": "bat_buoc", "trong_so": 5,
                    "trang_thai": "dat" if label == "phu_hop" else "khong_dat" if label == "khong_phu_hop" else "khong_xac_dinh",
                    "diem": decision.confidence if label == "phu_hop" else 0,
                    "vai_tro_khai_niem": "Chủ đề hoặc bối cảnh nghiên cứu" if label == "phu_hop" else None,
                    "phan_muc_pdf": [item.section for item in decision.evidence],
                    "bang_chung": evidence, "giai_thich_tieng_viet": decision.reason,
                }],
                "phuong_phap": "quy_tac_va_ollama" if model_calls else "cache" if pdf is None else "quy_tac",
                "so_lan_goi_ollama": model_calls,
                "ollama": getattr(gateway, "last_metrics", None) if model_calls else None,
                "cache_hit": pdf is None or pdf.hit,
                "doc_pdf_ms": round(pdf.read_seconds * 1000, 2) if pdf else 0,
                "tach_doan_ms": round(pdf.chunk_seconds * 1000, 2) if pdf else 0,
                "embedding_ms": 0, "mo_hinh_ms": round(model_seconds * 1000, 2),
                "thoi_gian_xu_ly_ms": round((time.perf_counter() - started) * 1000, 2),
            },
            model=gateway.model if model_calls or pdf is None else None,
        )
        return
    if intent.required_constructs_in_model:
        decision = cached_decision(content, intent.required_constructs_in_model, cache_dir, gateway.model)
        pdf = None
        if decision is None:
            pdf = cached_pdf(content, cache_dir)
            if not pdf.chunks:
                _set_lookup_result(
                    context.result, status="loi", label="chua_danh_gia",
                    reason="PDF không có lớp văn bản đủ để xác minh điều kiện mô hình.",
                    details={"dieu_kien_cung": hard_details, "cache_hit": pdf.hit},
                )
                return
        calls_before = getattr(gateway, "call_count", 0)
        ai_started = time.perf_counter()
        if decision is None:
            decision = check_constructs(pdf, intent.required_constructs_in_model, cache_dir, gateway)
        ai_seconds = time.perf_counter() - ai_started
        model_calls = getattr(gateway, "call_count", 0) - calls_before
        label = {"matched": "phu_hop", "unmatched": "khong_phu_hop", "insufficient": "chua_danh_gia"}[decision.status]
        evidence = [
            {"trang": item.page, "noi_dung": item.text, "dieu_kien": ", ".join(intent.required_constructs_in_model), "phan_muc": item.section, "dien_giai_tieng_viet": decision.reason}
            for item in decision.evidence
        ]
        _set_lookup_result(
            context.result, status="hoan_thanh", label=label,
            score=decision.confidence if label == "phu_hop" else 0 if label == "khong_phu_hop" else None,
            reason=decision.reason, evidence=evidence,
            details={
                "dieu_kien_cung": hard_details,
                "ket_qua_tung_dieu_kien": [
                    {
                        "noi_dung": item,
                        "loai": "khai_niem_trong_mo_hinh",
                        "trong_so": 5,
                        "trang_thai": "dat" if label == "phu_hop" else "khong_dat" if label == "khong_phu_hop" else "khong_xac_dinh",
                        "diem": decision.confidence if label == "phu_hop" else 0,
                        "vai_tro_khai_niem": "Biến trong mô hình hoặc giả thuyết" if label == "phu_hop" else None,
                        "phan_muc_pdf": [source.section for source in decision.evidence],
                        "bang_chung": evidence,
                        "giai_thich_tieng_viet": decision.reason,
                    } for item in intent.required_constructs_in_model
                ],
                "phuong_phap": "quy_tac_va_ollama" if model_calls else "cache" if pdf is None else "quy_tac",
                "so_lan_goi_ollama": model_calls,
                "ollama": getattr(gateway, "last_metrics", None) if model_calls else None,
                "cache_hit": pdf is None or pdf.hit, "doc_pdf_ms": round(pdf.read_seconds * 1000, 2) if pdf else 0,
                "tach_doan_ms": round(pdf.chunk_seconds * 1000, 2) if pdf else 0, "embedding_ms": 0,
                "mo_hinh_ms": round(ai_seconds * 1000, 2),
                "thoi_gian_xu_ly_ms": round((time.perf_counter() - started) * 1000, 2),
            },
            model=gateway.model if model_calls or pdf is None else None,
        )
        return
    pdf = cached_pdf(content, cache_dir)
    pages, chunks = pdf.pages, pdf.chunks
    if chunks:
        if intent.dieu_kien_nguyen_tu:
            selected = select_chunks_for_conditions(
                intent.dieu_kien_nguyen_tu,
                chunks,
                gateway,
                settings.ai_top_chunks,
            )
        else:
            selected = select_top_chunks(context.lookup.noi_dung_yeu_cau, chunks, gateway, settings.ai_top_chunks)
        evaluation = evaluate_document(
            context.lookup.noi_dung_yeu_cau,
            intent,
            context.document,
            selected,
            gateway,
            settings.ai_relevance_threshold,
        )
    elif settings.ai_allow_pdf_vision:
        evaluation = evaluate_document_with_vision(
            context.lookup.noi_dung_yeu_cau,
            intent,
            context.document,
            content,
            gateway,
            settings.ai_relevance_threshold,
        )
    else:
        _set_lookup_result(
            context.result,
            status="loi",
            label="chua_danh_gia",
            reason="PDF không có lớp văn bản và chế độ đọc PDF bằng thị giác đang tắt.",
            details={"dieu_kien_cung": hard_details},
        )
        return
    condition_details = [item.model_dump(mode="json") for item in evaluation.ket_qua_tung_dieu_kien]
    _set_lookup_result(
        context.result,
        status="hoan_thanh",
        label=evaluation.nhan,
        score=evaluation.diem_phu_hop,
        reason=evaluation.ly_do,
        evidence=[item.model_dump(mode="json") for item in evaluation.bang_chung],
        details={"dieu_kien_cung": hard_details, "ket_qua_tung_dieu_kien": condition_details},
        model=gateway.model,
    )


def _pdf_concurrency_controls(settings: Settings) -> tuple[threading.BoundedSemaphore, HostRequestLimiter]:
    with _PDF_LIMITER_LOCK:
        slots = _PDF_PROCESS_SLOTS.setdefault(
            settings.pdf_download_workers,
            threading.BoundedSemaphore(settings.pdf_download_workers),
        )
        host_limiter = _PDF_HOST_LIMITERS.setdefault(
            settings.pdf_download_per_host,
            HostRequestLimiter(settings.pdf_download_per_host),
        )
    return slots, host_limiter


def _mark_pdf_job_failed(job_id: uuid.UUID, search_id: uuid.UUID, exc: Exception, max_retries: int) -> None:
    logger.error("Job PDF lỗi nội bộ hoặc hạ tầng: job=%s lan=%s loai=%s", job_id, search_id, exc.__class__.__name__)
    with SessionLocal.begin() as db:
        failed = db.get(CongViecTaiPdf, job_id)
        if failed and failed.trang_thai == "dang_xu_ly":
            if isinstance(exc, PdfConnectivityError) and not isinstance(exc, PdfBrowserAccessRequired) and failed.so_lan_thu < max_retries:
                failed.trang_thai = "cho_xu_ly"
                failed.loi_xu_ly = "Kết nối tạm thời gián đoạn; đang tự thử lại."
                failed.ngay_cap_nhat = dt.datetime.now(dt.UTC)
                return
            if isinstance(exc, PdfBrowserAccessRequired):
                failed.loi_xu_ly = "ResearchGate đang hạn chế truy cập. Hãy kết nối Chrome thường trên trang đề tài rồi tải lại."
            elif isinstance(exc, PdfConnectivityError):
                failed.loi_xu_ly = "Không thể kết nối mạng hoặc Chrome để kiểm tra PDF. Hãy kiểm tra Chrome và thử lại."
            else:
                failed.loi_xu_ly = "Không thể hoàn tất công việc tải PDF. Vui lòng thử lại."
            failed.ngay_cap_nhat = dt.datetime.now(dt.UTC)
            failed.ngay_hoan_thanh = dt.datetime.now(dt.UTC)
            # Retry HTTP đã diễn ra theo từng nguồn; chỉ người dùng mới đưa job lỗi chạy lại.
            failed.trang_thai = "loi"


def _return_paused_pdf_job(job_id: uuid.UUID) -> None:
    with SessionLocal.begin() as db:
        job = db.get(CongViecTaiPdf, job_id)
        if job and job.trang_thai == "dang_xu_ly":
            job.trang_thai = "cho_xu_ly"
            job.so_lan_thu = max(0, job.so_lan_thu - 1)
            job.loi_xu_ly = None
            job.ngay_cap_nhat = dt.datetime.now(dt.UTC)
            job.ngay_hoan_thanh = None


def _process_pdf_job_in_thread(
    job_id: uuid.UUID,
    search_id: uuid.UUID,
    settings: Settings,
    slots: threading.BoundedSemaphore,
    host_limiter: HostRequestLimiter,
) -> None:
    try:
        with slots:
            limits = httpx.Limits(max_connections=8, max_keepalive_connections=4)
            with httpx.Client(
                timeout=settings.api_timeout_seconds,
                follow_redirects=False,
                limits=limits,
            ) as client:
                with bind_pdf_search(search_id):
                    process_pdf_job(job_id, settings, client, host_limiter)
    except Exception as exc:
        if isinstance(exc, PdfSearchCancelled) or pdf_search_cancelled(search_id):
            _return_paused_pdf_job(job_id)
        else:
            _mark_pdf_job_failed(job_id, search_id, exc, settings.pdf_max_retries)


def _claim_pdf_job(search_id: uuid.UUID, settings: Settings) -> uuid.UUID | None:
    with SessionLocal.begin() as db:
        search = db.get(LanTimKiem, search_id)
        if not search or search.yeu_cau_dung_tai_pdf:
            if search:
                update_pdf_search_progress(db, search_id)
            return None
        job = claim_next_pdf_job(db, settings.pdf_max_retries, search_id)
        job_id = job.id if job else None
        update_pdf_search_progress(db, search_id)
        return job_id


def _run_pdf_search_worker(search_id: uuid.UUID, settings: Settings) -> int:
    """Điều phối một pool cố định; mỗi job dùng session và HTTP client riêng."""
    processed = 0
    slots, host_limiter = _pdf_concurrency_controls(settings)
    futures: set[Future[None]] = set()
    accepting = True
    with ThreadPoolExecutor(
        max_workers=settings.pdf_download_workers,
        thread_name_prefix="tai-pdf",
    ) as executor:
        while accepting or futures:
            while accepting and len(futures) < settings.pdf_download_workers:
                job_id = _claim_pdf_job(search_id, settings)
                if job_id is None:
                    accepting = False
                    break
                futures.add(
                    executor.submit(
                        _process_pdf_job_in_thread,
                        job_id,
                        search_id,
                        settings,
                        slots,
                        host_limiter,
                    )
                )

            if not futures:
                break
            done, futures = wait(futures, return_when=FIRST_COMPLETED)
            for future in done:
                future.result()
            processed += len(done)
            with SessionLocal.begin() as db:
                progress = update_pdf_search_progress(db, search_id)
                search = db.get(LanTimKiem, search_id)
                if search and not search.yeu_cau_dung_tai_pdf and progress.waiting:
                    accepting = True

    with SessionLocal.begin() as db:
        update_pdf_search_progress(db, search_id)
    return processed


def run_pdf_batch(search_id: uuid.UUID) -> int:
    """Tải đồng thời có giới hạn và chặn hai pool trùng cho cùng một lần tìm kiếm."""
    settings = get_settings()
    with _PDF_BATCH_LOCK:
        search_lock = _PDF_SEARCH_LOCKS.setdefault(search_id, threading.Lock())
    # Lần gọi đến sau chờ pool hiện tại kết thúc rồi kiểm tra lại hàng đợi,
    # tránh bỏ sót job vừa được người dùng đưa lại hàng đợi ở đúng thời điểm giao nhau.
    with search_lock:
        with SessionLocal.begin() as db:
            recovered_ids = recover_stale_pdf_jobs(db, settings.pdf_max_retries)
            if search_id in recovered_ids:
                update_pdf_search_progress(db, search_id)
        return _run_pdf_search_worker(search_id, settings)


def run_ai_lookup_batch(lookup_id: uuid.UUID) -> int:
    """Xử lý PDF song song có giới hạn; mỗi job có session riêng."""
    settings = get_settings()
    with SessionLocal.begin() as db:
        recovered_ids = recover_stale_ai_lookup_jobs(db, settings.ai_max_retries)
        if lookup_id in recovered_ids:
            update_ai_lookup_progress(db, lookup_id)

    processed = 0
    futures: set[Future[None]] = set()
    accepting = True
    with ThreadPoolExecutor(max_workers=settings.ai_lookup_workers, thread_name_prefix="tra-cuu-ai") as executor:
        while accepting or futures:
            while accepting and len(futures) < settings.ai_lookup_workers:
                with SessionLocal.begin() as db:
                    lookup_job = claim_next_ai_lookup_job(db, settings.ai_max_retries, lookup_id)
                    lookup_job_id = lookup_job.id if lookup_job else None
                    update_ai_lookup_progress(db, lookup_id)
                if lookup_job_id is None:
                    accepting = False
                    break
                futures.add(executor.submit(_process_ai_lookup_job_in_thread, lookup_job_id, lookup_id, settings))
            if not futures:
                break
            done, futures = wait(futures, return_when=FIRST_COMPLETED)
            for future in done:
                future.result()
            processed += len(done)
    return processed


def _process_ai_lookup_job_in_thread(job_id: uuid.UUID, lookup_id: uuid.UUID, settings: Settings) -> None:
    gateway = OllamaGateway(settings)
    try:
        with SessionLocal() as db:
            process_ai_lookup_job(db, job_id, gateway, settings)
            db.commit()
        with SessionLocal.begin() as db:
            update_ai_lookup_progress(db, lookup_id)
    except Exception as exc:
        logger.error("Tra cứu AI lỗi: job=%s loai=%s", job_id, exc.__class__.__name__)
        with SessionLocal.begin() as db:
            failed = db.get(KetQuaTraCuuAi, job_id)
            if failed:
                failed.loi_xu_ly = "Không thể hoàn tất đánh giá AI. Hãy kiểm tra Ollama và PDF."
                failed.ly_do = "Không thể hoàn tất đánh giá AI. Hãy kiểm tra Ollama và PDF."
                failed.nhan = "chua_danh_gia"
                failed.ngay_cap_nhat = dt.datetime.now(dt.UTC)
                failed.trang_thai_xu_ly = "loi"
                update_ai_lookup_progress(db, lookup_id)
    finally:
        gateway.close()


def run_worker(continuous: bool = False, idle_seconds: float = 2) -> int:
    settings = get_settings()
    with SessionLocal.begin() as db:
        _recover_stale_jobs(db)
        recovered_search_ids = recover_stale_pdf_jobs(db, settings.pdf_max_retries)
        recovered_lookup_ids = recover_stale_ai_lookup_jobs(db, settings.ai_max_retries)
        for search_id in recovered_search_ids:
            update_pdf_search_progress(db, search_id)
        for lookup_id in recovered_lookup_ids:
            update_ai_lookup_progress(db, lookup_id)
    processed = 0
    gateway: AIGateway | None = None
    while True:
        with SessionLocal.begin() as db:
            pdf_search_id = db.scalar(
                select(CongViecTaiPdf.id_lan_tim_kiem)
                .join(LanTimKiem, LanTimKiem.id == CongViecTaiPdf.id_lan_tim_kiem)
                .where(
                    CongViecTaiPdf.trang_thai == "cho_xu_ly",
                    CongViecTaiPdf.so_lan_thu < settings.pdf_max_retries,
                    LanTimKiem.yeu_cau_dung_tai_pdf.is_(False),
                )
                .order_by(CongViecTaiPdf.ngay_tao, CongViecTaiPdf.id)
                .limit(1)
            )
        if pdf_search_id:
            batch_count = run_pdf_batch(pdf_search_id)
            processed += batch_count
            if batch_count:
                continue

        with SessionLocal.begin() as db:
            lookup_job = claim_next_ai_lookup_job(db, settings.ai_max_retries)
            lookup_job_id = lookup_job.id if lookup_job else None
            lookup_id = lookup_job.id_lan_tra_cuu_ai if lookup_job else None
        if lookup_job_id:
            try:
                gateway = gateway or OllamaGateway(settings)
                with SessionLocal() as db:
                    process_ai_lookup_job(db, lookup_job_id, gateway, settings)
                    db.commit()
                with SessionLocal.begin() as db:
                    update_ai_lookup_progress(db, lookup_id)
            except Exception as exc:
                logger.error("Tra cứu AI lỗi: job=%s loai=%s", lookup_job_id, exc.__class__.__name__)
                with SessionLocal.begin() as db:
                    failed_lookup = db.get(KetQuaTraCuuAi, lookup_job_id)
                    if failed_lookup:
                        failed_lookup.loi_xu_ly = "Không thể hoàn tất đánh giá AI. Hãy kiểm tra Ollama và PDF."
                        failed_lookup.ly_do = "Không thể hoàn tất đánh giá AI. Hãy kiểm tra Ollama và PDF."
                        failed_lookup.nhan = "chua_danh_gia"
                        failed_lookup.ngay_cap_nhat = dt.datetime.now(dt.UTC)
                        failed_lookup.trang_thai_xu_ly = "loi"
                        update_ai_lookup_progress(db, failed_lookup.id_lan_tra_cuu_ai)
            processed += 1
            continue

        if not lookup_job_id:
            if continuous:
                time.sleep(idle_seconds)
                continue
            return processed
