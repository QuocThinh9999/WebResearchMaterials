from __future__ import annotations

import datetime as dt
import ipaddress
import socket
import threading
import time
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import PurePosixPath
from typing import Callable, Iterator, Mapping
from urllib.parse import urljoin, urlsplit

import httpx
from app.services.pdf_cancel import check_pdf_search_not_cancelled

from app.services.files import is_pdf_signature


class PdfDownloadError(RuntimeError):
    pass


class UnsafePdfUrl(PdfDownloadError):
    pass


class PdfUnavailableError(PdfDownloadError):
    pass


class PdfConnectivityError(PdfDownloadError):
    pass


class PdfSourceUnavailableError(PdfDownloadError):
    """Máy chủ đã phản hồi nhưng tạm thời không phục vụ request."""
    pass


Resolver = Callable[[str], list[str]]
Sleep = Callable[[float], None]
TEMPORARY_HTTP_STATUSES = {429, 500, 502, 503, 504}
MAX_INLINE_RETRY_WAIT_SECONDS = 30


@dataclass(slots=True)
class DownloadedPdf:
    content: bytes
    final_url: str
    filename: str


class HostRequestLimiter:
    """Giới hạn số request đồng thời riêng cho từng hostname."""

    def __init__(self, per_host: int) -> None:
        self.per_host = max(1, per_host)
        self._lock = threading.Lock()
        self._semaphores: dict[str, threading.BoundedSemaphore] = {}
        self._deferred_until: dict[str, float] = {}

    def defer(self, url: str, seconds: float) -> None:
        hostname = (urlsplit(url).hostname or "").casefold().rstrip(".")
        with self._lock:
            self._deferred_until[hostname] = max(
                self._deferred_until.get(hostname, 0), time.monotonic() + seconds,
            )

    def is_deferred(self, url: str) -> bool:
        hostname = (urlsplit(url).hostname or "").casefold().rstrip(".")
        with self._lock:
            return time.monotonic() < self._deferred_until.get(hostname, 0)

    @contextmanager
    def request(self, url: str) -> Iterator[None]:
        hostname = (urlsplit(url).hostname or "").casefold().rstrip(".")
        with self._lock:
            semaphore = self._semaphores.setdefault(
                hostname,
                threading.BoundedSemaphore(self.per_host),
            )
        semaphore.acquire()
        try:
            yield
        finally:
            semaphore.release()


def _resolve_host(hostname: str) -> list[str]:
    try:
        return list({item[4][0] for item in socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)})
    except OSError as exc:
        raise PdfConnectivityError("Không thể kết nối Internet để xác minh máy chủ PDF.") from exc


def validate_public_https_url(url: str, resolver: Resolver | None = None) -> None:
    try:
        parsed = urlsplit(url)
        hostname_value = parsed.hostname
    except ValueError as exc:
        raise UnsafePdfUrl("Địa chỉ PDF không hợp lệ.") from exc
    if parsed.scheme.lower() != "https" or not hostname_value:
        raise UnsafePdfUrl("Chỉ tự động tải PDF qua địa chỉ HTTPS công khai.")
    try:
        port = parsed.port
    except ValueError as exc:
        raise UnsafePdfUrl("Địa chỉ PDF có cổng không hợp lệ.") from exc
    if parsed.username or parsed.password or (port not in {None, 443}):
        raise UnsafePdfUrl("Địa chỉ PDF có thông tin truy cập hoặc cổng không an toàn.")
    hostname = hostname_value.casefold().rstrip(".")
    if hostname in {"localhost", "localhost.localdomain", "metadata.google.internal"}:
        raise UnsafePdfUrl("Địa chỉ PDF trỏ tới mạng nội bộ.")
    addresses = (resolver or _resolve_host)(hostname)
    if not addresses:
        raise UnsafePdfUrl("Không xác minh được địa chỉ máy chủ PDF.")
    try:
        if any(not ipaddress.ip_address(address).is_global for address in addresses):
            raise UnsafePdfUrl("Địa chỉ PDF trỏ tới mạng nội bộ hoặc địa chỉ dành riêng.")
    except ValueError as exc:
        raise UnsafePdfUrl("Máy chủ PDF trả về địa chỉ IP không hợp lệ.") from exc


def validate_public_https_url_with_retries(
    url: str,
    resolver: Resolver | None,
    max_retries: int,
    sleep: Sleep = time.sleep,
) -> None:
    last_error: PdfConnectivityError | None = None
    for attempt in range(max(1, max_retries)):
        check_pdf_search_not_cancelled()
        try:
            validate_public_https_url(url, resolver)
            return
        except PdfConnectivityError as exc:
            last_error = exc
            if attempt + 1 < max(1, max_retries):
                sleep(_retry_delay(None, attempt))
    if last_error:
        raise last_error


def _filename_from_url(url: str) -> str:
    name = PurePosixPath(urlsplit(url).path).name
    return name[:250] if name.casefold().endswith(".pdf") else "tai-lieu.pdf"


def _request_slot(limiter: HostRequestLimiter | None, url: str):
    return limiter.request(url) if limiter else nullcontext()


def _retry_delay(response: httpx.Response | None, attempt: int) -> float:
    explicit = _retry_after_seconds(response)
    if explicit is not None:
        return explicit
    return min(0.5 * (2**attempt), 8.0)


def _retry_after_seconds(response: httpx.Response | None) -> float | None:
    value = response.headers.get("retry-after", "").strip() if response is not None else ""
    if value:
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(value)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=dt.UTC)
                return max(0.0, (retry_at - dt.datetime.now(dt.UTC)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                pass
    return None


def _defer_long_retry(url: str, response: httpx.Response, limiter: HostRequestLimiter | None) -> None:
    delay = _retry_after_seconds(response)
    if delay is None or delay <= MAX_INLINE_RETRY_WAIT_SECONDS:
        return
    if limiter:
        limiter.defer(url, delay)
    raise PdfSourceUnavailableError(
        f"Nguồn PDF yêu cầu chờ quá lâu sau HTTP {response.status_code}; chuyển sang nguồn khác."
    )


def get_with_retries(
    client: httpx.Client,
    url: str,
    *,
    max_retries: int,
    limiter: HostRequestLimiter | None = None,
    sleep: Sleep = time.sleep,
    **kwargs,
) -> httpx.Response:
    """GET cho nguồn khám phá PDF, có retry với lỗi hạ tầng tạm thời."""
    last_error: httpx.RequestError | None = None
    response: httpx.Response | None = None
    for attempt in range(max(1, max_retries)):
        check_pdf_search_not_cancelled()
        if limiter and limiter.is_deferred(url):
            raise PdfSourceUnavailableError("Nguồn PDF đang giới hạn tốc độ; chuyển sang nguồn khác.")
        try:
            with _request_slot(limiter, url):
                response = client.get(url, **kwargs)
        except httpx.RequestError as exc:
            last_error = exc
        else:
            last_error = None
            if response.status_code not in TEMPORARY_HTTP_STATUSES:
                return response
            _defer_long_retry(url, response, limiter)
        if attempt + 1 < max(1, max_retries):
            sleep(_retry_delay(response, attempt))
    if last_error:
        raise PdfConnectivityError("Không thể kết nối nguồn tìm PDF sau nhiều lần thử.") from last_error
    raise PdfSourceUnavailableError(
        f"Nguồn tìm PDF tạm thời không khả dụng (HTTP {response.status_code if response is not None else 'không xác định'})."
    )


def download_pdf(
    url: str,
    *,
    max_bytes: int,
    timeout: float = 30,
    max_redirects: int = 3,
    max_retries: int = 3,
    resolver: Resolver | None = None,
    client: httpx.Client | None = None,
    headers: Mapping[str, str] | None = None,
    limiter: HostRequestLimiter | None = None,
    sleep: Sleep = time.sleep,
) -> DownloadedPdf:
    current_url = url
    owned_client = client is None
    http_client = client or httpx.Client(timeout=timeout, follow_redirects=False)
    public_headers = {
        "Accept": "application/pdf",
        "User-Agent": "WebResearchMaterials/1.0 (public academic PDF downloader)",
    }
    credential_headers = dict(headers or {})
    try:
        credential_host = urlsplit(url).hostname
    except ValueError:
        credential_host = None
    try:
        for redirect_count in range(max_redirects + 1):
            check_pdf_search_not_cancelled()
            validate_public_https_url_with_retries(current_url, resolver, max_retries, sleep)
            if limiter and limiter.is_deferred(current_url):
                raise PdfSourceUnavailableError("Nguồn PDF đang giới hạn tốc độ; chuyển sang nguồn khác.")
            redirected_to: str | None = None
            for attempt in range(max(1, max_retries)):
                check_pdf_search_not_cancelled()
                response_for_delay: httpx.Response | None = None
                try:
                    request_headers = dict(public_headers)
                    if urlsplit(current_url).hostname == credential_host:
                        request_headers.update(credential_headers)
                    with _request_slot(limiter, current_url):
                        with http_client.stream(
                            "GET", current_url, headers=request_headers, follow_redirects=False
                        ) as response:
                            response_for_delay = response
                            if response.status_code in TEMPORARY_HTTP_STATUSES:
                                _defer_long_retry(current_url, response, limiter)
                            elif response.status_code in {301, 302, 303, 307, 308}:
                                location = response.headers.get("location")
                                if not location or redirect_count >= max_redirects:
                                    raise PdfUnavailableError("Liên kết PDF chuyển hướng không hợp lệ hoặc quá nhiều lần.")
                                redirected_to = urljoin(current_url, location)
                                break
                            elif response.status_code != 200:
                                raise PdfUnavailableError(f"Máy chủ PDF trả về mã {response.status_code}.")
                            else:
                                declared_length = response.headers.get("content-length")
                                if declared_length and declared_length.isdigit() and int(declared_length) > max_bytes:
                                    raise PdfUnavailableError("Tệp PDF vượt quá dung lượng cho phép.")
                                chunks: list[bytes] = []
                                total = 0
                                for chunk in response.iter_bytes():
                                    check_pdf_search_not_cancelled()
                                    if not chunk:
                                        continue
                                    total += len(chunk)
                                    if total > max_bytes:
                                        raise PdfUnavailableError("Tệp PDF vượt quá dung lượng cho phép.")
                                    chunks.append(chunk)
                                content = b"".join(chunks)
                                if not is_pdf_signature(content):
                                    raise PdfUnavailableError("Nội dung tải về không có chữ ký tệp PDF hợp lệ.")
                                return DownloadedPdf(content, current_url, _filename_from_url(current_url))
                except httpx.RequestError as exc:
                    if attempt + 1 >= max(1, max_retries):
                        raise PdfConnectivityError("Không thể kết nối Internet hoặc máy chủ PDF.") from exc
                if redirected_to:
                    break
                if response_for_delay is not None and response_for_delay.status_code in TEMPORARY_HTTP_STATUSES:
                    if attempt + 1 >= max(1, max_retries):
                        raise PdfSourceUnavailableError(
                            f"Máy chủ PDF tạm thời không khả dụng (HTTP {response_for_delay.status_code})."
                        )
                if attempt + 1 < max(1, max_retries):
                    sleep(_retry_delay(response_for_delay, attempt))
            if redirected_to:
                current_url = redirected_to
                continue
            raise PdfConnectivityError("Không thể hoàn tất request tải PDF sau nhiều lần thử.")
        raise PdfUnavailableError("Liên kết PDF chuyển hướng quá nhiều lần.")
    finally:
        if owned_client:
            http_client.close()
