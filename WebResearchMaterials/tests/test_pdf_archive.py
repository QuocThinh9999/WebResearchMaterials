from __future__ import annotations

import datetime as dt
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4
from zipfile import ZipFile

from fastapi.testclient import TestClient

from app.database import get_db
from app.dependencies import get_current_user
from app.main import app
from app.routers import topics
from app.services import pdf_archive


def test_pdf_archive_uses_article_titles_and_unique_safe_names(tmp_path, monkeypatch):
    first = tmp_path / "first.pdf"
    second = tmp_path / "second.pdf"
    first.write_bytes(b"%PDF-1.7\nfirst")
    second.write_bytes(b"%PDF-1.7\nsecond")
    records = [SimpleNamespace(mime_type="application/pdf", path=first), SimpleNamespace(mime_type="application/pdf", path=second)]
    monkeypatch.setattr(pdf_archive, "resolve_stored_path", lambda record: record.path)

    archive = pdf_archive.create_pdf_archive([("Bài / nghiên cứu", records[0]), ("Bài : nghiên cứu", records[1])])
    assert archive is not None
    try:
        with ZipFile(archive) as zipped:
            assert zipped.namelist() == ["PDF/Bài   nghiên cứu.pdf", "PDF/Bài   nghiên cứu (2).pdf"]
            assert zipped.read(zipped.namelist()[0]) == first.read_bytes()
    finally:
        archive.close()


def test_download_all_pdfs_respects_ai_filters(monkeypatch, tmp_path):
    topic_id, lookup_id = uuid4(), uuid4()
    user = SimpleNamespace(id=uuid4())
    lookup = SimpleNamespace(id=lookup_id)
    db = MagicMock()
    db.get.return_value = SimpleNamespace(id=topic_id)
    db.scalar.return_value = lookup
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.7\n")
    monkeypatch.setattr(pdf_archive, "resolve_stored_path", lambda _: pdf)
    observed = []
    monkeypatch.setattr(
        topics, "list_pdf_files",
        lambda _db, selected_topic, filters: (
            observed.append((selected_topic, filters.ai_lookup_id, filters.ai_label, filters.keyword, filters.pdf_status))
            or [("Tiêu đề bài viết", SimpleNamespace(mime_type="application/pdf"))]
        ),
    )
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        response = TestClient(app).get(
            f"/de-tai/{topic_id}/tai-pdf/tai-tat-ca?lan_tra_cuu_ai={lookup_id}&nhan=phu_hop&keyword=ngan+hang&pdf_status=khong_pdf"
        )
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/zip"
        with ZipFile(BytesIO(response.content)) as zipped:
            assert zipped.namelist() == ["PDF/Tiêu đề bài viết.pdf"]
        assert observed == [(topic_id, lookup_id, "phu_hop", "ngan hang", "co_pdf")]
    finally:
        app.dependency_overrides.clear()


def test_topic_page_shows_selected_ai_conditions_and_filters(monkeypatch):
    topic_id, lookup_id = uuid4(), uuid4()
    user = SimpleNamespace(id=uuid4(), ten_nguoi_dung="Người kiểm thử")
    topic = SimpleNamespace(id=topic_id, ten_de_tai="Đề tài", mo_ta="")
    topic_row = SimpleNamespace(topic=topic, status=SimpleNamespace(mau_trang_thai="#123456", ten_trang_thai="Mới"), creator=user)
    lookup = SimpleNamespace(
        id=lookup_id, ngay_tao=dt.datetime(2026, 9, 26), thoi_gian_chay_ms=1000,
        trang_thai="hoan_thanh", dieu_kien_ai={"dien_giai_ngan_gon": "Chỉ bài về ngân hàng", "dieu_kien_nguyen_tu": []},
    )
    db = MagicMock()
    db.scalars.return_value = []
    db.execute.return_value.scalars.return_value.all.return_value = [lookup]
    db.scalar.return_value = lookup
    observed = []
    monkeypatch.setattr(topics, "get_topic_row", lambda *_: topic_row)
    monkeypatch.setattr(topics, "list_documents", lambda _db, _topic, filters: (observed.append(filters) or [], False))
    monkeypatch.setattr(topics, "count_documents", lambda *_: 0)
    monkeypatch.setattr(topics, "list_catalog", lambda *_: [])
    monkeypatch.setattr(topics, "get_pdf_manager_state", lambda *_: SimpleNamespace(total_missing=0))
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        response = TestClient(app).get(f"/de-tai/{topic_id}?lan_tra_cuu_ai={lookup_id}&nhan=phu_hop")
        assert response.status_code == 200
        assert observed[0].ai_lookup_id == lookup_id
        assert observed[0].ai_label == "phu_hop"
        assert "Điều hệ thống AI đã hiểu" in response.text
        assert "Chỉ bài về ngân hàng" in response.text
        assert 'name="lan_tra_cuu_ai"' in response.text
        assert 'name="nhan"' in response.text
        assert "Tải tất cả PDF" in response.text
    finally:
        app.dependency_overrides.clear()


def test_load_more_keeps_ai_label(monkeypatch):
    topic_id, lookup_id = uuid4(), uuid4()
    user = SimpleNamespace(id=uuid4())
    db = MagicMock()
    db.scalar.return_value = SimpleNamespace(id=lookup_id)
    observed = []
    monkeypatch.setattr(topics, "list_documents", lambda _db, _topic, filters, _offset: (observed.append(filters) or [], False))
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        response = TestClient(app).get(
            f"/de-tai/{topic_id}/tai-lieu/tai-them?offset=30&lan_tra_cuu_ai={lookup_id}&nhan=phu_hop"
        )
        assert response.status_code == 200
        assert observed[0].ai_lookup_id == lookup_id
        assert observed[0].ai_label == "phu_hop"
    finally:
        app.dependency_overrides.clear()
