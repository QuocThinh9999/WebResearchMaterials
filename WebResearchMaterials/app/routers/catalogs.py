from __future__ import annotations

import re
import uuid

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.constants import COLOR_PATTERN
from app.database import get_db
from app.dependencies import get_current_user
from app.models import NguoiDung, TrangThaiDeTai, TrangThaiTaiLieuDanhMuc
from app.repositories.catalogs import list_catalog
from app.web import flash, render, require_csrf


router = APIRouter(prefix="/danh-muc")
CATALOGS = {
    "de-tai": (TrangThaiDeTai, "Trạng thái đề tài"),
    "tai-lieu": (TrangThaiTaiLieuDanhMuc, "Trạng thái tài liệu"),
}


def _catalog(kind: str):
    return CATALOGS.get(kind)


@router.get("/{kind}")
def catalog_page(
    kind: str,
    request: Request,
    db: Session = Depends(get_db),
    _: NguoiDung = Depends(get_current_user),
):
    config = _catalog(kind)
    if not config:
        return RedirectResponse("/de-tai", status_code=303)
    model, title = config
    return render(request, "catalog.html", {"items": list_catalog(db, model), "kind": kind, "title": title})


@router.post("/{kind}/them")
def add_catalog(
    kind: str,
    request: Request,
    name: str = Form(...),
    color: str = Form(...),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    _: NguoiDung = Depends(get_current_user),
):
    require_csrf(request, csrf_token)
    config = _catalog(kind)
    if not config:
        return RedirectResponse("/de-tai", status_code=303)
    if not name.strip() or not re.fullmatch(COLOR_PATTERN, color):
        flash(request, "Tên trạng thái hoặc màu không hợp lệ.", "error")
        return RedirectResponse(f"/danh-muc/{kind}", status_code=303)
    try:
        db.add(config[0](ten_trang_thai=name.strip(), mau_trang_thai=color))
        db.commit()
        flash(request, "Đã thêm trạng thái.")
    except IntegrityError:
        db.rollback()
        flash(request, "Tên trạng thái đã tồn tại.", "error")
    return RedirectResponse(f"/danh-muc/{kind}", status_code=303)


@router.post("/{kind}/{item_id}/sua")
def edit_catalog(
    kind: str,
    item_id: uuid.UUID,
    request: Request,
    name: str = Form(...),
    color: str = Form(...),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    _: NguoiDung = Depends(get_current_user),
):
    require_csrf(request, csrf_token)
    config = _catalog(kind)
    item = db.get(config[0], item_id) if config else None
    if not item or not name.strip() or not re.fullmatch(COLOR_PATTERN, color):
        flash(request, "Trạng thái hoặc dữ liệu cập nhật không hợp lệ.", "error")
    else:
        item.ten_trang_thai = name.strip()
        item.mau_trang_thai = color
        try:
            db.commit()
            flash(request, "Đã cập nhật trạng thái.")
        except IntegrityError:
            db.rollback()
            flash(request, "Tên trạng thái đã tồn tại.", "error")
    return RedirectResponse(f"/danh-muc/{kind}", status_code=303)


@router.post("/{kind}/{item_id}/xoa")
def delete_catalog(
    kind: str,
    item_id: uuid.UUID,
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    _: NguoiDung = Depends(get_current_user),
):
    require_csrf(request, csrf_token)
    config = _catalog(kind)
    item = db.get(config[0], item_id) if config else None
    if not item:
        flash(request, "Không tìm thấy trạng thái.", "error")
    else:
        try:
            db.delete(item)
            db.commit()
            flash(request, "Đã xóa trạng thái.")
        except IntegrityError:
            db.rollback()
            flash(request, "Không thể xóa trạng thái vì đang được dữ liệu khác sử dụng.", "error")
    return RedirectResponse(f"/danh-muc/{kind}", status_code=303)

