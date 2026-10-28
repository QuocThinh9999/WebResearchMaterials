"""Tải PDF bằng Chrome khi máy chủ từ chối HTTP thông thường."""

from __future__ import annotations

import atexit
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit

from app.services.pdf_downloader import DownloadedPdf, PdfConnectivityError, validate_public_https_url
from app.services.pdf_cancel import bind_pdf_search, check_pdf_search_not_cancelled, current_pdf_search_id
from app.services.providers import normalize_title


logger = logging.getLogger(__name__)


class PdfBrowserAccessRequired(PdfConnectivityError):
    """Nguồn PDF yêu cầu xác minh phiên trình duyệt trước khi tìm tiếp."""


_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tai-pdf-chrome")
_runtime = None
_context = None
_browser = None
_configuration = None


def _safe_url(url: str) -> bool:
    try:
        validate_public_https_url(url)
        return True
    except Exception:
        return False


def _cdp_endpoint(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"} or not parsed.port:
        raise PdfConnectivityError("Chrome phải mở cổng điều khiển trên máy này.")
    return value


def _get_context(settings):
    global _runtime, _context, _browser, _configuration
    key = (str(getattr(settings, "pdf_browser_cdp_url", None)),
           str(getattr(settings, "pdf_browser_profile_dir", ".pdf-browser-profile")),
           bool(getattr(settings, "pdf_browser_headless", False)))
    if _context is not None and key == _configuration:
        return _context
    _close_browser()
    from playwright.sync_api import sync_playwright

    _runtime = sync_playwright().start()
    try:
        cdp_url = getattr(settings, "pdf_browser_cdp_url", None)
        if cdp_url:
            _browser = _runtime.chromium.connect_over_cdp(
                _cdp_endpoint(cdp_url), timeout=5000, no_defaults=True,
            )
            _context = _browser.contexts[0]
        else:
            profile = Path(getattr(settings, "pdf_browser_profile_dir", ".pdf-browser-profile")).resolve()
            profile.mkdir(parents=True, exist_ok=True)
            _context = _runtime.chromium.launch_persistent_context(
                str(profile), channel="chrome", headless=bool(getattr(settings, "pdf_browser_headless", False)),
                accept_downloads=True, timeout=15000,
            )
        _configuration = key
        return _context
    except Exception as exc:
        _close_browser()
        raise PdfConnectivityError("Không thể khởi động hoặc kết nối Chrome để tải PDF.") from exc


def _close_browser() -> None:
    global _runtime, _context, _browser, _configuration
    try:
        if _browser is None and _context is not None:
            _context.close()
        # Khi kết nối Chrome cá nhân, chỉ ngắt Playwright; không đóng Chrome của người dùng.
    except Exception:
        pass
    try:
        if _runtime is not None:
            _runtime.stop()
    except Exception:
        pass
    _runtime = _context = _browser = _configuration = None


def _shutdown() -> None:
    try:
        _executor.submit(_close_browser).result(timeout=5)
    except Exception:
        pass
    _executor.shutdown(wait=False, cancel_futures=True)


atexit.register(_shutdown)


def _links(page) -> list[tuple[str, str]]:
    return page.locator("a[href],link[href],meta[name='citation_pdf_url']").evaluate_all(
        "elements => elements.map(e => [e.href || e.content || '', (e.innerText || e.title || '').trim()])"
    )


def _downloaded_file(download, url: str, max_bytes: int) -> DownloadedPdf | None:
    path = download.path()
    if not path or Path(path).stat().st_size > max_bytes:
        return None
    content = Path(path).read_bytes()
    if not content.startswith(b"%PDF-"):
        return None
    filename = download.suggested_filename or "tai-lieu.pdf"
    if not filename.casefold().endswith(".pdf"):
        filename = "tai-lieu.pdf"
    return DownloadedPdf(content, url, filename)


def _download_from_url(context, page, url: str, max_bytes: int) -> DownloadedPdf | None:
    if not _safe_url(url):
        return None
    downloads = []
    def on_download(download):
        downloads.append(download)
    page.on("download", on_download)
    try:
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=12000)
        except Exception:
            response = None
        if downloads:
            pdf = _downloaded_file(downloads[-1], url, max_bytes)
            if pdf:
                return pdf
        if response and response.ok:
            content = response.body()
            if len(content) <= max_bytes and content.startswith(b"%PDF-"):
                return DownloadedPdf(content, response.url, "tai-lieu.pdf")
        # APIRequestContext dùng cùng phiên đăng nhập của Chrome, không sao chép cookie ra ngoài.
        response = context.request.get(url, timeout=12000, max_redirects=0)
        if response.ok:
            content = response.body()
            if len(content) <= max_bytes and content.startswith(b"%PDF-"):
                return DownloadedPdf(content, url, "tai-lieu.pdf")
    except Exception:
        return None
    finally:
        page.remove_listener("download", on_download)
    return None


def _matching_publication_links(page, title: str) -> list[str]:
    expected = normalize_title(title)
    found = []
    for href, text in _links(page):
        full_url = urljoin(page.url, href)
        parsed = urlsplit(full_url)
        if parsed.hostname != "www.researchgate.net" or not re.match(r"^/publication/\d+_", parsed.path):
            continue
        slug = parsed.path.split("_", 1)[-1].replace("_", "-")
        if expected in {normalize_title(text), normalize_title(slug)} and _safe_url(full_url):
            found.append(full_url)
    return list(dict.fromkeys(found))[:3]


def _try_browser(document, settings, urls: list[str]) -> DownloadedPdf | None:
    check_pdf_search_not_cancelled()
    context = _get_context(settings)
    page = context.new_page()
    page.set_default_timeout(12000)
    page.route("**/*", lambda route: route.continue_() if _safe_url(route.request.url) else route.abort())
    max_bytes = settings.max_upload_mb * 1024 * 1024
    try:
        for url in list(dict.fromkeys(urls))[:6]:
            check_pdf_search_not_cancelled()
            if not _safe_url(url):
                continue
            pdf = _download_from_url(context, page, url, max_bytes)
            if pdf:
                return pdf
            # Trang bài có thể đưa liên kết PDF sau khi JavaScript chạy.
            if _safe_url(page.url) and page.locator("html").count():
                base_url = page.url
                for href, label in _links(page):
                    check_pdf_search_not_cancelled()
                    link = urljoin(base_url, href)
                    if any(marker in f"{link} {label}".lower() for marker in (".pdf", "/pdf/", "download pdf", "full-text pdf")):
                        pdf = _download_from_url(context, page, link, max_bytes)
                        if pdf:
                            return pdf

        # Chrome mở bằng hồ sơ riêng không dùng được phiên ResearchGate hằng ngày.
        if not getattr(settings, "pdf_browser_cdp_url", None):
            return None
        title = document.tieu_de
        if not normalize_title(title):
            return None
        search_url = f"https://www.researchgate.net/search?q={quote(title)}"
        if not _safe_url(search_url):
            return None
        try:
            page.goto(search_url, wait_until="domcontentloaded", timeout=15000)
            if "Access restricted" in page.locator("body").inner_text()[:800]:
                raise PdfBrowserAccessRequired("ResearchGate yêu cầu đăng nhập hoặc xác minh trong Chrome.")
            try:
                page.locator('a[href*="/publication/"]').first.wait_for(state="attached", timeout=5000)
            except Exception:
                pass
            publications = _matching_publication_links(page, title)
        except PdfBrowserAccessRequired:
            raise
        except Exception:
            return None
        for publication in publications:
            check_pdf_search_not_cancelled()
            try:
                page.goto(publication, wait_until="domcontentloaded", timeout=15000)
                publication_id = re.search(r"/publication/(\d+)_", publication).group(1)
                base_url = page.url
                for href, label in _links(page):
                    link = urljoin(base_url, href)
                    if f"/publication/{publication_id}_" not in urlsplit(link).path:
                        continue
                    if any(marker in f"{link} {label}".lower() for marker in (".pdf", "/pdf/", "download full-text pdf")):
                        pdf = _download_from_url(context, page, link, max_bytes)
                        if pdf:
                            return pdf
                if page.url != base_url:
                    page.goto(publication, wait_until="domcontentloaded", timeout=12000)
                button = page.get_by_role("button", name=re.compile(r"download.*pdf", re.IGNORECASE))
                if button.count():
                    try:
                        with page.expect_download(timeout=8000) as event:
                            button.first.click()
                        pdf = _downloaded_file(event.value, publication, max_bytes)
                        if pdf:
                            return pdf
                    except Exception:
                        pass
            except Exception:
                continue
        return None
    finally:
        page.close()


def find_pdf_in_browser(document, settings, urls: list[str]) -> DownloadedPdf | None:
    """Chạy Chrome trên một luồng cố định để dùng lại phiên đăng nhập giữa các bài."""
    from app.services.pdf_personal_chrome import find_pdf_in_personal_chrome, personal_chrome_connected

    if personal_chrome_connected():
        return find_pdf_in_personal_chrome(document, settings, urls)
    search_id = current_pdf_search_id()

    def run_separate_browser():
        if search_id is None:
            return _try_browser(document, settings, urls)
        with bind_pdf_search(search_id):
            return _try_browser(document, settings, urls)

    try:
        return _executor.submit(run_separate_browser).result()
    except PdfConnectivityError:
        raise
    except Exception as exc:
        logger.warning("Không thể tải PDF bằng Chrome: %s", exc.__class__.__name__)
        raise PdfConnectivityError("Không thể dùng Chrome để tải PDF.") from exc
