from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user
from app.models import NguoiDung
from app.repositories.users import get_by_username, get_user, list_users
from app.services.security import hash_password
from app.web import flash, render, require_csrf


router = APIRouter(prefix="/tai-khoan")


@router.get("")
def users_page(
    request: Request,
    db: Session = Depends(get_db),
    current_user: NguoiDung = Depends(get_current_user),
):
    return render(request, "users.html", {"users": list_users(db), "current_user": current_user})


@router.post("/them")
def add_user(
    request: Request,
    username: str = Form(...),
    full_name: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    _: NguoiDung = Depends(get_current_user),
):
    require_csrf(request, csrf_token)
    username = username.strip()
    full_name = full_name.strip()
    if not username or not full_name:
        flash(request, "Tên đăng nhập và tên người dùng không được để trống.", "error")
        return RedirectResponse("/tai-khoan", status_code=303)
    if get_by_username(db, username):
        flash(request, "Tên đăng nhập đã tồn tại.", "error")
        return RedirectResponse("/tai-khoan", status_code=303)
    try:
        user = NguoiDung(ten_dang_nhap=username, ten_nguoi_dung=full_name, mat_khau_hash=hash_password(password))
        db.add(user)
        db.commit()
        flash(request, "Đã thêm tài khoản.")
    except (ValueError, IntegrityError) as exc:
        db.rollback()
        message = str(exc) if isinstance(exc, ValueError) else "Không thể thêm tài khoản do dữ liệu bị trùng."
        flash(request, message, "error")
    return RedirectResponse("/tai-khoan", status_code=303)


@router.post("/{user_id}/sua")
def edit_user(
    user_id: uuid.UUID,
    request: Request,
    username: str = Form(...),
    full_name: str = Form(...),
    password: str = Form(""),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    _: NguoiDung = Depends(get_current_user),
):
    require_csrf(request, csrf_token)
    user = get_user(db, user_id)
    if not user:
        flash(request, "Không tìm thấy tài khoản.", "error")
        return RedirectResponse("/tai-khoan", status_code=303)
    user.ten_dang_nhap = username.strip()
    user.ten_nguoi_dung = full_name.strip()
    try:
        if password:
            user.mat_khau_hash = hash_password(password)
        db.commit()
        flash(request, "Đã cập nhật tài khoản.")
    except (ValueError, IntegrityError) as exc:
        db.rollback()
        message = str(exc) if isinstance(exc, ValueError) else "Tên đăng nhập đã được sử dụng."
        flash(request, message, "error")
    return RedirectResponse("/tai-khoan", status_code=303)


@router.post("/{user_id}/hoat-dong")
def toggle_user(
    user_id: uuid.UUID,
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    current_user: NguoiDung = Depends(get_current_user),
):
    require_csrf(request, csrf_token)
    user = get_user(db, user_id)
    if not user:
        flash(request, "Không tìm thấy tài khoản.", "error")
    elif user.id == current_user.id and user.dang_hoat_dong:
        flash(request, "Không thể ngừng hoạt động tài khoản đang đăng nhập.", "error")
    else:
        user.dang_hoat_dong = not user.dang_hoat_dong
        db.commit()
        flash(request, "Đã cập nhật trạng thái tài khoản.")
    return RedirectResponse("/tai-khoan", status_code=303)


@router.post("/{user_id}/xoa")
def delete_user(
    user_id: uuid.UUID,
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    current_user: NguoiDung = Depends(get_current_user),
):
    require_csrf(request, csrf_token)
    user = get_user(db, user_id)
    if not user:
        flash(request, "Không tìm thấy tài khoản.", "error")
    elif user.id == current_user.id:
        flash(request, "Không thể tự xóa tài khoản đang đăng nhập.", "error")
    else:
        try:
            db.delete(user)
            db.commit()
            flash(request, "Đã xóa tài khoản.")
        except IntegrityError:
            db.rollback()
            flash(request, "Không thể xóa tài khoản vì đang được đề tài, tìm kiếm hoặc lịch sử trạng thái tham chiếu.", "error")
    return RedirectResponse("/tai-khoan", status_code=303)

