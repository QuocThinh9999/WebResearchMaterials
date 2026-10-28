from app.services.providers.base import (
    NormalizedDocument,
    ProviderConfigurationError,
    ProviderError,
    SearchCriteria,
    SearchProvider,
    deduplicate_documents,
    merge_documents,
    matches_criteria,
    normalize_doi,
    normalize_title,
    split_search_terms,
)
from app.services.providers.scopus import ScopusProvider
from app.services.providers.wos import WebOfScienceProvider

__all__ = [
    "NormalizedDocument",
    "ProviderConfigurationError",
    "ProviderError",
    "ScopusProvider",
    "SearchCriteria",
    "SearchProvider",
    "WebOfScienceProvider",
    "deduplicate_documents",
    "merge_documents",
    "matches_criteria",
    "normalize_doi",
    "normalize_title",
    "split_search_terms",
]
