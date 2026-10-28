from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NhomTuDongNghia(StrictModel):
    khai_niem: str
    tu_khoa_tieng_anh: list[str]


class DieuKienNguyenTu(StrictModel):
    noi_dung: str
    loai: Literal[
        "bat_buoc",
        "loai_tru",
        "metadata_cung",
        "khai_niem_trong_mo_hinh",
        "su_dung_trong_nghien_cuu",
    ]
    trong_so: float = Field(default=1, gt=0, le=100)
    truy_van_tim_bang_chung: list[str] = Field(default_factory=list)


class DieuKienRutGon(StrictModel):
    k: Literal[
        "topic", "no_topic", "has", "no_has", "model", "from", "to", "q", "lang",
        "oa", "type", "pub", "no_pub", "author", "no_author", "origin", "no_origin", "other",
    ]
    v: str


class YeuCauChuanHoa(StrictModel):
    c: list[DieuKienRutGon]


class PhanTichYeuCauAI(StrictModel):
    parse_duration_ms: float | None = Field(default=None, ge=0)
    fast_rule_parsed: bool = False
    excluded_publishers: list[str] = Field(default_factory=list)
    required_constructs_in_model: list[str] = Field(default_factory=list)
    required_topics: list[str] = Field(default_factory=list)
    required_terms: list[str] = Field(default_factory=list)
    excluded_topics: list[str] = Field(default_factory=list)
    required_affiliation_countries: list[str] = Field(default_factory=list)
    excluded_affiliation_countries: list[str] = Field(default_factory=list)
    input_language: Literal["vi", "en"] | None = None
    needs_semantic_pdf_check: bool = False
    chu_de_bat_buoc: list[str]
    chu_de_tuy_chon: list[str]
    tu_khoa_tim_kiem_tieng_anh: list[str]
    tu_khoa_loai_tru: list[str]
    tieu_de_loai_tru: list[str]
    doi_loai_tru: list[str]
    tac_gia_bat_buoc: list[str]
    tac_gia_loai_tru: list[str]
    nha_xuat_ban_bat_buoc: list[str]
    nha_xuat_ban_loai_tru: list[str]
    nam_tu: int | None
    nam_den: int | None
    loai_tai_lieu: str | None
    ngon_ngu: str | None
    quartile: list[Literal["Q1", "Q2", "Q3", "Q4"]]
    chi_truy_cap_mo: bool
    dien_giai_ngan_gon: str
    ngon_ngu_yeu_cau: str
    ngon_ngu_tai_lieu_cho_phep: list[str]
    khai_niem_goc_tieng_viet: list[str]
    khai_niem_tim_kiem_tieng_anh: list[str]
    nhom_tu_dong_nghia: list[NhomTuDongNghia]
    cau_truy_van_scopus: str
    cau_truy_van_wos: str
    dieu_kien_nguyen_tu: list[DieuKienNguyenTu] = Field(default_factory=list)

    @property
    def nhom_tu_khoa_bat_buoc(self) -> list[list[str]]:
        return [
            list(dict.fromkeys(term.strip() for term in group.tu_khoa_tieng_anh if term.strip()))
            for group in self.nhom_tu_dong_nghia
            if group.tu_khoa_tieng_anh
        ]


class BangChungAI(StrictModel):
    noi_dung: str
    trang: int = Field(ge=1)
    dieu_kien: str
    dien_giai_tieng_viet: str | None


class KetQuaDieuKienAI(StrictModel):
    noi_dung: str
    loai: Literal[
        "bat_buoc",
        "loai_tru",
        "metadata_cung",
        "khai_niem_trong_mo_hinh",
        "su_dung_trong_nghien_cuu",
    ]
    trong_so: float = Field(gt=0, le=100)
    trang_thai: Literal["dat", "khong_dat", "khong_xac_dinh"]
    diem: float = Field(ge=0, le=100)
    vai_tro_khai_niem: str | None = None
    phan_muc_pdf: list[str] = Field(default_factory=list)
    bang_chung: list[BangChungAI] = Field(default_factory=list)
    giai_thich_tieng_viet: str


class KetQuaDanhGiaAI(StrictModel):
    nhan: Literal["phu_hop", "khong_phu_hop", "chua_danh_gia"]
    diem_phu_hop: float = Field(ge=0, le=100)
    ly_do: str
    dieu_kien_dat: list[str]
    dieu_kien_khong_dat: list[str]
    dieu_kien_khong_xac_dinh: list[str]
    bang_chung: list[BangChungAI]
    ket_qua_tung_dieu_kien: list[KetQuaDieuKienAI] = Field(default_factory=list)


class DoanPdf(StrictModel):
    trang: int
    noi_dung: str
