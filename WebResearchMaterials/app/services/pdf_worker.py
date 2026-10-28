from __future__ import annotations

import datetime as dt
import threading
import uuid
from dataclasses import dataclass

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import SessionLocal
from app.models import CongViecTaiPdf, DeTaiTaiLieu, DeTaiTaiLieuLanTimKiem, LanTimKiem, TaiLieu, TepTin
from app.services.files import resolve_stored_path, save_file_record, validate_file
from app.services.pdf_downloader import HostRequestLimiter
from app.services.pdf_sources import find_and_download_pdf


_DOCUMENT_LOCKS_GUARD = threading.Lock()
_DOCUMENT_LOCKS: dict[uuid.UUID, threading.Lock] = {}


@dataclass(slots=True)
class PdfJobContext:
    job: CongViecTaiPdf
    search: LanTimKiem
    document: TaiLieu


@dataclass(slots=True)
class PdfDocumentSnapshot:
    id: uuid.UUID
    doi: str | None
    tieu_de: str
    url_pdf: str | None
    url_trang_goc: str | None
    du_lieu_nguon: dict
    ma_ngoai: dict


@dataclass(slots=True)
class PdfJobSnapshot:
    job_id: uuid.UUID
    search_id: uuid.UUID
    document_id: uuid.UUID
    uploader_id: uuid.UUID
    document: PdfDocumentSnapshot


def _load_pdf_context(db: Session, job_id: uuid.UUID) -> PdfJobContext:
    row = db.execute(
        select(CongViecTaiPdf, LanTimKiem, TaiLieu)
        .join(LanTimKiem, LanTimKiem.id == CongViecTaiPdf.id_lan_tim_kiem)
        .join(DeTaiTaiLieu, DeTaiTaiLieu.id == CongViecTaiPdf.id_de_tai_tai_lieu)
        .join(TaiLieu, TaiLieu.id == DeTaiTaiLieu.id_tai_lieu)
        .where(CongViecTaiPdf.id == job_id)
    ).one_or_none()
    if not row:
        raise ValueError("Không tìm thấy công việc tải PDF.")
    return PdfJobContext(row[0], row[1], row[2])


def _existing_pdf_is_valid(db: Session, document: TaiLieu, settings: Settings) -> bool:
    if not document.id_file_pdf:
        return False
    record = db.get(TepTin, document.id_file_pdf)
    if not record:
        return False
    try:
        path = resolve_stored_path(record, settings)
        with path.open("rb") as stream:
            return stream.read(5) == b"%PDF-"
    except OSError:
        return False


def _load_pdf_snapshot(job_id: uuid.UUID, settings: Settings) -> PdfJobSnapshot | None:
    """Đọc dữ liệu cần thiết rồi đóng transaction trước khi truy cập mạng."""
    with SessionLocal.begin() as db:
        context = _load_pdf_context(db, job_id)
        if context.job.trang_thai != "dang_xu_ly":
            return None
        if _existing_pdf_is_valid(db, context.document, settings):
            context.job.trang_thai = "thanh_cong"
            context.job.nguon_pdf = "tep_da_co"
            context.job.loi_xu_ly = None
            context.job.ngay_hoan_thanh = dt.datetime.now(dt.UTC)
            return None
        context.document.id_file_pdf = None
        return PdfJobSnapshot(
            job_id=context.job.id,
            search_id=context.search.id,
            document_id=context.document.id,
            uploader_id=context.search.id_nguoi_dung,
            document=PdfDocumentSnapshot(
                id=context.document.id,
                doi=context.document.doi,
                tieu_de=context.document.tieu_de,
                url_pdf=context.document.url_pdf,
                url_trang_goc=context.document.url_trang_goc,
                du_lieu_nguon=dict(context.document.du_lieu_nguon or {}),
                ma_ngoai=dict(context.document.ma_ngoai or {}),
            ),
        )


def _finish_without_pdf(snapshot: PdfJobSnapshot, settings: Settings) -> None:
    with SessionLocal.begin() as db:
        job = db.scalar(
            select(CongViecTaiPdf)
            .where(CongViecTaiPdf.id == snapshot.job_id)
            .with_for_update()
        )
        document = db.scalar(
            select(TaiLieu).where(TaiLieu.id == snapshot.document_id).with_for_update()
        )
        if not job or not document or job.trang_thai != "dang_xu_ly":
            return
        if _existing_pdf_is_valid(db, document, settings):
            job.trang_thai = "thanh_cong"
            job.nguon_pdf = "tep_da_co"
            job.loi_xu_ly = None
        else:
            document.id_file_pdf = None
            job.trang_thai = "khong_tim_thay"
            job.loi_xu_ly = (
                "Chưa tải được PDF từ các nguồn tự động. Trang nguồn có thể có PDF "
                "nhưng chặn lượt tải của ứng dụng; hãy mở trang bài để kiểm tra."
            )
        job.ngay_hoan_thanh = dt.datetime.now(dt.UTC)
        job.ngay_cap_nhat = dt.datetime.now(dt.UTC)


def _save_downloaded_pdf(snapshot: PdfJobSnapshot, located, settings: Settings) -> None:
    downloaded = located.downloaded
    validate_file(
        downloaded.filename,
        "application/pdf",
        downloaded.content,
        "pdf",
        settings.max_upload_mb * 1024 * 1024,
    )
    with SessionLocal.begin() as db:
        job = db.scalar(
            select(CongViecTaiPdf)
            .where(CongViecTaiPdf.id == snapshot.job_id)
            .with_for_update()
        )
        document = db.scalar(
            select(TaiLieu).where(TaiLieu.id == snapshot.document_id).with_for_update()
        )
        if not job or not document or job.trang_thai != "dang_xu_ly":
            return
        if _existing_pdf_is_valid(db, document, settings):
            job.trang_thai = "thanh_cong"
            job.nguon_pdf = "tep_da_co"
            job.loi_xu_ly = None
            job.ngay_hoan_thanh = dt.datetime.now(dt.UTC)
            return

        # Chưa sửa ORM nào trước savepoint để xung đột SHA-256 chỉ rollback cục bộ.
        record, _ = save_file_record(
            db,
            downloaded.filename,
            downloaded.content,
            "application/pdf",
            snapshot.uploader_id,
            "pdf",
            settings,
        )
        document.id_file_pdf = record.id
        document.url_pdf = downloaded.final_url
        now = dt.datetime.now(dt.UTC)
        job.trang_thai = "thanh_cong"
        job.nguon_pdf = located.source
        job.url_pdf_cuoi = downloaded.final_url
        job.loi_xu_ly = None
        job.ngay_hoan_thanh = now
        job.ngay_cap_nhat = now


def process_pdf_job(
    job_id: uuid.UUID,
    settings: Settings,
    client: httpx.Client | None = None,
    limiter: HostRequestLimiter | None = None,
) -> None:
    """Xử lý một job bằng các transaction ngắn, không giữ khóa lúc chờ mạng."""
    with SessionLocal.begin() as db:
        document_id = db.scalar(
            select(DeTaiTaiLieu.id_tai_lieu)
            .join(CongViecTaiPdf, CongViecTaiPdf.id_de_tai_tai_lieu == DeTaiTaiLieu.id)
            .where(CongViecTaiPdf.id == job_id)
        )
    if document_id is None:
        raise ValueError("Không tìm thấy tài liệu của công việc tải PDF.")
    with _DOCUMENT_LOCKS_GUARD:
        document_lock = _DOCUMENT_LOCKS.setdefault(document_id, threading.Lock())
    with document_lock:
        snapshot = _load_pdf_snapshot(job_id, settings)
        if snapshot is None:
            return
        located = find_and_download_pdf(
            snapshot.document,
            settings,
            client=client,
            limiter=limiter,
        )
        if located is None:
            _finish_without_pdf(snapshot, settings)
            return
        _save_downloaded_pdf(snapshot, located, settings)


def prepare_pdf_jobs_for_topic(
    db: Session,
    topic_id: uuid.UUID,
    search: LanTimKiem,
    settings: Settings,
) -> int:
    """Chỉ chuẩn bị tài liệu thuộc lần tìm kiếm được chọn."""
    rows = db.execute(
        select(DeTaiTaiLieu, TaiLieu)
        .join(
            DeTaiTaiLieuLanTimKiem,
            DeTaiTaiLieuLanTimKiem.id_de_tai_tai_lieu == DeTaiTaiLieu.id,
        )
        .join(TaiLieu, TaiLieu.id == DeTaiTaiLieu.id_tai_lieu)
        .where(
            DeTaiTaiLieu.id_de_tai == topic_id,
            DeTaiTaiLieuLanTimKiem.id_lan_tim_kiem == search.id,
        )
        .order_by(DeTaiTaiLieu.ngay_them, DeTaiTaiLieu.id)
    )
    actionable = 0
    now = dt.datetime.now(dt.UTC)
    for link, document in rows:
        job = db.scalar(
            select(CongViecTaiPdf).where(
                CongViecTaiPdf.id_lan_tim_kiem == search.id,
                CongViecTaiPdf.id_de_tai_tai_lieu == link.id,
            )
        )
        if _existing_pdf_is_valid(db, document, settings):
            if job:
                job.trang_thai = "thanh_cong"
                job.nguon_pdf = job.nguon_pdf or "tep_da_co"
                job.loi_xu_ly = None
                job.ngay_hoan_thanh = job.ngay_hoan_thanh or now
            continue

        document.id_file_pdf = None
        if job is not None and job.trang_thai == "loi":
            # Tab Tiếp tục tải tuyệt đối không tự đưa lỗi trở lại hàng đợi.
            continue
        actionable += 1
        if job is None:
            statement = insert(CongViecTaiPdf).values(
                id_de_tai=topic_id,
                id_lan_tim_kiem=search.id,
                id_de_tai_tai_lieu=link.id,
            ).on_conflict_do_nothing(
                index_elements=["id_lan_tim_kiem", "id_de_tai_tai_lieu"]
            )
            db.execute(statement)
        elif job.trang_thai in {"khong_tim_thay", "thanh_cong"}:
            job.trang_thai = "cho_xu_ly"
            job.so_lan_thu = 0
            job.nguon_pdf = None
            job.url_pdf_cuoi = None
            job.loi_xu_ly = None
            job.ngay_cap_nhat = now
            job.ngay_hoan_thanh = None
        # Job cho_xu_ly và dang_xu_ly được giữ nguyên, không tạo bản ghi trùng.
    db.flush()
    return actionable
