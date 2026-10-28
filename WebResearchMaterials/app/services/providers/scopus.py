from __future__ import annotations

import re
import time
from datetime import date
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.services.providers.base import (
    NormalizedDocument,
    ProviderConfigurationError,
    ProviderError,
    SearchCriteria,
    SearchProvider,
    find_public_pdf_url,
    normalize_doi,
    normalize_title,
    split_search_terms,
)


def _quoted(value: str) -> str:
    return value.replace('"', '\\"').strip()


def build_scopus_query(criteria: SearchCriteria) -> str:
    parts: list[str] = []
    keyword_terms = split_search_terms(criteria.keywords)
    if keyword_terms:
        parts.append("(" + " OR ".join(f'TITLE-ABS-KEY("{_quoted(term)}")' for term in keyword_terms) + ")")
    required_keyword_terms = split_search_terms(criteria.keywords_and)
    if required_keyword_terms:
        parts.append(
            "(" + " AND ".join(f'TITLE-ABS-KEY("{_quoted(term)}")' for term in required_keyword_terms) + ")"
        )
    for group in criteria.keyword_groups:
        terms = split_search_terms(",".join(group))
        if terms:
            parts.append("(" + " OR ".join(f'TITLE-ABS-KEY("{_quoted(term)}")' for term in terms) + ")")
    title_terms = split_search_terms(criteria.title)
    if title_terms:
        parts.append("(" + " OR ".join(f'TITLE("{_quoted(term)}")' for term in title_terms) + ")")
    if criteria.author:
        parts.append(f'AUTH("{_quoted(criteria.author)}")')
    if criteria.year_from:
        parts.append(f"PUBYEAR AFT {criteria.year_from - 1}")
    if criteria.year_to:
        parts.append(f"PUBYEAR BEF {criteria.year_to + 1}")
    if criteria.language:
        parts.append(f'LANGUAGE("{_quoted(criteria.language)}")')
    if criteria.document_type:
        type_codes = {
            "article": "ar",
            "review": "re",
            "conference paper": "cp",
            "book": "bk",
            "book chapter": "ch",
        }
        type_code = type_codes.get(criteria.document_type.strip().lower())
        if type_code:
            parts.append(f"DOCTYPE({type_code})")
    if criteria.open_access:
        parts.append("OPENACCESS(1)")
    return " AND ".join(parts)


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _source_quartile(payload: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    entries = _as_list((payload.get("serial-metadata-response") or {}).get("entry"))
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        scores = entry.get("citeScoreYearInfoList") or {}
        current_year = str(scores.get("citeScoreCurrentMetricYear") or "")
        years = [
            item for item in _as_list(scores.get("citeScoreYearInfo"))
            if isinstance(item, dict)
            and str(item.get("@year") or "").isdigit()
            and (str(item.get("@status") or "").casefold() == "complete" or str(item.get("@year")) == current_year)
        ]
        for year in sorted(years, key=lambda item: int(item["@year"]), reverse=True):
            ranks: list[tuple[int, str | None]] = []
            for information in _as_list(year.get("citeScoreInformationList")):
                if not isinstance(information, dict):
                    continue
                for score in _as_list(information.get("citeScoreInfo")):
                    if not isinstance(score, dict) or score.get("docType") != "all":
                        continue
                    for rank in _as_list(score.get("citeScoreSubjectRank")):
                        if not isinstance(rank, dict):
                            continue
                        try:
                            percentile = int(rank["percentile"])
                        except (KeyError, TypeError, ValueError):
                            continue
                        if 0 <= percentile <= 100:
                            ranks.append((percentile, rank.get("subjectCode")))
            if ranks:
                percentile, subject_code = max(ranks, key=lambda item: item[0])
                quartile = "Q1" if percentile >= 75 else "Q2" if percentile >= 50 else "Q3" if percentile >= 25 else "Q4"
                return quartile, {
                    "source": "Scopus CiteScore",
                    "year": int(year["@year"]),
                    "percentile": percentile,
                    "subject_code": subject_code,
                }
    return None


def normalize_scopus_entry(entry: dict[str, Any]) -> NormalizedDocument:
    title = str(entry.get("dc:title") or "").strip()
    authors = entry.get("author") or []
    if isinstance(authors, dict):
        authors = [authors]
    normalized_authors = [
        {
            "name": item.get("authname") or item.get("ce:indexed-name") or item.get("surname") or "",
            "id": item.get("authid"),
        }
        for item in authors
        if isinstance(item, dict)
    ]
    if not normalized_authors and entry.get("dc:creator"):
        normalized_authors = [{"name": entry["dc:creator"]}]
    publication_date = _parse_date(entry.get("prism:coverDate"))
    eid = entry.get("eid") or entry.get("dc:identifier")
    links = entry.get("link") or []
    if isinstance(links, dict):
        links = [links]
    source_url = next((item.get("@href") for item in links if item.get("@ref") == "scopus"), None)
    open_access = str(entry.get("openaccess") or entry.get("openaccessFlag") or "").lower() in {"1", "true", "yes"}
    citation_count = entry.get("citedby-count")
    return NormalizedDocument(
        doi=normalize_doi(entry.get("prism:doi")),
        title=title,
        normalized_title=normalize_title(title),
        abstract=entry.get("dc:description"),
        authors=normalized_authors,
        keywords=[],
        document_type=entry.get("subtypeDescription") or entry.get("subtype"),
        language=entry.get("dc:language") or entry.get("language"),
        publication_date=publication_date,
        publication_year=publication_date.year if publication_date else None,
        journal=entry.get("prism:publicationName"),
        issn=entry.get("prism:issn") or entry.get("prism:eIssn"),
        volume=entry.get("prism:volume"),
        issue=entry.get("prism:issueIdentifier"),
        pages_or_article=entry.get("prism:pageRange") or entry.get("article-number"),
        citation_info={"scopus": int(citation_count)} if str(citation_count or "").isdigit() else None,
        external_ids={"scopus_eid": eid} if eid else {},
        source_data={"scopus": entry},
        open_access=open_access,
        source_url=source_url,
        pdf_url=find_public_pdf_url(entry),
    )


class ScopusProvider(SearchProvider):
    def __init__(
        self,
        api_key: str | None,
        api_url: str,
        timeout: float,
        max_results: int,
        inst_token: str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.api_key = api_key
        self.api_url = api_url
        self.timeout = timeout
        self.max_results = max_results
        self.inst_token = inst_token
        self.client = client
        self._last_serial_request = 0.0

    def _lookup_quartile(self, client: httpx.Client, headers: dict[str, str], issn: str) -> tuple[str, dict[str, Any]] | None:
        if self.client is None:
            delay = 0.18 - (time.monotonic() - self._last_serial_request)
            if delay > 0:
                time.sleep(delay)
            self._last_serial_request = time.monotonic()
        endpoint = urlsplit(self.api_url)
        url = f"{endpoint.scheme}://{endpoint.netloc}/content/serial/title/issn/{issn}"
        try:
            response = client.get(url, headers=headers, params={"view": "CITESCORE"})
            if response.status_code == 404:
                return None
            response.raise_for_status()
            return _source_quartile(response.json())
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            raise ProviderError("Không thể xác định quartile từ Scopus. Vui lòng kiểm tra quyền Serial Title API.") from exc

    def search(self, criteria: SearchCriteria) -> list[NormalizedDocument]:
        if not self.api_key:
            raise ProviderConfigurationError("Chưa cấu hình SCOPUS_API_KEY trong .env.")
        headers = {"Accept": "application/json", "X-ELS-APIKey": self.api_key}
        if self.inst_token:
            headers["X-ELS-Insttoken"] = self.inst_token
        client = self.client or httpx.Client(timeout=self.timeout)
        results: list[NormalizedDocument] = []
        quartile_cache: dict[str, tuple[str, dict[str, Any]] | None] = {}
        start = 0
        try:
            while len(results) < self.max_results:
                count = min(25, self.max_results - len(results))
                response = client.get(
                    self.api_url,
                    headers=headers,
                    params={
                        "query": build_scopus_query(criteria),
                        "view": "STANDARD",
                        "start": start,
                        "count": count,
                    },
                )
                response.raise_for_status()
                payload = response.json().get("search-results", {})
                entries = payload.get("entry") or []
                if not entries:
                    break
                for entry in entries:
                    document = normalize_scopus_entry(entry)
                    if document.title:
                        if criteria.quartiles and document.issn:
                            issn = document.issn.strip().replace("-", "").upper()
                            if re.fullmatch(r"\d{7}[\dX]", issn):
                                if issn not in quartile_cache:
                                    quartile_cache[issn] = self._lookup_quartile(client, headers, issn)
                                quartile = quartile_cache[issn]
                                if quartile:
                                    document.quartile, document.quartile_info = quartile
                        results.append(document)
                total = int(payload.get("opensearch:totalResults") or len(results))
                start += len(entries)
                if start >= total:
                    break
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            raise ProviderError("Không thể lấy dữ liệu từ Scopus. Vui lòng kiểm tra khóa API và điều kiện tìm kiếm.") from exc
        finally:
            if self.client is None:
                client.close()
        return results
