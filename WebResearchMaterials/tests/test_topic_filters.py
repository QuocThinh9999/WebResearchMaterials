from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers.topics import OptionalYearQuery, _filters, _topic_filters


app = FastAPI()


@app.get("/loc")
def filter_year(year_from: OptionalYearQuery = None, year_to: OptionalYearQuery = None):
    return {"year_from": year_from, "year_to": year_to}


client = TestClient(app)


def test_empty_year_query_is_treated_as_missing():
    response = client.get("/loc?year_from=&year_to=")

    assert response.status_code == 200
    assert response.json() == {"year_from": None, "year_to": None}


def test_year_query_is_parsed_and_validated():
    response = client.get("/loc?year_from=2020&year_to=2026")

    assert response.status_code == 200
    assert response.json() == {"year_from": 2020, "year_to": 2026}
    assert client.get("/loc?year_from=999").status_code == 422


def test_without_status_filter_is_recognized():
    filters = _filters(None, "without_status", None, None, None, None, "added_desc")

    assert filters.without_status
    assert filters.status_id is None


def test_topic_filters_are_normalized():
    creator_id = "c184f9e1-bb32-4ea8-8eb8-36d4bdf15514"
    status_id = "736b0dc7-730f-438f-8ff4-7c86771221d3"

    filters = _topic_filters("  trí tuệ nhân tạo  ", creator_id, status_id)

    assert filters.keyword == "trí tuệ nhân tạo"
    assert str(filters.creator_id) == creator_id
    assert str(filters.status_id) == status_id
