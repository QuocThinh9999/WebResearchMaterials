from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import BigInteger, Boolean, CheckConstraint, Date, DateTime, ForeignKey, ForeignKeyConstraint
from sqlalchemy import Index, Integer, Numeric, SmallInteger, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class TrangThaiDeTai(Base):
    __tablename__ = "dm_trang_thai_de_tai"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    ten_trang_thai: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    mau_trang_thai: Mapped[str] = mapped_column(String(9), nullable=False)
    ngay_tao: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"), nullable=False)


class TrangThaiTaiLieuDanhMuc(Base):
    __tablename__ = "dm_trang_thai_tai_lieu"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    ten_trang_thai: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    mau_trang_thai: Mapped[str] = mapped_column(String(9), nullable=False)
    ngay_tao: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"), nullable=False)


class NguoiDung(Base):
    __tablename__ = "nguoi_dung"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    ten_dang_nhap: Mapped[str] = mapped_column(String(100), nullable=False)
    ten_nguoi_dung: Mapped[str] = mapped_column(String(255), nullable=False)
    mat_khau_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    dang_hoat_dong: Mapped[bool] = mapped_column(Boolean, server_default=text("true"), nullable=False)
    ngay_tao: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"), nullable=False)
    ngay_cap_nhat: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"), nullable=False)


class DeTai(Base):
    __tablename__ = "de_tai"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    ten_de_tai: Mapped[str] = mapped_column(String(500), nullable=False)
    id_trang_thai: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("dm_trang_thai_de_tai.id", ondelete="RESTRICT"), nullable=False
    )
    mo_ta: Mapped[str | None] = mapped_column(Text)
    id_nguoi_tao: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("nguoi_dung.id", ondelete="RESTRICT"), nullable=False
    )
    ngay_tao: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"), nullable=False)
    ngay_cap_nhat: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"), nullable=False)


class TepTin(Base):
    __tablename__ = "file"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    ten_file_goc: Mapped[str] = mapped_column(String(255), nullable=False)
    duong_dan_luu: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    mime_type: Mapped[str] = mapped_column(String(100), nullable=False)
    kich_thuoc_byte: Mapped[int] = mapped_column(BigInteger, nullable=False)
    ma_bam_sha256: Mapped[str | None] = mapped_column(String(64))
    id_nguoi_tai_len: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("nguoi_dung.id", ondelete="SET NULL")
    )
    ngay_tao: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"), nullable=False)


class TaiLieu(Base):
    __tablename__ = "tai_lieu"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    doi: Mapped[str | None] = mapped_column(String(255))
    tieu_de: Mapped[str] = mapped_column(Text, nullable=False)
    tieu_de_chuan_hoa: Mapped[str | None] = mapped_column(Text)
    tom_tat: Mapped[str | None] = mapped_column(Text)
    tac_gia: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"), nullable=False)
    tu_khoa: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("ARRAY[]::text[]"), nullable=False)
    loai_tai_lieu: Mapped[str | None] = mapped_column(String(50))
    ngon_ngu: Mapped[str | None] = mapped_column(String(10))
    ngay_xuat_ban: Mapped[dt.date | None] = mapped_column(Date)
    nam_xuat_ban: Mapped[int | None] = mapped_column(SmallInteger)
    ten_tap_chi: Mapped[str | None] = mapped_column(String(500))
    issn: Mapped[str | None] = mapped_column(String(20))
    nha_xuat_ban: Mapped[str | None] = mapped_column(String(300))
    volume: Mapped[str | None] = mapped_column(String(50))
    issue: Mapped[str | None] = mapped_column(String(50))
    trang_hoac_so_bai: Mapped[str | None] = mapped_column(String(100))
    quartile: Mapped[str | None] = mapped_column(String(2))
    thong_tin_quartile: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    chi_so_trich_dan: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    ma_ngoai: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"), nullable=False)
    du_lieu_nguon: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"), nullable=False)
    truy_cap_mo: Mapped[bool] = mapped_column(Boolean, server_default=text("false"), nullable=False)
    url_trang_goc: Mapped[str | None] = mapped_column(Text)
    url_pdf: Mapped[str | None] = mapped_column(Text)
    ngay_tao: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"), nullable=False)
    ngay_cap_nhat: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"), nullable=False)
    id_file_pdf: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("file.id", ondelete="SET NULL"))


class LanTimKiem(Base):
    __tablename__ = "lan_tim_kiem"
    __table_args__ = (
        UniqueConstraint("id_de_tai", "lan", name="uq_lan_tim_kiem_de_tai_lan"),
        UniqueConstraint("id", "id_de_tai", name="uq_lan_tim_kiem_id_de_tai"),
        CheckConstraint("loai_tim_kiem IN ('thuong', 'ai')", name="ck_lan_tim_kiem_loai"),
        CheckConstraint(
            "trang_thai_ai IS NULL OR trang_thai_ai IN "
            "('cho_xu_ly', 'dang_xu_ly', 'hoan_thanh', 'loi_mot_phan', 'loi')",
            name="ck_lan_tim_kiem_trang_thai_ai",
        ),
        CheckConstraint(
            "trang_thai_tai_pdf IN ('cho_xu_ly', 'dang_xu_ly', 'tam_dung', 'hoan_thanh', 'loi_mot_phan', 'loi')",
            name="ck_lan_tim_kiem_trang_thai_tai_pdf",
        ),
        CheckConstraint(
            "thoi_gian_chay_ms IS NULL OR thoi_gian_chay_ms >= 0",
            name="ck_lan_tim_kiem_thoi_gian_chay",
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    id_de_tai: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("de_tai.id", ondelete="CASCADE"), nullable=False)
    id_nguoi_dung: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("nguoi_dung.id", ondelete="RESTRICT"), nullable=False)
    dieu_kien_tim_kiem: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"), nullable=False)
    lan: Mapped[int] = mapped_column(Integer, nullable=False)
    thoi_gian: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"), nullable=False)
    tong_ket_qua: Mapped[int | None] = mapped_column(Integer)
    so_tai_lieu_moi: Mapped[int | None] = mapped_column(Integer)
    loai_tim_kiem: Mapped[str] = mapped_column(String(20), server_default=text("'thuong'"), nullable=False)
    yeu_cau_ai: Mapped[str | None] = mapped_column(Text)
    dieu_kien_ai: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    trang_thai_ai: Mapped[str | None] = mapped_column(String(30))
    ngay_hoan_thanh_ai: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    thoi_gian_bat_dau: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    thoi_gian_hoan_thanh: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    thoi_gian_chay_ms: Mapped[int | None] = mapped_column(BigInteger)
    thoi_gian_bat_dau_dot_pdf: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    yeu_cau_dung_tai_pdf: Mapped[bool] = mapped_column(Boolean, server_default=text("false"), nullable=False)
    trang_thai_tai_pdf: Mapped[str] = mapped_column(
        String(30), server_default=text("'hoan_thanh'"), nullable=False
    )
    tong_pdf_can_tai: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)
    so_pdf_da_co: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)
    so_pdf_tai_thanh_cong: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)
    so_pdf_khong_tim_thay: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)
    so_pdf_loi: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)


class DeTaiTaiLieu(Base):
    __tablename__ = "de_tai_tai_lieu"
    __table_args__ = (
        UniqueConstraint("id_de_tai", "id_tai_lieu", name="uq_de_tai_tai_lieu"),
        UniqueConstraint("id", "id_de_tai", name="uq_de_tai_tai_lieu_id_de_tai"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    id_de_tai: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("de_tai.id", ondelete="CASCADE"), nullable=False)
    id_tai_lieu: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tai_lieu.id", ondelete="CASCADE"), nullable=False)
    ngay_them: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"), nullable=False)


class DeTaiTaiLieuLanTimKiem(Base):
    __tablename__ = "de_tai_tai_lieu_lan_tim_kiem"
    __table_args__ = (
        ForeignKeyConstraint(
            ["id_de_tai_tai_lieu", "id_de_tai"],
            ["de_tai_tai_lieu.id", "de_tai_tai_lieu.id_de_tai"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["id_lan_tim_kiem", "id_de_tai"],
            ["lan_tim_kiem.id", "lan_tim_kiem.id_de_tai"],
            ondelete="CASCADE",
        ),
    )
    id_de_tai: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    id_de_tai_tai_lieu: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    id_lan_tim_kiem: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    ngay_phat_hien: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"), nullable=False)


class DanhGiaAiTaiLieu(Base):
    __tablename__ = "danh_gia_ai_tai_lieu"
    __table_args__ = (
        ForeignKeyConstraint(
            ["id_de_tai_tai_lieu", "id_de_tai"],
            ["de_tai_tai_lieu.id", "de_tai_tai_lieu.id_de_tai"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["id_lan_tim_kiem", "id_de_tai"],
            ["lan_tim_kiem.id", "lan_tim_kiem.id_de_tai"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("id_de_tai_tai_lieu", "id_lan_tim_kiem", name="uq_danh_gia_ai_tai_lieu_lan"),
        CheckConstraint(
            "trang_thai_xu_ly IN ('cho_xu_ly', 'dang_xu_ly', 'hoan_thanh', 'khong_co_pdf', 'loi')",
            name="ck_danh_gia_ai_trang_thai",
        ),
        CheckConstraint(
            "nhan IS NULL OR nhan IN ('phu_hop', 'khong_phu_hop', 'chua_danh_gia')",
            name="ck_danh_gia_ai_nhan",
        ),
        CheckConstraint(
            "diem_phu_hop IS NULL OR diem_phu_hop BETWEEN 0 AND 100",
            name="ck_danh_gia_ai_diem",
        ),
        CheckConstraint("so_lan_thu >= 0", name="ck_danh_gia_ai_so_lan_thu"),
        Index("ix_danh_gia_ai_de_tai_lan_nhan", "id_de_tai", "id_lan_tim_kiem", "nhan"),
        Index("ix_danh_gia_ai_lan_trang_thai", "id_lan_tim_kiem", "trang_thai_xu_ly"),
        Index("ix_danh_gia_ai_diem", "diem_phu_hop"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    id_de_tai: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    id_de_tai_tai_lieu: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    id_lan_tim_kiem: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    trang_thai_xu_ly: Mapped[str] = mapped_column(String(30), server_default=text("'cho_xu_ly'"), nullable=False)
    nhan: Mapped[str] = mapped_column(String(30), server_default=text("'chua_danh_gia'"), nullable=False)
    diem_phu_hop: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    ly_do: Mapped[str | None] = mapped_column(Text)
    bang_chung: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"), nullable=False)
    chi_tiet_dieu_kien: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )
    mo_hinh: Mapped[str | None] = mapped_column(String(100))
    phien_ban_prompt: Mapped[str | None] = mapped_column(String(50))
    so_lan_thu: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"), nullable=False)
    loi_xu_ly: Mapped[str | None] = mapped_column(Text)
    ngay_tao: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"), nullable=False)
    ngay_cap_nhat: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"), nullable=False)


class CongViecTaiPdf(Base):
    __tablename__ = "cong_viec_tai_pdf"
    __table_args__ = (
        ForeignKeyConstraint(
            ["id_de_tai_tai_lieu", "id_de_tai"],
            ["de_tai_tai_lieu.id", "de_tai_tai_lieu.id_de_tai"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["id_lan_tim_kiem", "id_de_tai"],
            ["lan_tim_kiem.id", "lan_tim_kiem.id_de_tai"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("id_lan_tim_kiem", "id_de_tai_tai_lieu", name="uq_cong_viec_tai_pdf_lan_tai_lieu"),
        CheckConstraint(
            "trang_thai IN ('cho_xu_ly', 'dang_xu_ly', 'thanh_cong', 'khong_tim_thay', 'loi')",
            name="ck_cong_viec_tai_pdf_trang_thai",
        ),
        CheckConstraint("so_lan_thu >= 0", name="ck_cong_viec_tai_pdf_so_lan_thu"),
        Index("ix_cong_viec_tai_pdf_hang_doi", "trang_thai", "ngay_tao"),
        Index("ix_cong_viec_tai_pdf_lan_tim_kiem", "id_lan_tim_kiem", "trang_thai"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    id_de_tai: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    id_lan_tim_kiem: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    id_de_tai_tai_lieu: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    trang_thai: Mapped[str] = mapped_column(String(30), server_default=text("'cho_xu_ly'"), nullable=False)
    nguon_pdf: Mapped[str | None] = mapped_column(String(100))
    url_pdf_cuoi: Mapped[str | None] = mapped_column(Text)
    so_lan_thu: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"), nullable=False)
    loi_xu_ly: Mapped[str | None] = mapped_column(Text)
    ngay_tao: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"), nullable=False)
    ngay_cap_nhat: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"), nullable=False)
    ngay_hoan_thanh: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class LanTraCuuAi(Base):
    __tablename__ = "lan_tra_cuu_ai"
    __table_args__ = (
        UniqueConstraint("id", "id_de_tai", name="uq_lan_tra_cuu_ai_id_de_tai"),
        CheckConstraint(
            "trang_thai IN ('cho_xu_ly', 'dang_xu_ly', 'hoan_thanh', 'loi_mot_phan', 'loi')",
            name="ck_lan_tra_cuu_ai_trang_thai",
        ),
        CheckConstraint(
            "thoi_gian_chay_ms IS NULL OR thoi_gian_chay_ms >= 0",
            name="ck_lan_tra_cuu_ai_thoi_gian_chay",
        ),
        Index("ix_lan_tra_cuu_ai_nguoi_dung_ngay_tao", "id_nguoi_dung", "ngay_tao"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    id_de_tai: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("de_tai.id", ondelete="CASCADE"), nullable=False)
    id_nguoi_dung: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("nguoi_dung.id", ondelete="RESTRICT"), nullable=False)
    noi_dung_yeu_cau: Mapped[str] = mapped_column(Text, nullable=False)
    dieu_kien_ai: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"), nullable=False)
    trang_thai: Mapped[str] = mapped_column(String(30), server_default=text("'cho_xu_ly'"), nullable=False)
    tong_tai_lieu: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)
    so_tai_lieu_co_pdf: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)
    so_tai_lieu_khong_pdf: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)
    thoi_gian_bat_dau: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"), nullable=False)
    thoi_gian_hoan_thanh: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    thoi_gian_chay_ms: Mapped[int | None] = mapped_column(BigInteger)
    ngay_tao: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"), nullable=False)


class LanTraCuuAiNguon(Base):
    __tablename__ = "lan_tra_cuu_ai_nguon"
    __table_args__ = (
        ForeignKeyConstraint(
            ["id_lan_tra_cuu_ai", "id_de_tai"],
            ["lan_tra_cuu_ai.id", "lan_tra_cuu_ai.id_de_tai"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["id_lan_tim_kiem", "id_de_tai"],
            ["lan_tim_kiem.id", "lan_tim_kiem.id_de_tai"],
            ondelete="RESTRICT",
        ),
        Index("ix_lan_tra_cuu_ai_nguon_lan_tim", "id_lan_tim_kiem"),
    )
    id_lan_tra_cuu_ai: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    id_lan_tim_kiem: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    id_de_tai: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)


class KetQuaTraCuuAi(Base):
    __tablename__ = "ket_qua_tra_cuu_ai"
    __table_args__ = (
        ForeignKeyConstraint(
            ["id_lan_tra_cuu_ai", "id_de_tai"],
            ["lan_tra_cuu_ai.id", "lan_tra_cuu_ai.id_de_tai"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["id_de_tai_tai_lieu", "id_de_tai"],
            ["de_tai_tai_lieu.id", "de_tai_tai_lieu.id_de_tai"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("id_lan_tra_cuu_ai", "id_de_tai_tai_lieu", name="uq_ket_qua_tra_cuu_ai_lan_tai_lieu"),
        CheckConstraint(
            "trang_thai_xu_ly IN ('cho_xu_ly', 'dang_xu_ly', 'hoan_thanh', 'khong_co_pdf', 'loi')",
            name="ck_ket_qua_tra_cuu_ai_trang_thai",
        ),
        CheckConstraint(
            "nhan IN ('phu_hop', 'khong_phu_hop', 'chua_danh_gia')",
            name="ck_ket_qua_tra_cuu_ai_nhan",
        ),
        CheckConstraint("diem_phu_hop IS NULL OR diem_phu_hop BETWEEN 0 AND 100", name="ck_ket_qua_tra_cuu_ai_diem"),
        CheckConstraint("so_lan_thu >= 0", name="ck_ket_qua_tra_cuu_ai_so_lan_thu"),
        Index("ix_ket_qua_tra_cuu_ai_hang_doi", "trang_thai_xu_ly", "ngay_tao"),
        Index("ix_ket_qua_tra_cuu_ai_diem", "id_lan_tra_cuu_ai", "diem_phu_hop"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    id_lan_tra_cuu_ai: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    id_de_tai: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    id_de_tai_tai_lieu: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    trang_thai_xu_ly: Mapped[str] = mapped_column(String(30), server_default=text("'cho_xu_ly'"), nullable=False)
    nhan: Mapped[str] = mapped_column(String(30), server_default=text("'chua_danh_gia'"), nullable=False)
    diem_phu_hop: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    ly_do: Mapped[str | None] = mapped_column(Text)
    bang_chung: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"), nullable=False)
    chi_tiet_dieu_kien: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"), nullable=False)
    mo_hinh: Mapped[str | None] = mapped_column(String(100))
    phien_ban_prompt: Mapped[str | None] = mapped_column(String(50))
    so_lan_thu: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"), nullable=False)
    loi_xu_ly: Mapped[str | None] = mapped_column(Text)
    ngay_tao: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"), nullable=False)
    ngay_cap_nhat: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"), nullable=False)


class TrangThaiTaiLieu(Base):
    __tablename__ = "trang_thai_tai_lieu"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    id_de_tai_tai_lieu: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("de_tai_tai_lieu.id", ondelete="CASCADE"), nullable=False)
    id_trang_thai: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("dm_trang_thai_tai_lieu.id", ondelete="RESTRICT"), nullable=False)
    ly_do: Mapped[str | None] = mapped_column(Text)
    thoi_gian: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"), nullable=False)
    id_nguoi_dung: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("nguoi_dung.id", ondelete="RESTRICT"), nullable=False)


class TrangThaiTaiLieuTepTin(Base):
    __tablename__ = "trang_thai_tai_lieu_file"
    id_trang_thai_tai_lieu: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("trang_thai_tai_lieu.id", ondelete="CASCADE"), primary_key=True)
    id_file: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("file.id", ondelete="CASCADE"), primary_key=True)
    thu_tu: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"), nullable=False)


class TrangThaiTaiLieuHienTai(Base):
    __tablename__ = "vw_trang_thai_tai_lieu_hien_tai"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    id_de_tai_tai_lieu: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    id_nguoi_dung: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    id_trang_thai: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    ly_do: Mapped[str | None] = mapped_column(Text)
    thoi_gian: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ThongKeTrangThaiTaiLieu(Base):
    __tablename__ = "vw_thong_ke_trang_thai_tai_lieu"
    id_de_tai_tai_lieu: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    id_trang_thai: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    ten_trang_thai: Mapped[str] = mapped_column(String(100), nullable=False)
    mau_trang_thai: Mapped[str] = mapped_column(String(9), nullable=False)
    so_nguoi: Mapped[int] = mapped_column(Integer, nullable=False)
    danh_sach_nguoi_dung: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False)
