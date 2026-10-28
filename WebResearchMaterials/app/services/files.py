from __future__ import annotations

import hashlib
import mimetypes
import uuid
from pathlib import Path

from fastapi import UploadFile
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import TepTin


PDF_MIME_TYPES = {"application/pdf"}
IMAGE_MIME_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}


class InvalidUpload(ValueError):
    pass


def is_pdf_signature(content: bytes) -> bool:
    return content.startswith(b"%PDF-")


def detected_image_mime(content: bytes) -> str | None:
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(content) >= 12 and content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return "image/webp"
    return None


def validate_file(filename: str | None, content_type: str | None, content: bytes, kind: str, max_bytes: int) -> str:
    if not filename:
        raise InvalidUpload("Tệp tải lên phải có tên.")
    if not content:
        raise InvalidUpload("Tệp tải lên đang trống.")
    if len(content) > max_bytes:
        raise InvalidUpload(f"Tệp vượt quá giới hạn {max_bytes // (1024 * 1024)} MB.")
    extension = Path(filename).suffix.lower()
    if kind == "pdf":
        if extension != ".pdf" or content_type not in PDF_MIME_TYPES or not is_pdf_signature(content):
            raise InvalidUpload("Tệp PDF không hợp lệ. Hệ thống kiểm tra phần mở rộng, MIME type và chữ ký tệp.")
        return "application/pdf"
    if kind == "image":
        detected = detected_image_mime(content)
        if extension not in IMAGE_EXTENSIONS or content_type not in IMAGE_MIME_TYPES or detected != content_type:
            raise InvalidUpload("Ảnh không hợp lệ. Chỉ chấp nhận JPEG, PNG, GIF hoặc WebP đúng định dạng.")
        return detected
    raise InvalidUpload("Loại tệp tải lên không được hỗ trợ.")


async def read_and_validate(upload: UploadFile, kind: str, settings: Settings | None = None) -> tuple[bytes, str]:
    settings = settings or get_settings()
    max_bytes = settings.max_upload_mb * 1024 * 1024
    content = await upload.read(max_bytes + 1)
    mime_type = validate_file(
        upload.filename,
        upload.content_type,
        content,
        kind,
        max_bytes,
    )
    return content, mime_type


def save_file_record(
    db: Session,
    original_name: str,
    content: bytes,
    mime_type: str,
    uploader_id: uuid.UUID,
    category: str,
    settings: Settings | None = None,
) -> tuple[TepTin, Path | None]:
    settings = settings or get_settings()
    digest = hashlib.sha256(content).hexdigest()
    existing = db.scalar(select(TepTin).where(func.lower(TepTin.ma_bam_sha256) == digest))
    if existing:
        root = settings.upload_path
        existing_path = (root / existing.duong_dan_luu).resolve()
        if root not in existing_path.parents:
            raise InvalidUpload("Đường dẫn tệp trùng lặp không hợp lệ.")
        if existing_path.is_file():
            return existing, None
        existing_path.parent.mkdir(parents=True, exist_ok=True)
        existing_path.write_bytes(content)
        existing.kich_thuoc_byte = len(content)
        existing.mime_type = mime_type
        db.flush()
        return existing, existing_path

    extension = mimetypes.guess_extension(mime_type) or Path(original_name).suffix.lower()
    if mime_type == "image/jpeg":
        extension = ".jpg"
    root = settings.upload_path
    target_dir = (root / category).resolve()
    if root not in target_dir.parents and target_dir != root:
        raise InvalidUpload("Thư mục lưu tệp không hợp lệ.")
    target_dir.mkdir(parents=True, exist_ok=True)
    stored_name = f"{uuid.uuid4().hex}{extension}"
    target = (target_dir / stored_name).resolve()
    if target_dir not in target.parents:
        raise InvalidUpload("Đường dẫn lưu tệp không hợp lệ.")
    relative_path = target.relative_to(root).as_posix()
    record = TepTin(
        ten_file_goc=Path(original_name).name[:255],
        duong_dan_luu=relative_path,
        mime_type=mime_type,
        kich_thuoc_byte=len(content),
        ma_bam_sha256=digest,
        id_nguoi_tai_len=uploader_id,
    )
    try:
        # Savepoint chỉ hoàn tác lần chèn file đang tranh chấp, không ảnh hưởng dữ liệu job khác.
        with db.begin_nested():
            db.add(record)
            db.flush()
    except IntegrityError:
        existing = db.scalar(select(TepTin).where(func.lower(TepTin.ma_bam_sha256) == digest))
        if existing is None:
            raise
        existing_path = (root / existing.duong_dan_luu).resolve()
        if root not in existing_path.parents:
            raise InvalidUpload("Đường dẫn tệp trùng lặp không hợp lệ.")
        if not existing_path.is_file():
            existing_path.parent.mkdir(parents=True, exist_ok=True)
            existing_path.write_bytes(content)
        return existing, None

    try:
        target.write_bytes(content)
    except OSError:
        target.unlink(missing_ok=True)
        raise
    return record, target


def resolve_stored_path(record: TepTin, settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    root = settings.upload_path
    target = (root / record.duong_dan_luu).resolve()
    if root not in target.parents:
        raise FileNotFoundError("Đường dẫn tệp không hợp lệ.")
    if not target.is_file():
        raise FileNotFoundError("Không tìm thấy tệp trên hệ thống lưu trữ.")
    return target
