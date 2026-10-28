from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import SQLAlchemyError
from starlette.middleware.sessions import SessionMiddleware

from app.config import get_settings
from app.database import apply_database_migrations
from app.routers import ai_search, auth, catalogs, documents, health, topics, users
from app.web import render


settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI):
    apply_database_migrations()
    yield


app = FastAPI(title="Quản lý tài liệu nghiên cứu", docs_url=None, redoc_url=None, lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.app_secret_key,
    session_cookie="phien_nghien_cuu",
    same_site="lax",
    https_only=settings.cookie_secure,
    max_age=60 * 60 * 12,
)
app.mount("/static", StaticFiles(directory="app/static"), name="static")

app.include_router(health.router)
app.include_router(auth.router)
app.include_router(users.router)
app.include_router(catalogs.router)
app.include_router(topics.router)
app.include_router(documents.router)
app.include_router(ai_search.router)


@app.get("/")
def root():
    return RedirectResponse("/de-tai", status_code=303)


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    if exc.status_code == 401:
        return RedirectResponse(f"/dang-nhap?next={request.url.path}", status_code=303)
    if exc.status_code in {403, 404}:
        message = exc.detail if isinstance(exc.detail, str) else "Yêu cầu không hợp lệ."
        return render(request, "error.html", {"message": message}, status_code=exc.status_code)
    return await http_exception_handler(request, exc)


@app.exception_handler(SQLAlchemyError)
async def database_error(request: Request, _: SQLAlchemyError):
    return render(
        request,
        "error.html",
        {"message": "Không thể kết nối hoặc xử lý dữ liệu. Vui lòng thử lại sau."},
        status_code=503,
    )


@app.exception_handler(Exception)
async def unexpected_error(request: Request, _: Exception):
    return render(
        request,
        "error.html",
        {"message": "Đã xảy ra lỗi không mong muốn. Vui lòng thử lại."},
        status_code=500,
    )
