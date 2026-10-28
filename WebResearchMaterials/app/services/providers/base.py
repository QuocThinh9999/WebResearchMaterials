from __future__ import annotations

import re
import unicodedata
from abc import ABC, abstractmethod
from datetime import date
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, model_validator


class ProviderError(RuntimeError):
    pass


class ProviderConfigurationError(ProviderError):
    pass


def split_search_terms(value: str | None) -> list[str]:
    """Tách danh sách cụm từ, chuẩn hóa khoảng trắng và loại trùng không phân biệt hoa thường."""
    if not value:
        return []
    result: list[str] = []
    seen: set[str] = set()
    for raw_term in re.split(r"[,;\r\n]+", value):
        term = re.sub(r"\s+", " ", raw_term).strip()
        key = term.casefold()
        if term and key not in seen:
            seen.add(key)
            result.append(term)
    return result


class SearchCriteria(BaseModel):
    keywords: str | None = None
    keywords_and: str | None = None
    keyword_groups: list[list[str]] = Field(default_factory=list)
    title: str | None = None
    author: str | None = None
    year_from: int | None = Field(default=None, ge=1000, le=9999)
    year_to: int | None = Field(default=None, ge=1000, le=9999)
    document_type: str | None = None
    language: str | None = None
    quartiles: list[Literal["Q1", "Q2", "Q3", "Q4"]] = Field(default_factory=list)
    open_access: bool = False
    sources: list[Literal["scopus", "wos"]] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_search(self) -> "SearchCriteria":
        if self.year_from and self.year_to and self.year_from > self.year_to:
            raise ValueError("Năm bắt đầu không được lớn hơn năm kết thúc.")
        if not self.sources:
            raise ValueError("Vui lòng chọn ít nhất một nguồn tìm kiếm.")
        if not any([self.keywords, self.keywords_and, self.keyword_groups, self.title, self.author]):
            raise ValueError("Vui lòng nhập từ khóa, tiêu đề hoặc tác giả.")
        return self


class NormalizedDocument(BaseModel):
    doi: str | None = None
    title: str
    normalized_title: str
    abstract: str | None = None
    authors: list[Any] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    document_type: str | None = None
    language: str | None = None
    publication_date: date | None = None
    publication_year: int | None = None
    journal: str | None = None
    issn: str | None = None
    publisher: str | None = None
    volume: str | None = None
    issue: str | None = None
    pages_or_article: str | None = None
    quartile: str | None = None
    quartile_info: dict[str, Any] | None = None
    citation_info: dict[str, Any] | None = None
    external_ids: dict[str, Any] = Field(default_factory=dict)
    source_data: dict[str, Any] = Field(default_factory=dict)
    open_access: bool = False
    source_url: str | None = None
    pdf_url: str | None = None

    def dedup_key(self) -> tuple[Any, ...]:
        doi = normalize_doi(self.doi)
        if doi:
            return ("doi", doi)
        for name in ("scopus_eid", "wos_uid"):
            value = self.external_ids.get(name)
            if value:
                return (name, str(value).strip().lower())
        first_author = first_author_key(self.authors)
        return ("fallback", self.normalized_title, self.publication_year, first_author)


def normalize_doi(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix) :].strip()
    return normalized or None


def normalize_title(value: str) -> str:
    plain = unicodedata.normalize("NFKC", value or "").lower().strip()
    plain = re.sub(r"[^\w\s]", " ", plain, flags=re.UNICODE)
    return re.sub(r"\s+", " ", plain).strip()


def first_author_key(authors: list[Any]) -> str:
    if not authors:
        return ""
    author = authors[0]
    if isinstance(author, dict):
        value = author.get("name") or author.get("display_name") or author.get("full_name") or ""
    else:
        value = str(author)
    return normalize_title(str(value))


def find_public_pdf_url(value: Any, key_hint: str = "") -> str | None:
    """Lấy liên kết PDF HTTPS khi metadata nguồn có cung cấp trực tiếp."""
    if isinstance(value, dict):
        for key, nested in value.items():
            found = find_public_pdf_url(nested, str(key))
            if found:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = find_public_pdf_url(nested, key_hint)
            if found:
                return found
    elif isinstance(value, str) and value.startswith("https://"):
        path = urlsplit(value).path.casefold()
        if path.endswith(".pdf") or "pdf" in key_hint.casefold():
            return value
    return None


def merge_documents(current: NormalizedDocument, incoming: NormalizedDocument) -> NormalizedDocument:
    merged = current.model_copy(deep=True)
    scalar_fields = (
        "doi",
        "abstract",
        "document_type",
        "language",
        "publication_date",
        "publication_year",
        "journal",
        "issn",
        "publisher",
        "volume",
        "issue",
        "pages_or_article",
        "quartile",
        "quartile_info",
        "citation_info",
        "source_url",
        "pdf_url",
    )
    for name in scalar_fields:
        if getattr(merged, name) in (None, "", {}, []) and getattr(incoming, name) not in (None, "", {}, []):
            setattr(merged, name, getattr(incoming, name))
    if len(incoming.title) > len(merged.title):
        merged.title = incoming.title
        merged.normalized_title = incoming.normalized_title
    if len(incoming.authors) > len(merged.authors):
        merged.authors = incoming.authors
    merged.keywords = list(dict.fromkeys([*merged.keywords, *incoming.keywords]))
    merged.external_ids = {**merged.external_ids, **incoming.external_ids}
    merged.source_data = {**merged.source_data, **incoming.source_data}
    merged.open_access = merged.open_access or incoming.open_access
    return merged


def deduplicate_documents(documents: list[NormalizedDocument]) -> list[NormalizedDocument]:
    by_key: dict[tuple[Any, ...], NormalizedDocument] = {}
    for document in documents:
        key = document.dedup_key()
        if key in by_key:
            by_key[key] = merge_documents(by_key[key], document)
        else:
            by_key[key] = document
    return list(by_key.values())


def matches_criteria(document: NormalizedDocument, criteria: SearchCriteria) -> bool:
    if criteria.document_type:
        if not document.document_type or criteria.document_type.casefold() not in document.document_type.casefold():
            return False
    if criteria.language:
        if not document.language or criteria.language.casefold() not in document.language.casefold():
            return False
    if criteria.open_access and not document.open_access:
        return False
    if criteria.quartiles and document.quartile not in criteria.quartiles:
        return False
    return True


class SearchProvider(ABC):
    @abstractmethod
    def search(self, criteria: SearchCriteria) -> list[NormalizedDocument]:
        raise NotImplementedError
