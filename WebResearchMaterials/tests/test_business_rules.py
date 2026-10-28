from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from app.services.document_status import append_status_if_changed
from app.services.providers import SearchCriteria
from app.services.search import perform_search


class EmptyProvider:
    def search(self, criteria: SearchCriteria) -> list:
        return []


def test_ket_qua_rong_khong_tao_lan_tim_kiem() -> None:
    db = MagicMock()
    db.scalar.return_value = SimpleNamespace(id=uuid4())

    summary = perform_search(
        db,
        topic_id=uuid4(),
        user_id=uuid4(),
        criteria=SearchCriteria(keywords="online banking", sources=["scopus"]),
        providers=[EmptyProvider()],
    )

    assert summary.saved is False
    assert summary.search_number is None
    assert summary.total_results == 0
    db.add.assert_not_called()
    db.flush.assert_not_called()


def test_trang_thai_giong_hien_tai_khong_tao_lich_su() -> None:
    db = MagicMock()
    status_id = uuid4()
    db.scalar.side_effect = [
        SimpleNamespace(id=uuid4()),
        SimpleNamespace(id_trang_thai=status_id),
    ]

    result = append_status_if_changed(
        db,
        link_id=uuid4(),
        user_id=uuid4(),
        status_id=status_id,
        reason="Không thay đổi",
    )

    assert result is None
    db.add.assert_not_called()
    db.flush.assert_not_called()
