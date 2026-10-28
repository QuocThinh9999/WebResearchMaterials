from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import Text, and_, cast, exists, false, func, or_, select
from sqlalchemy.orm import Session

from app.constants import PAGE_SIZE
from app.models import (
    CongViecTaiPdf,
    DanhGiaAiTaiLieu,
    DeTaiTaiLieu,
    DeTaiTaiLieuLanTimKiem,
    KetQuaTraCuuAi,
    LanTimKiem,
    TaiLieu,
    TepTin,
    ThongKeTrangThaiTaiLieu,
    TrangThaiTaiLieuHienTai,
)


@dataclass(slots=True)
class DocumentFilters:
    keyword: str | None = None
    status_id: uuid.UUID | None = None
    without_status: bool = False
    year_from: int | None = None
    year_to: int | None = None
    quartile: str | None = None
    author: str | None = None
    pdf_status: str | None = None
    search_numbers: tuple[int, ...] = ()
    ai_label: str | None = None
    ai_search_id: uuid.UUID | None = None
    ai_lookup_id: uuid.UUID | None = None
    sort: str = "added_desc"


@dataclass(slots=True)
class StatusTag:
    name: str
    color: str
    count: int
    users: list[str]


@dataclass(slots=True)
class DocumentRow:
    document: TaiLieu
    link: DeTaiTaiLieu
    status_tags: list[StatusTag] = field(default_factory=list)
    search_numbers: list[int] = field(default_factory=list)
    ai_evaluation: DanhGiaAiTaiLieu | KetQuaTraCuuAi | None = None
    pdf_job_status: str | None = None


def _document_query(topic_id: uuid.UUID, filters: DocumentFilters):
    stmt = select(TaiLieu, DeTaiTaiLieu).join(
        DeTaiTaiLieu, DeTaiTaiLieu.id_tai_lieu == TaiLieu.id
    ).where(DeTaiTaiLieu.id_de_tai == topic_id)

    if filters.keyword:
        keyword = filters.keyword.strip()
        vector = func.to_tsvector("simple", func.coalesce(TaiLieu.tieu_de_chuan_hoa, TaiLieu.tieu_de, ""))
        stmt = stmt.where(
            or_(
                vector.op("@@")(func.plainto_tsquery("simple", keyword)),
                TaiLieu.tieu_de.ilike(f"%{keyword}%"),
                cast(TaiLieu.tu_khoa, Text).ilike(f"%{keyword}%"),
            )
        )
    if filters.without_status:
        stmt = stmt.where(
            ~exists(
                select(1).where(
                    TrangThaiTaiLieuHienTai.id_de_tai_tai_lieu == DeTaiTaiLieu.id
                )
            )
        )
    elif filters.status_id:
        stmt = stmt.where(
            exists(
                select(1).where(
                    and_(
                        TrangThaiTaiLieuHienTai.id_de_tai_tai_lieu == DeTaiTaiLieu.id,
                        TrangThaiTaiLieuHienTai.id_trang_thai == filters.status_id,
                    )
                )
            )
        )
    if filters.year_from is not None:
        stmt = stmt.where(TaiLieu.nam_xuat_ban >= filters.year_from)
    if filters.year_to is not None:
        stmt = stmt.where(TaiLieu.nam_xuat_ban <= filters.year_to)
    if filters.quartile:
        stmt = stmt.where(TaiLieu.quartile == filters.quartile.upper())
    if filters.author:
        stmt = stmt.where(cast(TaiLieu.tac_gia, Text).ilike(f"%{filters.author.strip()}%"))
    if filters.pdf_status == "co_pdf":
        stmt = stmt.where(TaiLieu.id_file_pdf.is_not(None))
    elif filters.pdf_status == "khong_pdf":
        stmt = stmt.where(TaiLieu.id_file_pdf.is_(None))
    if filters.search_numbers:
        stmt = stmt.where(
            exists(
                select(1)
                .select_from(DeTaiTaiLieuLanTimKiem)
                .join(LanTimKiem, LanTimKiem.id == DeTaiTaiLieuLanTimKiem.id_lan_tim_kiem)
                .where(
                    DeTaiTaiLieuLanTimKiem.id_de_tai_tai_lieu == DeTaiTaiLieu.id,
                    DeTaiTaiLieuLanTimKiem.id_de_tai == topic_id,
                    LanTimKiem.lan.in_(filters.search_numbers),
                )
            )
        )
    if filters.ai_lookup_id:
        ai_conditions = [
            KetQuaTraCuuAi.id_de_tai_tai_lieu == DeTaiTaiLieu.id,
            KetQuaTraCuuAi.id_lan_tra_cuu_ai == filters.ai_lookup_id,
        ]
        if filters.ai_label in {"phu_hop", "khong_phu_hop", "chua_danh_gia"}:
            ai_conditions.append(KetQuaTraCuuAi.nhan == filters.ai_label)
        stmt = stmt.where(exists(select(1).where(and_(*ai_conditions))))
    elif filters.ai_search_id:
        ai_conditions = [
            DanhGiaAiTaiLieu.id_de_tai_tai_lieu == DeTaiTaiLieu.id,
            DanhGiaAiTaiLieu.id_lan_tim_kiem == filters.ai_search_id,
        ]
        if filters.ai_label in {"phu_hop", "khong_phu_hop", "chua_danh_gia"}:
            ai_conditions.append(DanhGiaAiTaiLieu.nhan == filters.ai_label)
        stmt = stmt.where(exists(select(1).where(and_(*ai_conditions))))
    elif filters.ai_label:
        stmt = stmt.where(false())

    order_map = {
        "year_asc": (TaiLieu.nam_xuat_ban.asc().nullslast(), TaiLieu.id.asc()),
        "year_desc": (TaiLieu.nam_xuat_ban.desc().nullslast(), TaiLieu.id.desc()),
        "added_asc": (DeTaiTaiLieu.ngay_them.asc(), DeTaiTaiLieu.id.asc()),
        "added_desc": (DeTaiTaiLieu.ngay_them.desc(), DeTaiTaiLieu.id.desc()),
    }
    return stmt.order_by(*order_map.get(filters.sort, order_map["added_desc"]))


def count_documents(db: Session, topic_id: uuid.UUID, filters: DocumentFilters) -> int:
    query = _document_query(topic_id, filters).order_by(None).subquery()
    return db.scalar(select(func.count()).select_from(query)) or 0


def list_pdf_files(db: Session, topic_id: uuid.UUID, filters: DocumentFilters) -> list[tuple[str, TepTin]]:
    query = (
        _document_query(topic_id, filters)
        .order_by(None)
        .join(TepTin, TepTin.id == TaiLieu.id_file_pdf)
        .with_only_columns(TaiLieu.tieu_de, TepTin)
    )
    return [(title, file_record) for title, file_record in db.execute(query).all()]


def list_documents(
    db: Session,
    topic_id: uuid.UUID,
    filters: DocumentFilters,
    offset: int = 0,
    limit: int = PAGE_SIZE,
) -> tuple[list[DocumentRow], bool]:
    rows = db.execute(_document_query(topic_id, filters).offset(max(offset, 0)).limit(limit + 1)).all()
    visible = rows[:limit]
    result = [DocumentRow(document=row[0], link=row[1]) for row in visible]
    link_ids = [item.link.id for item in result]
    if link_ids:
        stats = db.scalars(
            select(ThongKeTrangThaiTaiLieu)
            .where(ThongKeTrangThaiTaiLieu.id_de_tai_tai_lieu.in_(link_ids))
            .order_by(ThongKeTrangThaiTaiLieu.ten_trang_thai)
        )
        by_link: dict[uuid.UUID, list[StatusTag]] = defaultdict(list)
        for stat in stats:
            by_link[stat.id_de_tai_tai_lieu].append(
                StatusTag(
                    name=stat.ten_trang_thai,
                    color=stat.mau_trang_thai,
                    count=stat.so_nguoi,
                    users=list(stat.danh_sach_nguoi_dung or []),
                )
            )
        for item in result:
            item.status_tags = by_link[item.link.id]

        search_rows = db.execute(
            select(DeTaiTaiLieuLanTimKiem.id_de_tai_tai_lieu, LanTimKiem.lan)
            .join(LanTimKiem, LanTimKiem.id == DeTaiTaiLieuLanTimKiem.id_lan_tim_kiem)
            .where(DeTaiTaiLieuLanTimKiem.id_de_tai_tai_lieu.in_(link_ids))
            .order_by(LanTimKiem.lan)
        )
        search_numbers_by_link: dict[uuid.UUID, list[int]] = defaultdict(list)
        for link_id, search_number in search_rows:
            search_numbers_by_link[link_id].append(search_number)
        for item in result:
            item.search_numbers = search_numbers_by_link[item.link.id]
        active_pdf_jobs = db.execute(
            select(CongViecTaiPdf.id_de_tai_tai_lieu, CongViecTaiPdf.trang_thai)
            .where(
                CongViecTaiPdf.id_de_tai_tai_lieu.in_(link_ids),
                CongViecTaiPdf.trang_thai.in_(("cho_xu_ly", "dang_xu_ly")),
            )
            .order_by(CongViecTaiPdf.ngay_cap_nhat.desc())
        )
        by_link_pdf_status: dict[uuid.UUID, str] = {}
        for link_id, status in active_pdf_jobs:
            by_link_pdf_status.setdefault(link_id, status)
        for item in result:
            item.pdf_job_status = by_link_pdf_status.get(item.link.id)
        if filters.ai_lookup_id:
            evaluations = db.scalars(
                select(KetQuaTraCuuAi).where(
                    KetQuaTraCuuAi.id_de_tai_tai_lieu.in_(link_ids),
                    KetQuaTraCuuAi.id_lan_tra_cuu_ai == filters.ai_lookup_id,
                )
            )
            by_link_evaluation = {item.id_de_tai_tai_lieu: item for item in evaluations}
            for item in result:
                item.ai_evaluation = by_link_evaluation.get(item.link.id)
        elif filters.ai_search_id:
            evaluations = db.scalars(
                select(DanhGiaAiTaiLieu).where(
                    DanhGiaAiTaiLieu.id_de_tai_tai_lieu.in_(link_ids),
                    DanhGiaAiTaiLieu.id_lan_tim_kiem == filters.ai_search_id,
                )
            )
            by_link_evaluation = {item.id_de_tai_tai_lieu: item for item in evaluations}
            for item in result:
                item.ai_evaluation = by_link_evaluation.get(item.link.id)
    return result, len(rows) > limit
