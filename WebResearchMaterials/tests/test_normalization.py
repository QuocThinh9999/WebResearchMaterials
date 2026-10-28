from app.services.providers import NormalizedDocument, deduplicate_documents, normalize_doi, normalize_title


def document(**values):
    defaults = {
        "title": "Ứng dụng AI trong kinh doanh",
        "normalized_title": normalize_title("Ứng dụng AI trong kinh doanh"),
        "authors": [{"name": "Nguyễn Văn A"}],
        "publication_year": 2026,
    }
    defaults.update(values)
    return NormalizedDocument(**defaults)


def test_normalize_doi():
    assert normalize_doi(" HTTPS://DOI.ORG/10.1000/ABC ") == "10.1000/abc"


def test_deduplicate_by_doi_and_merge_identifiers():
    items = deduplicate_documents(
        [
            document(doi="10.1000/ABC", external_ids={"scopus_eid": "2-s2.0-1"}, source_data={"scopus": {"x": 1}}),
            document(doi="https://doi.org/10.1000/abc", external_ids={"wos_uid": "WOS:1"}, source_data={"wos": {"y": 2}}),
        ]
    )
    assert len(items) == 1
    assert items[0].external_ids == {"scopus_eid": "2-s2.0-1", "wos_uid": "WOS:1"}
    assert set(items[0].source_data) == {"scopus", "wos"}


def test_deduplicate_fallback_title_year_first_author():
    assert len(deduplicate_documents([document(), document(title="Ứng dụng AI trong kinh doanh!")])) == 1

