BEGIN;

ALTER TABLE lan_tim_kiem
    ADD COLUMN IF NOT EXISTS thoi_gian_bat_dau timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS thoi_gian_hoan_thanh timestamptz,
    ADD COLUMN IF NOT EXISTS thoi_gian_chay_ms bigint,
    ADD COLUMN IF NOT EXISTS thoi_gian_bat_dau_dot_pdf timestamptz,
    ADD COLUMN IF NOT EXISTS yeu_cau_dung_tai_pdf boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS trang_thai_tai_pdf varchar(30) NOT NULL DEFAULT 'hoan_thanh',
    ADD COLUMN IF NOT EXISTS tong_pdf_can_tai integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS so_pdf_da_co integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS so_pdf_tai_thanh_cong integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS so_pdf_khong_tim_thay integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS so_pdf_loi integer NOT NULL DEFAULT 0;

DO $$
BEGIN
    ALTER TABLE lan_tim_kiem DROP CONSTRAINT IF EXISTS ck_lan_tim_kiem_trang_thai_tai_pdf;
    ALTER TABLE lan_tim_kiem ADD CONSTRAINT ck_lan_tim_kiem_trang_thai_tai_pdf
        CHECK (trang_thai_tai_pdf IN ('cho_xu_ly', 'dang_xu_ly', 'tam_dung', 'hoan_thanh', 'loi_mot_phan', 'loi'));
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_lan_tim_kiem_thoi_gian_chay') THEN
        ALTER TABLE lan_tim_kiem ADD CONSTRAINT ck_lan_tim_kiem_thoi_gian_chay
            CHECK (thoi_gian_chay_ms IS NULL OR thoi_gian_chay_ms >= 0);
    END IF;
END;
$$;

CREATE TABLE IF NOT EXISTS cong_viec_tai_pdf (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    id_de_tai uuid NOT NULL,
    id_lan_tim_kiem uuid NOT NULL,
    id_de_tai_tai_lieu uuid NOT NULL,
    trang_thai varchar(30) NOT NULL DEFAULT 'cho_xu_ly',
    nguon_pdf varchar(100),
    url_pdf_cuoi text,
    so_lan_thu smallint NOT NULL DEFAULT 0,
    loi_xu_ly text,
    ngay_tao timestamptz NOT NULL DEFAULT now(),
    ngay_cap_nhat timestamptz NOT NULL DEFAULT now(),
    ngay_hoan_thanh timestamptz,
    CONSTRAINT uq_cong_viec_tai_pdf_lan_tai_lieu UNIQUE (id_lan_tim_kiem, id_de_tai_tai_lieu),
    CONSTRAINT fk_cong_viec_tai_pdf_de_tai_tai_lieu FOREIGN KEY (id_de_tai_tai_lieu, id_de_tai)
        REFERENCES de_tai_tai_lieu(id, id_de_tai) ON DELETE CASCADE,
    CONSTRAINT fk_cong_viec_tai_pdf_lan_tim_kiem FOREIGN KEY (id_lan_tim_kiem, id_de_tai)
        REFERENCES lan_tim_kiem(id, id_de_tai) ON DELETE CASCADE,
    CONSTRAINT ck_cong_viec_tai_pdf_trang_thai CHECK
        (trang_thai IN ('cho_xu_ly', 'dang_xu_ly', 'thanh_cong', 'khong_tim_thay', 'loi')),
    CONSTRAINT ck_cong_viec_tai_pdf_so_lan_thu CHECK (so_lan_thu >= 0)
);

CREATE INDEX IF NOT EXISTS ix_cong_viec_tai_pdf_hang_doi
    ON cong_viec_tai_pdf (trang_thai, ngay_tao);
CREATE INDEX IF NOT EXISTS ix_cong_viec_tai_pdf_lan_tim_kiem
    ON cong_viec_tai_pdf (id_lan_tim_kiem, trang_thai);

CREATE TABLE IF NOT EXISTS lan_tra_cuu_ai (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    id_de_tai uuid NOT NULL REFERENCES de_tai(id) ON DELETE CASCADE,
    id_nguoi_dung uuid NOT NULL REFERENCES nguoi_dung(id) ON DELETE RESTRICT,
    noi_dung_yeu_cau text NOT NULL,
    dieu_kien_ai jsonb NOT NULL DEFAULT '{}'::jsonb,
    trang_thai varchar(30) NOT NULL DEFAULT 'cho_xu_ly',
    tong_tai_lieu integer NOT NULL DEFAULT 0,
    so_tai_lieu_co_pdf integer NOT NULL DEFAULT 0,
    so_tai_lieu_khong_pdf integer NOT NULL DEFAULT 0,
    thoi_gian_bat_dau timestamptz NOT NULL DEFAULT now(),
    thoi_gian_hoan_thanh timestamptz,
    thoi_gian_chay_ms bigint,
    ngay_tao timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_lan_tra_cuu_ai_id_de_tai UNIQUE (id, id_de_tai),
    CONSTRAINT ck_lan_tra_cuu_ai_trang_thai CHECK
        (trang_thai IN ('cho_xu_ly', 'dang_xu_ly', 'hoan_thanh', 'loi_mot_phan', 'loi')),
    CONSTRAINT ck_lan_tra_cuu_ai_thoi_gian_chay CHECK
        (thoi_gian_chay_ms IS NULL OR thoi_gian_chay_ms >= 0)
);

ALTER TABLE lan_tra_cuu_ai
    ADD COLUMN IF NOT EXISTS thoi_gian_bat_dau timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS thoi_gian_hoan_thanh timestamptz,
    ADD COLUMN IF NOT EXISTS thoi_gian_chay_ms bigint;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_lan_tra_cuu_ai_thoi_gian_chay') THEN
        ALTER TABLE lan_tra_cuu_ai ADD CONSTRAINT ck_lan_tra_cuu_ai_thoi_gian_chay
            CHECK (thoi_gian_chay_ms IS NULL OR thoi_gian_chay_ms >= 0);
    END IF;
END;
$$;

CREATE INDEX IF NOT EXISTS ix_lan_tra_cuu_ai_nguoi_dung_ngay_tao
    ON lan_tra_cuu_ai (id_nguoi_dung, ngay_tao DESC);

CREATE TABLE IF NOT EXISTS lan_tra_cuu_ai_nguon (
    id_lan_tra_cuu_ai uuid NOT NULL,
    id_lan_tim_kiem uuid NOT NULL,
    id_de_tai uuid NOT NULL,
    PRIMARY KEY (id_lan_tra_cuu_ai, id_lan_tim_kiem),
    CONSTRAINT fk_lan_tra_cuu_ai_nguon_lan_ai FOREIGN KEY (id_lan_tra_cuu_ai, id_de_tai)
        REFERENCES lan_tra_cuu_ai(id, id_de_tai) ON DELETE CASCADE,
    CONSTRAINT fk_lan_tra_cuu_ai_nguon_lan_tim FOREIGN KEY (id_lan_tim_kiem, id_de_tai)
        REFERENCES lan_tim_kiem(id, id_de_tai) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS ix_lan_tra_cuu_ai_nguon_lan_tim
    ON lan_tra_cuu_ai_nguon (id_lan_tim_kiem);

CREATE TABLE IF NOT EXISTS ket_qua_tra_cuu_ai (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    id_lan_tra_cuu_ai uuid NOT NULL,
    id_de_tai uuid NOT NULL,
    id_de_tai_tai_lieu uuid NOT NULL,
    trang_thai_xu_ly varchar(30) NOT NULL DEFAULT 'cho_xu_ly',
    nhan varchar(30) NOT NULL DEFAULT 'chua_danh_gia',
    diem_phu_hop numeric(5,2),
    ly_do text,
    bang_chung jsonb NOT NULL DEFAULT '[]'::jsonb,
    chi_tiet_dieu_kien jsonb NOT NULL DEFAULT '{}'::jsonb,
    mo_hinh varchar(100),
    phien_ban_prompt varchar(50),
    so_lan_thu smallint NOT NULL DEFAULT 0,
    loi_xu_ly text,
    ngay_tao timestamptz NOT NULL DEFAULT now(),
    ngay_cap_nhat timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_ket_qua_tra_cuu_ai_lan_tai_lieu UNIQUE (id_lan_tra_cuu_ai, id_de_tai_tai_lieu),
    CONSTRAINT fk_ket_qua_tra_cuu_ai_lan FOREIGN KEY (id_lan_tra_cuu_ai, id_de_tai)
        REFERENCES lan_tra_cuu_ai(id, id_de_tai) ON DELETE CASCADE,
    CONSTRAINT fk_ket_qua_tra_cuu_ai_tai_lieu FOREIGN KEY (id_de_tai_tai_lieu, id_de_tai)
        REFERENCES de_tai_tai_lieu(id, id_de_tai) ON DELETE CASCADE,
    CONSTRAINT ck_ket_qua_tra_cuu_ai_trang_thai CHECK
        (trang_thai_xu_ly IN ('cho_xu_ly', 'dang_xu_ly', 'hoan_thanh', 'khong_co_pdf', 'loi')),
    CONSTRAINT ck_ket_qua_tra_cuu_ai_nhan CHECK
        (nhan IN ('phu_hop', 'khong_phu_hop', 'chua_danh_gia')),
    CONSTRAINT ck_ket_qua_tra_cuu_ai_diem CHECK
        (diem_phu_hop IS NULL OR diem_phu_hop BETWEEN 0 AND 100),
    CONSTRAINT ck_ket_qua_tra_cuu_ai_so_lan_thu CHECK (so_lan_thu >= 0)
);

CREATE INDEX IF NOT EXISTS ix_ket_qua_tra_cuu_ai_hang_doi
    ON ket_qua_tra_cuu_ai (trang_thai_xu_ly, ngay_tao);
CREATE INDEX IF NOT EXISTS ix_ket_qua_tra_cuu_ai_diem
    ON ket_qua_tra_cuu_ai (id_lan_tra_cuu_ai, diem_phu_hop DESC);

DROP TRIGGER IF EXISTS trg_cong_viec_tai_pdf_ngay_cap_nhat ON cong_viec_tai_pdf;
CREATE TRIGGER trg_cong_viec_tai_pdf_ngay_cap_nhat
BEFORE UPDATE ON cong_viec_tai_pdf
FOR EACH ROW EXECUTE FUNCTION cap_nhat_ngay_cap_nhat();

DROP TRIGGER IF EXISTS trg_ket_qua_tra_cuu_ai_ngay_cap_nhat ON ket_qua_tra_cuu_ai;
CREATE TRIGGER trg_ket_qua_tra_cuu_ai_ngay_cap_nhat
BEFORE UPDATE ON ket_qua_tra_cuu_ai
FOR EACH ROW EXECUTE FUNCTION cap_nhat_ngay_cap_nhat();

COMMIT;
