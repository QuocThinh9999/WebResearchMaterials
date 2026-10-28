from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
import logging
import re
import time
from typing import Any, Iterable
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit

import httpx

from app.config import Settings
from app.models import TaiLieu
from app.services.pdf_cancel import check_pdf_search_not_cancelled
from app.services.pdf_downloader import (
    DownloadedPdf,
    HostRequestLimiter,
    PdfConnectivityError,
    PdfDownloadError,
    UnsafePdfUrl,
    Resolver,
    download_pdf,
    get_with_retries,
    validate_public_https_url_with_retries,
)
from app.services.providers import normalize_doi, normalize_title


USER_AGENT = "WebResearchMaterials/1.0 (public academic PDF discovery)"
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


@dataclass(slots=True)
class PdfCandidate:
    url: str
    source: str
    headers: dict[str, str] = field(default_factory=dict)
    landing_page: bool = False


@dataclass(slots=True)
class LocatedPdf:
    downloaded: DownloadedPdf
    source: str


class _PdfLinkParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__()
        self.base_url = base_url
        self.urls: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.casefold(): value for key, value in attrs if value}
        if tag.casefold() == "meta":
            name = (values.get("name") or values.get("property") or "").casefold()
            if name in {"citation_pdf_url", "dc.identifier", "og:pdf"} and values.get("content"):
                self.urls.append(urljoin(self.base_url, values["content"]))
        else:
            for attribute in ("href", "src", "data", "data-url", "data-href"):
                target = values.get(attribute)
                if not target:
                    continue
                marker = " ".join(
                    [
                        target,
                        values.get("type", ""),
                        values.get("rel", ""),
                        values.get("title", ""),
                        values.get("class", ""),
                        values.get("id", ""),
                        values.get("download", ""),
                    ]
                ).casefold()
                if any(
                    hint in marker
                    for hint in (
                        ".pdf",
                        "application/pdf",
                        "citation_pdf",
                        "/pdf/",
                        "/pdf?",
                        "/epdf/",
                        "download/pdf",
                        "article/download",
                        "fulltext/pdf",
                        "pdf-download",
                    )
                ):
                    self.urls.append(urljoin(self.base_url, target))


class _ResearchGateSearchParser(HTMLParser):
    def __init__(self, title: str) -> None:
        super().__init__()
        self.title = normalize_title(title)
        self.urls: list[str] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() != "a":
            return
        href = dict(attrs).get("href") or ""
        parsed = urlsplit(urljoin("https://www.researchgate.net", href))
        if parsed.scheme == "https" and parsed.hostname == "www.researchgate.net" and re.match(
            r"^/publication/\d+_[^/?#]+", parsed.path
        ):
            self._href = href
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() != "a" or self._href is None:
            return
        slug = unquote(urlsplit(self._href).path.split("_", 1)[-1]).replace("_", " ").replace("-", " ")
        if self.title in {normalize_title(" ".join(self._text)), normalize_title(slug)}:
            self.urls.append(urljoin("https://www.researchgate.net", self._href))
        self._href = None
        self._text = []


def _normalize_url(url: str) -> str:
    value = url.strip()
    try:
        parsed = urlsplit(value)
        hostname = (parsed.hostname or "").casefold().rstrip(".")
    except ValueError:
        return value
    if not parsed.scheme or not hostname:
        return value
    try:
        parsed_port = parsed.port
    except ValueError:
        return value
    port = f":{parsed_port}" if parsed_port and parsed_port != 443 else ""
    return urlunsplit((parsed.scheme.casefold(), f"{hostname}{port}", parsed.path or "/", parsed.query, ""))


def _deduplicate(candidates: Iterable[PdfCandidate]) -> list[PdfCandidate]:
    seen: set[tuple[str, bool]] = set()
    result: list[PdfCandidate] = []
    for candidate in candidates:
        url = _normalize_url(candidate.url)
        key = (url, candidate.landing_page)
        if url and key not in seen:
            seen.add(key)
            result.append(PdfCandidate(url, candidate.source, candidate.headers, candidate.landing_page))
    return result


def _metadata_pdf_urls(value: Any, key_hint: str = "") -> list[str]:
    urls: list[str] = []
    if isinstance(value, dict):
        mime = str(value.get("@type") or value.get("type") or value.get("content-type") or "").casefold()
        for key, nested in value.items():
            urls.extend(_metadata_pdf_urls(nested, f"{key_hint} {key} {mime}"))
    elif isinstance(value, list):
        for nested in value:
            urls.extend(_metadata_pdf_urls(nested, key_hint))
    elif isinstance(value, str) and value.startswith("https://"):
        marker = f"{key_hint} {value}".casefold()
        if any(token in marker for token in (".pdf", "application/pdf", "url_for_pdf", "full-text", "fulltext")):
            urls.append(value)
    return urls


def metadata_candidates(document: TaiLieu, settings: Settings) -> list[PdfCandidate]:
    candidates: list[PdfCandidate] = []
    if document.url_pdf:
        candidates.append(PdfCandidate(document.url_pdf, "metadata_pdf_url"))
    candidates.extend(PdfCandidate(url, "metadata_nguon") for url in _metadata_pdf_urls(document.du_lieu_nguon or {}))
    if document.url_trang_goc:
        try:
            landing_host = (urlsplit(document.url_trang_goc).hostname or "").casefold()
        except ValueError:
            landing_host = ""
        if (
            document.url_trang_goc.startswith("https://")
            and "scopus.com" not in landing_host
            and "webofscience.com" not in landing_host
            and "webofknowledge.com" not in landing_host
        ):
            candidates.append(PdfCandidate(document.url_trang_goc, "metadata_landing", landing_page=True))
    eid = (document.ma_ngoai or {}).get("scopus_eid")
    if eid and settings.scopus_api_key:
        headers = {"X-ELS-APIKey": settings.scopus_api_key}
        if settings.scopus_inst_token:
            headers["X-ELS-Insttoken"] = settings.scopus_inst_token
        candidates.append(PdfCandidate(
            f"https://api.elsevier.com/content/article/eid/{quote(str(eid), safe='')}",
            "elsevier_full_text",
            headers,
        ))
    return _deduplicate(candidates)


def _crossref_candidates(
    doi: str,
    email: str | None,
    client: httpx.Client,
    max_retries: int = 3,
    limiter: HostRequestLimiter | None = None,
    sleep=time.sleep,
) -> list[PdfCandidate]:
    try:
        params = {"mailto": email} if email else None
        response = get_with_retries(
            client,
            f"https://api.crossref.org/works/{quote(doi, safe='')}",
            max_retries=max_retries,
            limiter=limiter,
            sleep=sleep,
            params=params,
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
        )
        if response.status_code != 200:
            return []
        message = response.json().get("message") or {}
    except httpx.RequestError as exc:
        raise PdfConnectivityError("Không thể kết nối Crossref để tìm PDF.") from exc
    except (ValueError, AttributeError):
        return []
    result: list[PdfCandidate] = []
    for item in message.get("link") or []:
        if not isinstance(item, dict):
            continue
        url = item.get("URL") or item.get("url")
        mime = str(item.get("content-type") or item.get("content_type") or "").casefold()
        if isinstance(url, str) and url.startswith("https://") and ("pdf" in mime or url.casefold().endswith(".pdf")):
            result.append(PdfCandidate(url, "crossref"))
    return _deduplicate(result)


def _unpaywall_candidates(
    doi: str,
    email: str | None,
    client: httpx.Client,
    max_retries: int = 3,
    limiter: HostRequestLimiter | None = None,
    sleep=time.sleep,
) -> list[PdfCandidate]:
    if not email:
        return []
    try:
        response = get_with_retries(
            client,
            f"https://api.unpaywall.org/v2/{quote(doi, safe='')}",
            max_retries=max_retries,
            limiter=limiter,
            sleep=sleep,
            params={"email": email},
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
        )
        if response.status_code != 200:
            return []
        payload = response.json()
        locations = [payload.get("best_oa_location"), *(payload.get("oa_locations") or [])]
        candidates: list[PdfCandidate] = []
        for location in locations:
            if not isinstance(location, dict):
                continue
            pdf_url = location.get("url_for_pdf")
            if isinstance(pdf_url, str) and pdf_url.startswith("https://"):
                candidates.append(PdfCandidate(pdf_url, "unpaywall"))
            landing_url = location.get("url_for_landing_page") or location.get("url")
            if isinstance(landing_url, str) and landing_url.startswith("https://"):
                candidates.append(PdfCandidate(landing_url, "unpaywall_landing", landing_page=True))
        return _deduplicate(candidates)
    except httpx.RequestError as exc:
        raise PdfConnectivityError("Không thể kết nối Unpaywall để tìm PDF.") from exc
    except (ValueError, AttributeError):
        return []


def _publisher_candidates(doi: str) -> list[PdfCandidate]:
    candidates: list[PdfCandidate] = []
    if doi.casefold().startswith("10.1177/"):
        candidates.append(
            PdfCandidate(
                f"https://journals.sagepub.com/doi/pdf/{quote(doi, safe='/')}?download=true",
                "publisher_sage",
            )
        )
    irjms = re.fullmatch(
        r"10\.47857/irjms\.(?P<year>\d{4})\.v\d{2}i(?P<issue>0[1-4])\.(?P<article>\d+)",
        doi,
        flags=re.IGNORECASE,
    )
    if irjms:
        month = {"01": "01", "02": "04", "03": "07", "04": "10"}[irjms.group("issue")]
        candidates.append(PdfCandidate(
            "https://www.irjms.com/wp-content/uploads/"
            f"{irjms.group('year')}/{month}/Manuscript_IRJMS_{irjms.group('article')}_WS.pdf",
            "publisher_irjms",
        ))
    return candidates


def _semantic_scholar_candidates(
    document: TaiLieu,
    doi: str | None,
    client: httpx.Client,
    max_retries: int = 3,
    limiter: HostRequestLimiter | None = None,
    sleep=time.sleep,
) -> list[PdfCandidate]:
    fields = "title,openAccessPdf,externalIds"
    try:
        if doi:
            response = get_with_retries(
                client,
                f"https://api.semanticscholar.org/graph/v1/paper/DOI:{quote(doi, safe='')}",
                max_retries=max_retries,
                limiter=limiter,
                sleep=sleep,
                params={"fields": fields},
                headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            )
            if response.status_code != 200:
                return []
            papers = [response.json()]
        else:
            response = get_with_retries(
                client,
                "https://api.semanticscholar.org/graph/v1/paper/search",
                max_retries=max_retries,
                limiter=limiter,
                sleep=sleep,
                params={"query": document.tieu_de, "limit": 5, "fields": fields},
                headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            )
            if response.status_code != 200:
                return []
            papers = response.json().get("data") or []
    except httpx.RequestError as exc:
        raise PdfConnectivityError("Không thể kết nối Semantic Scholar để tìm PDF.") from exc
    except (ValueError, AttributeError):
        return []

    expected_title = normalize_title(document.tieu_de)
    candidates: list[PdfCandidate] = []
    for paper in papers:
        if not isinstance(paper, dict) or normalize_title(str(paper.get("title") or "")) != expected_title:
            continue
        location = paper.get("openAccessPdf") or {}
        url = location.get("url") if isinstance(location, dict) else None
        if isinstance(url, str) and url.startswith("https://"):
            candidates.append(PdfCandidate(url, "semantic_scholar"))
    return _deduplicate(candidates)


def _openalex_candidates(
    document: TaiLieu,
    doi: str | None,
    email: str | None,
    client: httpx.Client,
    max_retries: int = 3,
    limiter: HostRequestLimiter | None = None,
    sleep=time.sleep,
) -> list[PdfCandidate]:
    try:
        params = {
            "select": "title,best_oa_location,locations,open_access,content_urls",
        }
        if doi:
            params["filter"] = f"doi:{doi}"
        else:
            params.update({"search": document.tieu_de, "per-page": "5"})
        if email:
            params["mailto"] = email
        response = get_with_retries(
            client,
            "https://api.openalex.org/works",
            max_retries=max_retries,
            limiter=limiter,
            sleep=sleep,
            params=params,
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
        )
        if response.status_code != 200:
            return []
        results = response.json().get("results") or []
    except httpx.RequestError as exc:
        raise PdfConnectivityError("Không thể kết nối OpenAlex để tìm PDF.") from exc
    except (ValueError, AttributeError):
        return []
    expected_title = normalize_title(document.tieu_de)
    candidates: list[PdfCandidate] = []
    for work in results:
        if not isinstance(work, dict):
            continue
        if not doi and normalize_title(str(work.get("title") or "")) != expected_title:
            continue
        content_urls = work.get("content_urls") or {}
        cached_pdf = content_urls.get("pdf") if isinstance(content_urls, dict) else None
        if isinstance(cached_pdf, str) and cached_pdf.startswith("https://"):
            candidates.append(PdfCandidate(cached_pdf, "openalex_content"))

        locations = [work.get("best_oa_location"), *(work.get("locations") or [])]
        for location in locations:
            if not isinstance(location, dict) or location.get("is_oa") is False:
                continue
            pdf_url = location.get("pdf_url")
            if isinstance(pdf_url, str) and pdf_url.startswith("https://"):
                candidates.append(PdfCandidate(pdf_url, "openalex"))
            landing_url = location.get("landing_page_url")
            if isinstance(landing_url, str) and landing_url.startswith("https://"):
                candidates.append(PdfCandidate(landing_url, "openalex_landing", landing_page=True))

        open_access = work.get("open_access") or {}
        oa_url = open_access.get("oa_url") if isinstance(open_access, dict) else None
        if isinstance(oa_url, str) and oa_url.startswith("https://"):
            candidates.append(PdfCandidate(oa_url, "openalex_oa", landing_page=True))
    return _deduplicate(candidates)


def _link_header_urls(value: str, base_url: str) -> list[str]:
    result: list[str] = []
    for part in value.split(","):
        sections = [item.strip() for item in part.split(";")]
        if not sections or not sections[0].startswith("<") or not sections[0].endswith(">"):
            continue
        metadata = ";".join(sections[1:]).casefold()
        if "application/pdf" in metadata or 'rel="alternate"' in metadata:
            result.append(urljoin(base_url, sections[0][1:-1]))
    return result


def _landing_page_candidates(
    url: str,
    source: str,
    client: httpx.Client,
    resolver: Resolver | None,
    max_redirects: int,
    max_retries: int = 3,
    limiter: HostRequestLimiter | None = None,
    sleep=time.sleep,
) -> list[PdfCandidate]:
    current_url = url
    try:
        for redirect_count in range(max_redirects + 1):
            validate_public_https_url_with_retries(current_url, resolver, max_retries, sleep)
            response = get_with_retries(
                client,
                current_url,
                max_retries=max_retries,
                limiter=limiter,
                sleep=sleep,
                headers={"Accept": "text/html,application/pdf", "User-Agent": USER_AGENT},
                follow_redirects=False,
            )
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                if not location or redirect_count >= max_redirects:
                    return []
                current_url = urljoin(current_url, location)
                continue
            if response.status_code != 200:
                return []
            content_type = response.headers.get("content-type", "").split(";", 1)[0].casefold()
            links = _link_header_urls(response.headers.get("link", ""), current_url)
            if content_type in {"application/pdf", "application/x-pdf"}:
                links.insert(0, current_url)
            elif content_type in {"text/html", "application/xhtml+xml", ""}:
                parser = _PdfLinkParser(current_url)
                parser.feed(response.content[:2_000_000].decode("utf-8", errors="replace"))
                if (urlsplit(current_url).hostname or "").casefold() == "www.researchgate.net":
                    publication = re.search(r"/publication/(\d+)", urlsplit(current_url).path)
                    if publication:
                        marker = f"/publication/{publication.group(1)}_"
                        links.extend(
                            link for link in parser.urls
                            if (urlsplit(link).hostname or "").casefold() == "www.researchgate.net"
                            and marker in urlsplit(link).path
                        )
                else:
                    links.extend(parser.urls)
            return _deduplicate(PdfCandidate(url, source) for url in links if url.startswith("https://"))
    except PdfConnectivityError:
        raise
    except httpx.RequestError as exc:
        raise PdfConnectivityError("Không thể kết nối trang DOI để tìm PDF.") from exc
    except UnsafePdfUrl:
        raise
    except (PdfDownloadError, UnicodeError):
        return []
    return []


def _doi_landing_candidates(
    doi: str,
    client: httpx.Client,
    resolver: Resolver | None,
    max_redirects: int,
    max_retries: int = 3,
    limiter: HostRequestLimiter | None = None,
    sleep=time.sleep,
) -> list[PdfCandidate]:
    return _landing_page_candidates(
        f"https://doi.org/{quote(doi, safe='/')}",
        "doi_metadata",
        client,
        resolver,
        max_redirects,
        max_retries,
        limiter,
        sleep,
    )


def _researchgate_candidates(
    title: str,
    client: httpx.Client,
    resolver: Resolver | None,
    max_retries: int = 3,
    limiter: HostRequestLimiter | None = None,
    sleep=time.sleep,
) -> list[PdfCandidate]:
    if not normalize_title(title):
        return []
    url = f"https://www.researchgate.net/search?q={quote(title)}"
    validate_public_https_url_with_retries(url, resolver, max_retries, sleep)
    try:
        response = get_with_retries(
            client,
            url,
            max_retries=max_retries,
            limiter=limiter,
            sleep=sleep,
            headers={"Accept": "text/html", "User-Agent": USER_AGENT},
            follow_redirects=False,
        )
    except httpx.RequestError as exc:
        raise PdfConnectivityError("Không thể kết nối ResearchGate để tìm PDF.") from exc
    if response.status_code != 200 or "text/html" not in response.headers.get("content-type", "").casefold():
        return []
    parser = _ResearchGateSearchParser(title)
    parser.feed(response.content[:2_000_000].decode("utf-8", errors="replace"))
    return _deduplicate(
        PdfCandidate(page_url, "researchgate", landing_page=True)
        for page_url in parser.urls[:5]
    )


def find_and_download_pdf(
    document: TaiLieu,
    settings: Settings,
    *,
    client: httpx.Client | None = None,
    resolver: Resolver | None = None,
    limiter: HostRequestLimiter | None = None,
    sleep=time.sleep,
) -> LocatedPdf | None:
    owned_client = client is None
    http_client = client or httpx.Client(timeout=settings.api_timeout_seconds, follow_redirects=False)
    max_bytes = settings.max_upload_mb * 1024 * 1024
    max_retries = int(getattr(settings, "pdf_max_retries", 3))
    attempted_downloads: set[str] = set()
    inspected_pages: set[str] = set()
    connectivity_errors: list[tuple[str, PdfConnectivityError]] = []
    browser_urls: list[str] = []
    completed_checks = 0

    def log_result(source: str, url: str | None, result: str, reason: str = "") -> None:
        # Không ghi path, query string hay header vì URL có thể chứa thông tin nhạy cảm.
        try:
            host = (urlsplit(url).hostname or "") if url else ""
        except ValueError:
            host = ""
        level = logging.WARNING if result in {"loi_ket_noi", "bo_qua"} else logging.INFO
        logger.log(level, "Tìm PDF: nguồn=%s host=%s kết_quả=%s lý_do=%s", source, host, result, reason)

    def attempt(candidates: Iterable[PdfCandidate]) -> LocatedPdf | None:
        nonlocal completed_checks
        for candidate in _deduplicate(candidates):
            check_pdf_search_not_cancelled()
            if candidate.landing_page:
                if candidate.url in inspected_pages:
                    continue
                inspected_pages.add(candidate.url)
                browser_urls.append(candidate.url)
                page_candidates = discover(
                    candidate.source,
                    lambda candidate=candidate: _landing_page_candidates(
                        candidate.url,
                        candidate.source,
                        http_client,
                        resolver,
                        settings.pdf_max_redirects,
                        max_retries,
                        limiter,
                        sleep,
                    ),
                )
                located = attempt(page_candidates)
                if located:
                    return located
                continue
            if candidate.url in attempted_downloads:
                continue
            attempted_downloads.add(candidate.url)
            try:
                downloaded = download_pdf(
                    candidate.url,
                    max_bytes=max_bytes,
                    timeout=settings.api_timeout_seconds,
                    max_redirects=settings.pdf_max_redirects,
                    max_retries=max_retries,
                    resolver=resolver,
                    client=http_client,
                    headers=candidate.headers,
                    limiter=limiter,
                    sleep=sleep,
                )
                completed_checks += 1
                log_result(candidate.source, candidate.url, "thanh_cong")
                return LocatedPdf(downloaded, candidate.source)
            except PdfConnectivityError as exc:
                connectivity_errors.append((candidate.source, exc))
                log_result(candidate.source, candidate.url, "loi_ket_noi", exc.__class__.__name__)
                continue
            except PdfDownloadError as exc:
                if "403" in str(exc) or "401" in str(exc) or "429" in str(exc):
                    browser_urls.append(candidate.url)
                if not isinstance(exc, UnsafePdfUrl):
                    completed_checks += 1
                log_result(candidate.source, candidate.url, "bo_qua", str(exc))
                continue
        return None

    def discover(source: str, factory) -> list[PdfCandidate]:
        nonlocal completed_checks
        check_pdf_search_not_cancelled()
        try:
            candidates = factory()
            completed_checks += 1
            log_result(source, None, "tim_thay_url" if candidates else "khong_co_url", str(len(candidates)))
            return candidates
        except PdfConnectivityError as exc:
            connectivity_errors.append((source, exc))
            log_result(source, None, "loi_ket_noi", exc.__class__.__name__)
            return []
        except PdfDownloadError as exc:
            if not isinstance(exc, UnsafePdfUrl):
                completed_checks += 1
            log_result(source, None, "bo_qua", str(exc))
            return []

    def raise_incomplete_discovery() -> None:
        # Chỉ toàn bộ mạng không sử dụng được mới khiến job thành lỗi hạ tầng.
        if not connectivity_errors or completed_checks:
            return
        sources = ", ".join(dict.fromkeys(source for source, _ in connectivity_errors))
        raise PdfConnectivityError(
            "Không thể kiểm tra đầy đủ mọi nguồn PDF công khai do lỗi kết nối hoặc hạ tầng. "
            f"Nguồn chưa kiểm tra xong: {sources}."
        ) from connectivity_errors[-1][1]

    def attempt_browser() -> LocatedPdf | None:
        check_pdf_search_not_cancelled()
        if not getattr(settings, "pdf_browser_enabled", False):
            return None
        from app.services.pdf_browser import find_pdf_in_browser

        browser_targets = list(browser_urls)
        doi = normalize_doi(document.doi)
        if doi:
            browser_targets.insert(0, f"https://doi.org/{quote(doi, safe='/')}")
        downloaded = find_pdf_in_browser(document, settings, browser_targets)
        if downloaded:
            log_result("chrome", downloaded.final_url, "thanh_cong")
            return LocatedPdf(downloaded, "chrome")
        return None

    try:
        check_pdf_search_not_cancelled()
        located = attempt(metadata_candidates(document, settings))
        if located:
            return located
        doi = normalize_doi(document.doi)
        if not doi:
            located = attempt(discover("openalex", lambda: _openalex_candidates(
                document,
                None,
                settings.unpaywall_email,
                http_client,
                max_retries,
                limiter,
                sleep,
            )))
            if located:
                return located
            located = attempt(discover("semantic_scholar", lambda: _semantic_scholar_candidates(
                document, None, http_client, max_retries, limiter, sleep
            )))
            if located:
                return located
            located = attempt(discover("researchgate", lambda: _researchgate_candidates(
                document.tieu_de, http_client, resolver, max_retries, limiter, sleep
            )))
            if located:
                return located
            located = attempt_browser()
            if located:
                return located
            raise_incomplete_discovery()
            return None
        located = attempt(_publisher_candidates(doi))
        if located:
            return located
        located = attempt(discover("crossref", lambda: _crossref_candidates(
            doi, settings.unpaywall_email, http_client, max_retries, limiter, sleep
        )))
        if located:
            return located
        if settings.unpaywall_email:
            located = attempt(discover("unpaywall", lambda: _unpaywall_candidates(
                doi, settings.unpaywall_email, http_client, max_retries, limiter, sleep
            )))
            if located:
                return located
        located = attempt(discover("openalex", lambda: _openalex_candidates(
            document,
            doi,
            settings.unpaywall_email,
            http_client,
            max_retries,
            limiter,
            sleep,
        )))
        if located:
            return located
        located = attempt(discover("semantic_scholar", lambda: _semantic_scholar_candidates(
            document, doi, http_client, max_retries, limiter, sleep
        )))
        if located:
            return located
        located = attempt(
            discover("doi", lambda: _doi_landing_candidates(
                doi,
                http_client,
                resolver,
                settings.pdf_max_redirects,
                max_retries,
                limiter,
                sleep,
            ))
        )
        if located:
            return located
        located = attempt(discover("researchgate", lambda: _researchgate_candidates(
            document.tieu_de, http_client, resolver, max_retries, limiter, sleep
        )))
        if located:
            return located
        located = attempt_browser()
        if located:
            return located
        raise_incomplete_discovery()
        return None
    finally:
        if owned_client:
            http_client.close()
