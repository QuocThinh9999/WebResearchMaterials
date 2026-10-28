from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings


settings = get_settings()
engine = create_engine(settings.database_url, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)

MIGRATION_FILES = (
    "nang_cap_tim_kiem_ai.sql",
    "nang_cap_tai_pdf_va_tra_cuu_ai.sql",
)
MIGRATION_LOCK_ID = 2_026_092_501


def apply_database_migrations() -> None:
    """Tự động bổ sung schema còn thiếu trước khi web nhận yêu cầu."""
    migration_dir = Path(__file__).resolve().parents[1] / "sql"
    connection = engine.raw_connection()
    cursor = connection.cursor()
    try:
        connection.autocommit = True
        cursor.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_LOCK_ID,))
        for filename in MIGRATION_FILES:
            sql = (migration_dir / filename).read_text(encoding="utf-8")
            cursor.execute(sql)
    finally:
        try:
            cursor.execute("SELECT pg_advisory_unlock(%s)", (MIGRATION_LOCK_ID,))
        finally:
            cursor.close()
            connection.close()


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
