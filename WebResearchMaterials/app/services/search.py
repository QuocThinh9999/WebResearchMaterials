from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import (
    CongViecTaiPdf,
    DanhGiaAiTaiLieu,
    DeTai,
    DeTaiTaiLieu,
    DeTaiTaiLieuLanTimKiem,
    LanTimKiem,
    TaiLieu,
    TepTin,
)
from app.services.files import resolve_stored_path
from app.services.providers import (
    NormalizedDocument,
    ScopusProvider,
    SearchCriteria,
    SearchProvider,
    WebOfScienceProvider,
    deduplicate_documents,
    merge_documents,
    matches_criteria,
    normalize_doi,
)
from app.services.providers.base import first_author_key


@dataclass(slots=True)
class SearchSummary:
    search_number: int | None
    total_results: int
    new_documents: int
    saved: bool
    search_id: uuid.UUID | None = None
    pdf_jobs: int = 0


def build_providers(criteria: SearchCriteria, settings: Settings | None = None) -> list[SearchProvider]:
    settings = settings or get_settings()
    providers: list[SearchProvider] = []
    if "scopus" in criteria.sources:
        providers.append(
            ScopusProvider(
                api_key=settings.scopus_api_key,
                inst_token=settings.scopus_inst_token,
                api_url=settings.scopus_api_url,
                timeout=settings.api_timeout_seconds,
                max_results=settings.search_max_results,
            )
        )
    if "wos" in criteria.sources:
        providers.append(
            WebOfScienceProvider(
                api_key=settings.wos_api_key,
                api_url=settings.wos_api_url,
                timeout=settings.api_timeout_seconds,
                max_results=settings.search_max_results,
            )
        )
    return providers


def _find_existing(db: Session, document: NormalizedDocument) -> TaiLieu | None:
    doi = normalize_doi(document.doi)
    if doi:
        existing = db.scalar(select(TaiLieu).where(func.lower(func.btrim(TaiLieu.doi)) == doi))
        if existing:
            return existing
    for name in ("scopus_eid", "wos_uid"):
        value = document.external_ids.get(name)
        if value:
            existing = db.scalar(select(TaiLieu).where(TaiLieu.ma_ngoai.contains({name: value})))
            if existing:
                return existing
    if document.normalized_title:
        candidates = db.scalars(
            select(TaiLieu).where(
                TaiLieu.tieu_de_chuan_hoa == document.normalized_title,
                TaiLieu.nam_xuat_ban == document.publication_year,
            )
        )
        wanted_author = first_author_key(document.authors)
        for candidate in candidates:
            if first_author_key(candidate.tac_gia or []) == wanted_author:
                return candidate
    return None


def _new_entity(document: NormalizedDocument) -> TaiLieu:
    return TaiLieu(
        doi=normalize_doi(document.doi),
        tieu_de=document.title,
        tieu_de_chuan_hoa=document.normalized_title,
        tom_tat=document.abstract,
        tac_gia=document.authors,
        tu_khoa=document.keywords,
        loai_tai_lieu=document.document_type,
        ngon_ngu=document.language,
        ngay_xuat_ban=document.publication_date,
        nam_xuat_ban=document.publication_year,
        ten_tap_chi=document.journal,
        issn=document.issn,
        nha_xuat_ban=document.publisher,
        volume=document.volume,
        issue=document.issue,
        trang_hoac_so_bai=document.pages_or_article,
        quartile=document.quartile,
        thong_tin_quartile=document.quartile_info,
        chi_so_trich_dan=document.citation_info,
        ma_ngoai=document.external_ids,
        du_lieu_nguon=document.source_data,
        truy_cap_mo=document.open_access,
        url_trang_goc=document.source_url,
        url_pdf=document.pdf_url,
    )


def _merge_entity(entity: TaiLieu, incoming: NormalizedDocument) -> None:
    current = NormalizedDocument(
        doi=entity.doi,
        title=entity.tieu_de,
        normalized_title=entity.tieu_de_chuan_hoa or incoming.normalized_title,
        abstract=entity.tom_tat,
        authors=entity.tac_gia or [],
        keywords=entity.tu_khoa or [],
        document_type=entity.loai_tai_lieu,
        language=entity.ngon_ngu,
        publication_date=entity.ngay_xuat_ban,
        publication_year=entity.nam_xuat_ban,
        journal=entity.ten_tap_chi,
        issn=entity.issn,
        publisher=entity.nha_xuat_ban,
        volume=entity.volume,
        issue=entity.issue,
        pages_or_article=entity.trang_hoac_so_bai,
        quartile=entity.quartile,
        quartile_info=entity.thong_tin_quartile,
        citation_info=entity.chi_so_trich_dan,
        external_ids=entity.ma_ngoai or {},
        source_data=entity.du_lieu_nguon or {},
        open_access=entity.truy_cap_mo,
        source_url=entity.url_trang_goc,
        pdf_url=entity.url_pdf,
    )
    merged = merge_documents(current, incoming)
    entity.doi = normalize_doi(merged.doi)
    entity.tieu_de = merged.title
    entity.tieu_de_chuan_hoa = merged.normalized_title
    entity.tom_tat = merged.abstract
    entity.tac_gia = merged.authors
    entity.tu_khoa = merged.keywords
    entity.loai_tai_lieu = merged.document_type
    entity.ngon_ngu = merged.language
    entity.ngay_xuat_ban = merged.publication_date
    entity.nam_xuat_ban = merged.publication_year
    entity.ten_tap_chi = merged.journal
    entity.issn = merged.issn
    entity.nha_xuat_ban = merged.publisher
    entity.volume = merged.volume
    entity.issue = merged.issue
    entity.trang_hoac_so_bai = merged.pages_or_article
    entity.quartile = merged.quartile
    entity.thong_tin_quartile = merged.quartile_info
    entity.chi_so_trich_dan = merged.citation_info
    entity.ma_ngoai = merged.external_ids
    entity.du_lieu_nguon = merged.source_data
    entity.truy_cap_mo = merged.open_access
    entity.url_trang_goc = merged.source_url
    entity.url_pdf = merged.pdf_url


def perform_search(
    db: Session,
    topic_id: uuid.UUID,
    user_id: uuid.UUID,
    criteria: SearchCriteria,
    providers: list[SearchProvider] | None = None,
    search_type: str = "thuong",
    ai_request: str | None = None,
    ai_conditions: dict[str, Any] | None = None,
    create_ai_jobs: bool = False,
) -> SearchSummary:
    started_at = dt.datetime.now(dt.UTC)
    settings = get_settings()
    if search_type not in {"thuong", "ai"}:
        raise ValueError("Loại tìm kiếm không hợp lệ.")
    topic = db.scalar(select(DeTai).where(DeTai.id == topic_id).with_for_update())
    if not topic:
        raise ValueError("Không tìm thấy đề tài.")
    all_documents: list[NormalizedDocument] = []
    for provider in providers if providers is not None else build_providers(criteria):
        all_documents.extend(provider.search(criteria))
    documents = [item for item in deduplicate_documents(all_documents) if matches_criteria(item, criteria)]
    if not documents and search_type == "thuong":
        return SearchSummary(None, 0, 0, False)

    current_number = db.scalar(
        select(func.coalesce(func.max(LanTimKiem.lan), 0)).where(LanTimKiem.id_de_tai == topic_id)
    )
    search_row = LanTimKiem(
        id_de_tai=topic_id,
        id_nguoi_dung=user_id,
        dieu_kien_tim_kiem=criteria.model_dump(mode="json"),
        lan=int(current_number or 0) + 1,
        loai_tim_kiem=search_type,
        yeu_cau_ai=ai_request if search_type == "ai" else None,
        dieu_kien_ai=ai_conditions if search_type == "ai" else None,
        trang_thai_ai="cho_xu_ly" if search_type == "ai" and documents else (
            "hoan_thanh" if search_type == "ai" else None
        ),
        ngay_hoan_thanh_ai=func.now() if search_type == "ai" and not documents else None,
        thoi_gian_bat_dau=started_at,
        thoi_gian_bat_dau_dot_pdf=started_at if search_type == "thuong" else None,
        thoi_gian_chay_ms=0 if search_type == "thuong" else None,
        trang_thai_tai_pdf="cho_xu_ly" if search_type == "thuong" else "hoan_thanh",
    )
    db.add(search_row)
    db.flush()
    new_count = 0
    pdf_already_available = 0
    pdf_jobs = 0
    for normalized in documents:
        entity = _find_existing(db, normalized)
        if entity is None:
            entity = _new_entity(normalized)
            db.add(entity)
            db.flush()
        else:
            _merge_entity(entity, normalized)
            db.flush()

        link = db.scalar(
            select(DeTaiTaiLieu).where(
                DeTaiTaiLieu.id_de_tai == topic_id,
                DeTaiTaiLieu.id_tai_lieu == entity.id,
            )
        )
        if link is None:
            link = DeTaiTaiLieu(id_de_tai=topic_id, id_tai_lieu=entity.id)
            db.add(link)
            db.flush()
            new_count += 1
        association = db.get(DeTaiTaiLieuLanTimKiem, (link.id, search_row.id))
        if association is None:
            db.add(
                DeTaiTaiLieuLanTimKiem(
                    id_de_tai=topic_id,
                    id_de_tai_tai_lieu=link.id,
                    id_lan_tim_kiem=search_row.id,
                )
            )
        if search_type == "thuong":
            has_physical_pdf = False
            if entity.id_file_pdf:
                file_record = db.get(TepTin, entity.id_file_pdf)
                if file_record:
                    try:
                        has_physical_pdf = resolve_stored_path(file_record, settings).is_file()
                    except OSError:
                        has_physical_pdf = False
            if has_physical_pdf:
                pdf_already_available += 1
            else:
                entity.id_file_pdf = None
                existing_pdf_job = db.scalar(
                    select(CongViecTaiPdf).where(
                        CongViecTaiPdf.id_lan_tim_kiem == search_row.id,
                        CongViecTaiPdf.id_de_tai_tai_lieu == link.id,
                    )
                )
                if existing_pdf_job is None:
                    db.add(
                        CongViecTaiPdf(
                            id_de_tai=topic_id,
                            id_lan_tim_kiem=search_row.id,
                            id_de_tai_tai_lieu=link.id,
                        )
                    )
                    pdf_jobs += 1
        if create_ai_jobs:
            evaluation = db.scalar(
                select(DanhGiaAiTaiLieu).where(
                    DanhGiaAiTaiLieu.id_de_tai_tai_lieu == link.id,
                    DanhGiaAiTaiLieu.id_lan_tim_kiem == search_row.id,
                )
            )
            if evaluation is None:
                db.add(
                    DanhGiaAiTaiLieu(
                        id_de_tai=topic_id,
                        id_de_tai_tai_lieu=link.id,
                        id_lan_tim_kiem=search_row.id,
                    )
                )

    search_row.tong_ket_qua = len(documents)
    search_row.so_tai_lieu_moi = new_count
    search_row.so_pdf_da_co = pdf_already_available
    search_row.tong_pdf_can_tai = pdf_jobs
    if search_type == "thuong" and pdf_jobs == 0:
        finished_at = dt.datetime.now(dt.UTC)
        search_row.trang_thai_tai_pdf = "hoan_thanh"
        search_row.thoi_gian_hoan_thanh = finished_at
        search_row.thoi_gian_chay_ms = max(0, int((finished_at - started_at).total_seconds() * 1000))
        search_row.thoi_gian_bat_dau_dot_pdf = None
    db.flush()
    return SearchSummary(search_row.lan, len(documents), new_count, True, search_row.id, pdf_jobs)
