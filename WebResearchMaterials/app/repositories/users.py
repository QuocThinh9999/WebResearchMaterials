from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import NguoiDung


def list_users(db: Session) -> list[NguoiDung]:
    return list(db.scalars(select(NguoiDung).order_by(NguoiDung.ten_nguoi_dung, NguoiDung.ten_dang_nhap)))


def get_by_username(db: Session, username: str) -> NguoiDung | None:
    normalized = username.strip().lower()
    return db.scalar(select(NguoiDung).where(func.lower(NguoiDung.ten_dang_nhap) == normalized))


def get_user(db: Session, user_id: uuid.UUID) -> NguoiDung | None:
    return db.get(NguoiDung, user_id)

