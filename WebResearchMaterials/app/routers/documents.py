from __future__ import annotations

import uuid
from collections import defaultdict
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user
from app.models import (
    DeTaiTaiLieu,
    NguoiDung,
    TaiLieu,
    TepTin,
    TrangThaiTaiLieu,
    TrangThaiTaiLieuDanhMuc,
    TrangThaiTaiLieuHienTai,
    TrangThaiTaiLieuTepTin,
)
from app.repositories.catalogs import list_catalog
from app.repositories.pdf_jobs import synchronize_manual_pdf_jobs
from app.services.files import InvalidUpload, read_and_validate, resolve_stored_path, save_file_record
from app.services.document_status import append_status_if_changed
from app.web import flash, render, require_csrf


router = APIRouter()


def _document_link(db: Session, topic_id: uuid.UUID, document_id: uuid.UUID):
    return db.execute(
        select(DeTaiTaiLieu, TaiLieu)
        .join(TaiLieu, TaiLieu.id == DeTaiTaiLieu.id_tai_lieu)
        .where(DeTaiTaiLieu.id_de_tai == topic_id, TaiLieu.id == document_id)
    ).one_or_none()


@router.get("/de-tai/{topic_id}/tai-lieu/{document_id}")
def document_detail(
    topic_id: uuid.UUID,
    document_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    _: NguoiDung = Depends(get_current_user),
):
    row = _document_link(db, topic_id, document_id)
    if not row:
        flash(request, "Không tìm thấy tài liệu trong đề tài.", "error")
        return RedirectResponse(f"/de-tai/{topic_id}", status_code=303)
    link, document = row
    current_states = db.execute(
        select(TrangThaiTaiLieuHienTai, NguoiDung, TrangThaiTaiLieuDanhMuc)
        .join(NguoiDung, NguoiDung.id == TrangThaiTaiLieuHienTai.id_nguoi_dung)
        .join(TrangThaiTaiLieuDanhMuc, TrangThaiTaiLieuDanhMuc.id == TrangThaiTaiLieuHienTai.id_trang_thai)
        .where(TrangThaiTaiLieuHienTai.id_de_tai_tai_lieu == link.id)
        .order_by(NguoiDung.ten_nguoi_dung)
    ).all()
    history = db.execute(
        select(TrangThaiTaiLieu, NguoiDung, TrangThaiTaiLieuDanhMuc)
        .join(NguoiDung, NguoiDung.id == TrangThaiTaiLieu.id_nguoi_dung)
        .join(TrangThaiTaiLieuDanhMuc, TrangThaiTaiLieuDanhMuc.id == TrangThaiTaiLieu.id_trang_thai)
        .where(TrangThaiTaiLieu.id_de_tai_tai_lieu == link.id)
        .order_by(TrangThaiTaiLieu.thoi_gian.desc(), TrangThaiTaiLieu.id.desc())
    ).all()
    history_ids = [item[0].id for item in history]
    files_by_state: dict[uuid.UUID, list[TepTin]] = defaultdict(list)
    if history_ids:
        file_rows = db.execute(
            select(TrangThaiTaiLieuTepTin.id_trang_thai_tai_lieu, TepTin)
            .join(TepTin, TepTin.id == TrangThaiTaiLieuTepTin.id_file)
            .where(TrangThaiTaiLieuTepTin.id_trang_thai_tai_lieu.in_(history_ids))
            .order_by(TrangThaiTaiLieuTepTin.thu_tu)
        )
        for state_id, file_record in file_rows:
            files_by_state[state_id].append(file_record)
    return render(
        request,
        "document_detail.html",
        {
            "topic_id": topic_id,
            "link": link,
            "document": document,
            "pdf_file": db.get(TepTin, document.id_file_pdf) if document.id_file_pdf else None,
            "current_states": current_states,
            "history": history,
            "files_by_state": files_by_state,
            "statuses": list_catalog(db, TrangThaiTaiLieuDanhMuc),
        },
    )


@router.post("/de-tai/{topic_id}/tai-lieu/{document_id}/pdf")
async def upload_pdf(
    topic_id: uuid.UUID,
    document_id: uuid.UUID,
    request: Request,
    pdf: UploadFile = File(...),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    current_user: NguoiDung = Depends(get_current_user),
):
    require_csrf(request, csrf_token)
    row = _document_link(db, topic_id, document_id)
    redirect = f"/de-tai/{topic_id}/tai-lieu/{document_id}"
    if not row:
        flash(request, "Không tìm thấy tài liệu trong đề tài.", "error")
        return RedirectResponse(f"/de-tai/{topic_id}", status_code=303)
    created_path: Path | None = None
    try:
        content, mime_type = await read_and_validate(pdf, "pdf")
        record, created_path = save_file_record(
            db, pdf.filename or "tai-lieu.pdf", content, mime_type, current_user.id, "pdf"
        )
        row[1].id_file_pdf = record.id
        db.flush()
        synchronize_manual_pdf_jobs(db, document_id)
        db.commit()
        flash(request, "Đã tải lên tệp PDF.")
    except (InvalidUpload, IntegrityError, SQLAlchemyError, OSError) as exc:
        db.rollback()
        if created_path:
            created_path.unlink(missing_ok=True)
        message = str(exc) if isinstance(exc, InvalidUpload) else "Không thể lưu tệp PDF."
        flash(request, message, "error")
    return RedirectResponse(redirect, status_code=303)


@router.post("/de-tai/{topic_id}/tai-lieu/{document_id}/trang-thai")
async def change_status(
    topic_id: uuid.UUID,
    document_id: uuid.UUID,
    request: Request,
    status_id: uuid.UUID = Form(...),
    reason: str = Form(""),
    images: list[UploadFile] = File(default=[]),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    current_user: NguoiDung = Depends(get_current_user),
):
    require_csrf(request, csrf_token)
    row = _document_link(db, topic_id, document_id)
    redirect = f"/de-tai/{topic_id}/tai-lieu/{document_id}"
    if not row or not db.get(TrangThaiTaiLieuDanhMuc, status_id):
        flash(request, "Tài liệu hoặc trạng thái không hợp lệ.", "error")
        return RedirectResponse(redirect, status_code=303)
    created_paths: list[Path] = []
    try:
        state = append_status_if_changed(
            db,
            link_id=row[0].id,
            user_id=current_user.id,
            status_id=status_id,
            reason=reason,
        )
        if state is None:
            db.rollback()
            flash(request, "Trạng thái không thay đổi nên hệ thống không ghi thêm lịch sử.", "info")
            return RedirectResponse(redirect, status_code=303)
        for index, image in enumerate(item for item in images if item.filename):
            content, mime_type = await read_and_validate(image, "image")
            record, created_path = save_file_record(
                db, image.filename or "anh", content, mime_type, current_user.id, "anh-trang-thai"
            )
            if created_path:
                created_paths.append(created_path)
            db.add(TrangThaiTaiLieuTepTin(id_trang_thai_tai_lieu=state.id, id_file=record.id, thu_tu=index))
        db.commit()
        flash(request, "Đã ghi nhận trạng thái mới và giữ nguyên lịch sử cũ.")
    except (InvalidUpload, IntegrityError, SQLAlchemyError) as exc:
        db.rollback()
        for path in created_paths:
            path.unlink(missing_ok=True)
        message = str(exc) if isinstance(exc, InvalidUpload) else "Không thể lưu thay đổi trạng thái."
        flash(request, message, "error")
    return RedirectResponse(redirect, status_code=303)


@router.get("/tep/{file_id}")
def serve_file(
    file_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: NguoiDung = Depends(get_current_user),
):
    record = db.get(TepTin, file_id)
    if not record:
        return RedirectResponse("/de-tai", status_code=303)
    try:
        path = resolve_stored_path(record)
    except FileNotFoundError:
        return RedirectResponse("/de-tai", status_code=303)
    disposition = "inline" if record.mime_type in {"application/pdf", "image/jpeg", "image/png", "image/gif", "image/webp"} else "attachment"
    return FileResponse(path, media_type=record.mime_type, filename=record.ten_file_goc, content_disposition_type=disposition)
