from __future__ import annotations

import asyncio
import datetime as dt
import re
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from fastapi import BackgroundTasks, UploadFile
from fastapi.testclient import TestClient
from starlette.datastructures import Headers
from starlette.requests import Request

from app.database import get_db
from app.dependencies import get_current_user
from app.main import app
from app.models import TaiLieu
from app.repositories.pdf_jobs import PdfProgress, synchronize_manual_pdf_jobs
from app.routers import ai_search, documents, topics


def test_optional_ai_uuid_and_pdf_filters_render_html(monkeypatch):
    topic_id = uuid4()
    topic = SimpleNamespace(id=topic_id, ten_de_tai="Đề tài kiểm thử", mo_ta="")
    user = SimpleNamespace(id=uuid4(), ten_nguoi_dung="Người kiểm thử")
    db = MagicMock()
    db.get.return_value = topic
    db.scalars.return_value = []
    observed = []
    monkeypatch.setattr(ai_search, "list_source_searches", lambda *_: [])
    monkeypatch.setattr(ai_search, "list_documents", lambda _db, _topic, filters: (observed.append(filters.pdf_status) or [], False))
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        client = TestClient(app)
        for suffix, expected in (
            ("lan_tra_cuu_ai=&pdf_status=co_pdf", "co_pdf"),
            ("lan_tra_cuu_ai=&pdf_status=khong_pdf", "khong_pdf"),
            ("lan_tra_cuu_ai=&pdf_status=", None),
            ("lan_tra_cuu_ai=sai&pdf_status=co_pdf", "co_pdf"),
        ):
            response = client.get(f"/tra-cuu-ai?topic_id={topic_id}&{suffix}")
            assert response.status_code == 200
            assert "Tra cứu bằng AI" in response.text
            assert "uuid_parsing" not in response.text
            assert observed[-1] == expected
        assert 'data-omit-empty-uuid' in response.text
        assert 'data-search-clock' in response.text
        assert "Tải tất cả PDF" in response.text
    finally:
        app.dependency_overrides.clear()


def test_two_pdf_tabs_render_same_progress_without_error_list(monkeypatch):
    topic_id = uuid4()
    search_id = uuid4()
    user = SimpleNamespace(id=uuid4(), ten_nguoi_dung="Người kiểm thử")
    search = SimpleNamespace(
        id=search_id, lan=1, loai_tim_kiem="thuong", trang_thai_tai_pdf="loi",
        yeu_cau_dung_tai_pdf=False, thoi_gian_chay_ms=1_186_000,
        thoi_gian_bat_dau_dot_pdf=None, thoi_gian=dt.datetime(2026, 9, 25, tzinfo=dt.UTC),
        tong_ket_qua=2, so_tai_lieu_moi=2,
    )
    topic = SimpleNamespace(id=topic_id, ten_de_tai="Đề tài kiểm thử", mo_ta="")
    topic_row = SimpleNamespace(topic=topic, status=SimpleNamespace(mau_trang_thai="#123456", ten_trang_thai="Mới"), creator=user)
    db = MagicMock()
    db.scalars.return_value = [search]
    monkeypatch.setattr(topics, "get_topic_row", lambda *_: topic_row)
    monkeypatch.setattr(topics, "list_documents", lambda *_: ([], False))
    monkeypatch.setattr(topics, "list_catalog", lambda *_: [])
    monkeypatch.setattr(topics, "get_pdf_progress", lambda *_: PdfProgress(2, 0, 0, 1, 0, 1, 2))
    monkeypatch.setattr(topics, "get_pdf_manager_state", lambda *_: SimpleNamespace(total_missing=1))
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        response = TestClient(app).get(f"/de-tai/{topic_id}?pdf_search_id={search_id}")
        assert response.status_code == 200
        assert response.text.count('role="tab"') == 2
        assert response.text.count('class="pdf-stat-grid"') == 2
        assert "Tổng số công việc lỗi cần thử lại: <span data-pdf-failed-count>1</span>" in response.text
        assert "00 giờ 19 phút 46 giây" in response.text
        assert "1186s" not in response.text
        assert "pdf-failed-list" not in response.text
        assert "Tải lại PDF lỗi" in response.text
    finally:
        app.dependency_overrides.clear()


def test_manual_pdf_updates_related_jobs(monkeypatch):
    document_id = uuid4()
    search_ids = [uuid4(), uuid4()]
    jobs = [SimpleNamespace(id_lan_tim_kiem=search_id, trang_thai="loi", nguon_pdf=None,
                            url_pdf_cuoi="https://example.org/old.pdf", loi_xu_ly="Cũ",
                            ngay_cap_nhat=None, ngay_hoan_thanh=None) for search_id in search_ids]
    db = MagicMock()
    db.execute.return_value.scalars.return_value.all.return_value = jobs
    refreshed = []
    monkeypatch.setattr("app.repositories.pdf_jobs.update_pdf_search_progress", lambda _db, search_id: refreshed.append(search_id))
    assert synchronize_manual_pdf_jobs(db, document_id) == set(search_ids)
    assert set(refreshed) == set(search_ids)
    assert all(job.trang_thai == "thanh_cong" and job.nguon_pdf == "tai_len_thu_cong" and job.loi_xu_ly is None and job.ngay_hoan_thanh for job in jobs)


def test_manual_pdf_upload_accepts_real_signature_and_rejects_fake(monkeypatch):
    topic_id, document_id = uuid4(), uuid4()
    document = TaiLieu(id=document_id, tieu_de="Bài báo", tac_gia=[], tu_khoa=[], ma_ngoai={}, du_lieu_nguon={})
    db = MagicMock()
    db.execute.return_value.one_or_none.return_value = (SimpleNamespace(id=uuid4()), document)
    user = SimpleNamespace(id=uuid4())
    saved = []
    synchronized = []
    monkeypatch.setattr(documents, "save_file_record", lambda *_: (saved.append(True) or SimpleNamespace(id=uuid4()), None))
    monkeypatch.setattr(documents, "synchronize_manual_pdf_jobs", lambda _db, doc_id: synchronized.append(doc_id))

    def request():
        return Request({"type": "http", "method": "POST", "path": "/", "headers": [], "query_string": b"", "session": {"csrf_token": "hop-le"}})

    def upload(content):
        return UploadFile(file=BytesIO(content), filename="paper.pdf", headers=Headers({"content-type": "application/pdf"}))

    good = asyncio.run(documents.upload_pdf(topic_id, document_id, request(), upload(b"%PDF-1.7\n"), "hop-le", db, user))
    assert good.status_code == 303
    assert document.id_file_pdf is not None
    assert synchronized == [document_id]
    assert len(saved) == 1
    bad = asyncio.run(documents.upload_pdf(topic_id, document_id, request(), upload("<html>Đăng nhập</html>".encode()), "hop-le", db, user))
    assert bad.status_code == 303
    assert len(saved) == 1
    assert synchronized == [document_id]


def test_retry_failed_route_queues_only_selected_search_and_starts_worker(monkeypatch):
    topic_id, search_id = uuid4(), uuid4()
    search = SimpleNamespace(
        id=search_id, trang_thai_tai_pdf="loi", yeu_cau_dung_tai_pdf=False,
        thoi_gian_chay_ms=1000, thoi_gian_bat_dau_dot_pdf=None, thoi_gian_hoan_thanh=None,
    )
    db = MagicMock()
    db.scalar.return_value = search
    queued = []
    monkeypatch.setattr(topics, "retry_failed_pdf_jobs", lambda _db, selected: queued.append(selected) or 2)
    monkeypatch.setattr(topics, "update_pdf_search_progress", lambda *_: None)
    background = BackgroundTasks()
    request = Request({"type": "http", "method": "POST", "path": "/", "headers": [], "query_string": b"", "session": {"csrf_token": "hop-le"}})
    response = topics.retry_failed_pdf_download(topic_id, request, background, str(search_id), "hop-le", db, SimpleNamespace(id=uuid4()))
    assert response.status_code == 303
    assert response.headers["location"] == f"/de-tai/{topic_id}?pdf_search_id={search_id}#pdf-panel-failed"
    assert queued == [search_id]
    assert search.trang_thai_tai_pdf == "cho_xu_ly"
    assert len(background.tasks) == 1
    assert background.tasks[0].func == topics.run_pdf_batch
    assert background.tasks[0].args == (search_id,)


def test_stop_pdf_route_cancels_active_requests_and_disconnects_chrome(monkeypatch):
    topic_id, search_id = uuid4(), uuid4()
    search = SimpleNamespace(
        id=search_id, trang_thai_tai_pdf="dang_xu_ly", yeu_cau_dung_tai_pdf=False,
        thoi_gian_chay_ms=0, thoi_gian_bat_dau_dot_pdf=None,
    )
    db = MagicMock()
    db.scalar.return_value = search
    cancelled = []
    disconnected = []
    monkeypatch.setattr(topics, "cancel_pdf_search", cancelled.append)
    monkeypatch.setattr(topics, "disconnect_personal_chrome", lambda: disconnected.append(True))
    request = Request({"type": "http", "method": "POST", "path": "/", "headers": [], "query_string": b"", "session": {"csrf_token": "hop-le"}})

    response = topics.stop_pdf_download(topic_id, search_id, request, "hop-le", db, SimpleNamespace(id=uuid4()))

    assert response.status_code == 303
    assert search.trang_thai_tai_pdf == "tam_dung"
    assert search.yeu_cau_dung_tai_pdf is True
    assert cancelled == [search_id]
    assert disconnected == [True]


def test_main_http_routes_with_isolated_testclient(monkeypatch):
    topic_id, document_id, search_id, status_id = uuid4(), uuid4(), uuid4(), uuid4()
    user = SimpleNamespace(id=uuid4(), ten_nguoi_dung="Người kiểm thử")
    topic = SimpleNamespace(id=topic_id, ten_de_tai="Đề tài kiểm thử", mo_ta="")
    topic_row = SimpleNamespace(topic=topic, status=SimpleNamespace(mau_trang_thai="#123456", ten_trang_thai="Mới"), creator=user)
    document = TaiLieu(id=document_id, tieu_de="Bài báo", tac_gia=[], tu_khoa=[], ma_ngoai={}, du_lieu_nguon={})
    link = SimpleNamespace(id=uuid4())
    search = SimpleNamespace(
        id=search_id, lan=1, loai_tim_kiem="thuong", trang_thai_tai_pdf="loi",
        yeu_cau_dung_tai_pdf=False, thoi_gian_chay_ms=0,
        thoi_gian_bat_dau_dot_pdf=None, thoi_gian_hoan_thanh=None,
        thoi_gian_bat_dau=dt.datetime(2026, 9, 25, tzinfo=dt.UTC),
        thoi_gian=dt.datetime(2026, 9, 25, tzinfo=dt.UTC),
        tong_ket_qua=1, so_tai_lieu_moi=1,
    )
    db = MagicMock()
    db.scalars.return_value = [search]
    db.scalar.return_value = search
    db.get.return_value = SimpleNamespace(id=status_id)
    db.execute.return_value.one_or_none.return_value = (link, document)
    db.execute.return_value.all.return_value = []
    monkeypatch.setattr(topics, "list_topics", lambda *_args, **_kwargs: ([], False))
    monkeypatch.setattr(topics, "list_users", lambda *_: [])
    monkeypatch.setattr(topics, "list_catalog", lambda *_: [])
    monkeypatch.setattr(topics, "list_documents", lambda *_args, **_kwargs: ([], False))
    monkeypatch.setattr(topics, "get_topic_row", lambda *_: topic_row)
    monkeypatch.setattr(topics, "get_pdf_progress", lambda *_: PdfProgress(1, 0, 0, 0, 0, 1, 1))
    monkeypatch.setattr(topics, "get_pdf_manager_state", lambda *_: SimpleNamespace(total_missing=1))
    monkeypatch.setattr(documents, "list_catalog", lambda *_: [])
    monkeypatch.setattr(documents, "save_file_record", lambda *_: (SimpleNamespace(id=uuid4()), None))
    uploads = []
    monkeypatch.setattr(documents, "synchronize_manual_pdf_jobs", lambda _db, doc_id: uploads.append(doc_id))
    monkeypatch.setattr(documents, "append_status_if_changed", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(topics, "prepare_pdf_jobs_for_topic", lambda *_: 1)
    monkeypatch.setattr(topics, "retry_failed_pdf_jobs", lambda *_: 1)
    monkeypatch.setattr(topics, "update_pdf_search_progress", lambda *_: PdfProgress(1, 1, 0, 0, 0, 0, 0))
    worker_calls = []
    monkeypatch.setattr(topics, "run_pdf_batch", lambda selected: worker_calls.append(selected))
    browser_calls = []
    monkeypatch.setattr(topics, "connect_personal_chrome", lambda: browser_calls.append(True))
    monkeypatch.setattr(topics, "personal_chrome_connected", lambda: False)

    class WorkDb:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def begin(self):
            return self

    monkeypatch.setattr(topics, "SessionLocal", lambda: WorkDb())
    monkeypatch.setattr(topics, "perform_search", lambda *_: SimpleNamespace(search_id=None, pdf_jobs=0, saved=False))
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        client = TestClient(app)
        login = client.get("/dang-nhap")
        assert login.status_code == 200
        for path in (
            "/de-tai", "/de-tai/tai-them?offset=0",
            f"/de-tai/{topic_id}",
            f"/de-tai/{topic_id}/tai-lieu/tai-them?offset=0&pdf_status=co_pdf",
            f"/de-tai/{topic_id}/tai-lieu/{document_id}",
            f"/de-tai/{topic_id}/lan-tim-kiem/{search_id}/tien-do-pdf",
        ):
            assert client.get(path).status_code == 200, path
        detail = client.get(f"/de-tai/{topic_id}/tai-lieu/{document_id}")
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', detail.text).group(1)
        topic_page = client.get(f"/de-tai/{topic_id}")
        assert "Kết nối Chrome thường để tải PDF" in topic_page.text
        browser_open = client.post(
            f"/de-tai/{topic_id}/tai-pdf/ket-noi-chrome",
            data={"csrf_token": csrf}, follow_redirects=False,
        )
        assert browser_open.status_code == 303 and browser_calls == [True]
        upload = client.post(
            f"/de-tai/{topic_id}/tai-lieu/{document_id}/pdf",
            data={"csrf_token": csrf}, files={"pdf": ("paper.pdf", b"%PDF-1.7\n", "application/pdf")},
            follow_redirects=False,
        )
        assert upload.status_code == 303 and uploads == [document_id]
        change = client.post(
            f"/de-tai/{topic_id}/tai-lieu/{document_id}/trang-thai",
            data={"csrf_token": csrf, "status_id": str(status_id)}, follow_redirects=False,
        )
        assert change.status_code == 303
        search_result = client.post(
            f"/de-tai/{topic_id}/tim-kiem",
            data={"csrf_token": csrf, "keywords": "AI", "source_scopus": "1"}, follow_redirects=False,
        )
        assert search_result.status_code == 303
        monkeypatch.setattr(
            topics, "perform_search",
            lambda *_: SimpleNamespace(search_id=None, pdf_jobs=0, saved=True, search_number=7, total_results=1, new_documents=0),
        )
        saved_search = client.post(
            f"/de-tai/{topic_id}/tim-kiem",
            data={"csrf_token": csrf, "keywords": "AI", "source_scopus": "1", "q1": "1"},
            follow_redirects=False,
        )
        assert saved_search.headers["location"] == f"/de-tai/{topic_id}?search_numbers=7"
        continued = client.post(
            f"/de-tai/{topic_id}/tai-pdf/tiep-tuc",
            data={"csrf_token": csrf, "search_id": str(search_id)}, follow_redirects=False,
        )
        assert continued.status_code == 303 and worker_calls == [search_id]
        retried = client.post(
            f"/de-tai/{topic_id}/tai-pdf/tai-lai-loi",
            data={"csrf_token": csrf, "search_id": str(search_id)}, follow_redirects=False,
        )
        assert retried.status_code == 303 and worker_calls == [search_id, search_id]
    finally:
        app.dependency_overrides.clear()
