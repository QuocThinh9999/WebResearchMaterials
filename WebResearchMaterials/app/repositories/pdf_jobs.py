from __future__ import annotations

import datetime as dt
import math
import uuid
from dataclasses import dataclass

from sqlalchemy import case, func, select, update
from sqlalchemy.orm import Session

from app.models import CongViecTaiPdf, DeTaiTaiLieu, DeTaiTaiLieuLanTimKiem, LanTimKiem, TaiLieu


TERMINAL_PDF_STATUSES = {"thanh_cong", "khong_tim_thay", "loi"}
TERMINAL_PDF_SEARCH_STATUSES = {"hoan_thanh", "loi_mot_phan", "loi"}


@dataclass(slots=True)
class PdfProgress:
    total: int
    waiting: int
    processing: int
    successful: int
    not_found: int
    failed: int
    processed: int

    @property
    def finished(self) -> int:
        return self.successful + self.not_found + self.failed

    @property
    def percent(self) -> int:
        if not self.total:
            return 100
        if not self.processed:
            return 0
        return min(100, math.ceil(self.processed * 100 / self.total))


@dataclass(slots=True)
class PdfManagerState:
    total_missing: int
    waiting: int
    processing: int
    not_found: int
    failed: int
    successful: int


def claim_next_pdf_job(
    db: Session,
    max_retries: int,
    search_id: uuid.UUID | None = None,
) -> CongViecTaiPdf | None:
    statement = (
        select(CongViecTaiPdf)
        .join(LanTimKiem, LanTimKiem.id == CongViecTaiPdf.id_lan_tim_kiem)
        .where(
            CongViecTaiPdf.trang_thai == "cho_xu_ly",
            CongViecTaiPdf.so_lan_thu < max_retries,
            LanTimKiem.yeu_cau_dung_tai_pdf.is_(False),
        )
    )
    if search_id is not None:
        statement = statement.where(CongViecTaiPdf.id_lan_tim_kiem == search_id)
    job = db.scalar(
        statement
        .order_by(CongViecTaiPdf.ngay_tao, CongViecTaiPdf.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if job:
        job.trang_thai = "dang_xu_ly"
        job.so_lan_thu += 1
        job.loi_xu_ly = None
        job.ngay_cap_nhat = dt.datetime.now(dt.UTC)
        db.flush()
    return job


def recover_stale_pdf_jobs(db: Session, max_retries: int = 3, stale_minutes: int = 30) -> set[uuid.UUID]:
    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=stale_minutes)
    search_ids = set(
        db.scalars(
            select(CongViecTaiPdf.id_lan_tim_kiem).where(
                CongViecTaiPdf.trang_thai == "dang_xu_ly",
                CongViecTaiPdf.ngay_cap_nhat < cutoff,
            )
        )
    )
    db.execute(
        update(CongViecTaiPdf)
        .where(CongViecTaiPdf.trang_thai == "dang_xu_ly", CongViecTaiPdf.ngay_cap_nhat < cutoff)
        .values(
            trang_thai=case((CongViecTaiPdf.so_lan_thu >= max_retries, "loi"), else_="cho_xu_ly"),
            loi_xu_ly="Tự động phục hồi sau khi worker gián đoạn.",
            ngay_cap_nhat=dt.datetime.now(dt.UTC),
            ngay_hoan_thanh=case(
                (CongViecTaiPdf.so_lan_thu >= max_retries, dt.datetime.now(dt.UTC)),
                else_=CongViecTaiPdf.ngay_hoan_thanh,
            ),
        )
    )
    return search_ids


def retry_failed_pdf_jobs(db: Session, search_id: uuid.UUID) -> int:
    """Đưa riêng các PDF đã đánh dấu lỗi về hàng đợi để thử lại."""
    result = db.execute(
        update(CongViecTaiPdf)
        .where(
            CongViecTaiPdf.id_lan_tim_kiem == search_id,
            CongViecTaiPdf.trang_thai == "loi",
        )
        .values(
            trang_thai="cho_xu_ly",
            nguon_pdf=None,
            url_pdf_cuoi=None,
            so_lan_thu=0,
            loi_xu_ly=None,
            ngay_cap_nhat=dt.datetime.now(dt.UTC),
            ngay_hoan_thanh=None,
        )
        .execution_options(synchronize_session=False)
    )
    db.flush()
    return int(result.rowcount or 0)


def get_pdf_manager_state(
    db: Session,
    topic_id: uuid.UUID,
    search_id: uuid.UUID | None,
) -> PdfManagerState:
    if search_id is None:
        total_missing = db.scalar(
            select(func.count(DeTaiTaiLieu.id))
            .join(TaiLieu, TaiLieu.id == DeTaiTaiLieu.id_tai_lieu)
            .where(DeTaiTaiLieu.id_de_tai == topic_id, TaiLieu.id_file_pdf.is_(None))
        )
        return PdfManagerState(int(total_missing or 0), int(total_missing or 0), 0, 0, 0, 0)

    jobs = (
        select(
            CongViecTaiPdf.id.label("job_id"),
            CongViecTaiPdf.id_de_tai_tai_lieu.label("link_id"),
            CongViecTaiPdf.trang_thai.label("status"),
        )
        .where(CongViecTaiPdf.id_lan_tim_kiem == search_id)
        .subquery()
    )
    row = db.execute(
        select(
            func.count(DeTaiTaiLieu.id),
            func.sum(case(((jobs.c.job_id.is_(None)) | (jobs.c.status == "cho_xu_ly"), 1), else_=0)),
            func.sum(case((jobs.c.status == "dang_xu_ly", 1), else_=0)),
            func.sum(case((jobs.c.status == "khong_tim_thay", 1), else_=0)),
            func.sum(case((jobs.c.status == "loi", 1), else_=0)),
        )
        .join(
            DeTaiTaiLieuLanTimKiem,
            (DeTaiTaiLieuLanTimKiem.id_de_tai_tai_lieu == DeTaiTaiLieu.id)
            & (DeTaiTaiLieuLanTimKiem.id_lan_tim_kiem == search_id),
        )
        .join(TaiLieu, TaiLieu.id == DeTaiTaiLieu.id_tai_lieu)
        .outerjoin(jobs, jobs.c.link_id == DeTaiTaiLieu.id)
        .where(DeTaiTaiLieu.id_de_tai == topic_id, TaiLieu.id_file_pdf.is_(None))
    ).one()
    successful = db.scalar(
        select(func.count(CongViecTaiPdf.id)).where(
            CongViecTaiPdf.id_lan_tim_kiem == search_id,
            CongViecTaiPdf.trang_thai == "thanh_cong",
        )
    )
    return PdfManagerState(
        *(int(value or 0) for value in row),
        successful=int(successful or 0),
    )


def synchronize_manual_pdf_jobs(db: Session, document_id: uuid.UUID) -> set[uuid.UUID]:
    """Đồng bộ mọi job gắn với tài liệu sau khi người dùng tải PDF hợp lệ."""
    now = dt.datetime.now(dt.UTC)
    rows = db.execute(
        select(CongViecTaiPdf)
        .join(DeTaiTaiLieu, DeTaiTaiLieu.id == CongViecTaiPdf.id_de_tai_tai_lieu)
        .where(DeTaiTaiLieu.id_tai_lieu == document_id)
        .with_for_update(of=CongViecTaiPdf)
    ).scalars().all()
    search_ids: set[uuid.UUID] = set()
    for job in rows:
        job.trang_thai = "thanh_cong"
        job.nguon_pdf = "tai_len_thu_cong"
        job.url_pdf_cuoi = None
        job.loi_xu_ly = None
        job.ngay_cap_nhat = now
        job.ngay_hoan_thanh = now
        search_ids.add(job.id_lan_tim_kiem)
    db.flush()
    for search_id in search_ids:
        update_pdf_search_progress(db, search_id)
    return search_ids


def get_pdf_progress(db: Session, search_id: uuid.UUID) -> PdfProgress:
    row = db.execute(
        select(
            func.count(CongViecTaiPdf.id),
            func.sum(case((CongViecTaiPdf.trang_thai == "cho_xu_ly", 1), else_=0)),
            func.sum(case((CongViecTaiPdf.trang_thai == "dang_xu_ly", 1), else_=0)),
            func.sum(case((CongViecTaiPdf.trang_thai == "thanh_cong", 1), else_=0)),
            func.sum(case((CongViecTaiPdf.trang_thai == "khong_tim_thay", 1), else_=0)),
            func.sum(case((CongViecTaiPdf.trang_thai == "loi", 1), else_=0)),
            func.sum(case((CongViecTaiPdf.ngay_hoan_thanh.is_not(None), 1), else_=0)),
        ).where(CongViecTaiPdf.id_lan_tim_kiem == search_id)
    ).one()
    return PdfProgress(*(int(value or 0) for value in row))


def get_pdf_elapsed_ms(search: LanTimKiem, now: dt.datetime | None = None) -> int:
    elapsed = int(search.thoi_gian_chay_ms or 0)
    if search.thoi_gian_bat_dau_dot_pdf:
        now = now or dt.datetime.now(dt.UTC)
        elapsed += max(0, int((now - search.thoi_gian_bat_dau_dot_pdf).total_seconds() * 1000))
    return elapsed


def _finish_pdf_timer(search: LanTimKiem, now: dt.datetime | None = None) -> None:
    now = now or dt.datetime.now(dt.UTC)
    search.thoi_gian_chay_ms = get_pdf_elapsed_ms(search, now)
    search.thoi_gian_bat_dau_dot_pdf = None


def pause_pdf_search(search: LanTimKiem, now: dt.datetime | None = None) -> None:
    if search.trang_thai_tai_pdf in TERMINAL_PDF_SEARCH_STATUSES:
        return
    search.yeu_cau_dung_tai_pdf = True
    search.trang_thai_tai_pdf = "tam_dung"
    _finish_pdf_timer(search, now)


def resume_pdf_search(search: LanTimKiem, now: dt.datetime | None = None) -> None:
    now = now or dt.datetime.now(dt.UTC)
    search.yeu_cau_dung_tai_pdf = False
    search.trang_thai_tai_pdf = "cho_xu_ly"
    search.thoi_gian_hoan_thanh = None
    search.thoi_gian_bat_dau_dot_pdf = now
    if search.thoi_gian_chay_ms is None:
        search.thoi_gian_chay_ms = 0


def update_pdf_search_progress(db: Session, search_id: uuid.UUID) -> PdfProgress:
    progress = get_pdf_progress(db, search_id)
    search = db.get(LanTimKiem, search_id)
    if not search:
        return progress
    search.tong_pdf_can_tai = progress.total
    search.so_pdf_tai_thanh_cong = progress.successful
    search.so_pdf_khong_tim_thay = progress.not_found
    search.so_pdf_loi = progress.failed
    if progress.total == 0 or progress.finished == progress.total:
        if progress.total and progress.failed == progress.total:
            search.trang_thai_tai_pdf = "loi"
        elif progress.failed:
            search.trang_thai_tai_pdf = "loi_mot_phan"
        else:
            search.trang_thai_tai_pdf = "hoan_thanh"
        finished_at = dt.datetime.now(dt.UTC)
        _finish_pdf_timer(search, finished_at)
        search.thoi_gian_hoan_thanh = search.thoi_gian_hoan_thanh or finished_at
        search.yeu_cau_dung_tai_pdf = False
    elif search.yeu_cau_dung_tai_pdf:
        pause_pdf_search(search)
    elif progress.processing:
        search.trang_thai_tai_pdf = "dang_xu_ly"
    else:
        search.trang_thai_tai_pdf = "cho_xu_ly"
    db.flush()
    return progress
