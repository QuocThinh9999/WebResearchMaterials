from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import DeTaiTaiLieu, TrangThaiTaiLieu, TrangThaiTaiLieuHienTai


def append_status_if_changed(
    db: Session,
    link_id: uuid.UUID,
    user_id: uuid.UUID,
    status_id: uuid.UUID,
    reason: str | None = None,
) -> TrangThaiTaiLieu | None:
    """Thêm lịch sử trạng thái, trừ khi trạng thái hiện tại của người dùng không đổi."""
    link = db.scalar(
        select(DeTaiTaiLieu).where(DeTaiTaiLieu.id == link_id).with_for_update()
    )
    if not link:
        raise ValueError("Không tìm thấy tài liệu trong đề tài.")
    current = db.scalar(
        select(TrangThaiTaiLieuHienTai).where(
            TrangThaiTaiLieuHienTai.id_de_tai_tai_lieu == link_id,
            TrangThaiTaiLieuHienTai.id_nguoi_dung == user_id,
        )
    )
    if current and current.id_trang_thai == status_id:
        return None
    state = TrangThaiTaiLieu(
        id_de_tai_tai_lieu=link_id,
        id_trang_thai=status_id,
        ly_do=reason.strip() if reason and reason.strip() else None,
        id_nguoi_dung=user_id,
    )
    db.add(state)
    db.flush()
    return state

