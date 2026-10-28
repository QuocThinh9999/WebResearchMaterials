from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.repositories.users import get_by_username
from app.services.security import verify_password
from app.web import render, require_csrf


router = APIRouter()


@router.get("/dang-nhap")
def login_page(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse("/de-tai", status_code=303)
    return render(request, "login.html")


@router.post("/dang-nhap")
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    require_csrf(request, csrf_token)
    user = get_by_username(db, username)
    if not user or not user.dang_hoat_dong or not verify_password(password, user.mat_khau_hash):
        return render(
            request,
            "login.html",
            {"error": "Tên đăng nhập hoặc mật khẩu không đúng.", "username": username.strip()},
            status_code=400,
        )
    request.session.clear()
    request.session["user_id"] = str(user.id)
    return RedirectResponse("/de-tai", status_code=303)


@router.post("/dang-xuat")
def logout(request: Request, csrf_token: str = Form(...)):
    require_csrf(request, csrf_token)
    request.session.clear()
    return RedirectResponse("/dang-nhap", status_code=303)

