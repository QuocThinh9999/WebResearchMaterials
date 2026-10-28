from __future__ import annotations

import re
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.database import get_db
from app.main import app
from app.models import (
    DeTai,
    DeTaiTaiLieu,
    DeTaiTaiLieuLanTimKiem,
    LanTimKiem,
    NguoiDung,
    TaiLieu,
    ThongKeTrangThaiTaiLieu,
    TrangThaiDeTai,
    TrangThaiTaiLieu,
    TrangThaiTaiLieuDanhMuc,
    TrangThaiTaiLieuHienTai,
)
from app.repositories.documents import DocumentFilters, list_documents
from app.repositories.topics import TopicFilters, list_topics
from app.services.providers import NormalizedDocument, SearchCriteria, SearchProvider, normalize_title
from app.services.search import perform_search
from app.services.security import hash_password
from app.services.document_status import append_status_if_changed


pytestmark = pytest.mark.integration


class StubProvider(SearchProvider):
    def __init__(self, documents):
        self.documents = documents

    def search(self, criteria):
        return self.documents


@pytest.fixture
def seeded(db):
    suffix = uuid.uuid4().hex[:10]
    user = NguoiDung(
        ten_dang_nhap=f"test_{suffix}",
        ten_nguoi_dung="Người kiểm thử",
        mat_khau_hash=hash_password("mat-khau-test"),
    )
    topic_status = TrangThaiDeTai(ten_trang_thai=f"Đang test {suffix}", mau_trang_thai="#3B82F6")
    doc_status = TrangThaiTaiLieuDanhMuc(ten_trang_thai=f"Chấp nhận test {suffix}", mau_trang_thai="#22C55E")
    db.add_all([user, topic_status, doc_status])
    db.commit()
    return user, topic_status, doc_status


def _csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match
    return match.group(1)


def test_login_right_and_wrong(db, seeded):
    user, _, _ = seeded

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            token = _csrf(client.get("/dang-nhap").text)
            wrong = client.post(
                "/dang-nhap",
                data={"username": user.ten_dang_nhap, "password": "sai-mat-khau", "csrf_token": token},
            )
            assert wrong.status_code == 400
            token = _csrf(wrong.text)
            right = client.post(
                "/dang-nhap",
                data={"username": user.ten_dang_nhap, "password": "mat-khau-test", "csrf_token": token},
                follow_redirects=False,
            )
            assert right.status_code == 303
            assert right.headers["location"] == "/de-tai"
    finally:
        app.dependency_overrides.clear()


def test_topic_crud_and_ten_row_pagination(db, seeded):
    user, status, _ = seeded
    topics = [
        DeTai(ten_de_tai=f"Đề tài {index} {uuid.uuid4().hex}", id_trang_thai=status.id, id_nguoi_tao=user.id)
        for index in range(11)
    ]
    db.add_all(topics)
    db.commit()
    first, has_more = list_topics(db, 0)
    second, has_more_second = list_topics(db, 10)
    assert len(first) == 10 and has_more
    assert len(second) >= 1
    filtered, filtered_has_more = list_topics(
        db,
        filters=TopicFilters(keyword=topics[0].ten_de_tai, creator_id=user.id, status_id=status.id),
    )
    assert [row.topic.id for row in filtered] == [topics[0].id]
    assert not filtered_has_more
    topic = topics[0]
    topic.ten_de_tai = "Tên đã sửa"
    db.commit()
    assert db.get(DeTai, topic.id).ten_de_tai == "Tên đã sửa"
    db.delete(topic)
    db.commit()
    assert db.get(DeTai, topic.id) is None


def test_two_searches_keep_two_detection_links(db, seeded):
    user, status, _ = seeded
    topic = DeTai(ten_de_tai="Đề tài tìm kiếm", id_trang_thai=status.id, id_nguoi_tao=user.id)
    db.add(topic)
    db.commit()
    doc = NormalizedDocument(
        doi="10.1000/duplicate",
        title="Duplicate paper",
        normalized_title=normalize_title("Duplicate paper"),
        authors=[{"name": "Author A"}],
        publication_year=2025,
        external_ids={"scopus_eid": "EID-1"},
        source_data={"scopus": {"id": 1}},
    )
    criteria = SearchCriteria(keywords="duplicate", sources=["scopus"])
    perform_search(db, topic.id, user.id, criteria, [StubProvider([doc])])
    db.commit()
    perform_search(db, topic.id, user.id, criteria, [StubProvider([doc])])
    db.commit()
    assert db.scalar(select(func.count(TaiLieu.id)).where(TaiLieu.doi == "10.1000/duplicate")) == 1
    assert db.scalar(select(func.count(LanTimKiem.id)).where(LanTimKiem.id_de_tai == topic.id)) == 2
    link_id = db.scalar(select(DeTaiTaiLieu.id).where(DeTaiTaiLieu.id_de_tai == topic.id))
    assert db.scalar(
        select(func.count()).select_from(DeTaiTaiLieuLanTimKiem).where(
            DeTaiTaiLieuLanTimKiem.id_de_tai_tai_lieu == link_id
        )
    ) == 2
    rows, _ = list_documents(db, topic.id, DocumentFilters(search_numbers=(1, 3)))
    assert len(rows) == 1
    assert rows[0].search_numbers == [1, 2]
    rows_outside_search, _ = list_documents(db, topic.id, DocumentFilters(search_numbers=(3,)))
    assert rows_outside_search == []


def test_empty_search_is_not_saved_and_next_success_is_first(db, seeded):
    user, status, _ = seeded
    topic = DeTai(ten_de_tai="Đề tài không lưu lần rỗng", id_trang_thai=status.id, id_nguoi_tao=user.id)
    db.add(topic)
    db.commit()
    criteria = SearchCriteria(keywords="không có kết quả", sources=["scopus"])
    empty_summary = perform_search(db, topic.id, user.id, criteria, [StubProvider([])])
    db.commit()
    assert not empty_summary.saved
    assert empty_summary.search_number is None
    assert db.scalar(select(func.count(LanTimKiem.id)).where(LanTimKiem.id_de_tai == topic.id)) == 0

    document = NormalizedDocument(
        doi="10.1000/first-after-empty",
        title="First result",
        normalized_title=normalize_title("First result"),
        authors=[{"name": "Author"}],
        publication_year=2026,
    )
    first_summary = perform_search(db, topic.id, user.id, criteria, [StubProvider([document])])
    db.commit()
    assert first_summary.saved
    assert first_summary.search_number == 1
    assert db.scalar(select(func.count(LanTimKiem.id)).where(LanTimKiem.id_de_tai == topic.id)) == 1


def test_same_consecutive_status_is_not_inserted(db, seeded):
    user, topic_status, document_status = seeded
    topic = DeTai(ten_de_tai="Đề tài chống trạng thái trùng", id_trang_thai=topic_status.id, id_nguoi_tao=user.id)
    document = TaiLieu(
        tieu_de="Tài liệu chống trạng thái trùng",
        tieu_de_chuan_hoa=normalize_title("Tài liệu chống trạng thái trùng"),
        tac_gia=[],
        tu_khoa=[],
        ma_ngoai={},
        du_lieu_nguon={},
    )
    db.add_all([topic, document])
    db.flush()
    link = DeTaiTaiLieu(id_de_tai=topic.id, id_tai_lieu=document.id)
    db.add(link)
    db.commit()

    first = append_status_if_changed(db, link.id, user.id, document_status.id, "Lần đầu")
    db.commit()
    second = append_status_if_changed(db, link.id, user.id, document_status.id, "Không ghi lần hai")
    db.commit()
    assert first is not None
    assert second is None
    assert db.scalar(select(func.count(TrangThaiTaiLieu.id)).where(TrangThaiTaiLieu.id_de_tai_tai_lieu == link.id)) == 1


def test_current_state_statistics_history_and_filters(db, seeded):
    user, topic_status, doc_status = seeded
    second_user = NguoiDung(
        ten_dang_nhap=f"second_{uuid.uuid4().hex}",
        ten_nguoi_dung="Người thứ hai",
        mat_khau_hash=hash_password("mat-khau-test"),
    )
    topic = DeTai(ten_de_tai="Đề tài trạng thái", id_trang_thai=topic_status.id, id_nguoi_tao=user.id)
    document = TaiLieu(
        tieu_de="Tài liệu lọc AI",
        tieu_de_chuan_hoa=normalize_title("Tài liệu lọc AI"),
        tac_gia=[{"name": "Tác giả A"}],
        tu_khoa=["AI"],
        nam_xuat_ban=2026,
        quartile="Q1",
        ma_ngoai={},
        du_lieu_nguon={},
    )
    document_without_status = TaiLieu(
        tieu_de="Tài liệu chưa có trạng thái",
        tieu_de_chuan_hoa=normalize_title("Tài liệu chưa có trạng thái"),
        tac_gia=[],
        tu_khoa=[],
        ma_ngoai={},
        du_lieu_nguon={},
    )
    db.add_all([second_user, topic, document, document_without_status])
    db.flush()
    link = DeTaiTaiLieu(id_de_tai=topic.id, id_tai_lieu=document.id)
    link_without_status = DeTaiTaiLieu(id_de_tai=topic.id, id_tai_lieu=document_without_status.id)
    db.add_all([link, link_without_status])
    db.flush()
    db.add_all(
        [
            TrangThaiTaiLieu(id_de_tai_tai_lieu=link.id, id_trang_thai=doc_status.id, id_nguoi_dung=user.id, ly_do="Lần đầu"),
            TrangThaiTaiLieu(id_de_tai_tai_lieu=link.id, id_trang_thai=doc_status.id, id_nguoi_dung=user.id, ly_do="Lần mới"),
            TrangThaiTaiLieu(id_de_tai_tai_lieu=link.id, id_trang_thai=doc_status.id, id_nguoi_dung=second_user.id),
        ]
    )
    db.commit()
    current = list(db.scalars(select(TrangThaiTaiLieuHienTai).where(TrangThaiTaiLieuHienTai.id_de_tai_tai_lieu == link.id)))
    assert len(current) == 2
    assert db.scalar(select(func.count(TrangThaiTaiLieu.id)).where(TrangThaiTaiLieu.id_de_tai_tai_lieu == link.id)) == 3
    stat = db.scalar(select(ThongKeTrangThaiTaiLieu).where(ThongKeTrangThaiTaiLieu.id_de_tai_tai_lieu == link.id))
    assert stat.so_nguoi == 2
    rows, _ = list_documents(
        db,
        topic.id,
        DocumentFilters(keyword="AI", year_from=2026, year_to=2026, quartile="Q1", author="Tác giả", sort="year_desc"),
    )
    assert len(rows) == 1
    assert rows[0].status_tags[0].count == 2
    rows_without_status, _ = list_documents(db, topic.id, DocumentFilters(without_status=True))
    assert [row.document.id for row in rows_without_status] == [document_without_status.id]
