from __future__ import annotations

import datetime as dt
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import httpx
import pytest
from fastapi import BackgroundTasks, HTTPException
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from starlette.requests import Request

import app.services.ai.worker as worker_module
from app.routers.topics import continue_pdf_download, retry_failed_pdf_download
from app.models import CongViecTaiPdf, KetQuaTraCuuAi, TaiLieu, TepTin
from app.repositories.documents import DocumentFilters, _document_query
from app.repositories.pdf_jobs import (
    PdfProgress,
    claim_next_pdf_job,
    get_pdf_elapsed_ms,
    pause_pdf_search,
    resume_pdf_search,
    retry_failed_pdf_jobs,
)
from app.services.files import save_file_record
from app.services.pdf_cancel import PdfSearchCancelled, bind_pdf_search, cancel_pdf_search, resume_pdf_requests
from app.services.pdf_downloader import HostRequestLimiter, PdfConnectivityError, PdfSourceUnavailableError, download_pdf, get_with_retries
from app.services.pdf_sources import PdfCandidate, _deduplicate, _PdfLinkParser, _publisher_candidates, find_and_download_pdf
from app.services.pdf_worker import PdfDocumentSnapshot, _existing_pdf_is_valid, prepare_pdf_jobs_for_topic
import app.services.pdf_worker as pdf_worker_module
from app.services.time_format import format_elapsed_ms


def settings(tmp_path, **changes):
    values = {
        "scopus_api_key": None,
        "scopus_inst_token": None,
        "api_timeout_seconds": 5,
        "max_upload_mb": 1,
        "pdf_max_redirects": 3,
        "pdf_max_retries": 1,
        "pdf_download_workers": 6,
        "pdf_download_per_host": 2,
        "unpaywall_email": None,
        "upload_path": tmp_path,
    }
    values.update(changes)
    return SimpleNamespace(**values)


def public_resolver(_: str) -> list[str]:
    return ["93.184.216.34"]


def test_pdf_worker_snapshot_contains_original_landing_page():
    assert "url_trang_goc" in {item.name for item in fields(PdfDocumentSnapshot)}


def test_pdf_url_from_metadata_is_used_first(tmp_path):
    calls = []

    def handler(request: httpx.Request):
        calls.append(str(request.url))
        return httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF-1.7\n")

    document = TaiLieu(
        tieu_de="Bài báo",
        tac_gia=[],
        tu_khoa=[],
        du_lieu_nguon={},
        ma_ngoai={},
        url_pdf="https://public.test/paper.pdf",
    )
    located = find_and_download_pdf(
        document,
        settings(tmp_path),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        resolver=public_resolver,
    )
    assert located and located.source == "metadata_pdf_url"
    assert calls == ["https://public.test/paper.pdf"]


def test_pdf_signature_is_accepted_when_server_labels_it_as_binary(tmp_path):
    document = TaiLieu(
        tieu_de="Bài báo", tac_gia=[], tu_khoa=[], du_lieu_nguon={}, ma_ngoai={},
        url_pdf="https://public.test/paper.pdf",
    )
    client = httpx.Client(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, headers={"content-type": "application/octet-stream"}, content=b"%PDF-1.7\n")
    ))
    located = find_and_download_pdf(document, settings(tmp_path), client=client, resolver=public_resolver)
    assert located and located.downloaded.content.startswith(b"%PDF-")


def test_browser_retries_a_public_pdf_blocked_for_http(tmp_path, monkeypatch):
    import app.services.pdf_browser as browser_module

    calls = []
    def browser_download(_document, _settings, urls):
        calls.extend(urls)
        return browser_module.DownloadedPdf(b"%PDF-1.7\n", "https://public.test/paper.pdf", "paper.pdf")
    monkeypatch.setattr(browser_module, "find_pdf_in_browser", browser_download)

    def handler(request: httpx.Request):
        if request.url.host == "public.test":
            return httpx.Response(403)
        return httpx.Response(404)

    document = TaiLieu(
        tieu_de="Bài báo", doi="10.1000/example", tac_gia=[], tu_khoa=[],
        du_lieu_nguon={}, ma_ngoai={}, url_pdf="https://public.test/paper.pdf",
    )
    located = find_and_download_pdf(
        document, settings(tmp_path, pdf_browser_enabled=True),
        client=httpx.Client(transport=httpx.MockTransport(handler)), resolver=public_resolver,
    )
    assert located and located.source == "chrome"
    assert "https://public.test/paper.pdf" in calls


def test_researchgate_access_restriction_is_not_reported_as_no_pdf(tmp_path, monkeypatch):
    import app.services.pdf_browser as browser_module

    class Page:
        def set_default_timeout(self, *_): pass
        def route(self, *_): pass
        def goto(self, *_args, **_kwargs): pass
        def close(self): pass
        def locator(self, _selector):
            return SimpleNamespace(inner_text=lambda: "Access restricted. Log in to verify your access.")

    monkeypatch.setattr(browser_module, "_get_context", lambda _: SimpleNamespace(new_page=Page))
    monkeypatch.setattr(browser_module, "_safe_url", lambda _: True)
    document = TaiLieu(tieu_de="Bài báo", tac_gia=[], tu_khoa=[], du_lieu_nguon={}, ma_ngoai={})
    with pytest.raises(browser_module.PdfBrowserAccessRequired):
        browser_module._try_browser(document, settings(tmp_path, pdf_browser_cdp_url="http://127.0.0.1:9222"), [])


def test_personal_chrome_uses_exact_researchgate_result(tmp_path, monkeypatch):
    import app.services.pdf_personal_chrome as personal

    monkeypatch.setattr(personal, "_connected", True)
    monkeypatch.setattr(personal, "_page", lambda _: 7)
    visits = []
    monkeypatch.setattr(personal, "_navigate", lambda _page, url: visits.append(url))
    monkeypatch.setattr(personal, "_command", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(personal, "_safe_url", lambda _: True)
    monkeypatch.setattr(personal, "_pdf_matches_document", lambda _document, _pdf: True)
    monkeypatch.setattr(personal, "_fetch_pdf", lambda _page, url, _limit: (
        personal.DownloadedPdf(b"%PDF-1.7\n", url, "paper.pdf") if url.endswith("/fulltext.pdf") else None
    ))
    monkeypatch.setattr(personal, "_page_info", lambda _: {
        "url": visits[-1],
        "text": "",
        "links": (
            [("https://www.researchgate.net/publication/123_Other_Title", "Other Title"),
             ("https://www.researchgate.net/publication/456_Target_Title", "Target Title")]
            if "/search?" in visits[-1] else
            [("https://www.researchgate.net/publication/456_Target_Title/fulltext.pdf", "Download full-text PDF")]
        ),
    })
    document = TaiLieu(tieu_de="Target Title", tac_gia=[], tu_khoa=[], du_lieu_nguon={}, ma_ngoai={})
    result = personal.find_pdf_in_personal_chrome(document, settings(tmp_path), [])
    assert result and result.content.startswith(b"%PDF-")
    assert any("/publication/456_Target_Title" in url for url in visits)
    assert all("/publication/123_Other_Title" not in url for url in visits)


def test_chrome_cli_error_payload_is_not_a_success(tmp_path, monkeypatch):
    import app.services.pdf_personal_chrome as personal

    cli = tmp_path / "chrome-devtools.cmd"
    cli.write_text("", encoding="utf-8")
    monkeypatch.setattr(personal, "_CLI", cli)
    monkeypatch.setattr(personal.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(
        returncode=0,
        stdout='[{"type":"text","text":"Could not connect to Chrome"}]',
    ))
    with pytest.raises(PdfConnectivityError):
        personal._command("list_pages", "--output-format=json")


def test_google_pdf_result_requires_matching_article_title():
    import pymupdf
    import app.services.pdf_personal_chrome as personal

    document = TaiLieu(tieu_de="Green Leadership and Employee Engagement", tac_gia=[], tu_khoa=[], du_lieu_nguon={}, ma_ngoai={})
    pdf = pymupdf.open()
    page = pdf.new_page()
    page.insert_text((50, 70), "Different Article About Leadership")
    wrong = personal.DownloadedPdf(pdf.tobytes(), "https://public.test/wrong.pdf", "wrong.pdf")
    assert not personal._pdf_matches_document(document, wrong)
    page.insert_text((50, 100), "Green Leadership and Employee Engagement")
    right = personal.DownloadedPdf(pdf.tobytes(), "https://public.test/right.pdf", "right.pdf")
    assert personal._pdf_matches_document(document, right)
    pdf.close()


def test_chrome_rejects_conference_program_even_if_it_lists_article():
    import pymupdf
    import app.services.pdf_personal_chrome as personal

    document = TaiLieu(tieu_de="Leadership in the AI Era", tac_gia=[], tu_khoa=[], du_lieu_nguon={}, ma_ngoai={})
    pdf = pymupdf.open()
    page = pdf.new_page()
    page.insert_text((50, 70), "Leadership in the AI Era")
    content = pdf.tobytes()
    pdf.close()
    program = personal.DownloadedPdf(content, "https://example.org/Conference%20Program.pdf", "tai-lieu.pdf")
    assert not personal._pdf_matches_document(document, program)


def test_chrome_accepts_scanned_pdf_with_exact_title_in_url():
    import pymupdf
    import app.services.pdf_personal_chrome as personal

    document = TaiLieu(tieu_de="Leadership and Knowledge Sharing", tac_gia=[], tu_khoa=[], du_lieu_nguon={}, ma_ngoai={})
    pdf = pymupdf.open()
    pdf.new_page()
    content = pdf.tobytes()
    pdf.close()
    scanned = personal.DownloadedPdf(content, "https://example.org/Leadership-and-Knowledge-Sharing.pdf", "tai-lieu.pdf")
    assert personal._pdf_matches_document(document, scanned)


def test_pdf_worker_retries_temporary_chrome_failure_before_marking_error(monkeypatch):
    job = SimpleNamespace(trang_thai="dang_xu_ly", so_lan_thu=1, loi_xu_ly=None, ngay_cap_nhat=None, ngay_hoan_thanh=None)

    class Database:
        def get(self, *_):
            return job

    class Transaction:
        def __enter__(self):
            return Database()

        def __exit__(self, *_):
            return False

    monkeypatch.setattr(worker_module, "SessionLocal", SimpleNamespace(begin=lambda: Transaction()))
    worker_module._mark_pdf_job_failed(uuid4(), uuid4(), PdfConnectivityError("Chrome tạm lỗi"), 3)
    assert job.trang_thai == "cho_xu_ly"
    assert job.ngay_hoan_thanh is None

    job.trang_thai = "dang_xu_ly"
    job.so_lan_thu = 3
    worker_module._mark_pdf_job_failed(uuid4(), uuid4(), PdfConnectivityError("Chrome tạm lỗi"), 3)
    assert job.trang_thai == "loi"
    assert job.ngay_hoan_thanh is not None


def test_pausing_pdf_search_stops_before_next_external_url(tmp_path):
    search_id = uuid4()
    calls = []

    def handler(request: httpx.Request):
        calls.append(str(request.url))
        cancel_pdf_search(search_id)
        return httpx.Response(403)

    document = TaiLieu(
        tieu_de="Bài báo", tac_gia=[], tu_khoa=[], ma_ngoai={},
        du_lieu_nguon={"links": ["https://repository.test/second.pdf"]},
        url_pdf="https://repository.test/first.pdf",
    )
    try:
        with bind_pdf_search(search_id), pytest.raises(PdfSearchCancelled):
            find_and_download_pdf(
                document, settings(tmp_path),
                client=httpx.Client(transport=httpx.MockTransport(handler)),
                resolver=public_resolver,
            )
    finally:
        resume_pdf_requests(search_id)
    assert calls == ["https://repository.test/first.pdf"]


def test_google_search_checks_publisher_page_before_pdf_only_results(monkeypatch):
    import app.services.pdf_personal_chrome as personal

    document = TaiLieu(tieu_de="Target Title", tac_gia=[], tu_khoa=[], du_lieu_nguon={}, ma_ngoai={})
    visits = []
    monkeypatch.setattr(personal, "_navigate", lambda _page, url: visits.append(url))
    monkeypatch.setattr(personal, "_safe_url", lambda _: True)
    monkeypatch.setattr(personal, "_page_info", lambda _: {
        "url": visits[-1], "text": "Results", "links": [
            ("https://publisher.test/article.html", "Target Title")
            if "google.com/search" in visits[-1] else
            ("https://publisher.test/article.pdf", "Download PDF")
        ],
    })
    monkeypatch.setattr(personal, "_fetch_pdf", lambda _page, url, _limit: (
        personal.DownloadedPdf(b"%PDF-1.7\n", url, "article.pdf") if url.endswith(".pdf") else None
    ))
    monkeypatch.setattr(personal, "_pdf_matches_document", lambda _document, _pdf: True)
    result = personal._search_google(7, document, 1000)
    assert result and result.final_url.endswith("article.pdf")
    assert "filetype" not in visits[0]


def test_google_search_uses_doi_when_title_results_have_no_pdf(monkeypatch):
    import app.services.pdf_personal_chrome as personal

    document = TaiLieu(tieu_de="Leadership and Employee Engagement", doi="10.1000/example", tac_gia=[], tu_khoa=[], du_lieu_nguon={}, ma_ngoai={})
    visits = []
    monkeypatch.setattr(personal, "_navigate", lambda _page, url: visits.append(url))
    monkeypatch.setattr(personal, "_safe_url", lambda _: True)
    monkeypatch.setattr(personal, "_page_info", lambda _: {
        "url": visits[-1], "text": "Results",
        "links": [("https://publisher.test/10.1000%2Fexample.pdf", "Download")]
        if "10.1000" in visits[-1] else [],
    })
    monkeypatch.setattr(personal, "_fetch_pdf", lambda _page, url, _limit: personal.DownloadedPdf(b"%PDF-1.7\n", url, "article.pdf"))
    monkeypatch.setattr(personal, "_pdf_matches_document", lambda _document, _pdf: True)
    result = personal._search_google(7, document, 1000)
    assert result and "example.pdf" in result.final_url
    assert any("10.1000" in url and "filetype" in url for url in visits)


def test_browser_control_port_must_be_local():
    import app.services.pdf_browser as browser_module

    with pytest.raises(PdfConnectivityError):
        browser_module._cdp_endpoint("http://example.org:9222")


def test_crossref_is_used_after_missing_metadata(tmp_path):
    def handler(request: httpx.Request):
        if request.url.host == "api.crossref.org":
            return httpx.Response(
                200,
                json={"message": {"link": [{"URL": "https://repository.test/open.pdf", "content-type": "application/pdf"}]}},
            )
        if request.url.host == "repository.test":
            return httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF-1.7\n")
        raise AssertionError(f"Không mong đợi URL {request.url}")

    document = TaiLieu(tieu_de="Bài báo", doi="10.1000/example", tac_gia=[], tu_khoa=[], du_lieu_nguon={}, ma_ngoai={})
    located = find_and_download_pdf(
        document,
        settings(tmp_path),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        resolver=public_resolver,
    )
    assert located and located.source == "crossref"
    assert located.downloaded.final_url == "https://repository.test/open.pdf"


def test_an_unreachable_source_falls_back_to_another_source(tmp_path):
    calls = []

    def handler(request: httpx.Request):
        calls.append(str(request.url))
        if request.url.host == "unreachable.test":
            raise httpx.ConnectError("Nguồn đầu tiên không truy cập được", request=request)
        if request.url.host == "api.crossref.org":
            return httpx.Response(
                200,
                json={"message": {"link": [{"URL": "https://repository.test/open.pdf", "content-type": "application/pdf"}]}},
            )
        if request.url.host == "repository.test":
            return httpx.Response(
                200,
                headers={"content-type": "application/pdf"},
                content=b"%PDF-1.7\n",
            )
        raise AssertionError(f"Không mong đợi URL {request.url}")

    document = TaiLieu(
        tieu_de="Bài báo",
        doi="10.1000/fallback",
        tac_gia=[],
        tu_khoa=[],
        du_lieu_nguon={},
        ma_ngoai={},
        url_pdf="https://unreachable.test/paper.pdf",
    )
    located = find_and_download_pdf(
        document,
        settings(tmp_path),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        resolver=public_resolver,
    )

    assert located and located.source == "crossref"
    assert located.downloaded.content.startswith(b"%PDF-")
    assert calls == [
        "https://unreachable.test/paper.pdf",
        "https://api.crossref.org/works/10.1000%2Ffallback",
        "https://repository.test/open.pdf",
    ]


def test_public_researchgate_direct_pdf_is_allowed_without_login(tmp_path):
    def handler(request: httpx.Request):
        assert request.url.host == "www.researchgate.net"
        assert "cookie" not in request.headers
        return httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF-1.7\n")

    document = TaiLieu(
        tieu_de="Bài báo",
        tac_gia=[],
        tu_khoa=[],
        ma_ngoai={},
        du_lieu_nguon={"public_pdf": "https://www.researchgate.net/profile/a/publication/b.pdf"},
    )
    located = find_and_download_pdf(
        document,
        settings(tmp_path),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        resolver=public_resolver,
    )
    assert located and located.source == "metadata_nguon"


def test_doi_html_without_pdf_metadata_is_not_saved(tmp_path):
    def handler(request: httpx.Request):
        if request.url.host in {
            "api.crossref.org",
            "api.unpaywall.org",
            "api.semanticscholar.org",
            "api.openalex.org",
        }:
            return httpx.Response(404)
        if request.url.host == "doi.org":
            return httpx.Response(302, headers={"location": "https://publisher.test/article"})
        if request.url.host == "publisher.test":
            return httpx.Response(200, headers={"content-type": "text/html"}, text="<html><form>Đăng nhập</form></html>")
        if request.url.host == "www.researchgate.net":
            return httpx.Response(403, headers={"content-type": "text/html"}, text="Bị chặn")
        raise AssertionError(f"Không mong đợi URL {request.url}")

    document = TaiLieu(tieu_de="Bài báo", doi="10.1000/paywall", tac_gia=[], tu_khoa=[], du_lieu_nguon={}, ma_ngoai={})
    assert find_and_download_pdf(
        document,
        settings(tmp_path),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        resolver=public_resolver,
    ) is None


def test_researchgate_title_search_downloads_matching_public_pdf(tmp_path):
    title = "Green Transformational Leadership and Green Voice Behavior"
    seen: list[str] = []

    def handler(request: httpx.Request):
        seen.append(str(request.url))
        if request.url.host != "www.researchgate.net":
            return httpx.Response(404)
        if request.url.path == "/search":
            assert request.url.params["q"] == title
            return httpx.Response(200, headers={"content-type": "text/html"}, text=(
                '<a href="/publication/111_Unrelated_Article">Unrelated Article</a>'
                '<a href="/publication/222_Green_Transformational_Leadership_and_Green_Voice_Behavior">'
                'Green Transformational Leadership and Green Voice Behavior</a>'
            ))
        if request.url.path.endswith("paper.pdf"):
            return httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF-1.7\n")
        if request.url.path.startswith("/publication/222_"):
            return httpx.Response(200, headers={"content-type": "text/html"}, text=(
                '<a href="/publication/333_Other_Article/links/def/wrong.pdf">Related PDF</a>'
                '<a href="/publication/222_Green_Transformational_Leadership_and_Green_Voice_Behavior/'
                'links/abc/paper.pdf">Download full-text PDF</a>'
            ))
        return httpx.Response(404)

    document = TaiLieu(tieu_de=title, tac_gia=[], tu_khoa=[], du_lieu_nguon={}, ma_ngoai={})
    located = find_and_download_pdf(
        document, settings(tmp_path), client=httpx.Client(transport=httpx.MockTransport(handler)),
        resolver=public_resolver,
    )
    assert located and located.source == "researchgate"
    assert located.downloaded.content.startswith(b"%PDF-")
    assert all("/publication/111_" not in url for url in seen)
    assert all("wrong.pdf" not in url for url in seen)


def test_file_sha256_reuses_existing_record(tmp_path):
    (tmp_path / "pdf").mkdir()
    (tmp_path / "pdf" / "cu.pdf").write_bytes(b"%PDF-1.7")
    existing = TepTin(
        id=uuid4(),
        ten_file_goc="cu.pdf",
        duong_dan_luu="pdf/cu.pdf",
        mime_type="application/pdf",
        kich_thuoc_byte=9,
        ma_bam_sha256="0" * 64,
    )
    db = MagicMock()
    db.scalar.return_value = existing
    record, created_path = save_file_record(
        db,
        "moi.pdf",
        b"%PDF-1.7",
        "application/pdf",
        uuid4(),
        "pdf",
        settings(tmp_path),
    )
    assert record is existing
    assert created_path is None
    db.add.assert_not_called()


def test_sha256_race_rolls_back_only_savepoint_and_reuses_winner(tmp_path):
    pdf_dir = tmp_path / "pdf"
    pdf_dir.mkdir()
    content = b"%PDF-1.7\n"
    (pdf_dir / "winner.pdf").write_bytes(content)
    winner = TepTin(
        id=uuid4(),
        ten_file_goc="winner.pdf",
        duong_dan_luu="pdf/winner.pdf",
        mime_type="application/pdf",
        kich_thuoc_byte=len(content),
        ma_bam_sha256="0" * 64,
    )

    class Savepoint:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    class RacingSession:
        def __init__(self):
            self.scalar_calls = 0
            self.rollback_called = False

        def scalar(self, _):
            self.scalar_calls += 1
            return None if self.scalar_calls == 1 else winner

        def begin_nested(self):
            return Savepoint()

        def add(self, _):
            pass

        def flush(self):
            raise IntegrityError("INSERT", {}, RuntimeError("unique sha256"))

        def rollback(self):
            self.rollback_called = True

    db = RacingSession()
    record, path = save_file_record(
        db,
        "loser.pdf",
        content,
        "application/pdf",
        uuid4(),
        "pdf",
        settings(tmp_path),
    )
    assert record is winner
    assert path is None
    assert not db.rollback_called
    assert [item.name for item in pdf_dir.iterdir()] == ["winner.pdf"]


def test_existing_physical_pdf_is_not_downloaded_again(tmp_path):
    pdf_dir = tmp_path / "pdf"
    pdf_dir.mkdir()
    (pdf_dir / "da-co.pdf").write_bytes(b"%PDF-1.7\n")
    record = TepTin(
        id=uuid4(),
        ten_file_goc="da-co.pdf",
        duong_dan_luu="pdf/da-co.pdf",
        mime_type="application/pdf",
        kich_thuoc_byte=9,
        ma_bam_sha256="0" * 64,
    )
    document = TaiLieu(tieu_de="Bài báo", tac_gia=[], tu_khoa=[], ma_ngoai={}, du_lieu_nguon={}, id_file_pdf=record.id)
    db = MagicMock()
    db.get.return_value = record
    assert _existing_pdf_is_valid(db, document, settings(tmp_path))


def test_pdf_filter_and_queue_claim_are_postgresql_queries():
    topic_id = uuid4()
    with_pdf = str(_document_query(topic_id, DocumentFilters(pdf_status="co_pdf")).compile(dialect=postgresql.dialect())).upper()
    without_pdf = str(_document_query(topic_id, DocumentFilters(pdf_status="khong_pdf")).compile(dialect=postgresql.dialect())).upper()
    assert "ID_FILE_PDF IS NOT NULL" in with_pdf
    assert "ID_FILE_PDF IS NULL" in without_pdf

    class CapturingSession:
        statement = None

        def scalar(self, statement):
            self.statement = statement
            return None

    db = CapturingSession()
    assert claim_next_pdf_job(db, 3) is None
    sql = str(db.statement.compile(dialect=postgresql.dialect())).upper()
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert CongViecTaiPdf.__tablename__.upper() in sql


def test_ai_lookup_filter_uses_the_new_ai_results_table():
    lookup_id = uuid4()
    sql = str(
        _document_query(uuid4(), DocumentFilters(ai_lookup_id=lookup_id, ai_label="phu_hop")).compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).upper()
    assert KetQuaTraCuuAi.__tablename__.upper() in sql
    assert str(lookup_id).upper() in sql


def test_pdf_claim_can_be_limited_to_one_search():
    class CapturingSession:
        statement = None

        def scalar(self, statement):
            self.statement = statement
            return None

    db = CapturingSession()
    search_id = uuid4()
    assert claim_next_pdf_job(db, 3, search_id) is None
    sql = str(
        db.statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    ).upper()
    assert str(search_id).upper() in sql


def test_pdf_batch_uses_configured_pool_runner(monkeypatch):
    class Begin:
        def __enter__(self):
            return object()

        def __exit__(self, *_):
            return False

    fake_settings = SimpleNamespace(pdf_max_retries=3)
    calls = []
    monkeypatch.setattr(worker_module, "get_settings", lambda: fake_settings)
    monkeypatch.setattr(worker_module, "SessionLocal", SimpleNamespace(begin=lambda: Begin()))
    monkeypatch.setattr(worker_module, "recover_stale_pdf_jobs", lambda _db, _max_retries: set())
    monkeypatch.setattr(
        worker_module,
        "_run_pdf_search_worker",
        lambda search_id, worker_settings: calls.append((search_id, worker_settings)) or 3,
    )

    search_id = uuid4()
    assert worker_module.run_pdf_batch(search_id) == 3
    assert calls == [(search_id, fake_settings)]


def test_two_batches_for_same_search_are_serialized(monkeypatch):
    class Begin:
        def __enter__(self):
            return object()

        def __exit__(self, *_):
            return False

    lock = threading.Lock()
    active = 0
    maximum = 0

    def run(*_):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.03)
        with lock:
            active -= 1
        return 1

    fake_settings = SimpleNamespace(pdf_max_retries=3)
    monkeypatch.setattr(worker_module, "get_settings", lambda: fake_settings)
    monkeypatch.setattr(worker_module, "SessionLocal", SimpleNamespace(begin=lambda: Begin()))
    monkeypatch.setattr(worker_module, "recover_stale_pdf_jobs", lambda *_: set())
    monkeypatch.setattr(worker_module, "_run_pdf_search_worker", run)
    search_id = uuid4()
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(worker_module.run_pdf_batch, [search_id, search_id]))
    assert results == [1, 1]
    assert maximum == 1


def test_only_failed_pdf_jobs_are_requeued():
    class Result:
        rowcount = 2

    class Session:
        statement = None
        flushed = False

        def execute(self, statement):
            self.statement = statement
            return Result()

        def flush(self):
            self.flushed = True

    db = Session()
    search_id = uuid4()
    assert retry_failed_pdf_jobs(db, search_id) == 2
    sql = str(db.statement.compile(dialect=postgresql.dialect())).upper()
    assert "UPDATE CONG_VIEC_TAI_PDF" in sql
    assert "TRANG_THAI" in sql and "ID_LAN_TIM_KIEM" in sql
    assert db.flushed


def test_pdf_timer_pauses_and_resumes_without_counting_pause_time():
    started = dt.datetime(2026, 9, 25, 1, 0, tzinfo=dt.UTC)
    search = SimpleNamespace(
        trang_thai_tai_pdf="dang_xu_ly",
        yeu_cau_dung_tai_pdf=False,
        thoi_gian_chay_ms=0,
        thoi_gian_bat_dau_dot_pdf=started,
        thoi_gian_hoan_thanh=None,
    )
    pause_pdf_search(search, started + dt.timedelta(seconds=2.5))
    assert search.trang_thai_tai_pdf == "tam_dung"
    assert search.thoi_gian_chay_ms == 2500
    assert search.thoi_gian_bat_dau_dot_pdf is None

    resumed_at = started + dt.timedelta(seconds=10)
    resume_pdf_search(search, resumed_at)
    assert search.trang_thai_tai_pdf == "cho_xu_ly"
    assert get_pdf_elapsed_ms(search, resumed_at + dt.timedelta(seconds=1)) == 3500


def test_pdf_controls_and_pause_state_are_present_in_ui_and_migration():
    template = open("app/templates/topic_detail.html", encoding="utf-8").read()
    panel_template = open("app/templates/partials/pdf_progress_panel.html", encoding="utf-8").read()
    ai_template = open("app/templates/ai_search.html", encoding="utf-8").read()
    document_template = open("app/templates/document_detail.html", encoding="utf-8").read()
    migration = open("sql/nang_cap_tai_pdf_va_tra_cuu_ai.sql", encoding="utf-8").read()
    readme = open("README.md", encoding="utf-8").read()
    assert "Dừng tải PDF ngay" in panel_template
    assert "Tiếp tục tải PDF" in panel_template
    assert "Tải lại PDF lỗi" in panel_template
    assert 'role="tablist"' in template
    assert template.count('role="tab"') == 2
    assert 'aria-selected="true"' in template
    assert "/tai-pdf/tai-lai-loi" in panel_template
    assert "Đã tải thành công trong lần chạy" in panel_template
    assert "pdf-failed-list" not in template
    assert "job.error" not in template
    assert "Tải bằng trình duyệt cá nhân" in document_template
    assert "Tìm trên ResearchGate" in document_template
    assert "Kiểm tra và lưu PDF" in document_template
    assert 'name="ai_search_id"' not in template
    assert 'name="ai_label"' not in template
    assert 'name="lan_tra_cuu_ai"' in ai_template
    assert 'name="nhan"' in ai_template
    assert 'name="topic_id"' in ai_template
    assert "tam_dung" in migration
    assert "python -m app.cli xu-ly-nen --lien-tuc" not in readme


def test_small_nonzero_pdf_progress_is_visible_and_monotonic():
    progress = PdfProgress(201, 200, 0, 0, 0, 0, 1)
    assert progress.finished == 0
    assert progress.processed == 1
    assert progress.percent == 1


def test_irjms_public_pdf_url_is_derived_from_doi():
    candidates = _publisher_candidates("10.47857/irjms.2026.v07i02.010069")
    assert [item.url for item in candidates] == [
        "https://www.irjms.com/wp-content/uploads/2026/04/Manuscript_IRJMS_010069_WS.pdf"
    ]


def test_sage_public_pdf_url_is_derived_from_doi_and_downloaded(tmp_path):
    def handler(request: httpx.Request):
        assert request.url.host == "journals.sagepub.com"
        assert request.url.params.get("download") == "true"
        return httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF-1.7\n")

    document = TaiLieu(
        tieu_de="Bài báo SAGE Open",
        doi="10.1177/21582440221094606",
        tac_gia=[],
        tu_khoa=[],
        du_lieu_nguon={},
        ma_ngoai={},
    )
    located = find_and_download_pdf(
        document,
        settings(tmp_path),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        resolver=public_resolver,
    )
    assert located and located.source == "publisher_sage"


def test_network_failure_is_not_reported_as_missing_pdf(tmp_path):
    def handler(request: httpx.Request):
        raise httpx.ConnectError("Mạng bị chặn", request=request)

    document = TaiLieu(
        tieu_de="Bài báo",
        tac_gia=[],
        tu_khoa=[],
        du_lieu_nguon={},
        ma_ngoai={},
        url_pdf="https://repository.test/paper.pdf",
    )
    with pytest.raises(PdfConnectivityError, match="Không thể kiểm tra đầy đủ"):
        find_and_download_pdf(
            document,
            settings(tmp_path),
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            resolver=public_resolver,
        )


def test_one_unavailable_provider_does_not_fail_entire_document(tmp_path, caplog):
    calls = []
    def handler(request: httpx.Request):
        calls.append(request.url.host)
        if request.url.host == "api.crossref.org":
            raise httpx.ConnectError("Crossref tạm thời không phản hồi", request=request)
        return httpx.Response(404)

    document = TaiLieu(
        tieu_de="Bài báo không có PDF công khai",
        doi="10.1000/partial-outage",
        tac_gia=[],
        tu_khoa=[],
        du_lieu_nguon={},
        ma_ngoai={},
    )
    assert find_and_download_pdf(
        document,
        settings(tmp_path),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        resolver=public_resolver,
    ) is None
    assert "api.crossref.org" in calls and "api.openalex.org" in calls
    assert "nguồn=crossref" in caplog.text and "kết_quả=loi_ket_noi" in caplog.text


def test_embedded_pdf_link_is_discovered_from_html():
    parser = _PdfLinkParser("https://repository.test/article")
    parser.feed('<iframe src="/files/paper.pdf" type="application/pdf"></iframe>')
    assert parser.urls == ["https://repository.test/files/paper.pdf"]


def test_pdf_link_without_dot_pdf_is_discovered_from_html():
    parser = _PdfLinkParser("https://publisher.test/article")
    parser.feed('<a class="pdf-download" href="/article/download/42">Tải toàn văn</a>')
    assert parser.urls == ["https://publisher.test/article/download/42"]


def test_openalex_landing_page_is_inspected_before_giving_up(tmp_path):
    calls = []

    def handler(request: httpx.Request):
        calls.append((request.url.host, request.url.path))
        if request.url.host == "api.crossref.org":
            return httpx.Response(404)
        if request.url.host == "api.openalex.org":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "title": "Bài báo mở",
                            "best_oa_location": {
                                "is_oa": True,
                                "pdf_url": None,
                                "landing_page_url": "https://repository.test/article/42",
                            },
                            "locations": [],
                            "open_access": {"oa_url": "https://repository.test/article/42"},
                            "content_urls": {},
                        }
                    ]
                },
            )
        if request.url.host == "repository.test" and request.url.path == "/article/42":
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                text='<a class="pdf-download" href="/article/download/42">PDF</a>',
            )
        if request.url.host == "repository.test" and request.url.path == "/article/download/42":
            return httpx.Response(
                200,
                headers={"content-type": "application/pdf"},
                content=b"%PDF-1.7\n",
            )
        raise AssertionError(f"Không mong đợi URL {request.url}")

    document = TaiLieu(
        tieu_de="Bài báo mở",
        doi="10.1000/open-landing",
        tac_gia=[],
        tu_khoa=[],
        du_lieu_nguon={},
        ma_ngoai={},
    )
    located = find_and_download_pdf(
        document,
        settings(tmp_path),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        resolver=public_resolver,
    )

    assert located and located.source == "openalex_landing"
    assert located.downloaded.final_url == "https://repository.test/article/download/42"
    assert all(host != "api.semanticscholar.org" for host, _ in calls)


def test_openalex_title_search_handles_document_without_doi(tmp_path):
    def handler(request: httpx.Request):
        if request.url.host == "api.openalex.org":
            assert request.url.params.get("search") == "Bài báo không DOI"
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "title": "Bài báo không DOI",
                            "best_oa_location": {
                                "is_oa": True,
                                "pdf_url": "https://repository.test/no-doi.pdf",
                                "landing_page_url": None,
                            },
                            "locations": [],
                            "open_access": {},
                            "content_urls": {},
                        }
                    ]
                },
            )
        if request.url.host == "repository.test":
            return httpx.Response(
                200,
                headers={"content-type": "application/pdf"},
                content=b"%PDF-1.7\n",
            )
        raise AssertionError(f"Không mong đợi URL {request.url}")

    document = TaiLieu(
        tieu_de="Bài báo không DOI",
        tac_gia=[],
        tu_khoa=[],
        du_lieu_nguon={},
        ma_ngoai={},
    )
    located = find_and_download_pdf(
        document,
        settings(tmp_path),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        resolver=public_resolver,
    )

    assert located and located.source == "openalex"
    assert located.downloaded.final_url == "https://repository.test/no-doi.pdf"


def test_retry_clears_completion_marker_for_correct_progress(tmp_path):
    link = SimpleNamespace(id=uuid4())
    document = TaiLieu(tieu_de="Bài báo", tac_gia=[], tu_khoa=[], ma_ngoai={}, du_lieu_nguon={})
    completed_at = dt.datetime(2026, 9, 25, tzinfo=dt.UTC)
    job = SimpleNamespace(
        trang_thai="khong_tim_thay",
        so_lan_thu=1,
        nguon_pdf=None,
        url_pdf_cuoi=None,
        loi_xu_ly="Không tìm thấy",
        ngay_hoan_thanh=completed_at,
        ngay_cap_nhat=completed_at,
    )

    class Session:
        def execute(self, _):
            return [(link, document)]

        def scalar(self, _):
            return job

        def flush(self):
            pass

    search = SimpleNamespace(id=uuid4())
    assert prepare_pdf_jobs_for_topic(Session(), uuid4(), search, settings(tmp_path)) == 1
    assert job.trang_thai == "cho_xu_ly"
    assert job.ngay_hoan_thanh is None


@pytest.mark.parametrize(
    ("milliseconds", "expected"),
    [
        (0, "00 giờ 00 phút 00 giây"),
        (5_000, "00 giờ 00 phút 05 giây"),
        (65_000, "00 giờ 01 phút 05 giây"),
        (1_186_000, "00 giờ 19 phút 46 giây"),
        (3_661_000, "01 giờ 01 phút 01 giây"),
        (97_385_000, "27 giờ 03 phút 05 giây"),
    ],
)
def test_elapsed_time_uses_hours_minutes_seconds(milliseconds, expected):
    assert format_elapsed_ms(milliseconds) == expected


def test_three_pdf_candidates_are_tried_in_order_until_success(tmp_path):
    calls = []

    def handler(request: httpx.Request):
        calls.append(str(request.url))
        if request.url.path.endswith("first.pdf"):
            return httpx.Response(404)
        if request.url.path.endswith("second.pdf"):
            return httpx.Response(200, headers={"content-type": "text/html"}, text="<html>Đăng nhập</html>")
        return httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF-1.7\n")

    document = TaiLieu(
        tieu_de="Bài báo",
        tac_gia=[],
        tu_khoa=[],
        ma_ngoai={},
        url_pdf="https://repository.test/first.pdf",
        du_lieu_nguon={
            "links": [
                "https://repository.test/second.pdf",
                "https://repository.test/third.pdf",
            ]
        },
    )
    located = find_and_download_pdf(
        document,
        settings(tmp_path),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        resolver=public_resolver,
        sleep=lambda _: None,
    )
    assert located and located.downloaded.final_url.endswith("third.pdf")
    assert calls == [
        "https://repository.test/first.pdf",
        "https://repository.test/second.pdf",
        "https://repository.test/third.pdf",
    ]


def test_duplicate_pdf_urls_are_normalized_and_tried_once():
    candidates = _deduplicate(
        [
            PdfCandidate("https://Repository.Test:443/paper.pdf#download", "first"),
            PdfCandidate("https://repository.test/paper.pdf", "second"),
        ]
    )
    assert [(item.url, item.source) for item in candidates] == [
        ("https://repository.test/paper.pdf", "first")
    ]


def test_temporary_http_error_retries_with_retry_after():
    calls = 0
    delays = []

    def handler(_: httpx.Request):
        nonlocal calls
        calls += 1
        if calls < 3:
            return httpx.Response(429, headers={"retry-after": "0"})
        return httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF-1.7\n")

    result = download_pdf(
        "https://repository.test/paper.pdf",
        max_bytes=1_000_000,
        max_retries=3,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        resolver=public_resolver,
        sleep=delays.append,
    )
    assert result.content.startswith(b"%PDF-")
    assert calls == 3
    assert delays == [0.0, 0.0]


def test_long_retry_after_skips_host_and_uses_next_pdf(tmp_path):
    calls: list[str] = []
    delays: list[float] = []

    def handler(request: httpx.Request):
        calls.append(request.url.host)
        if request.url.host == "limited.test":
            return httpx.Response(429, headers={"retry-after": "22232"})
        return httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF-1.7\n")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    limiter = HostRequestLimiter(2)
    document = TaiLieu(
        tieu_de="Bài báo", tac_gia=[], tu_khoa=[], ma_ngoai={},
        url_pdf="https://limited.test/paper.pdf",
        du_lieu_nguon={"public_pdf": "https://available.test/paper.pdf"},
    )
    located = find_and_download_pdf(
        document, settings(tmp_path, pdf_max_retries=3), client=client,
        resolver=public_resolver, limiter=limiter, sleep=delays.append,
    )
    assert located and located.source == "metadata_nguon"
    assert calls == ["limited.test", "available.test"]
    assert delays == []
    with pytest.raises(PdfSourceUnavailableError):
        get_with_retries(client, "https://limited.test/again", max_retries=3, limiter=limiter)
    assert calls == ["limited.test", "available.test"]


def test_discovery_api_long_retry_after_is_not_slept_or_retried():
    calls = 0
    delays: list[float] = []

    def handler(_: httpx.Request):
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"retry-after": "22232"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    limiter = HostRequestLimiter(2)
    with pytest.raises(PdfSourceUnavailableError):
        get_with_retries(
            client, "https://api.openalex.org/works", max_retries=3,
            limiter=limiter, sleep=delays.append,
        )
    with pytest.raises(PdfSourceUnavailableError):
        get_with_retries(client, "https://api.openalex.org/works/next", max_retries=3, limiter=limiter)
    assert calls == 1
    assert delays == []


def test_dns_error_is_retried_before_downloading_pdf():
    resolutions = 0
    delays = []

    def resolver(_: str):
        nonlocal resolutions
        resolutions += 1
        if resolutions < 3:
            raise PdfConnectivityError("DNS tạm thời lỗi")
        return public_resolver("")

    result = download_pdf(
        "https://repository.test/paper.pdf",
        max_bytes=1_000_000,
        max_retries=3,
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    headers={"content-type": "application/pdf"},
                    content=b"%PDF-1.7\n",
                )
            )
        ),
        resolver=resolver,
        sleep=delays.append,
    )
    assert result.content.startswith(b"%PDF-")
    assert resolutions == 3
    assert delays == [0.5, 1.0]


def test_host_request_limiter_never_exceeds_configured_limit():
    limiter = HostRequestLimiter(2)
    lock = threading.Lock()
    active = 0
    maximum = 0

    def request():
        nonlocal active, maximum
        with limiter.request("https://repository.test/paper.pdf"):
            with lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.02)
            with lock:
                active -= 1

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(request) for _ in range(8)]
        for future in futures:
            future.result()
    assert maximum == 2


def test_pdf_pool_processes_concurrently_without_exceeding_worker_limit(monkeypatch):
    job_ids = [uuid4() for _ in range(9)]
    lock = threading.Lock()
    active = 0
    maximum = 0

    def claim(*_):
        with lock:
            return job_ids.pop(0) if job_ids else None

    def process(*_):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.02)
        with lock:
            active -= 1

    class Db:
        def get(self, *_):
            return SimpleNamespace(yeu_cau_dung_tai_pdf=False)

    class Begin:
        def __enter__(self):
            return Db()

        def __exit__(self, *_):
            return False

    monkeypatch.setattr(worker_module, "_claim_pdf_job", claim)
    monkeypatch.setattr(worker_module, "_process_pdf_job_in_thread", process)
    monkeypatch.setattr(worker_module, "SessionLocal", SimpleNamespace(begin=lambda: Begin()))
    monkeypatch.setattr(
        worker_module,
        "update_pdf_search_progress",
        lambda *_: SimpleNamespace(waiting=len(job_ids)),
    )
    fake_settings = SimpleNamespace(pdf_download_workers=3, pdf_download_per_host=2)
    assert worker_module._run_pdf_search_worker(uuid4(), fake_settings) == 9
    assert 1 < maximum <= 3


def test_continue_download_does_not_reset_failed_job(tmp_path):
    link = SimpleNamespace(id=uuid4())
    document = TaiLieu(tieu_de="Bài báo", tac_gia=[], tu_khoa=[], ma_ngoai={}, du_lieu_nguon={})
    failed_at = dt.datetime(2026, 9, 25, tzinfo=dt.UTC)
    job = SimpleNamespace(
        trang_thai="loi",
        so_lan_thu=3,
        nguon_pdf=None,
        url_pdf_cuoi=None,
        loi_xu_ly="Mất kết nối",
        ngay_hoan_thanh=failed_at,
        ngay_cap_nhat=failed_at,
    )

    class Session:
        def execute(self, _):
            return [(link, document)]

        def scalar(self, _):
            return job

        def flush(self):
            pass

    count = prepare_pdf_jobs_for_topic(
        Session(),
        uuid4(),
        SimpleNamespace(id=uuid4()),
        settings(tmp_path),
    )
    assert count == 0
    assert job.trang_thai == "loi"
    assert job.so_lan_thu == 3
    assert job.loi_xu_ly == "Mất kết nối"


def test_missing_pdf_job_is_inserted_with_conflict_protection(tmp_path):
    link = SimpleNamespace(id=uuid4())
    document = TaiLieu(tieu_de="Bài báo", tac_gia=[], tu_khoa=[], ma_ngoai={}, du_lieu_nguon={})

    class Session:
        def __init__(self):
            self.calls = 0
            self.insert_statement = None

        def execute(self, statement):
            self.calls += 1
            if self.calls == 1:
                return [(link, document)]
            self.insert_statement = statement
            return SimpleNamespace()

        def scalar(self, _):
            return None

        def flush(self):
            pass

    db = Session()
    assert prepare_pdf_jobs_for_topic(
        db,
        uuid4(),
        SimpleNamespace(id=uuid4()),
        settings(tmp_path),
    ) == 1
    sql = str(db.insert_statement.compile(dialect=postgresql.dialect())).upper()
    assert "ON CONFLICT" in sql and "DO NOTHING" in sql


@pytest.mark.parametrize("handler", [continue_pdf_download, retry_failed_pdf_download])
def test_pdf_mutation_routes_still_require_csrf(handler):
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [],
            "query_string": b"",
            "session": {"csrf_token": "token-dung"},
        }
    )
    with pytest.raises(HTTPException) as error:
        handler(
            topic_id=uuid4(),
            request=request,
            background_tasks=BackgroundTasks(),
            search_id=None,
            csrf_token="token-sai",
            db=MagicMock(),
            _=MagicMock(),
        )
    assert error.value.status_code == 403


def test_crossref_timeout_still_uses_openalex_pdf(tmp_path):
    calls = []

    def handler(request: httpx.Request):
        calls.append(request.url.host)
        if request.url.host == "api.crossref.org":
            raise httpx.ReadTimeout("Quá thời gian", request=request)
        if request.url.host == "api.openalex.org":
            return httpx.Response(200, json={"results": [{
                "title": "Bài báo", "content_urls": {"pdf": "https://repository.test/open.pdf"},
                "locations": [], "open_access": {},
            }]})
        if request.url.host == "repository.test":
            return httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF-1.7\n")
        return httpx.Response(404)

    document = TaiLieu(tieu_de="Bài báo", doi="10.1000/example", tac_gia=[], tu_khoa=[], du_lieu_nguon={}, ma_ngoai={})
    located = find_and_download_pdf(document, settings(tmp_path), client=httpx.Client(transport=httpx.MockTransport(handler)), resolver=public_resolver, sleep=lambda _: None)
    assert located and located.source == "openalex_content"
    assert calls.index("api.crossref.org") < calls.index("api.openalex.org")


def test_blocked_doi_still_uses_unpaywall_pdf(tmp_path):
    calls = []
    def handler(request: httpx.Request):
        calls.append(request.url.host)
        if request.url.host == "api.unpaywall.org":
            return httpx.Response(200, json={"best_oa_location": {"url_for_pdf": "https://repository.test/open.pdf"}, "oa_locations": []})
        if request.url.host == "repository.test":
            return httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF-1.7\n")
        if request.url.host == "doi.org":
            return httpx.Response(403)
        return httpx.Response(404)

    document = TaiLieu(tieu_de="Bài báo", doi="10.1000/example", tac_gia=[], tu_khoa=[], du_lieu_nguon={}, ma_ngoai={}, url_trang_goc="https://doi.org/10.1000/example")
    located = find_and_download_pdf(document, settings(tmp_path, unpaywall_email="contact@example.org"), client=httpx.Client(transport=httpx.MockTransport(handler)), resolver=public_resolver)
    assert located and located.source == "unpaywall"
    assert calls.index("doi.org") < calls.index("api.unpaywall.org")


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_temporary_http_failure_moves_to_next_url(tmp_path, status):
    calls = []
    delays = []

    def handler(request: httpx.Request):
        calls.append(request.url.path)
        if request.url.path == "/first.pdf":
            return httpx.Response(status, headers={"retry-after": "0"})
        return httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF-1.7\n")

    document = TaiLieu(tieu_de="Bài báo", tac_gia=[], tu_khoa=[], ma_ngoai={}, du_lieu_nguon={"links": ["https://repository.test/second.pdf"]}, url_pdf="https://repository.test/first.pdf")
    located = find_and_download_pdf(document, settings(tmp_path, pdf_max_retries=2), client=httpx.Client(transport=httpx.MockTransport(handler)), resolver=public_resolver, sleep=delays.append)
    assert located and located.downloaded.final_url.endswith("second.pdf")
    assert calls == ["/first.pdf", "/first.pdf", "/second.pdf"]
    assert delays == [0.0]


def test_oversized_pdf_is_rejected_before_next_source(tmp_path):
    document = TaiLieu(tieu_de="Bài báo", tac_gia=[], tu_khoa=[], ma_ngoai={}, du_lieu_nguon={"links": ["https://repository.test/second.pdf"]}, url_pdf="https://repository.test/first.pdf")

    def handler(request: httpx.Request):
        if request.url.path == "/first.pdf":
            return httpx.Response(200, headers={"content-type": "application/pdf", "content-length": "2000000"}, content=b"%PDF-1.7\n")
        return httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF-1.7\n")

    located = find_and_download_pdf(document, settings(tmp_path), client=httpx.Client(transport=httpx.MockTransport(handler)), resolver=public_resolver)
    assert located and located.downloaded.final_url.endswith("second.pdf")


def test_redirect_to_private_host_is_rejected_before_next_source(tmp_path):
    calls = []
    document = TaiLieu(tieu_de="Bài báo", tac_gia=[], tu_khoa=[], ma_ngoai={}, du_lieu_nguon={"links": ["https://repository.test/second.pdf"]}, url_pdf="https://repository.test/first.pdf")

    def handler(request: httpx.Request):
        calls.append(request.url.host)
        if request.url.path == "/first.pdf":
            return httpx.Response(302, headers={"location": "https://internal.test/private.pdf"})
        return httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF-1.7\n")

    resolver = lambda host: ["127.0.0.1"] if host == "internal.test" else public_resolver(host)
    located = find_and_download_pdf(document, settings(tmp_path), client=httpx.Client(transport=httpx.MockTransport(handler)), resolver=resolver)
    assert located and located.downloaded.final_url.endswith("second.pdf")
    assert "internal.test" not in calls


def test_all_public_sources_returning_404_are_not_found(tmp_path):
    document = TaiLieu(tieu_de="Bài báo", doi="10.1000/missing", tac_gia=[], tu_khoa=[], ma_ngoai={}, du_lieu_nguon={}, url_pdf="https://repository.test/missing.pdf")
    located = find_and_download_pdf(document, settings(tmp_path), client=httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(404))), resolver=public_resolver)
    assert located is None


def test_too_many_redirects_move_to_next_source(tmp_path):
    document = TaiLieu(tieu_de="Bài báo", tac_gia=[], tu_khoa=[], ma_ngoai={}, du_lieu_nguon={"links": ["https://repository.test/second.pdf"]}, url_pdf="https://repository.test/first.pdf")

    def handler(request: httpx.Request):
        if request.url.path == "/first.pdf":
            return httpx.Response(302, headers={"location": "/redirected.pdf"})
        return httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF-1.7\n")

    located = find_and_download_pdf(document, settings(tmp_path, pdf_max_redirects=0), client=httpx.Client(transport=httpx.MockTransport(handler)), resolver=public_resolver)
    assert located and located.downloaded.final_url.endswith("second.pdf")


def test_pdf_log_omits_sensitive_query(caplog, tmp_path):
    document = TaiLieu(tieu_de="Bài báo", tac_gia=[], tu_khoa=[], ma_ngoai={}, du_lieu_nguon={}, url_pdf="https://repository.test/missing.pdf?token=private-secret")
    assert find_and_download_pdf(document, settings(tmp_path), client=httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(404))), resolver=public_resolver) is None
    assert "repository.test" in caplog.text
    assert "metadata_pdf_url" in caplog.text
    assert "private-secret" not in caplog.text


def test_unsafe_metadata_does_not_hide_total_network_failure(tmp_path):
    document = TaiLieu(tieu_de="Bài báo", tac_gia=[], tu_khoa=[], ma_ngoai={}, du_lieu_nguon={}, url_pdf="https://127.0.0.1/private.pdf")

    def handler(request: httpx.Request):
        raise httpx.ConnectError("Mạng bị chặn", request=request)

    with pytest.raises(PdfConnectivityError):
        find_and_download_pdf(document, settings(tmp_path), client=httpx.Client(transport=httpx.MockTransport(handler)), resolver=lambda host: ["127.0.0.1"] if host == "127.0.0.1" else public_resolver(host))


def test_same_document_jobs_do_not_download_twice_in_one_process(monkeypatch, tmp_path):
    document_id = uuid4()
    completed = False
    downloads = 0

    class Db:
        def scalar(self, _):
            return document_id

    class Begin:
        def __enter__(self):
            return Db()

        def __exit__(self, *_):
            return False

    def load(*_):
        return None if completed else SimpleNamespace(document=SimpleNamespace(id=document_id))

    def find(*_, **__):
        nonlocal downloads
        downloads += 1
        time.sleep(0.02)
        return object()

    def save(*_):
        nonlocal completed
        completed = True

    monkeypatch.setattr(pdf_worker_module, "SessionLocal", SimpleNamespace(begin=lambda: Begin()))
    monkeypatch.setattr(pdf_worker_module, "_load_pdf_snapshot", load)
    monkeypatch.setattr(pdf_worker_module, "find_and_download_pdf", find)
    monkeypatch.setattr(pdf_worker_module, "_save_downloaded_pdf", save)
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(lambda _: pdf_worker_module.process_pdf_job(uuid4(), settings(tmp_path)), range(2)))
    assert downloads == 1


def test_semantic_scholar_pdf_is_used_after_other_catalogs(tmp_path):
    def handler(request: httpx.Request):
        if request.url.host == "api.semanticscholar.org":
            return httpx.Response(200, json={"title": "Bài báo", "openAccessPdf": {"url": "https://repository.test/semantic.pdf"}})
        if request.url.host == "repository.test":
            return httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF-1.7\n")
        return httpx.Response(404)

    document = TaiLieu(tieu_de="Bài báo", doi="10.1000/semantic", tac_gia=[], tu_khoa=[], ma_ngoai={}, du_lieu_nguon={})
    located = find_and_download_pdf(document, settings(tmp_path), client=httpx.Client(transport=httpx.MockTransport(handler)), resolver=public_resolver)
    assert located and located.source == "semantic_scholar"


def test_doi_landing_http_link_header_leads_to_pdf(tmp_path):
    def handler(request: httpx.Request):
        if request.url.host == "doi.org":
            return httpx.Response(302, headers={"location": "https://publisher.test/article"})
        if request.url.host == "publisher.test":
            return httpx.Response(200, headers={"content-type": "text/html", "link": '<https://repository.test/file.pdf>; rel="alternate"; type="application/pdf"'}, text="<html></html>")
        if request.url.host == "repository.test":
            return httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF-1.7\n")
        return httpx.Response(404)

    document = TaiLieu(tieu_de="Bài báo", doi="10.1000/doi-link", tac_gia=[], tu_khoa=[], ma_ngoai={}, du_lieu_nguon={})
    located = find_and_download_pdf(document, settings(tmp_path), client=httpx.Client(transport=httpx.MockTransport(handler)), resolver=public_resolver)
    assert located and located.source == "doi_metadata"
