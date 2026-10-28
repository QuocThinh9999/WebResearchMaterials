BEGIN;

ALTER TABLE lan_tim_kiem
    ADD COLUMN IF NOT EXISTS loai_tim_kiem varchar(20) NOT NULL DEFAULT 'thuong',
    ADD COLUMN IF NOT EXISTS yeu_cau_ai text,
    ADD COLUMN IF NOT EXISTS dieu_kien_ai jsonb,
    ADD COLUMN IF NOT EXISTS trang_thai_ai varchar(30),
    ADD COLUMN IF NOT EXISTS ngay_hoan_thanh_ai timestamptz;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_lan_tim_kiem_loai') THEN
        ALTER TABLE lan_tim_kiem
            ADD CONSTRAINT ck_lan_tim_kiem_loai
            CHECK (loai_tim_kiem IN ('thuong', 'ai'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_lan_tim_kiem_trang_thai_ai') THEN
        ALTER TABLE lan_tim_kiem
            ADD CONSTRAINT ck_lan_tim_kiem_trang_thai_ai
            CHECK (
                trang_thai_ai IS NULL OR trang_thai_ai IN
                ('cho_xu_ly', 'dang_xu_ly', 'hoan_thanh', 'loi_mot_phan', 'loi')
            );
    END IF;
END;
$$;

CREATE TABLE IF NOT EXISTS danh_gia_ai_tai_lieu (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    id_de_tai uuid NOT NULL,
    id_de_tai_tai_lieu uuid NOT NULL,
    id_lan_tim_kiem uuid NOT NULL,
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
    CONSTRAINT uq_danh_gia_ai_tai_lieu_lan
        UNIQUE (id_de_tai_tai_lieu, id_lan_tim_kiem),
    CONSTRAINT fk_danh_gia_ai_de_tai_tai_lieu
        FOREIGN KEY (id_de_tai_tai_lieu, id_de_tai)
        REFERENCES de_tai_tai_lieu(id, id_de_tai) ON DELETE CASCADE,
    CONSTRAINT fk_danh_gia_ai_lan_tim_kiem
        FOREIGN KEY (id_lan_tim_kiem, id_de_tai)
        REFERENCES lan_tim_kiem(id, id_de_tai) ON DELETE CASCADE,
    CONSTRAINT ck_danh_gia_ai_trang_thai
        CHECK (trang_thai_xu_ly IN ('cho_xu_ly', 'dang_xu_ly', 'hoan_thanh', 'khong_co_pdf', 'loi')),
    CONSTRAINT ck_danh_gia_ai_nhan
        CHECK (nhan IN ('phu_hop', 'khong_phu_hop', 'chua_danh_gia')),
    CONSTRAINT ck_danh_gia_ai_diem
        CHECK (diem_phu_hop IS NULL OR diem_phu_hop BETWEEN 0 AND 100),
    CONSTRAINT ck_danh_gia_ai_so_lan_thu CHECK (so_lan_thu >= 0)
);

CREATE INDEX IF NOT EXISTS ix_danh_gia_ai_de_tai_lan_nhan
    ON danh_gia_ai_tai_lieu (id_de_tai, id_lan_tim_kiem, nhan);

CREATE INDEX IF NOT EXISTS ix_danh_gia_ai_lan_trang_thai
    ON danh_gia_ai_tai_lieu (id_lan_tim_kiem, trang_thai_xu_ly);

CREATE INDEX IF NOT EXISTS ix_danh_gia_ai_diem
    ON danh_gia_ai_tai_lieu (diem_phu_hop);

DROP TRIGGER IF EXISTS trg_danh_gia_ai_ngay_cap_nhat ON danh_gia_ai_tai_lieu;
CREATE TRIGGER trg_danh_gia_ai_ngay_cap_nhat
BEFORE UPDATE ON danh_gia_ai_tai_lieu
FOR EACH ROW EXECUTE FUNCTION cap_nhat_ngay_cap_nhat();

COMMIT;
