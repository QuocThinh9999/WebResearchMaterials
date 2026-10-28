from __future__ import annotations

from typing import TypeVar

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import TrangThaiDeTai, TrangThaiTaiLieuDanhMuc


CatalogModel = TypeVar("CatalogModel", TrangThaiDeTai, TrangThaiTaiLieuDanhMuc)


def list_catalog(db: Session, model: type[CatalogModel]) -> list[CatalogModel]:
    return list(db.scalars(select(model).order_by(model.ngay_tao, model.ten_trang_thai)))

