from __future__ import annotations

from datetime import date
from typing import Any

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


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _escape(value: str) -> str:
    return value.replace('"', '\\"').strip()


def build_wos_query(criteria: SearchCriteria) -> str:
    parts: list[str] = []
    keyword_terms = split_search_terms(criteria.keywords)
    if keyword_terms:
        parts.append("TS=(" + " OR ".join(f'"{_escape(term)}"' for term in keyword_terms) + ")")
    required_keyword_terms = split_search_terms(criteria.keywords_and)
    if required_keyword_terms:
        parts.append("TS=(" + " AND ".join(f'"{_escape(term)}"' for term in required_keyword_terms) + ")")
    for group in criteria.keyword_groups:
        terms = split_search_terms(",".join(group))
        if terms:
            parts.append("TS=(" + " OR ".join(f'"{_escape(term)}"' for term in terms) + ")")
    title_terms = split_search_terms(criteria.title)
    if title_terms:
        parts.append("TI=(" + " OR ".join(f'"{_escape(term)}"' for term in title_terms) + ")")
    if criteria.author:
        parts.append(f'AU=("{_escape(criteria.author)}")')
    if criteria.document_type:
        parts.append(f'DT=("{_escape(criteria.document_type)}")')
    if criteria.language:
        parts.append(f'LA=("{_escape(criteria.language)}")')
    return " AND ".join(parts)


def _title(summary: dict[str, Any], title_type: str) -> str | None:
    for item in _as_list((summary.get("titles") or {}).get("title")):
        if isinstance(item, dict) and item.get("type") == title_type:
            return item.get("content") or item.get("value")
    return None


def _identifiers(record: dict[str, Any]) -> dict[str, str]:
    dynamic = record.get("dynamic_data") or {}
    identifiers = (((dynamic.get("cluster_related") or {}).get("identifiers") or {}).get("identifier"))
    values: dict[str, str] = {}
    for item in _as_list(identifiers):
        if isinstance(item, dict) and item.get("type") and item.get("value"):
            values[str(item["type"]).lower()] = str(item["value"])
    return values


def normalize_wos_record(record: dict[str, Any]) -> NormalizedDocument:
    static = record.get("static_data") or {}
    summary = static.get("summary") or {}
    metadata = static.get("fullrecord_metadata") or {}
    pub_info = summary.get("pub_info") or {}
    title = (_title(summary, "item") or _title(summary, "document") or "").strip()
    names = (summary.get("names") or {}).get("name")
    authors = [
        {
            "name": item.get("display_name") or item.get("full_name") or item.get("wos_standard") or "",
            "orcid": item.get("orcid_id"),
            "researcher_id": item.get("r_id"),
        }
        for item in _as_list(names)
        if isinstance(item, dict) and item.get("role", "author") == "author"
    ]
    identifiers = _identifiers(record)
    year_raw = pub_info.get("pubyear")
    try:
        year = int(year_raw) if year_raw else None
    except (TypeError, ValueError):
        year = None
    sort_date = pub_info.get("sortdate")
    try:
        publication_date = date.fromisoformat(str(sort_date)[:10]) if sort_date else None
    except ValueError:
        publication_date = None
    abstracts = ((metadata.get("abstracts") or {}).get("abstract") or {})
    abstract_text = ((abstracts.get("abstract_text") or {}).get("p")) if isinstance(abstracts, dict) else None
    if isinstance(abstract_text, list):
        abstract_text = "\n".join(str(item) for item in abstract_text)
    keywords = [str(value) for value in _as_list((metadata.get("keywords") or {}).get("keyword"))]
    doctypes = _as_list((summary.get("doctypes") or {}).get("doctype"))
    languages = _as_list((metadata.get("normalized_languages") or {}).get("language"))
    language = None
    if languages:
        first_language = languages[0]
        language = first_language.get("content") if isinstance(first_language, dict) else str(first_language)
    uid = record.get("UID")
    journal_oa = str(pub_info.get("journal_oas_gold") or "").upper() == "Y"
    publisher_data = (summary.get("publishers") or {}).get("publisher")
    publisher_name = None
    if isinstance(publisher_data, dict):
        publisher_name_data = (publisher_data.get("names") or {}).get("name")
        if isinstance(publisher_name_data, dict):
            publisher_name = publisher_name_data.get("display_name") or publisher_name_data.get("full_name")
    return NormalizedDocument(
        doi=normalize_doi(identifiers.get("doi")),
        title=title,
        normalized_title=normalize_title(title),
        abstract=str(abstract_text) if abstract_text else None,
        authors=authors,
        keywords=keywords,
        document_type=str(doctypes[0]) if doctypes else None,
        language=language,
        publication_date=publication_date,
        publication_year=year,
        journal=_title(summary, "source"),
        issn=identifiers.get("issn") or identifiers.get("eissn"),
        publisher=publisher_name,
        volume=str(pub_info.get("vol")) if pub_info.get("vol") is not None else None,
        issue=str(pub_info.get("issue")) if pub_info.get("issue") is not None else None,
        pages_or_article=str((pub_info.get("page") or {}).get("begin")) if isinstance(pub_info.get("page"), dict) else None,
        external_ids={"wos_uid": uid} if uid else {},
        source_data={"wos": record},
        open_access=journal_oa,
        source_url=f"https://www.webofscience.com/wos/woscc/full-record/{uid}" if uid else None,
        pdf_url=find_public_pdf_url(record),
    )


def _extract_records(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("Data") or payload.get("data") or {}
    records = data.get("Records") or data.get("records") or {}
    if isinstance(records, dict):
        records = records.get("records") or records.get("REC") or records.get("rec") or []
    return [item for item in _as_list(records) if isinstance(item, dict)]


class WebOfScienceProvider(SearchProvider):
    def __init__(
        self,
        api_key: str | None,
        api_url: str,
        timeout: float,
        max_results: int,
        client: httpx.Client | None = None,
    ) -> None:
        self.api_key = api_key
        self.api_url = api_url.rstrip("/")
        self.timeout = timeout
        self.max_results = max_results
        self.client = client

    def search(self, criteria: SearchCriteria) -> list[NormalizedDocument]:
        if not self.api_key:
            raise ProviderConfigurationError("Chưa cấu hình WOS_API_KEY trong .env.")
        client = self.client or httpx.Client(timeout=self.timeout)
        results: list[NormalizedDocument] = []
        first_record = 1
        try:
            while len(results) < self.max_results:
                count = min(100, self.max_results - len(results))
                params: dict[str, Any] = {
                    "databaseId": "WOS",
                    "usrQuery": build_wos_query(criteria),
                    "count": count,
                    "firstRecord": first_record,
                    "optionView": "FR",
                }
                if criteria.year_from or criteria.year_to:
                    year_from = criteria.year_from or 1900
                    year_to = criteria.year_to or date.today().year
                    params["publishTimeSpan"] = f"{year_from}-01-01 {year_to}-12-31"
                response = client.get(self.api_url, headers={"X-ApiKey": self.api_key, "Accept": "application/json"}, params=params)
                response.raise_for_status()
                payload = response.json()
                records = _extract_records(payload)
                if not records:
                    break
                for record in records:
                    document = normalize_wos_record(record)
                    if document.title:
                        results.append(document)
                first_record += len(records)
                query_result = payload.get("QueryResult") or payload.get("queryResult") or {}
                total = int(query_result.get("RecordsFound") or query_result.get("recordsFound") or len(results))
                if first_record > total:
                    break
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            raise ProviderError("Không thể lấy dữ liệu từ Web of Science. Vui lòng kiểm tra khóa API và điều kiện tìm kiếm.") from exc
        finally:
            if self.client is None:
                client.close()
        return results
