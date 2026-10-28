from __future__ import annotations

import uuid
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BeforeValidator, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.constants import PAGE_SIZE
from app.database import SessionLocal, get_db
from app.dependencies import get_current_user
from app.models import DeTai, LanTimKiem, LanTraCuuAi, NguoiDung, TrangThaiDeTai, TrangThaiTaiLieuDanhMuc
from app.repositories.catalogs import list_catalog
from app.repositories.documents import DocumentFilters, count_documents, list_documents, list_pdf_files
from app.repositories.pdf_jobs import (
    TERMINAL_PDF_SEARCH_STATUSES,
    get_pdf_elapsed_ms,
    get_pdf_manager_state,
    get_pdf_progress,
    pause_pdf_search,
    retry_failed_pdf_jobs,
    resume_pdf_search,
    update_pdf_search_progress,
)
from app.repositories.topics import TopicFilters, get_topic_row, list_topics
from app.repositories.users import list_users
from app.services.ai.worker import run_pdf_batch
from app.services.pdf_cancel import cancel_pdf_search, resume_pdf_requests
from app.services.pdf_personal_chrome import connect_personal_chrome, disconnect_personal_chrome, personal_chrome_connected
from app.services.pdf_downloader import PdfConnectivityError
from app.services.pdf_archive import create_pdf_archive
from app.services.pdf_worker import prepare_pdf_jobs_for_topic
from app.services.providers import ProviderError, SearchCriteria
from app.services.search import perform_search
from app.web import flash, render, require_csrf


router = APIRouter(prefix="/de-tai")


OptionalYearQuery = Annotated[
    Annotated[int, Field(ge=1000, le=9999)] | None,
    BeforeValidator(lambda value: None if value == "" else value),
    Query(),
]


def _uuid_or_none(value: str | None) -> uuid.UUID | None:
    try:
        return uuid.UUID(value) if value else None
    except ValueError:
        return None


def _filters(
    keyword: str | None,
    status_id: str | None,
    year_from: int | None,
    year_to: int | None,
    quartile: str | None,
    author: str | None,
    sort: str,
    pdf_status: str | None = None,
    search_numbers: list[int] | None = None,
    ai_label: str | None = None,
    ai_lookup_id: uuid.UUID | None = None,
) -> DocumentFilters:
    return DocumentFilters(
        keyword=keyword,
        status_id=_uuid_or_none(status_id),
        without_status=status_id == "without_status",
        year_from=year_from,
        year_to=year_to,
        quartile=quartile if quartile in {"Q1", "Q2", "Q3", "Q4"} else None,
        author=author,
        pdf_status=pdf_status if pdf_status in {"co_pdf", "khong_pdf"} else None,
        search_numbers=tuple(dict.fromkeys(number for number in (search_numbers or []) if number > 0)),
        ai_label=ai_label if ai_label in {"phu_hop", "khong_phu_hop", "chua_danh_gia"} else None,
        ai_lookup_id=ai_lookup_id,
        sort=sort,
    )


def _topic_filters(keyword: str | None, creator_id: str | None, status_id: str | None) -> TopicFilters:
    return TopicFilters(
        keyword=keyword.strip() if keyword and keyword.strip() else None,
        creator_id=_uuid_or_none(creator_id),
        status_id=_uuid_or_none(status_id),
    )


def _ai_lookups(db: Session, topic_id: uuid.UUID, user_id: uuid.UUID) -> list[LanTraCuuAi]:
    return db.execute(
        select(LanTraCuuAi)
        .where(LanTraCuuAi.id_de_tai == topic_id, LanTraCuuAi.id_nguoi_dung == user_id)
        .order_by(LanTraCuuAi.ngay_tao.desc())
        .limit(30)
    ).scalars().all()


def _selected_ai_lookup(
    db: Session, topic_id: uuid.UUID, user_id: uuid.UUID, lookup_id: str | None
) -> LanTraCuuAi | None:
    parsed_id = _uuid_or_none(lookup_id)
    if not parsed_id:
        return None
    return db.scalar(
        select(LanTraCuuAi).where(
            LanTraCuuAi.id == parsed_id,
            LanTraCuuAi.id_de_tai == topic_id,
            LanTraCuuAi.id_nguoi_dung == user_id,
        )
    )


@router.get("")
def topics_page(
    request: Request,
    offset: int = Query(0, ge=0),
    keyword: str | None = None,
    creator_id: str | None = None,
    status_id: str | None = None,
    db: Session = Depends(get_db),
    _: NguoiDung = Depends(get_current_user),
):
    filters = _topic_filters(keyword, creator_id, status_id)
    rows, has_more = list_topics(db, offset, filters=filters)
    statuses = list_catalog(db, TrangThaiDeTai)
    return render(
        request,
        "topics.html",
        {
            "topics": rows,
            "has_more": has_more,
            "next_offset": offset + PAGE_SIZE,
            "statuses": statuses,
            "creators": list_users(db),
            "topic_filters": filters,
        },
    )


@router.get("/tai-them")
def load_more_topics(
    request: Request,
    offset: int = Query(..., ge=0),
    keyword: str | None = None,
    creator_id: str | None = None,
    status_id: str | None = None,
    db: Session = Depends(get_db),
    _: NguoiDung = Depends(get_current_user),
):
    filters = _topic_filters(keyword, creator_id, status_id)
    rows, has_more = list_topics(db, offset, filters=filters)
    return render(
        request,
        "partials/topic_cards.html",
        {
            "topics": rows,
            "has_more": has_more,
            "next_offset": offset + PAGE_SIZE,
            "statuses": list_catalog(db, TrangThaiDeTai),
            "topic_filters": filters,
        },
    )


@router.post("/them")
def add_topic(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    status_id: uuid.UUID = Form(...),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    current_user: NguoiDung = Depends(get_current_user),
):
    require_csrf(request, csrf_token)
    if not name.strip() or len(name.strip()) > 500 or not db.get(TrangThaiDeTai, status_id):
        flash(request, "Tên đề tài hoặc trạng thái không hợp lệ.", "error")
    else:
        db.add(
            DeTai(
                ten_de_tai=name.strip(),
                mo_ta=description.strip() or None,
                id_trang_thai=status_id,
                id_nguoi_tao=current_user.id,
            )
        )
        db.commit()
        flash(request, "Đã thêm đề tài.")
    return RedirectResponse("/de-tai", status_code=303)


@router.post("/{topic_id}/sua")
def edit_topic(
    topic_id: uuid.UUID,
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    status_id: uuid.UUID = Form(...),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    _: NguoiDung = Depends(get_current_user),
):
    require_csrf(request, csrf_token)
    topic = db.get(DeTai, topic_id)
    if not topic or not name.strip() or len(name.strip()) > 500 or not db.get(TrangThaiDeTai, status_id):
        flash(request, "Đề tài hoặc dữ liệu cập nhật không hợp lệ.", "error")
    else:
        topic.ten_de_tai = name.strip()
        topic.mo_ta = description.strip() or None
        topic.id_trang_thai = status_id
        db.commit()
        flash(request, "Đã cập nhật đề tài.")
    return RedirectResponse("/de-tai", status_code=303)


@router.post("/{topic_id}/xoa")
def delete_topic(
    topic_id: uuid.UUID,
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    _: NguoiDung = Depends(get_current_user),
):
    require_csrf(request, csrf_token)
    topic = db.get(DeTai, topic_id)
    if not topic:
        flash(request, "Không tìm thấy đề tài.", "error")
    else:
        try:
            db.delete(topic)
            db.commit()
            flash(request, "Đã xóa đề tài và các liên kết thuộc đề tài.")
        except IntegrityError:
            db.rollback()
            flash(request, "Không thể xóa đề tài do dữ liệu đang được tham chiếu.", "error")
    return RedirectResponse("/de-tai", status_code=303)


@router.get("/{topic_id}")
def topic_detail(
    topic_id: uuid.UUID,
    request: Request,
    pdf_search_id: str | None = None,
    keyword: str | None = None,
    status_id: str | None = None,
    year_from: OptionalYearQuery = None,
    year_to: OptionalYearQuery = None,
    quartile: str | None = None,
    author: str | None = None,
    pdf_status: str | None = None,
    search_numbers: list[int] | None = Query(None),
    lan_tra_cuu_ai: str | None = None,
    nhan: str | None = None,
    sort: str = "added_desc",
    db: Session = Depends(get_db),
    current_user: NguoiDung = Depends(get_current_user),
):
    topic = get_topic_row(db, topic_id)
    if not topic:
        flash(request, "Không tìm thấy đề tài.", "error")
        return RedirectResponse("/de-tai", status_code=303)
    all_searches = list(
        db.scalars(
            select(LanTimKiem)
            .where(LanTimKiem.id_de_tai == topic_id)
            .order_by(LanTimKiem.lan.desc())
        )
    )
    ai_lookups = _ai_lookups(db, topic_id, current_user.id)
    ai_lookup = _selected_ai_lookup(db, topic_id, current_user.id, lan_tra_cuu_ai)
    filters = _filters(
        keyword, status_id, year_from, year_to, quartile, author, sort,
        pdf_status, search_numbers, nhan, ai_lookup.id if ai_lookup else None,
    )
    if filters.ai_label and ai_lookup is None and ai_lookups:
        ai_lookup = ai_lookups[0]
        query_items = [(key, value) for key, value in request.query_params.multi_items() if key != "lan_tra_cuu_ai"]
        query_items.append(("lan_tra_cuu_ai", str(ai_lookup.id)))
        return RedirectResponse(f"/de-tai/{topic_id}?{urlencode(query_items)}", status_code=303)
    documents, has_more = list_documents(db, topic_id, filters)
    active_pdf_search = next(
        (
            item
            for item in all_searches
            if item.loai_tim_kiem == "thuong"
            and item.trang_thai_tai_pdf in {"cho_xu_ly", "dang_xu_ly", "tam_dung"}
        ),
        None,
    )
    active_pdf_progress = get_pdf_progress(db, active_pdf_search.id) if active_pdf_search else None
    latest_normal_search = next((item for item in all_searches if item.loai_tim_kiem == "thuong"), None)
    selected_pdf_search_id = _uuid_or_none(pdf_search_id)
    if pdf_search_id and selected_pdf_search_id is None:
        flash(request, "Mã lần tìm kiếm PDF không hợp lệ; đang hiển thị lần mới nhất.", "error")
    selected_pdf_search = next(
        (item for item in all_searches if item.id == selected_pdf_search_id and item.loai_tim_kiem == "thuong"),
        None,
    )
    pdf_manager_search = selected_pdf_search or active_pdf_search or latest_normal_search
    pdf_manager_search_id = pdf_manager_search.id if pdf_manager_search else None
    return render(
        request,
        "topic_detail.html",
        {
            "topic_row": topic,
            "documents": documents,
            "document_count": count_documents(db, topic_id, filters),
            "has_more": has_more,
            "next_offset": PAGE_SIZE,
            "filters": filters,
            "ai_lookup": ai_lookup,
            "ai_lookups": ai_lookups,
            "search_history": all_searches[:20],
            "available_searches": [item for item in all_searches if item.loai_tim_kiem == "thuong"],
            "document_statuses": list_catalog(db, TrangThaiTaiLieuDanhMuc),
            "active_pdf_search": active_pdf_search,
            "active_pdf_progress": active_pdf_progress,
            "active_pdf_elapsed_ms": get_pdf_elapsed_ms(active_pdf_search) if active_pdf_search else 0,
            "latest_normal_search": latest_normal_search,
            "latest_pdf_elapsed_ms": get_pdf_elapsed_ms(latest_normal_search) if latest_normal_search else 0,
            "pdf_manager_search": pdf_manager_search,
            "pdf_manager_progress": get_pdf_progress(db, pdf_manager_search_id) if pdf_manager_search_id else None,
            "pdf_manager_state": get_pdf_manager_state(db, topic_id, pdf_manager_search_id),
            "pdf_download_workers": get_settings().pdf_download_workers,
            "pdf_browser_enabled": get_settings().pdf_browser_enabled,
            "pdf_personal_chrome_connected": personal_chrome_connected(),
        },
    )


@router.get("/{topic_id}/tai-lieu/tai-them")
def load_more_documents(
    topic_id: uuid.UUID,
    request: Request,
    offset: int = Query(..., ge=0),
    keyword: str | None = None,
    status_id: str | None = None,
    year_from: OptionalYearQuery = None,
    year_to: OptionalYearQuery = None,
    quartile: str | None = None,
    author: str | None = None,
    pdf_status: str | None = None,
    search_numbers: list[int] | None = Query(None),
    ai_label: str | None = None,
    nhan: str | None = None,
    lan_tra_cuu_ai: str | None = None,
    sort: str = "added_desc",
    db: Session = Depends(get_db),
    current_user: NguoiDung = Depends(get_current_user),
):
    ai_lookup = _selected_ai_lookup(db, topic_id, current_user.id, lan_tra_cuu_ai)
    filters = _filters(
        keyword,
        status_id,
        year_from,
        year_to,
        quartile,
        author,
        sort,
        pdf_status,
        search_numbers,
        nhan or ai_label,
        ai_lookup.id if ai_lookup else None,
    )
    documents, has_more = list_documents(db, topic_id, filters, offset)
    return render(
        request,
        "partials/document_cards.html",
        {
            "documents": documents,
            "topic_id": topic_id,
            "has_more": has_more,
            "next_offset": offset + PAGE_SIZE,
        },
    )


@router.get("/{topic_id}/tai-pdf/tai-tat-ca")
def download_all_pdfs(
    topic_id: uuid.UUID,
    keyword: str | None = None,
    status_id: str | None = None,
    year_from: OptionalYearQuery = None,
    year_to: OptionalYearQuery = None,
    quartile: str | None = None,
    author: str | None = None,
    pdf_status: str | None = None,
    search_numbers: list[int] | None = Query(None),
    lan_tra_cuu_ai: str | None = None,
    nhan: str | None = None,
    sort: str = "added_desc",
    db: Session = Depends(get_db),
    current_user: NguoiDung = Depends(get_current_user),
):
    if not db.get(DeTai, topic_id):
        raise HTTPException(404, "Không tìm thấy đề tài.")
    ai_lookup = _selected_ai_lookup(db, topic_id, current_user.id, lan_tra_cuu_ai)
    if nhan and not ai_lookup:
        lookups = _ai_lookups(db, topic_id, current_user.id)
        ai_lookup = lookups[0] if lookups else None
    filters = _filters(
        keyword, status_id, year_from, year_to, quartile, author, sort,
        pdf_status, search_numbers, nhan, ai_lookup.id if ai_lookup else None,
    )
    filters.pdf_status = "co_pdf"
    archive = create_pdf_archive(list_pdf_files(db, topic_id, filters))
    if archive is None:
        raise HTTPException(404, "Không có PDF phù hợp với bộ lọc.")

    def stream_archive():
        try:
            while chunk := archive.read(1024 * 1024):
                yield chunk
        finally:
            archive.close()

    return StreamingResponse(
        stream_archive(),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="tai-lieu-pdf.zip"'},
    )


@router.get("/{topic_id}/lan-tim-kiem/{search_id}/tien-do-pdf")
def pdf_search_progress(
    topic_id: uuid.UUID,
    search_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: NguoiDung = Depends(get_current_user),
):
    search = db.scalar(
        select(LanTimKiem).where(LanTimKiem.id == search_id, LanTimKiem.id_de_tai == topic_id)
    )
    if not search:
        return JSONResponse({"message": "Không tìm thấy lần tìm kiếm."}, status_code=404)
    progress = get_pdf_progress(db, search_id)
    manager_state = get_pdf_manager_state(db, topic_id, search_id)
    return {
        "status": search.trang_thai_tai_pdf,
        "total": progress.total,
        "waiting": progress.waiting,
        "processing": progress.processing,
        "successful": progress.successful,
        "not_found": progress.not_found,
        "failed": progress.failed,
        "finished": progress.finished,
        "processed": progress.processed,
        "percent": progress.percent,
        "started_at": search.thoi_gian_bat_dau.isoformat(),
        "finished_at": search.thoi_gian_hoan_thanh.isoformat() if search.thoi_gian_hoan_thanh else None,
        "elapsed_ms": get_pdf_elapsed_ms(search),
        "max_workers": get_settings().pdf_download_workers,
        "total_missing": manager_state.total_missing,
    }


@router.post("/{topic_id}/lan-tim-kiem/{search_id}/dung-tai-pdf")
def stop_pdf_download(
    topic_id: uuid.UUID,
    search_id: uuid.UUID,
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    _: NguoiDung = Depends(get_current_user),
):
    require_csrf(request, csrf_token)
    search = db.scalar(
        select(LanTimKiem).where(
            LanTimKiem.id == search_id,
            LanTimKiem.id_de_tai == topic_id,
            LanTimKiem.loai_tim_kiem == "thuong",
        )
    )
    if not search:
        flash(request, "Không tìm thấy đợt tải PDF.", "error")
    elif search.trang_thai_tai_pdf in TERMINAL_PDF_SEARCH_STATUSES:
        flash(request, "Đợt tải PDF này đã kết thúc.", "info")
    else:
        pause_pdf_search(search)
        db.commit()
        cancel_pdf_search(search.id)
        disconnect_personal_chrome()
        flash(request, "Đã dừng tải PDF và ngắt kết nối Chrome. Các bài chưa xong vẫn ở hàng đợi.", "info")
    return RedirectResponse(f"/de-tai/{topic_id}?pdf_search_id={search_id}", status_code=303)


@router.post("/{topic_id}/tai-pdf/ket-noi-chrome")
def connect_pdf_browser(
    topic_id: uuid.UUID,
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    _: NguoiDung = Depends(get_current_user),
):
    require_csrf(request, csrf_token)
    if not db.get(DeTai, topic_id):
        flash(request, "Không tìm thấy đề tài.", "error")
    elif not get_settings().pdf_browser_enabled:
        flash(request, "Chế độ tải PDF bằng Chrome chưa được bật.", "error")
    else:
        try:
            connect_personal_chrome()
            flash(request, "Đã kết nối Chrome thường. Bấm Tiếp tục tải PDF để thử lại các bài còn thiếu.")
        except PdfConnectivityError as exc:
            flash(request, str(exc), "error")
    return RedirectResponse(f"/de-tai/{topic_id}#pdf-panel-continue", status_code=303)


@router.post("/{topic_id}/tai-pdf/tiep-tuc")
def continue_pdf_download(
    topic_id: uuid.UUID,
    request: Request,
    background_tasks: BackgroundTasks,
    search_id: str | None = Form(None),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    _: NguoiDung = Depends(get_current_user),
):
    require_csrf(request, csrf_token)
    parsed_search_id = _uuid_or_none(search_id)
    if search_id and parsed_search_id is None:
        flash(request, "Mã lần tìm kiếm không hợp lệ.", "error")
        return RedirectResponse(f"/de-tai/{topic_id}", status_code=303)
    statement = select(LanTimKiem).where(
        LanTimKiem.id_de_tai == topic_id,
        LanTimKiem.loai_tim_kiem == "thuong",
    )
    if parsed_search_id:
        statement = statement.where(LanTimKiem.id == parsed_search_id)
    search = db.scalar(statement.order_by(LanTimKiem.lan.desc()).limit(1))
    if not search:
        flash(request, "Hãy thực hiện ít nhất một lần tìm kiếm trước khi tải PDF.", "info")
        return RedirectResponse(f"/de-tai/{topic_id}", status_code=303)
    actionable = prepare_pdf_jobs_for_topic(db, topic_id, search, get_settings())
    if search.yeu_cau_dung_tai_pdf or search.trang_thai_tai_pdf not in {"cho_xu_ly", "dang_xu_ly"}:
        resume_pdf_search(search)
    progress = update_pdf_search_progress(db, search.id)
    db.commit()
    resume_pdf_requests(search.id)
    if progress.waiting:
        background_tasks.add_task(run_pdf_batch, search.id)
        flash(request, f"Đang tiếp tục tải PDF cho {actionable} tài liệu chưa có tệp.")
    elif progress.failed:
        flash(request, "Các tài liệu còn lại đang ở tab Tải lại lỗi.", "info")
    else:
        flash(request, "Tất cả tài liệu trong đề tài đã có PDF hợp lệ.", "info")
    return RedirectResponse(f"/de-tai/{topic_id}?pdf_search_id={search.id}", status_code=303)


@router.post("/{topic_id}/tai-pdf/tai-lai-loi")
def retry_failed_pdf_download(
    topic_id: uuid.UUID,
    request: Request,
    background_tasks: BackgroundTasks,
    search_id: str | None = Form(None),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    _: NguoiDung = Depends(get_current_user),
):
    require_csrf(request, csrf_token)
    parsed_search_id = _uuid_or_none(search_id)
    if search_id and parsed_search_id is None:
        flash(request, "Mã lần tìm kiếm không hợp lệ.", "error")
        return RedirectResponse(f"/de-tai/{topic_id}#pdf-panel-failed", status_code=303)
    statement = select(LanTimKiem).where(
        LanTimKiem.id_de_tai == topic_id,
        LanTimKiem.loai_tim_kiem == "thuong",
    )
    if parsed_search_id:
        statement = statement.where(LanTimKiem.id == parsed_search_id)
    search = db.scalar(statement.order_by(LanTimKiem.lan.desc()).limit(1))
    if not search:
        flash(request, "Không tìm thấy đợt tải PDF.", "error")
        return RedirectResponse(f"/de-tai/{topic_id}#pdf-panel-failed", status_code=303)
    target = f"/de-tai/{topic_id}?pdf_search_id={search.id}#pdf-panel-failed"
    already_running = search.trang_thai_tai_pdf in {"cho_xu_ly", "dang_xu_ly"} and not search.yeu_cau_dung_tai_pdf
    failed_count = retry_failed_pdf_jobs(db, search.id)
    if not failed_count:
        flash(request, "Đợt tải này không có PDF nào được đánh dấu lỗi.", "info")
        db.commit()
        return RedirectResponse(target, status_code=303)

    if not already_running:
        resume_pdf_search(search)
    update_pdf_search_progress(db, search.id)
    db.commit()
    resume_pdf_requests(search.id)
    background_tasks.add_task(run_pdf_batch, search.id)
    flash(request, f"Đang tải lại {failed_count} PDF đã đánh dấu lỗi.")
    return RedirectResponse(target, status_code=303)


@router.post("/{topic_id}/tim-kiem")
def search_documents(
    topic_id: uuid.UUID,
    request: Request,
    background_tasks: BackgroundTasks,
    keywords: str = Form(""),
    keywords_and: str = Form(""),
    title: str = Form(""),
    author: str = Form(""),
    year_from: int | None = Form(None),
    year_to: int | None = Form(None),
    document_type: str = Form(""),
    language: str = Form(""),
    q1: str | None = Form(None),
    q2: str | None = Form(None),
    q3: str | None = Form(None),
    q4: str | None = Form(None),
    open_access: str | None = Form(None),
    source_scopus: str | None = Form(None),
    source_wos: str | None = Form(None),
    csrf_token: str = Form(...),
    current_user: NguoiDung = Depends(get_current_user),
):
    require_csrf(request, csrf_token)
    redirect_url = f"/de-tai/{topic_id}"
    try:
        criteria = SearchCriteria(
            keywords=keywords.strip() or None,
            keywords_and=keywords_and.strip() or None,
            title=title.strip() or None,
            author=author.strip() or None,
            year_from=year_from,
            year_to=year_to,
            document_type=document_type.strip() or None,
            language=language.strip() or None,
            quartiles=[q for q, value in (("Q1", q1), ("Q2", q2), ("Q3", q3), ("Q4", q4)) if value],
            open_access=bool(open_access),
            sources=[source for source, value in (("scopus", source_scopus), ("wos", source_wos)) if value],
        )
        with SessionLocal() as work_db:
            with work_db.begin():
                summary = perform_search(work_db, topic_id, current_user.id, criteria)
        if summary.search_id and summary.pdf_jobs:
            background_tasks.add_task(run_pdf_batch, summary.search_id)
        if summary.saved:
            redirect_url += f"?search_numbers={summary.search_number}"
            pdf_message = (
                f" {summary.pdf_jobs} PDF công khai đang được xử lý nền."
                if summary.pdf_jobs
                else " Không có PDF nào cần tải thêm."
            )
            flash(
                request,
                f"Đã lưu metadata lần {summary.search_number}: {summary.total_results} kết quả, "
                f"{summary.new_documents} tài liệu mới.{pdf_message}",
            )
        else:
            flash(
                request,
                "Không tìm thấy tài liệu phù hợp. Lần tìm kiếm này không được ghi vào lịch sử.",
                "info",
            )
    except ValidationError as exc:
        flash(request, exc.errors()[0].get("msg", "Điều kiện tìm kiếm không hợp lệ."), "error")
    except ProviderError as exc:
        flash(request, str(exc), "error")
    except (ValueError, SQLAlchemyError):
        flash(request, "Không thể lưu kết quả tìm kiếm. Toàn bộ thay đổi của lần tìm kiếm đã được hoàn tác.", "error")
    return RedirectResponse(redirect_url, status_code=303)
