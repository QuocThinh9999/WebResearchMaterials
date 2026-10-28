"""Dừng ngay các lượt truy cập nguồn PDF của đợt tải đã tạm dừng."""

from __future__ import annotations

import threading
import uuid
from contextlib import contextmanager


class PdfSearchCancelled(Exception):
    """Người dùng đã dừng đợt tải PDF."""


_events: dict[uuid.UUID, threading.Event] = {}
_lock = threading.Lock()
_current = threading.local()


def _event(search_id: uuid.UUID) -> threading.Event:
    with _lock:
        return _events.setdefault(search_id, threading.Event())


def cancel_pdf_search(search_id: uuid.UUID) -> None:
    _event(search_id).set()


def resume_pdf_requests(search_id: uuid.UUID) -> None:
    _event(search_id).clear()


def pdf_search_cancelled(search_id: uuid.UUID) -> bool:
    return _event(search_id).is_set()


def current_pdf_search_id() -> uuid.UUID | None:
    return getattr(_current, "search_id", None)


@contextmanager
def bind_pdf_search(search_id: uuid.UUID):
    previous = getattr(_current, "search_id", None)
    _current.search_id = search_id
    try:
        yield
    finally:
        _current.search_id = previous


def check_pdf_search_not_cancelled() -> None:
    search_id = getattr(_current, "search_id", None)
    if search_id is not None and pdf_search_cancelled(search_id):
        raise PdfSearchCancelled()
