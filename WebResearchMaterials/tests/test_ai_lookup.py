from __future__ import annotations

import datetime as dt
from uuid import uuid4
from types import SimpleNamespace

from sqlalchemy.dialects import postgresql

import app.database as database_module
from app.models import TaiLieu
from app.repositories.ai_evaluations import (
    AILookupProgress,
    claim_next_ai_lookup_job,
    get_ai_lookup_elapsed_ms,
    list_ai_lookup_results,
    update_ai_lookup_progress,
)
import app.repositories.ai_evaluations as ai_evaluations
from app.routers.ai_search import redirect_old_ai_route
from app.services.ai.evaluator import calculate_relevance_score
from app.services.ai.hard_rules import evaluate_hard_rules, prohibited_publisher
from app.services.ai.schemas import KetQuaDieuKienAI, PhanTichYeuCauAI


def empty_intent() -> PhanTichYeuCauAI:
    return PhanTichYeuCauAI(
        chu_de_bat_buoc=[], chu_de_tuy_chon=[], tu_khoa_tim_kiem_tieng_anh=[], tu_khoa_loai_tru=[],
        tieu_de_loai_tru=[], doi_loai_tru=[], tac_gia_bat_buoc=[], tac_gia_loai_tru=[],
        nha_xuat_ban_bat_buoc=[], nha_xuat_ban_loai_tru=[], nam_tu=None, nam_den=None,
        loai_tai_lieu=None, ngon_ngu=None, quartile=[], chi_truy_cap_mo=False,
        dien_giai_ngan_gon="", ngon_ngu_yeu_cau="vi", ngon_ngu_tai_lieu_cho_phep=[],
        khai_niem_goc_tieng_viet=[], khai_niem_tim_kiem_tieng_anh=[], nhom_tu_dong_nghia=[],
        cau_truy_van_scopus="", cau_truy_van_wos="",
    )


def condition(*, role: str, sections: list[str], status: str = "dat", score: float = 90) -> KetQuaDieuKienAI:
    return KetQuaDieuKienAI(
        noi_dung="Transformational Leadership phải tham gia nghiên cứu",
        loai="khai_niem_trong_mo_hinh",
        trong_so=3,
        trang_thai=status,
        diem=score,
        vai_tro_khai_niem=role,
        phan_muc_pdf=sections,
        bang_chung=[],
        giai_thich_tieng_viet="Kiểm thử",
    )


def test_only_literature_review_is_scored_below_real_model_usage():
    mentioned_score, mentioned = calculate_relevance_score(
        [condition(role="chỉ nhắc đến", sections=["Literature Review"])], 70
    )
    model_score, model = calculate_relevance_score(
        [condition(role="biến độc lập", sections=["Hypotheses", "Results"])], 70
    )
    assert mentioned[0].trang_thai == "khong_dat"
    assert mentioned_score < 70
    assert model[0].trang_thai == "dat"
    assert model_score > mentioned_score


def test_unknown_mandatory_condition_is_not_treated_as_passed():
    score, _ = calculate_relevance_score(
        [condition(role="không xác định", sections=[], status="khong_xac_dinh", score=80)], 70
    )
    assert score < 70


def test_prohibited_publishers_always_score_as_hard_rule_violation():
    for publisher in ("Frontiers", "Frontiers Media SA", "MDPI AG", "Hindawi Limited"):
        document = TaiLieu(tieu_de="Paper", tac_gia=[], tu_khoa=[], nha_xuat_ban=publisher, ma_ngoai={}, du_lieu_nguon={})
        assert prohibited_publisher(document)
        assert evaluate_hard_rules(document, empty_intent()).violations


def test_ai_lookup_claim_uses_skip_locked():
    class CapturingSession:
        statement = None

        def scalar(self, statement):
            self.statement = statement
            return None

    db = CapturingSession()
    assert claim_next_ai_lookup_job(db, 3) is None
    assert "FOR UPDATE SKIP LOCKED" in str(db.statement.compile(dialect=postgresql.dialect())).upper()

    lookup_id = uuid4()
    assert claim_next_ai_lookup_job(db, 3, lookup_id) is None
    filtered_sql = str(
        db.statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    ).upper()
    assert str(lookup_id).upper() in filtered_sql


def test_score_filter_is_strictly_greater_than():
    class CapturingSession:
        statement = None

        def execute(self, statement):
            self.statement = statement

            class Result:
                def all(self):
                    return []

            return Result()

    db = CapturingSession()
    assert list_ai_lookup_results(db, uuid4(), score_greater_than=50) == ([], False)
    sql = str(db.statement.compile(dialect=postgresql.dialect()))
    assert "diem_phu_hop >" in sql


def test_new_migration_is_idempotent_and_old_ai_table_is_not_dropped():
    sql = open("sql/nang_cap_tai_pdf_va_tra_cuu_ai.sql", encoding="utf-8").read().casefold()
    assert "add column if not exists" in sql
    assert "create table if not exists cong_viec_tai_pdf" in sql
    assert "create table if not exists lan_tra_cuu_ai" in sql
    assert "alter table lan_tra_cuu_ai\n    add column if not exists thoi_gian_bat_dau" in sql
    assert "add column if not exists thoi_gian_chay_ms bigint" in sql
    assert "create table if not exists ket_qua_tra_cuu_ai" in sql
    assert "drop table" not in sql


def test_database_migrations_run_automatically_with_advisory_lock(monkeypatch):
    calls = []

    class Cursor:
        def execute(self, statement, parameters=None):
            calls.append((statement, parameters))

        def close(self):
            calls.append(("cursor-close", None))

    class Connection:
        autocommit = False

        def cursor(self):
            return Cursor()

        def close(self):
            calls.append(("connection-close", None))

    connection = Connection()
    monkeypatch.setattr(
        database_module.engine,
        "raw_connection",
        lambda: connection,
    )
    database_module.apply_database_migrations()
    executed_sql = "\n".join(statement for statement, _ in calls if isinstance(statement, str))
    assert connection.autocommit is True
    assert "pg_advisory_lock" in executed_sql
    assert "CREATE TABLE IF NOT EXISTS lan_tra_cuu_ai" in executed_sql
    assert "pg_advisory_unlock" in executed_sql


def test_user_interface_no_longer_contains_old_ai_agent_name():
    for path in ("app/templates/base.html", "app/templates/ai_search.html", "README.md"):
        assert "Tìm kiếm AI Agent" not in open(path, encoding="utf-8").read()


def test_ai_lookup_elapsed_time_is_saved_and_does_not_keep_growing(monkeypatch):
    started = dt.datetime(2026, 9, 26, 12, tzinfo=dt.UTC)
    lookup = SimpleNamespace(
        thoi_gian_bat_dau=started,
        thoi_gian_hoan_thanh=None,
        thoi_gian_chay_ms=None,
        trang_thai="dang_xu_ly",
        so_tai_lieu_khong_pdf=0,
        so_tai_lieu_co_pdf=0,
    )
    assert get_ai_lookup_elapsed_ms(lookup, started + dt.timedelta(seconds=3)) == 3000
    lookup.thoi_gian_hoan_thanh = started + dt.timedelta(seconds=4)
    assert get_ai_lookup_elapsed_ms(lookup, started + dt.timedelta(hours=1)) == 4000

    progress = AILookupProgress(1, 0, 0, 1, 0, 0)
    monkeypatch.setattr(ai_evaluations, "get_ai_lookup_progress", lambda *_: progress)
    db = SimpleNamespace(get=lambda *_: lookup, flush=lambda: None)
    update_ai_lookup_progress(db, uuid4())
    update_ai_lookup_progress(db, uuid4())
    assert lookup.trang_thai == "hoan_thanh"
    assert lookup.thoi_gian_chay_ms == 4000
    assert get_ai_lookup_elapsed_ms(lookup, started + dt.timedelta(hours=2)) == 4000


def test_old_ai_route_redirects_to_new_route():
    response = redirect_old_ai_route(SimpleNamespace(url=SimpleNamespace(query="")), None)
    assert response.status_code == 307
    assert response.headers["location"] == "/tra-cuu-ai"
