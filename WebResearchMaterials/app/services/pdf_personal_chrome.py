"""Tải PDF qua Chrome đang dùng sau khi người dùng cho phép kết nối DevTools."""

from __future__ import annotations

import base64
import json
import re
import subprocess
import tempfile
import threading
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urljoin, urlsplit

from app.services.pdf_downloader import DownloadedPdf, PdfConnectivityError, validate_public_https_url
from app.services.pdf_cancel import check_pdf_search_not_cancelled
from app.services.providers import normalize_title


_ROOT = Path(__file__).resolve().parents[2]
_CLI = _ROOT / ".browser-tools" / "node_modules" / ".bin" / "chrome-devtools.cmd"
_connected = False
_browser_lock = threading.Lock()
_browser_slots = threading.BoundedSemaphore(2)


def _command(*args: str, timeout: int = 40) -> dict:
    check_pdf_search_not_cancelled()
    if not _CLI.is_file():
        raise PdfConnectivityError("Thiếu công cụ kết nối Chrome. Chạy npm install --prefix .browser-tools.")
    try:
        result = subprocess.run(
            [str(_CLI), *args], cwd=_ROOT, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PdfConnectivityError("Không thể liên lạc với Chrome thường.") from exc
    check_pdf_search_not_cancelled()
    if result.returncode:
        raise PdfConnectivityError("Chrome thường chưa cho phép kết nối hoặc đã ngắt kết nối.")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PdfConnectivityError("Chrome trả về dữ liệu không hợp lệ.") from exc
    # CLI có thể trả lỗi dưới dạng mảng nội dung dù mã thoát vẫn bằng 0.
    if not isinstance(payload, dict):
        raise PdfConnectivityError("Chrome thường chưa cho phép kết nối hoặc đã ngắt kết nối.")
    return payload


def connect_personal_chrome() -> None:
    """Chrome tự hỏi người dùng có cho phép ứng dụng truy cập phiên hiện tại không."""
    global _connected
    if not _CLI.is_file():
        raise PdfConnectivityError("Thiếu công cụ Chrome. Chạy npm install --prefix .browser-tools.")
    with _browser_lock:
        try:
            status = subprocess.run(
                [str(_CLI), "status"], cwd=_ROOT, capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                timeout=10, check=False,
            )
            if status.returncode == 0 and '"--auto-connect"' in status.stdout:
                try:
                    _command("list_pages", "--output-format=json", timeout=30)
                    _connected = True
                    return
                except PdfConnectivityError:
                    pass
            result = subprocess.run(
                [str(_CLI), "start", "--autoConnect", "--no-usage-statistics", "--no-performance-crux"],
                cwd=_ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=30, check=False,
            )
            if result.returncode:
                raise PdfConnectivityError("Không thể yêu cầu kết nối Chrome thường.")
            try:
                _command("list_pages", "--output-format=json", timeout=90)
            except PdfConnectivityError as exc:
                raise PdfConnectivityError(
                    "Trong Chrome, mở chrome://inspect/#remote-debugging, bật Remote Debugging "
                    "rồi cho phép yêu cầu kết nối của WebResearchMaterials."
                ) from exc
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PdfConnectivityError("Chrome không phản hồi yêu cầu kết nối.") from exc
        _connected = True


def personal_chrome_connected() -> bool:
    return _connected


def disconnect_personal_chrome() -> None:
    """Ngắt điều khiển Chrome khi người dùng dừng tải PDF."""
    global _connected
    _connected = False
    if _CLI.is_file():
        try:
            subprocess.run(
                [str(_CLI), "stop"], cwd=_ROOT, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=15, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass


def _safe_url(url: str) -> bool:
    try:
        validate_public_https_url(url)
        return True
    except Exception:
        return False


def _page(url: str) -> int:
    with _browser_lock:
        before = _command("list_pages", "--output-format=json")
        existing = {page["id"] for page in before.get("pages", [])}
        result = _command("new_page", url, "--background", "--output-format=json", timeout=35)
        pages = result.get("pages") or []
        matches = [page["id"] for page in pages if page["id"] not in existing]
        if not matches:
            raise PdfConnectivityError("Không thể mở trang PDF trong Chrome thường.")
        return max(matches)


def _navigate(page_id: int, url: str) -> None:
    _command("navigate_page", str(page_id), "--url", url, "--output-format=json", timeout=35)


def _evaluate(page_id: int, script: str, *, timeout: int = 45) -> object:
    script = " ".join(script.splitlines())
    with tempfile.TemporaryDirectory(prefix="pdf-chrome-") as directory:
        output = Path(directory) / "result.json"
        _command(
            "evaluate_script", script, "--pageId", str(page_id), "--filePath", str(output),
            "--waitForStableDom", "false", "--output-format=json", timeout=timeout,
        )
        if not output.is_file():
            raise PdfConnectivityError("Chrome không trả về nội dung trang.")
        return json.loads(output.read_text(encoding="utf-8"))


def _page_info(page_id: int) -> dict:
    result = _evaluate(page_id, """() => ({
      url: location.href,
      text: (document.body?.innerText || '').slice(0, 1000),
      links: [...document.querySelectorAll('a[href],link[href],meta[name="citation_pdf_url"]')]
        .map(e => [e.href || e.content || '', (e.innerText || e.title || '').trim()])
        .slice(0, 300)
    })""")
    if not isinstance(result, dict):
        raise PdfConnectivityError("Chrome không đọc được trang bài nghiên cứu.")
    return result


def _fetch_pdf(page_id: int, url: str, max_bytes: int) -> DownloadedPdf | None:
    if not _safe_url(url):
        return None
    parsed = urlsplit(url)
    origin = f"{parsed.scheme}://{parsed.netloc}/"
    _navigate(page_id, origin)
    # Chạy trong đúng miền để trình duyệt dùng phiên đăng nhập mà không xuất cookie.
    script = """async () => {
      const response = await fetch(URL, {credentials: 'include'});
      if (!response.ok) return {status: response.status};
      if (Number(response.headers.get('content-length') || 0) > LIMIT) return {status: 0};
      const bytes = new Uint8Array(await response.arrayBuffer());
      if (bytes.length > LIMIT || bytes.length < 5) return {status: 0};
      let binary = '';
      for (let i = 0; i < bytes.length; i += 32768)
        binary += String.fromCharCode(...bytes.subarray(i, i + 32768));
      return {status: response.status, url: response.url, body: btoa(binary)};
    }""".replace("URL", json.dumps(url)).replace("LIMIT", str(max_bytes))
    try:
        result = _evaluate(page_id, script, timeout=75)
        if not isinstance(result, dict) or result.get("status") != 200:
            return None
        content = base64.b64decode(result.get("body", ""), validate=True)
        if not content.startswith(b"%PDF-") or len(content) > max_bytes:
            return None
        final_url = result.get("url", url)
        if not _safe_url(final_url):
            return None
        return DownloadedPdf(content, final_url, "tai-lieu.pdf")
    except (PdfConnectivityError, ValueError):
        return None


def _pdf_matches_document(document, pdf: DownloadedPdf) -> bool:
    """Đối chiếu PDF từ tìm kiếm rộng với tên bài hoặc DOI trước khi lưu."""
    title = normalize_title(document.tieu_de)
    path = normalize_title(unquote(urlsplit(pdf.final_url).path).replace("-", " ").replace("_", " "))
    filename = unquote(urlsplit(pdf.final_url).path.rsplit("/", 1)[-1]).lower()
    if any(marker in filename for marker in ("conference-program", "conference%20program", "conference program", "programme.pdf", "schedule.pdf")):
        return False
    try:
        import pymupdf

        opened = pymupdf.open(stream=pdf.content, filetype="pdf")
        try:
            first_page = opened[0].get_text("text") if len(opened) else ""
        finally:
            opened.close()
    except Exception:
        return False
    if not first_page.strip():
        return bool(title and title in path)
    page = normalize_title(first_page[:4000])
    if title and title in page:
        return True
    doi = (getattr(document, "doi", None) or "").strip().lower()
    if doi and doi in first_page[:4000].lower().replace("\n", ""):
        return True
    words = [word for word in title.split() if len(word) > 3]
    first_words = set(page[:1600].split())
    return len(words) >= 5 and sum(word in first_words for word in words) / len(words) >= 0.85


def _google_result_url(href: str) -> str | None:
    parsed = urlsplit(href)
    if parsed.hostname in {"www.google.com", "google.com"} and parsed.path == "/url":
        href = (parse_qs(parsed.query).get("q") or [""])[0]
        parsed = urlsplit(href)
    if parsed.hostname in {"www.google.com", "google.com"} or not _safe_url(href):
        return None
    return href


def _search_google(page_id: int, document, max_bytes: int) -> DownloadedPdf | None:
    title = normalize_title(document.tieu_de)
    doi = (getattr(document, "doi", None) or "").strip().lower()
    searches = [f'"{document.tieu_de}"']
    searches.append(f'"{doi}" filetype:pdf' if doi else f'"{document.tieu_de}" filetype:pdf')
    for search_text in searches:
        search = f"https://www.google.com/search?q={quote(search_text)}"
        try:
            _navigate(page_id, search)
            info = _page_info(page_id)
        except PdfConnectivityError:
            continue
        if "unusual traffic" in info.get("text", "").lower():
            return None
        results = []
        for href, label in info.get("links", []):
            url = _google_result_url(href)
            if not url:
                continue
            direct_pdf = any(marker in urlsplit(url).path.lower() for marker in (".pdf", "/pdf/"))
            label_text = normalize_title(label)
            path_text = normalize_title(unquote(urlsplit(url).path).replace("-", " ").replace("_", " "))
            title_match = title in label_text or title in path_text
            label_words = set(label_text.split())
            title_words = set(title.split())
            shared = len(label_words & title_words)
            partial_match = len(title_words) >= 5 and shared >= 5 and shared / min(len(label_words), len(title_words)) >= 0.75
            doi_match = bool(doi and (doi in unquote(href).lower() or doi in label.lower()))
            if direct_pdf or title_match or partial_match or doi_match:
                rank = 0 if title_match else 1 if doi_match else 2 if partial_match else 3
                results.append((rank, url))
        ordered = dict.fromkeys(url for _, url in sorted(results, key=lambda item: item[0]))
        for url in list(ordered)[:6]:
            if any(marker in urlsplit(url).path.lower() for marker in (".pdf", "/pdf/")):
                pdf = _fetch_pdf(page_id, url, max_bytes)
                if pdf and _pdf_matches_document(document, pdf):
                    return pdf
            try:
                _navigate(page_id, url)
                info = _page_info(page_id)
            except PdfConnectivityError:
                continue
            for href, label in info.get("links", []):
                link = urljoin(info.get("url", url), href)
                if any(marker in f"{link} {label}".lower() for marker in (".pdf", "/pdf/", "download pdf")):
                    pdf = _fetch_pdf(page_id, link, max_bytes)
                    if pdf and _pdf_matches_document(document, pdf):
                        return pdf
    return None


def find_pdf_in_personal_chrome(document, settings, urls: list[str]) -> DownloadedPdf | None:
    if not _connected:
        return None
    with _browser_slots:
        return _find_pdf_locked(document, settings, urls)


def _find_pdf_locked(document, settings, urls: list[str]) -> DownloadedPdf | None:
    first_url = next((url for url in urls if _safe_url(url)), "https://www.researchgate.net/")
    parsed = urlsplit(first_url)
    page_id = _page(f"{parsed.scheme}://{parsed.netloc}/")
    max_bytes = settings.max_upload_mb * 1024 * 1024
    try:
        for url in list(dict.fromkeys(urls))[:6]:
            if not _safe_url(url):
                continue
            pdf = _fetch_pdf(page_id, url, max_bytes)
            if pdf and _pdf_matches_document(document, pdf):
                return pdf
            try:
                _navigate(page_id, url)
                info = _page_info(page_id)
            except PdfConnectivityError:
                continue
            for href, label in info.get("links", []):
                link = urljoin(info.get("url", url), href)
                if any(term in f"{link} {label}".lower() for term in (".pdf", "/pdf/", "download pdf", "full-text pdf")):
                    pdf = _fetch_pdf(page_id, link, max_bytes)
                    if pdf and _pdf_matches_document(document, pdf):
                        return pdf

        title = normalize_title(document.tieu_de)
        if not title:
            return None
        try:
            _navigate(page_id, f"https://www.researchgate.net/search?q={quote(document.tieu_de)}")
            info = _page_info(page_id)
        except PdfConnectivityError:
            info = {"url": "https://www.researchgate.net/", "text": "", "links": []}
        if "Access restricted" in info.get("text", ""):
            info["links"] = []
        publications = []
        for href, text in info.get("links", []):
            link = urljoin(info.get("url", ""), href)
            parsed = urlsplit(link)
            if parsed.hostname != "www.researchgate.net" or not re.match(r"^/publication/\d+_", parsed.path):
                continue
            slug = parsed.path.split("_", 1)[-1].replace("_", "-")
            if title in {normalize_title(text), normalize_title(slug)}:
                publications.append(link)
        for publication in list(dict.fromkeys(publications))[:3]:
            _navigate(page_id, publication)
            info = _page_info(page_id)
            for href, label in info.get("links", []):
                link = urljoin(info.get("url", publication), href)
                if any(term in f"{link} {label}".lower() for term in (".pdf", "/pdf/", "download full-text pdf")):
                    pdf = _fetch_pdf(page_id, link, max_bytes)
                    if pdf and _pdf_matches_document(document, pdf):
                        return pdf
        return _search_google(page_id, document, max_bytes)
    finally:
        try:
            _command("close_page", str(page_id), "--output-format=json")
        except PdfConnectivityError:
            pass
