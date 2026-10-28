import httpx
import pytest

from app.services.providers import ProviderConfigurationError, ProviderError, SearchCriteria, deduplicate_documents, matches_criteria, split_search_terms
from app.services.providers.scopus import ScopusProvider, _source_quartile, build_scopus_query
from app.services.providers.wos import WebOfScienceProvider, build_wos_query


def criteria(sources):
    return SearchCriteria(keywords="artificial intelligence", sources=sources)


def test_split_keywords_normalizes_spacing_case_and_duplicates():
    value = "  Online   Banking, customer experience; CUSTOMER EXPERIENCE\ncustomer satisfaction  "
    assert split_search_terms(value) == ["Online Banking", "customer experience", "customer satisfaction"]


def test_comma_separated_keywords_use_or_for_both_sources():
    search = SearchCriteria(
        keywords="online banking, customer experience, customer satisfaction",
        sources=["scopus", "wos"],
    )
    assert build_scopus_query(search) == (
        '(TITLE-ABS-KEY("online banking") OR TITLE-ABS-KEY("customer experience") '
        'OR TITLE-ABS-KEY("customer satisfaction"))'
    )
    assert build_wos_query(search) == (
        'TS=("online banking" OR "customer experience" OR "customer satisfaction")'
    )


def test_comma_separated_required_keywords_use_and_for_both_sources():
    search = SearchCriteria(
        keywords_and="online banking, customer experience, customer satisfaction",
        sources=["scopus", "wos"],
    )
    assert build_scopus_query(search) == (
        '(TITLE-ABS-KEY("online banking") AND TITLE-ABS-KEY("customer experience") '
        'AND TITLE-ABS-KEY("customer satisfaction"))'
    )
    assert build_wos_query(search) == (
        'TS=("online banking" AND "customer experience" AND "customer satisfaction")'
    )


def test_or_and_keywords_can_be_combined():
    search = SearchCriteria(
        keywords="online banking, digital banking",
        keywords_and="customer satisfaction, loyalty",
        sources=["scopus", "wos"],
    )
    assert build_scopus_query(search) == (
        '(TITLE-ABS-KEY("online banking") OR TITLE-ABS-KEY("digital banking")) AND '
        '(TITLE-ABS-KEY("customer satisfaction") AND TITLE-ABS-KEY("loyalty"))'
    )
    assert build_wos_query(search) == (
        'TS=("online banking" OR "digital banking") AND '
        'TS=("customer satisfaction" AND "loyalty")'
    )


def test_scopus_provider_with_mock_http():
    def handler(request: httpx.Request):
        assert request.headers["X-ELS-APIKey"] == "test-key"
        return httpx.Response(
            200,
            json={
                "search-results": {
                    "opensearch:totalResults": "1",
                    "entry": [
                        {
                            "dc:title": "AI in Business",
                            "prism:doi": "10.1/ABC",
                            "eid": "2-s2.0-1",
                            "dc:creator": "Author A",
                            "prism:coverDate": "2025-01-02",
                            "link": [
                                {"@ref": "scopus", "@href": "https://example.test/article"},
                                {"@ref": "full-text", "@href": "https://example.test/article.pdf"},
                            ],
                        }
                    ],
                }
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = ScopusProvider("test-key", "https://example.test/scopus", 5, 25, client=client)
    results = provider.search(criteria(["scopus"]))
    assert len(results) == 1
    assert results[0].doi == "10.1/abc"
    assert results[0].external_ids["scopus_eid"] == "2-s2.0-1"
    assert results[0].pdf_url == "https://example.test/article.pdf"


@pytest.mark.parametrize(
    ("percentile", "expected"),
    [(75, "Q1"), (74, "Q2"), (50, "Q2"), (49, "Q3"), (25, "Q3"), (24, "Q4")],
)
def test_citescore_percentile_to_quartile(percentile, expected):
    payload = {
        "serial-metadata-response": {"entry": [{
            "citeScoreYearInfoList": {
                "citeScoreCurrentMetricYear": "2025",
                "citeScoreYearInfo": [
                    {
                        "@year": "2026", "@status": "In-Progress",
                        "citeScoreInformationList": [{"citeScoreInfo": [{"docType": "all", "citeScoreSubjectRank": [{"percentile": "99"}]}]}],
                    },
                    {
                        "@year": "2025", "@status": "Complete",
                        "citeScoreInformationList": [{"citeScoreInfo": [
                            {"docType": "ar", "citeScoreSubjectRank": [{"percentile": "99"}]},
                            {"docType": "all", "citeScoreSubjectRank": [
                                {"subjectCode": "1000", "percentile": str(percentile)},
                                {"subjectCode": "2000", "percentile": "1"},
                            ]},
                        ]}],
                    },
                ],
            },
        }]},
    }
    quartile, info = _source_quartile(payload)
    assert quartile == expected
    assert info["year"] == 2025
    assert info["percentile"] == percentile


def test_scopus_quartile_filter_looks_up_serial_once_and_rejects_unknown():
    serial_requests = []

    def handler(request: httpx.Request):
        if request.url.path == "/scopus":
            return httpx.Response(200, json={"search-results": {
                "opensearch:totalResults": "3",
                "entry": [
                    {"dc:title": "Bài Q1 A", "prism:issn": "1234-5678"},
                    {"dc:title": "Bài Q1 B", "prism:issn": "1234-5678"},
                    {"dc:title": "Bài chưa rõ Q"},
                ],
            }})
        serial_requests.append(str(request.url))
        return httpx.Response(200, json={"serial-metadata-response": {"entry": [{
            "citeScoreYearInfoList": {"citeScoreCurrentMetricYear": "2025", "citeScoreYearInfo": [{
                "@year": "2025", "@status": "Complete",
                "citeScoreInformationList": [{"citeScoreInfo": [{
                    "docType": "all", "citeScoreSubjectRank": [{"percentile": "88", "subjectCode": "1000"}],
                }]}],
            }]},
        }]}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = ScopusProvider("key", "https://example.test/scopus", 5, 10, client=client)
    selected = SearchCriteria(keywords="AI", sources=["scopus"], quartiles=["Q1"])
    results = provider.search(selected)
    assert [item.title for item in results if matches_criteria(item, selected)] == ["Bài Q1 A", "Bài Q1 B"]
    assert len(serial_requests) == 1
    assert "/content/serial/title/issn/12345678" in serial_requests[0]
    assert "view=CITESCORE" in serial_requests[0]

    serial_requests.clear()
    unfiltered = SearchCriteria(keywords="AI", sources=["scopus"])
    assert len(provider.search(unfiltered)) == 3
    assert serial_requests == []


def test_scopus_quartile_lookup_permission_error_is_visible():
    def handler(request: httpx.Request):
        if request.url.path == "/scopus":
            return httpx.Response(200, json={"search-results": {
                "opensearch:totalResults": "1",
                "entry": [{"dc:title": "Bài báo", "prism:issn": "1234-5678"}],
            }})
        return httpx.Response(403)

    provider = ScopusProvider(
        "key", "https://example.test/scopus", 5, 10,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ProviderError, match="quartile"):
        provider.search(SearchCriteria(keywords="AI", sources=["scopus"], quartiles=["Q1"]))


def test_wos_provider_with_mock_http():
    def handler(request: httpx.Request):
        assert request.headers["X-ApiKey"] == "wos-key"
        return httpx.Response(
            200,
            json={
                "QueryResult": {"RecordsFound": 1},
                "Data": {
                    "Records": {
                        "records": [
                            {
                                "UID": "WOS:1",
                                "static_data": {
                                    "summary": {
                                        "pub_info": {"pubyear": 2024, "sortdate": "2024-02-03"},
                                        "titles": {"title": [{"type": "item", "content": "AI Governance"}]},
                                        "names": {"name": [{"role": "author", "display_name": "Author B"}]},
                                    },
                                    "fullrecord_metadata": {},
                                },
                                "dynamic_data": {
                                    "cluster_related": {"identifiers": {"identifier": [{"type": "doi", "value": "10.2/XYZ"}]}}
                                },
                            }
                        ]
                    }
                },
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = WebOfScienceProvider("wos-key", "https://example.test/wos", 5, 100, client=client)
    results = provider.search(criteria(["wos"]))
    assert len(results) == 1
    assert results[0].external_ids["wos_uid"] == "WOS:1"
    assert results[0].doi == "10.2/xyz"


def test_search_both_sources_with_mock_http_and_merge_duplicate():
    def scopus_handler(_: httpx.Request):
        return httpx.Response(
            200,
            json={
                "search-results": {
                    "opensearch:totalResults": "1",
                    "entry": [{"dc:title": "Shared paper", "prism:doi": "10.9/shared", "eid": "EID-9"}],
                }
            },
        )

    def wos_handler(_: httpx.Request):
        return httpx.Response(
            200,
            json={
                "QueryResult": {"RecordsFound": 1},
                "Data": {
                    "Records": {
                        "records": [
                            {
                                "UID": "WOS:9",
                                "static_data": {
                                    "summary": {"titles": {"title": [{"type": "item", "content": "Shared paper"}]}},
                                    "fullrecord_metadata": {},
                                },
                                "dynamic_data": {
                                    "cluster_related": {"identifiers": {"identifier": [{"type": "doi", "value": "10.9/shared"}]}}
                                },
                            }
                        ]
                    }
                },
            },
        )

    search = criteria(["scopus", "wos"])
    scopus = ScopusProvider(
        "key", "https://example.test/scopus", 5, 10, client=httpx.Client(transport=httpx.MockTransport(scopus_handler))
    )
    wos = WebOfScienceProvider(
        "key", "https://example.test/wos", 5, 10, client=httpx.Client(transport=httpx.MockTransport(wos_handler))
    )
    merged = deduplicate_documents([*scopus.search(search), *wos.search(search)])
    assert len(merged) == 1
    assert merged[0].external_ids == {"scopus_eid": "EID-9", "wos_uid": "WOS:9"}


@pytest.mark.parametrize(
    "provider,source",
    [
        (ScopusProvider(None, "https://example.test", 5, 10), "scopus"),
        (WebOfScienceProvider(None, "https://example.test", 5, 10), "wos"),
    ],
)
def test_missing_api_key_is_clear(provider, source):
    with pytest.raises(ProviderConfigurationError):
        provider.search(criteria([source]))
