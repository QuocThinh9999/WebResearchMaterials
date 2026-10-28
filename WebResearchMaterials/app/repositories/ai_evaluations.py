from __future__ import annotations

import datetime as dt
import time
import uuid
from dataclasses import dataclass

from sqlalchemy import case, func, select, update
from sqlalchemy.orm import Session

from app.models import (
    DanhGiaAiTaiLieu,
    DeTaiTaiLieu,
    DeTaiTaiLieuLanTimKiem,
    KetQuaTraCuuAi,
    LanTimKiem,
    LanTraCuuAi,
    LanTraCuuAiNguon,
    TaiLieu,
)
from app.services.ai.hard_rules import evaluate_hard_rules
from app.services.ai.schemas import PhanTichYeuCauAI
from app.services.ai.topic_relevance import metadata_mention, metadata_topic_evidence


@dataclass(slots=True)
class AIProgress:
    total: int
    waiting: int
    processing: int
    completed: int
    without_pdf: int
    failed: int

    @property
    def finished(self) -> int:
        return self.completed + self.without_pdf + self.failed

    @property
    def percent(self) -> int:
        return round(self.finished * 100 / self.total) if self.total else 100


def claim_next_job(db: Session, max_retries: int) -> DanhGiaAiTaiLieu | None:
    job = db.scalar(
        select(DanhGiaAiTaiLieu)
        .where(
            DanhGiaAiTaiLieu.trang_thai_xu_ly == "cho_xu_ly",
            DanhGiaAiTaiLieu.so_lan_thu < max_retries,
        )
        .order_by(DanhGiaAiTaiLieu.ngay_tao, DanhGiaAiTaiLieu.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if job:
        job.trang_thai_xu_ly = "dang_xu_ly"
        job.so_lan_thu += 1
        job.loi_xu_ly = None
        db.flush()
    return job


def get_progress(db: Session, search_id: uuid.UUID) -> AIProgress:
    row = db.execute(
        select(
            func.count(DanhGiaAiTaiLieu.id),
            func.sum(case((DanhGiaAiTaiLieu.trang_thai_xu_ly == "cho_xu_ly", 1), else_=0)),
            func.sum(case((DanhGiaAiTaiLieu.trang_thai_xu_ly == "dang_xu_ly", 1), else_=0)),
            func.sum(case((DanhGiaAiTaiLieu.trang_thai_xu_ly == "hoan_thanh", 1), else_=0)),
            func.sum(case((DanhGiaAiTaiLieu.trang_thai_xu_ly == "khong_co_pdf", 1), else_=0)),
            func.sum(case((DanhGiaAiTaiLieu.trang_thai_xu_ly == "loi", 1), else_=0)),
        ).where(DanhGiaAiTaiLieu.id_lan_tim_kiem == search_id)
    ).one()
    values = [int(value or 0) for value in row]
    return AIProgress(*values)


def update_search_progress(db: Session, search_id: uuid.UUID) -> AIProgress:
    progress = get_progress(db, search_id)
    search = db.get(LanTimKiem, search_id)
    if not search:
        return progress
    if progress.total == 0 or progress.finished == progress.total:
        if progress.total and progress.failed == progress.total:
            search.trang_thai_ai = "loi"
        elif progress.failed:
            search.trang_thai_ai = "loi_mot_phan"
        else:
            search.trang_thai_ai = "hoan_thanh"
        search.ngay_hoan_thanh_ai = dt.datetime.now(dt.UTC)
    elif progress.processing:
        search.trang_thai_ai = "dang_xu_ly"
    else:
        search.trang_thai_ai = "cho_xu_ly"
    db.flush()
    return progress


def list_ai_results(
    db: Session,
    search_id: uuid.UUID,
    label: str | None = None,
    offset: int = 0,
    limit: int = 10,
) -> tuple[list[tuple[DanhGiaAiTaiLieu, TaiLieu, DeTaiTaiLieu]], bool]:
    stmt = (
        select(DanhGiaAiTaiLieu, TaiLieu, DeTaiTaiLieu)
        .join(DeTaiTaiLieu, DeTaiTaiLieu.id == DanhGiaAiTaiLieu.id_de_tai_tai_lieu)
        .join(TaiLieu, TaiLieu.id == DeTaiTaiLieu.id_tai_lieu)
        .where(DanhGiaAiTaiLieu.id_lan_tim_kiem == search_id)
    )
    if label in {"phu_hop", "khong_phu_hop", "chua_danh_gia"}:
        stmt = stmt.where(DanhGiaAiTaiLieu.nhan == label)
    stmt = stmt.order_by(
        DanhGiaAiTaiLieu.diem_phu_hop.desc().nullslast(),
        DanhGiaAiTaiLieu.ngay_cap_nhat.desc(),
    )
    rows = list(db.execute(stmt.offset(max(offset, 0)).limit(limit + 1)).all())
    return rows[:limit], len(rows) > limit


@dataclass(slots=True)
class AILookupProgress:
    total: int
    waiting: int
    processing: int
    completed: int
    without_pdf: int
    failed: int

    @property
    def finished(self) -> int:
        return self.completed + self.without_pdf + self.failed

    @property
    def percent(self) -> int:
        return round(self.finished * 100 / self.total) if self.total else 100


@dataclass(slots=True)
class SearchRoundOption:
    search: LanTimKiem
    total_documents: int
    pdf_documents: int


def list_source_searches(db: Session, topic_id: uuid.UUID) -> list[SearchRoundOption]:
    rows = db.execute(
        select(
            LanTimKiem,
            func.count(func.distinct(DeTaiTaiLieuLanTimKiem.id_de_tai_tai_lieu)),
            func.count(func.distinct(case((TaiLieu.id_file_pdf.is_not(None), DeTaiTaiLieu.id)))),
        )
        .outerjoin(
            DeTaiTaiLieuLanTimKiem,
            DeTaiTaiLieuLanTimKiem.id_lan_tim_kiem == LanTimKiem.id,
        )
        .outerjoin(DeTaiTaiLieu, DeTaiTaiLieu.id == DeTaiTaiLieuLanTimKiem.id_de_tai_tai_lieu)
        .outerjoin(TaiLieu, TaiLieu.id == DeTaiTaiLieu.id_tai_lieu)
        .where(LanTimKiem.id_de_tai == topic_id, LanTimKiem.loai_tim_kiem == "thuong")
        .group_by(LanTimKiem.id)
        .order_by(LanTimKiem.lan.desc())
    )
    return [SearchRoundOption(row[0], int(row[1] or 0), int(row[2] or 0)) for row in rows]


def create_ai_lookup(
    db: Session,
    topic_id: uuid.UUID,
    user_id: uuid.UUID,
    source_search_ids: list[uuid.UUID],
    request_text: str,
    conditions: dict,
    started_at: dt.datetime | None = None,
) -> LanTraCuuAi:
    selected_ids = tuple(dict.fromkeys(source_search_ids))
    if not selected_ids:
        raise ValueError("Vui lòng chọn ít nhất một lần tìm kiếm nguồn.")
    searches = list(
        db.scalars(
            select(LanTimKiem).where(
                LanTimKiem.id.in_(selected_ids),
                LanTimKiem.id_de_tai == topic_id,
                LanTimKiem.loai_tim_kiem == "thuong",
            )
        )
    )
    if len(searches) != len(selected_ids):
        raise ValueError("Lần tìm kiếm nguồn không hợp lệ hoặc không thuộc đề tài đã chọn.")

    started_at = started_at or dt.datetime.now(dt.UTC)
    lookup = LanTraCuuAi(
        id_de_tai=topic_id,
        id_nguoi_dung=user_id,
        noi_dung_yeu_cau=request_text.strip(),
        dieu_kien_ai=conditions,
        thoi_gian_bat_dau=started_at,
    )
    db.add(lookup)
    db.flush()
    for search_id in selected_ids:
        db.add(LanTraCuuAiNguon(id_lan_tra_cuu_ai=lookup.id, id_lan_tim_kiem=search_id, id_de_tai=topic_id))

    link_ids = list(
        db.scalars(
            select(DeTaiTaiLieuLanTimKiem.id_de_tai_tai_lieu)
            .where(
                DeTaiTaiLieuLanTimKiem.id_de_tai == topic_id,
                DeTaiTaiLieuLanTimKiem.id_lan_tim_kiem.in_(selected_ids),
            )
            .group_by(DeTaiTaiLieuLanTimKiem.id_de_tai_tai_lieu)
        )
    )
    documents = list(
        db.execute(
            select(DeTaiTaiLieu, TaiLieu)
            .join(TaiLieu, TaiLieu.id == DeTaiTaiLieu.id_tai_lieu)
            .where(DeTaiTaiLieu.id_de_tai == topic_id, DeTaiTaiLieu.id.in_(link_ids))
        )
    ) if link_ids else []
    with_pdf = 0
    pending = 0
    parsed_conditions = PhanTichYeuCauAI.model_validate(conditions)
    simple_condition_count = sum(len(group) for group in (
        parsed_conditions.required_topics, parsed_conditions.required_terms,
        parsed_conditions.excluded_topics, parsed_conditions.tu_khoa_loai_tru,
        parsed_conditions.required_affiliation_countries,
        parsed_conditions.excluded_affiliation_countries,
    ))
    for link, document in documents:
        item_started = time.perf_counter()
        has_pdf = document.id_file_pdf is not None
        topic = parsed_conditions.required_topics[0] if simple_condition_count == 1 and len(parsed_conditions.required_topics) == 1 else None
        topic_evidence = metadata_topic_evidence(document, topic) if topic else None
        required_term = parsed_conditions.required_terms[0] if simple_condition_count == 1 and len(parsed_conditions.required_terms) == 1 else None
        required_mention = metadata_mention(document, required_term) if required_term else None
        excluded_topic = parsed_conditions.excluded_topics[0] if simple_condition_count == 1 and len(parsed_conditions.excluded_topics) == 1 else None
        excluded_evidence = metadata_topic_evidence(document, excluded_topic) if excluded_topic else None
        if has_pdf:
            with_pdf += 1
        hard = evaluate_hard_rules(document, parsed_conditions)
        if hard.violations:
            state, label, score = "hoan_thanh", "khong_phu_hop", 0
            reason = "Tài liệu vi phạm điều kiện cứng: " + " ".join(hard.violations)
        elif topic_evidence:
            state, label, score = "hoan_thanh", "phu_hop", 95
            reason = f"{topic_evidence[0]} cho thấy bài nghiên cứu liên quan đến {'ngân hàng' if topic == 'banking' else topic}."
        elif required_mention:
            state, label, score = "hoan_thanh", "phu_hop", 95
            reason = f"{required_mention[0]} chứa nội dung bắt buộc: {required_term}."
        elif excluded_evidence:
            state, label, score = "hoan_thanh", "khong_phu_hop", 0
            reason = f"{excluded_evidence[0]} cho thấy bài nghiên cứu liên quan đến chủ đề đã loại trừ."
        elif parsed_conditions.input_language and not parsed_conditions.needs_semantic_pdf_check:
            state = "hoan_thanh"
            label, score = ("chua_danh_gia", None) if hard.unknown else ("phu_hop", 100)
            reason = "Thiếu metadata để xác minh điều kiện." if hard.unknown else "Đạt toàn bộ điều kiện metadata."
        elif not has_pdf:
            state, label, score = "khong_co_pdf", "chua_danh_gia", None
            reason = "Tài liệu không có PDF đã lưu nên được bỏ qua."
        else:
            state, label, score, reason = "cho_xu_ly", "chua_danh_gia", None, None
            pending += 1
        if state == "hoan_thanh" and label == "phu_hop" and hard.unknown:
            label, score = "chua_danh_gia", None
            reason = "Thiếu metadata để xác minh điều kiện: " + " ".join(hard.unknown)
        db.add(
            KetQuaTraCuuAi(
                id_lan_tra_cuu_ai=lookup.id,
                id_de_tai=topic_id,
                id_de_tai_tai_lieu=link.id,
                trang_thai_xu_ly=state,
                nhan=label,
                diem_phu_hop=score,
                ly_do=reason,
                bang_chung=[{"trang": None, "nguon": topic_evidence[0], "noi_dung": topic_evidence[1], "dieu_kien": f"Liên quan đến {topic}", "dien_giai_tieng_viet": reason}] if state == "hoan_thanh" and label == "phu_hop" and topic_evidence else [],
                chi_tiet_dieu_kien={
                    "dieu_kien_cung": {"dat": hard.passed, "vi_pham": hard.violations, "khong_xac_dinh": hard.unknown},
                    "phuong_phap": "quy_tac",
                    "thoi_gian_xu_ly_ms": round((time.perf_counter() - item_started) * 1000, 2),
                    "so_lan_goi_ollama": 0,
                } if state == "hoan_thanh" else {},
            )
        )
    lookup.tong_tai_lieu = len(documents)
    lookup.so_tai_lieu_co_pdf = with_pdf
    lookup.so_tai_lieu_khong_pdf = len(documents) - with_pdf
    if pending == 0:
        finished_at = dt.datetime.now(dt.UTC)
        lookup.trang_thai = "hoan_thanh"
        lookup.thoi_gian_hoan_thanh = finished_at
        lookup.thoi_gian_chay_ms = max(0, int((finished_at - started_at).total_seconds() * 1000))
    db.flush()
    return lookup


def claim_next_ai_lookup_job(
    db: Session,
    max_retries: int,
    lookup_id: uuid.UUID | None = None,
) -> KetQuaTraCuuAi | None:
    statement = select(KetQuaTraCuuAi).where(
        KetQuaTraCuuAi.trang_thai_xu_ly == "cho_xu_ly",
        KetQuaTraCuuAi.so_lan_thu < max_retries,
    )
    if lookup_id is not None:
        statement = statement.where(KetQuaTraCuuAi.id_lan_tra_cuu_ai == lookup_id)
    job = db.scalar(
        statement
        .order_by(KetQuaTraCuuAi.ngay_tao, KetQuaTraCuuAi.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if job:
        job.trang_thai_xu_ly = "dang_xu_ly"
        job.so_lan_thu += 1
        job.loi_xu_ly = None
        job.ngay_cap_nhat = dt.datetime.now(dt.UTC)
        db.flush()
    return job


def recover_stale_ai_lookup_jobs(db: Session, max_retries: int = 3, stale_minutes: int = 30) -> set[uuid.UUID]:
    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=stale_minutes)
    lookup_ids = set(
        db.scalars(
            select(KetQuaTraCuuAi.id_lan_tra_cuu_ai).where(
                KetQuaTraCuuAi.trang_thai_xu_ly == "dang_xu_ly",
                KetQuaTraCuuAi.ngay_cap_nhat < cutoff,
            )
        )
    )
    db.execute(
        update(KetQuaTraCuuAi)
        .where(
            KetQuaTraCuuAi.trang_thai_xu_ly == "dang_xu_ly",
            KetQuaTraCuuAi.ngay_cap_nhat < cutoff,
        )
        .values(
            trang_thai_xu_ly=case(
                (KetQuaTraCuuAi.so_lan_thu >= max_retries, "loi"),
                else_="cho_xu_ly",
            ),
            loi_xu_ly="Tự động phục hồi sau khi worker gián đoạn.",
            ngay_cap_nhat=dt.datetime.now(dt.UTC),
        )
    )
    return lookup_ids


def get_ai_lookup_progress(db: Session, lookup_id: uuid.UUID) -> AILookupProgress:
    row = db.execute(
        select(
            func.count(KetQuaTraCuuAi.id),
            func.sum(case((KetQuaTraCuuAi.trang_thai_xu_ly == "cho_xu_ly", 1), else_=0)),
            func.sum(case((KetQuaTraCuuAi.trang_thai_xu_ly == "dang_xu_ly", 1), else_=0)),
            func.sum(case((KetQuaTraCuuAi.trang_thai_xu_ly == "hoan_thanh", 1), else_=0)),
            func.sum(case((KetQuaTraCuuAi.trang_thai_xu_ly == "khong_co_pdf", 1), else_=0)),
            func.sum(case((KetQuaTraCuuAi.trang_thai_xu_ly == "loi", 1), else_=0)),
        ).where(KetQuaTraCuuAi.id_lan_tra_cuu_ai == lookup_id)
    ).one()
    return AILookupProgress(*(int(value or 0) for value in row))


def get_ai_lookup_elapsed_ms(lookup: LanTraCuuAi, now: dt.datetime | None = None) -> int:
    if lookup.thoi_gian_chay_ms is not None:
        return max(0, int(lookup.thoi_gian_chay_ms))
    end = lookup.thoi_gian_hoan_thanh or now or dt.datetime.now(dt.UTC)
    return max(0, int((end - lookup.thoi_gian_bat_dau).total_seconds() * 1000))


def update_ai_lookup_progress(db: Session, lookup_id: uuid.UUID) -> AILookupProgress:
    progress = get_ai_lookup_progress(db, lookup_id)
    lookup = db.get(LanTraCuuAi, lookup_id)
    if not lookup:
        return progress
    lookup.so_tai_lieu_khong_pdf = progress.without_pdf
    lookup.so_tai_lieu_co_pdf = max(0, progress.total - progress.without_pdf)
    if progress.total == 0 or progress.finished == progress.total:
        if progress.total and progress.failed == progress.total:
            lookup.trang_thai = "loi"
        elif progress.failed:
            lookup.trang_thai = "loi_mot_phan"
        else:
            lookup.trang_thai = "hoan_thanh"
        finished_at = lookup.thoi_gian_hoan_thanh or dt.datetime.now(dt.UTC)
        lookup.thoi_gian_hoan_thanh = finished_at
        if lookup.thoi_gian_chay_ms is None:
            lookup.thoi_gian_chay_ms = get_ai_lookup_elapsed_ms(lookup, finished_at)
    elif progress.processing:
        lookup.trang_thai = "dang_xu_ly"
    else:
        lookup.trang_thai = "cho_xu_ly"
    db.flush()
    return progress


def list_ai_lookup_results(
    db: Session,
    lookup_id: uuid.UUID,
    label: str | None = None,
    score_greater_than: float | None = None,
    offset: int = 0,
    limit: int = 10,
) -> tuple[list[tuple[KetQuaTraCuuAi, TaiLieu, DeTaiTaiLieu, list[int]]], bool]:
    stmt = (
        select(KetQuaTraCuuAi, TaiLieu, DeTaiTaiLieu)
        .join(DeTaiTaiLieu, DeTaiTaiLieu.id == KetQuaTraCuuAi.id_de_tai_tai_lieu)
        .join(TaiLieu, TaiLieu.id == DeTaiTaiLieu.id_tai_lieu)
        .where(KetQuaTraCuuAi.id_lan_tra_cuu_ai == lookup_id)
    )
    if label in {"phu_hop", "khong_phu_hop", "chua_danh_gia"}:
        stmt = stmt.where(KetQuaTraCuuAi.nhan == label)
    if score_greater_than is not None:
        stmt = stmt.where(KetQuaTraCuuAi.diem_phu_hop > score_greater_than)
    stmt = stmt.order_by(KetQuaTraCuuAi.diem_phu_hop.desc().nullslast(), KetQuaTraCuuAi.ngay_cap_nhat.desc())
    rows = list(db.execute(stmt.offset(max(offset, 0)).limit(limit + 1)).all())
    visible = rows[:limit]
    link_ids = [row[2].id for row in visible]
    source_numbers: dict[uuid.UUID, list[int]] = {link_id: [] for link_id in link_ids}
    if link_ids:
        source_rows = db.execute(
            select(DeTaiTaiLieuLanTimKiem.id_de_tai_tai_lieu, LanTimKiem.lan)
            .join(LanTimKiem, LanTimKiem.id == DeTaiTaiLieuLanTimKiem.id_lan_tim_kiem)
            .join(
                LanTraCuuAiNguon,
                LanTraCuuAiNguon.id_lan_tim_kiem == DeTaiTaiLieuLanTimKiem.id_lan_tim_kiem,
            )
            .where(
                LanTraCuuAiNguon.id_lan_tra_cuu_ai == lookup_id,
                DeTaiTaiLieuLanTimKiem.id_de_tai_tai_lieu.in_(link_ids),
            )
            .order_by(LanTimKiem.lan)
        )
        for link_id, number in source_rows:
            source_numbers[link_id].append(number)
    return [(*row, source_numbers.get(row[2].id, [])) for row in visible], len(rows) > limit
