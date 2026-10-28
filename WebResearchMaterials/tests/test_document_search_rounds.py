from app.routers.topics import _filters


def test_multiple_search_numbers_are_normalized() -> None:
    filters = _filters(
        keyword=None,
        status_id=None,
        year_from=None,
        year_to=None,
        quartile=None,
        author=None,
        sort="added_desc",
        search_numbers=[1, 3, 1, -2],
    )

    assert filters.search_numbers == (1, 3)
