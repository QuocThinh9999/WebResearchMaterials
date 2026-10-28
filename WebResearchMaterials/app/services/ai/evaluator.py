from __future__ import annotations

import json

from app.models import TaiLieu
from app.services.ai.client import AIGateway
from app.services.ai.prompts import HUONG_DAN_DANH_GIA
from app.services.ai.schemas import DoanPdf, KetQuaDanhGiaAI, KetQuaDieuKienAI, PhanTichYeuCauAI


def _only_mentioned(result: KetQuaDieuKienAI) -> bool:
    role = (result.vai_tro_khai_niem or "").casefold()
    sections = " ".join(result.phan_muc_pdf).casefold()
    active_markers = (
        "hypothes",
        "conceptual framework",
        "research model",
        "method",
        "measure",
        "analysis",
        "result",
        "discussion",
        "independent",
        "dependent",
        "mediator",
        "moderator",
        "control",
        "biến",
    )
    passive_markers = ("chỉ nhắc", "only mentioned", "literature review", "introduction", "references")
    return any(marker in f"{role} {sections}" for marker in passive_markers) and not any(
        marker in f"{role} {sections}" for marker in active_markers
    )


def calculate_relevance_score(results: list[KetQuaDieuKienAI], threshold: int) -> tuple[float, list[KetQuaDieuKienAI]]:
    if not results:
        return 0.0, results
    normalized: list[KetQuaDieuKienAI] = []
    for item in results:
        if item.loai in {"khai_niem_trong_mo_hinh", "su_dung_trong_nghien_cuu"} and _only_mentioned(item):
            item = item.model_copy(
                update={
                    "trang_thai": "khong_dat",
                    "diem": 0,
                    "giai_thich_tieng_viet": (
                        "Khái niệm chỉ được nhắc trong phần tổng quan/giới thiệu, không tham gia mô hình hoặc phân tích."
                    ),
                }
            )
        normalized.append(item)
    if any(item.loai == "loai_tru" and item.trang_thai == "khong_dat" for item in normalized):
        return 0.0, normalized
    total_weight = sum(item.trong_so for item in normalized)
    score = sum(item.diem * item.trong_so for item in normalized) / total_weight if total_weight else 0.0
    mandatory_failed = any(
        item.loai in {"bat_buoc", "metadata_cung", "khai_niem_trong_mo_hinh", "su_dung_trong_nghien_cuu"}
        and item.trang_thai != "dat"
        for item in normalized
    )
    if mandatory_failed:
        score = min(score, max(0, threshold - 0.01))
    return round(max(0, min(100, score)), 2), normalized


def _complete_condition_results(
    intent: PhanTichYeuCauAI,
    results: list[KetQuaDieuKienAI],
) -> list[KetQuaDieuKienAI]:
    if not intent.dieu_kien_nguyen_tu:
        return results
    unused = list(results)
    completed: list[KetQuaDieuKienAI] = []
    for condition in intent.dieu_kien_nguyen_tu:
        match = next(
            (
                item
                for item in unused
                if item.noi_dung.strip().casefold() == condition.noi_dung.strip().casefold()
            ),
            None,
        )
        if match:
            unused.remove(match)
            completed.append(
                match.model_copy(
                    update={
                        "noi_dung": condition.noi_dung,
                        "loai": condition.loai,
                        "trong_so": condition.trong_so,
                    }
                )
            )
        else:
            completed.append(
                KetQuaDieuKienAI(
                    noi_dung=condition.noi_dung,
                    loai=condition.loai,
                    trong_so=condition.trong_so,
                    trang_thai="khong_xac_dinh",
                    diem=0,
                    vai_tro_khai_niem=None,
                    phan_muc_pdf=[],
                    bang_chung=[],
                    giai_thich_tieng_viet="Không có đủ kết quả có cấu trúc để xác định điều kiện này.",
                )
            )
    return completed


def _evaluation_prompt(
    original_request: str,
    intent: PhanTichYeuCauAI,
    document: TaiLieu,
    chunks: list[DoanPdf],
) -> str:
    analyzed = intent.model_dump(mode="json", exclude={
        "parse_duration_ms", "fast_rule_parsed", "input_language", "needs_semantic_pdf_check",
        "cau_truy_van_scopus", "cau_truy_van_wos",
    })
    analyzed = {key: value for key, value in analyzed.items() if value is not None and value is not False and value != "" and value != []}
    data = {
        "yeu_cau_goc_tieng_viet": original_request,
        "dieu_kien_da_phan_tich": analyzed,
        "metadata_tai_lieu": {
            "tieu_de": document.tieu_de,
            "tom_tat": document.tom_tat,
            "tac_gia": document.tac_gia,
            "tu_khoa": document.tu_khoa,
            "nam_xuat_ban": document.nam_xuat_ban,
            "tap_chi": document.ten_tap_chi,
            "nha_xuat_ban": document.nha_xuat_ban,
            "doi": document.doi,
        },
        "doan_pdf_ngon_ngu_goc": [chunk.model_dump() for chunk in chunks],
    }
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def evaluate_document(
    original_request: str,
    intent: PhanTichYeuCauAI,
    document: TaiLieu,
    chunks: list[DoanPdf],
    gateway: AIGateway,
    threshold: int,
) -> KetQuaDanhGiaAI:
    result = gateway.structured_output(
        KetQuaDanhGiaAI,
        HUONG_DAN_DANH_GIA,
        _evaluation_prompt(original_request, intent, document, chunks),
    )
    condition_results = _complete_condition_results(intent, result.ket_qua_tung_dieu_kien)
    if condition_results:
        score, condition_results = calculate_relevance_score(condition_results, threshold)
        result = result.model_copy(
            update={"diem_phu_hop": score, "ket_qua_tung_dieu_kien": condition_results}
        )
    label = "phu_hop" if result.diem_phu_hop >= threshold else "khong_phu_hop"
    if (
        (result.nhan == "chua_danh_gia" and not result.ket_qua_tung_dieu_kien)
        or (intent.dieu_kien_nguyen_tu and not result.ket_qua_tung_dieu_kien)
        or (
            result.ket_qua_tung_dieu_kien
            and all(item.trang_thai == "khong_xac_dinh" for item in result.ket_qua_tung_dieu_kien)
        )
    ):
        label = "chua_danh_gia"
    return result.model_copy(update={"nhan": label})


def evaluate_document_with_vision(
    original_request: str,
    intent: PhanTichYeuCauAI,
    document: TaiLieu,
    pdf_content: bytes,
    gateway: AIGateway,
    threshold: int,
) -> KetQuaDanhGiaAI:
    result = gateway.structured_output_with_pdf(
        KetQuaDanhGiaAI,
        HUONG_DAN_DANH_GIA,
        _evaluation_prompt(original_request, intent, document, []),
        pdf_content,
    )
    condition_results = _complete_condition_results(intent, result.ket_qua_tung_dieu_kien)
    if condition_results:
        score, condition_results = calculate_relevance_score(condition_results, threshold)
        result = result.model_copy(
            update={"diem_phu_hop": score, "ket_qua_tung_dieu_kien": condition_results}
        )
    label = "phu_hop" if result.diem_phu_hop >= threshold else "khong_phu_hop"
    if (
        (result.nhan == "chua_danh_gia" and not result.ket_qua_tung_dieu_kien)
        or (intent.dieu_kien_nguyen_tu and not result.ket_qua_tung_dieu_kien)
        or (
            result.ket_qua_tung_dieu_kien
            and all(item.trang_thai == "khong_xac_dinh" for item in result.ket_qua_tung_dieu_kien)
        )
    ):
        label = "chua_danh_gia"
    return result.model_copy(update={"nhan": label})
