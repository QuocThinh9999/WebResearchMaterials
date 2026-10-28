import uuid

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import NguoiDung


def get_current_user(request: Request, db: Session = Depends(get_db)) -> NguoiDung:
    raw_id = request.session.get("user_id")
    if not raw_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    try:
        user_id = uuid.UUID(raw_id)
    except (TypeError, ValueError):
        request.session.clear()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED) from None
    user = db.get(NguoiDung, user_id)
    if not user or not user.dang_hoat_dong:
        request.session.clear()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    request.state.current_user = user
    return user

