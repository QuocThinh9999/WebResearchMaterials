from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status
from fastapi.templating import Jinja2Templates
from starlette.requests import Request
from starlette.responses import Response

from app.services.security import get_csrf_token
from app.services.time_format import format_elapsed_ms


templates = Jinja2Templates(directory="app/templates")
templates.env.filters["format_elapsed"] = format_elapsed_ms


def flash(request: Request, message: str, category: str = "success") -> None:
    request.session["flash"] = {"message": message, "category": category}


def render(request: Request, name: str, context: dict[str, Any] | None = None, status_code: int = 200) -> Response:
    values = dict(context or {})
    values.setdefault("request", request)
    values.setdefault("csrf_token", get_csrf_token(request))
    values.setdefault("current_user", getattr(request.state, "current_user", None))
    values.setdefault("flash", request.session.pop("flash", None))
    return templates.TemplateResponse(request=request, name=name, context=values, status_code=status_code)


def require_csrf(request: Request, token: str | None) -> None:
    from app.services.security import validate_csrf

    if not validate_csrf(request, token):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Phiên biểu mẫu không hợp lệ. Vui lòng tải lại trang.")

