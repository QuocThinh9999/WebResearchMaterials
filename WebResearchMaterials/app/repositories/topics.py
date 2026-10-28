from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.constants import PAGE_SIZE
from app.models import DeTai, NguoiDung, TrangThaiDeTai


@dataclass(slots=True)
class TopicRow:
    topic: DeTai
    status: TrangThaiDeTai
    creator: NguoiDung


@dataclass(slots=True)
class TopicFilters:
    keyword: str | None = None
    creator_id: uuid.UUID | None = None
    status_id: uuid.UUID | None = None


def _topic_query(filters: TopicFilters) -> Select[tuple[DeTai, TrangThaiDeTai, NguoiDung]]:
    stmt = (
        select(DeTai, TrangThaiDeTai, NguoiDung)
        .join(TrangThaiDeTai, TrangThaiDeTai.id == DeTai.id_trang_thai)
        .join(NguoiDung, NguoiDung.id == DeTai.id_nguoi_tao)
    )
    if filters.keyword:
        stmt = stmt.where(DeTai.ten_de_tai.ilike(f"%{filters.keyword.strip()}%"))
    if filters.creator_id:
        stmt = stmt.where(DeTai.id_nguoi_tao == filters.creator_id)
    if filters.status_id:
        stmt = stmt.where(DeTai.id_trang_thai == filters.status_id)
    return stmt.order_by(DeTai.ngay_cap_nhat.desc(), DeTai.id.desc())


def list_topics(
    db: Session,
    offset: int = 0,
    limit: int = PAGE_SIZE,
    filters: TopicFilters | None = None,
) -> tuple[list[TopicRow], bool]:
    stmt = _topic_query(filters or TopicFilters()).offset(max(offset, 0)).limit(limit + 1)
    rows = db.execute(stmt).all()
    return [TopicRow(*row) for row in rows[:limit]], len(rows) > limit


def get_topic_row(db: Session, topic_id: uuid.UUID) -> TopicRow | None:
    stmt = (
        select(DeTai, TrangThaiDeTai, NguoiDung)
        .join(TrangThaiDeTai, TrangThaiDeTai.id == DeTai.id_trang_thai)
        .join(NguoiDung, NguoiDung.id == DeTai.id_nguoi_tao)
        .where(DeTai.id == topic_id)
    )
    row = db.execute(stmt).one_or_none()
    return TopicRow(*row) if row else None
