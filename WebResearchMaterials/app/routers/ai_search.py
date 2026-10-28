from __future__ import annotations

import datetime as dt
import time
import uuid
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BeforeValidator, Field
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.database import SessionLocal, get_db
from app.constants import PAGE_SIZE
from app.dependencies import get_current_user
from app.models import DeTai, LanTimKiem, LanTraCuuAi, LanTraCuuAiNguon, NguoiDung, TrangThaiTaiLieuDanhMuc
from app.repositories.catalogs import list_catalog
from app.repositories.ai_evaluations import (
    create_ai_lookup,
    get_ai_lookup_elapsed_ms,
    get_ai_lookup_progress,
    list_ai_lookup_results,
    list_source_searches,
)
from app.repositories.documents import DocumentFilters, count_documents, list_documents
from app.services.ai import AIConfigurationError, AIResponseError
from app.services.ai.intent_parser import analyze_ai_request
from app.services.ai.worker import run_ai_lookup_batch
from app.web import flash, render, require_csrf


router = APIRouter()


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


def _get_lookup(
    db: Session,
    lookup_id: uuid.UUID | None,
    user_id: uuid.UUID,
    topic_id: uuid.UUID | None = None,
) -> LanTraCuuAi | None:
    if not lookup_id:
        return None
    conditions = [
        LanTraCuuAi.id == lookup_id,
        LanTraCuuAi.id_nguoi_dung == user_id,
    ]
    if topic_id:
        conditions.append(LanTraCuuAi.id_de_tai == topic_id)
    return db.scalar(select(LanTraCuuAi).where(*conditions))


def _document_filters(
    keyword: str | None,
    status_id: str | None,
    year_from: int | None,
    year_to: int | None,
    quartile: str | None,
    author: str | None,
    pdf_status: str | None,
    search_numbers: list[int] | None,
    sort: str,
    ai_label: str | None,
    ai_lookup_id: uuid.UUID | None,
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


@router.get("/tim-kiem-ai")
def redirect_old_ai_route(request: Request, _: NguoiDung = Depends(get_current_user)):
    suffix = f"?{request.url.query}" if request.url.query else ""
    return RedirectResponse(f"/tra-cuu-ai{suffix}", status_code=307)


@router.get("/tra-cuu-ai")
def ai_lookup_page(
    request: Request,
    topic_id: str | None = None,
    lan_tra_cuu_ai: str | None = None,
    nhan: str | None = None,
    keyword: str | None = None,
    status_id: str | None = None,
    year_from: OptionalYearQuery = None,
    year_to: OptionalYearQuery = None,
    quartile: str | None = None,
    author: str | None = None,
    pdf_status: str | None = None,
    search_numbers: list[int] | None = Query(None),
    sort: str = "added_desc",
    db: Session = Depends(get_db),
    current_user: NguoiDung = Depends(get_current_user),
):
    parsed_topic_id = _uuid_or_none(topic_id)
    parsed_lookup_id = _uuid_or_none(lan_tra_cuu_ai)
    if topic_id and parsed_topic_id is None:
        flash(request, "Mã đề tài không hợp lệ.", "error")
        return RedirectResponse("/tra-cuu-ai", status_code=303)
    if lan_tra_cuu_ai and parsed_lookup_id is None:
        flash(request, "Mã lần đánh giá AI không hợp lệ; đã bỏ qua bộ lọc.", "error")
    lookup = _get_lookup(db, parsed_lookup_id, current_user.id)
    if lookup and parsed_topic_id is None:
        parsed_topic_id = lookup.id_de_tai
    selected_topic = db.get(DeTai, parsed_topic_id) if parsed_topic_id else None
    if parsed_topic_id and not selected_topic:
        flash(request, "Không tìm thấy đề tài.", "error")
        return RedirectResponse("/tra-cuu-ai", status_code=303)

    lookups: list[LanTraCuuAi] = []
    source_options = []
    available_searches: list[LanTimKiem] = []
    documents = []
    has_more = False
    filters = _document_filters(
        keyword, status_id, year_from, year_to, quartile, author, pdf_status,
        search_numbers, sort, nhan, lookup.id if lookup else None,
    )
    if selected_topic:
        lookups = list(
            db.scalars(
                select(LanTraCuuAi)
                .where(
                    LanTraCuuAi.id_nguoi_dung == current_user.id,
                    LanTraCuuAi.id_de_tai == selected_topic.id,
                )
                .order_by(LanTraCuuAi.ngay_tao.desc())
                .limit(30)
            )
        )
        if lookup and lookup.id_de_tai != selected_topic.id:
            lookup = None
            filters.ai_lookup_id = None
        if filters.ai_label and lookup is None and lookups:
            lookup = lookups[0]
            filters.ai_lookup_id = lookup.id
            query_items = [
                (key, value)
                for key, value in request.query_params.multi_items()
                if key != "lan_tra_cuu_ai"
            ]
            query_items.append(("lan_tra_cuu_ai", str(lookup.id)))
            return RedirectResponse(f"/tra-cuu-ai?{urlencode(query_items)}", status_code=303)
        source_options = list_source_searches(db, selected_topic.id)
        available_searches = [item.search for item in source_options]
        documents, has_more = list_documents(db, selected_topic.id, filters)

    source_searches: list[LanTimKiem] = []
    ai_elapsed_ms = 0
    if lookup:
        source_searches = list(
            db.scalars(
                select(LanTimKiem)
                .join(LanTraCuuAiNguon, LanTraCuuAiNguon.id_lan_tim_kiem == LanTimKiem.id)
                .where(LanTraCuuAiNguon.id_lan_tra_cuu_ai == lookup.id)
                .order_by(LanTimKiem.lan)
            )
        )
        ai_elapsed_ms = get_ai_lookup_elapsed_ms(lookup)
    return render(
        request,
        "ai_search.html",
        {
            "topics": list(db.scalars(select(DeTai).order_by(DeTai.ngay_cap_nhat.desc()))),
            "selected_topic": selected_topic,
            "ai_lookup": lookup,
            "ai_lookups": lookups,
            "source_options": source_options,
            "available_searches": available_searches,
            "source_searches": source_searches,
            "progress": get_ai_lookup_progress(db, lookup.id) if lookup else None,
            "ai_elapsed_ms": ai_elapsed_ms,
            "documents": documents,
            "document_count": count_documents(db, selected_topic.id, filters) if selected_topic else 0,
            "filters": filters,
            "document_statuses": list_catalog(db, TrangThaiTaiLieuDanhMuc),
            "has_more": has_more,
            "next_offset": PAGE_SIZE,
            "selected_label": nhan,
        },
    )


@router.get("/tra-cuu-ai/de-tai/{topic_id}/lan-tim-kiem")
def source_search_options(
    topic_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: NguoiDung = Depends(get_current_user),
):
    if not db.get(DeTai, topic_id):
        return JSONResponse({"message": "Không tìm thấy đề tài."}, status_code=404)
    return {
        "items": [
            {
                "id": str(item.search.id),
                "number": item.search.lan,
                "created_at": item.search.thoi_gian.isoformat(),
                "total_results": item.search.tong_ket_qua or 0,
                "pdf_documents": item.pdf_documents,
            }
            for item in list_source_searches(db, topic_id)
        ]
    }


@router.post("/tra-cuu-ai")
def start_ai_lookup(
    request: Request,
    background_tasks: BackgroundTasks,
    topic_id: uuid.UUID = Form(...),
    source_search_ids: list[uuid.UUID] = Form(...),
    ai_request: str = Form(...),
    csrf_token: str = Form(...),
    current_user: NguoiDung = Depends(get_current_user),
):
    require_csrf(request, csrf_token)
    try:
        started_at = dt.datetime.now(dt.UTC)
        request_text = ai_request.strip()
        if not request_text:
            raise ValueError("Nội dung yêu cầu không được để trống.")
        parse_started = time.perf_counter()
        intent = analyze_ai_request(request_text)
        intent = intent.model_copy(update={"parse_duration_ms": round((time.perf_counter() - parse_started) * 1000, 2)})
        with SessionLocal() as work_db:
            with work_db.begin():
                lookup = create_ai_lookup(
                    work_db,
                    topic_id,
                    current_user.id,
                    source_search_ids,
                    request_text,
                    intent.model_dump(mode="json"),
                    started_at,
                )
        if lookup.trang_thai != "hoan_thanh":
            background_tasks.add_task(run_ai_lookup_batch, lookup.id)
            flash(
                request,
                f"Đã tạo tra cứu cho {lookup.tong_tai_lieu} tài liệu; các PDF cần thiết sẽ được đánh giá.",
            )
        else:
            flash(request, "Đã hoàn tất các điều kiện có thể xác minh bằng metadata.", "info")
        return RedirectResponse(
            f"/tra-cuu-ai?topic_id={topic_id}&lan_tra_cuu_ai={lookup.id}",
            status_code=303,
        )
    except (AIConfigurationError, AIResponseError) as exc:
        flash(request, str(exc), "error")
    except ValueError:
        flash(request, "Yêu cầu hoặc lần tìm kiếm nguồn không hợp lệ. Hãy kiểm tra và thử lại.", "error")
    except SQLAlchemyError:
        flash(request, "Không thể lưu lần tra cứu bằng AI. Dữ liệu đã được hoàn tác.", "error")
    return RedirectResponse(f"/tra-cuu-ai?topic_id={topic_id}", status_code=303)


@router.get("/tra-cuu-ai/{lookup_id}/tien-do")
def ai_lookup_progress(
    lookup_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: NguoiDung = Depends(get_current_user),
):
    lookup = _get_lookup(db, lookup_id, current_user.id)
    if not lookup:
        return JSONResponse({"message": "Không tìm thấy lần tra cứu bằng AI."}, status_code=404)
    progress = get_ai_lookup_progress(db, lookup_id)
    return {
        "status": lookup.trang_thai,
        "total": progress.total,
        "waiting": progress.waiting,
        "processing": progress.processing,
        "completed": progress.completed,
        "without_pdf": progress.without_pdf,
        "failed": progress.failed,
        "finished": progress.finished,
        "percent": progress.percent,
        "elapsed_ms": get_ai_lookup_elapsed_ms(lookup),
    }


@router.get("/tra-cuu-ai/{lookup_id}/ket-qua/tai-them")
def load_more_ai_results(
    lookup_id: uuid.UUID,
    request: Request,
    offset: int = Query(..., ge=0),
    nhan: str | None = None,
    diem_lon_hon: float | None = Query(None, ge=0, le=100),
    db: Session = Depends(get_db),
    current_user: NguoiDung = Depends(get_current_user),
):
    lookup = _get_lookup(db, lookup_id, current_user.id)
    if not lookup:
        return JSONResponse({"message": "Không tìm thấy lần tra cứu bằng AI."}, status_code=404)
    results, has_more = list_ai_lookup_results(db, lookup_id, nhan, diem_lon_hon, offset)
    return render(
        request,
        "partials/ai_result_cards.html",
        {
            "ai_results": results,
            "ai_lookup": lookup,
            "has_more": has_more,
            "next_offset": offset + 10,
            "selected_label": nhan,
            "score_greater_than": diem_lon_hon,
        },
    )
