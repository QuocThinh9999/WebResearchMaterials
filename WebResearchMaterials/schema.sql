BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS dm_trang_thai_de_tai (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    ten_trang_thai varchar(100) NOT NULL UNIQUE,
    mau_trang_thai varchar(9) NOT NULL,
    ngay_tao timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_dm_trang_thai_de_tai_mau
        CHECK (mau_trang_thai ~ '^#[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?$')
);

CREATE TABLE IF NOT EXISTS dm_trang_thai_tai_lieu (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    ten_trang_thai varchar(100) NOT NULL UNIQUE,
    mau_trang_thai varchar(9) NOT NULL,
    ngay_tao timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_dm_trang_thai_tai_lieu_mau
        CHECK (mau_trang_thai ~ '^#[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?$')
);

CREATE TABLE IF NOT EXISTS nguoi_dung (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    ten_dang_nhap varchar(100) NOT NULL,
    ten_nguoi_dung varchar(255) NOT NULL,
    mat_khau_hash varchar(255) NOT NULL,
    dang_hoat_dong boolean NOT NULL DEFAULT true,
    ngay_tao timestamptz NOT NULL DEFAULT now(),
    ngay_cap_nhat timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_nguoi_dung_ten_dang_nhap_lower
    ON nguoi_dung (lower(ten_dang_nhap));

CREATE TABLE IF NOT EXISTS de_tai (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    ten_de_tai varchar(500) NOT NULL,
    id_trang_thai uuid NOT NULL
        REFERENCES dm_trang_thai_de_tai(id) ON DELETE RESTRICT,
    mo_ta text,
    id_nguoi_tao uuid NOT NULL
        REFERENCES nguoi_dung(id) ON DELETE RESTRICT,
    ngay_tao timestamptz NOT NULL DEFAULT now(),
    ngay_cap_nhat timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_de_tai_id_trang_thai
    ON de_tai (id_trang_thai);

CREATE TABLE IF NOT EXISTS file (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    ten_file_goc varchar(255) NOT NULL,
    duong_dan_luu text NOT NULL UNIQUE,
    mime_type varchar(100) NOT NULL,
    kich_thuoc_byte bigint NOT NULL,
    ma_bam_sha256 char(64),
    id_nguoi_tai_len uuid
        REFERENCES nguoi_dung(id) ON DELETE SET NULL,
    ngay_tao timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_file_kich_thuoc CHECK (kich_thuoc_byte >= 0),
    CONSTRAINT ck_file_ma_bam_sha256
        CHECK (ma_bam_sha256 IS NULL OR ma_bam_sha256 ~ '^[0-9A-Fa-f]{64}$')
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_file_ma_bam_sha256
    ON file (lower(ma_bam_sha256))
    WHERE ma_bam_sha256 IS NOT NULL;

CREATE TABLE IF NOT EXISTS tai_lieu (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    doi varchar(255),
    tieu_de text NOT NULL,
    tieu_de_chuan_hoa text,
    tom_tat text,
    tac_gia jsonb NOT NULL DEFAULT '[]'::jsonb,
    tu_khoa text[] NOT NULL DEFAULT ARRAY[]::text[],
    loai_tai_lieu varchar(50),
    ngon_ngu varchar(10),
    ngay_xuat_ban date,
    nam_xuat_ban smallint,
    ten_tap_chi varchar(500),
    issn varchar(20),
    nha_xuat_ban varchar(300),
    volume varchar(50),
    issue varchar(50),
    trang_hoac_so_bai varchar(100),
    quartile varchar(2),
    thong_tin_quartile jsonb,
    chi_so_trich_dan jsonb,
    ma_ngoai jsonb NOT NULL DEFAULT '{}'::jsonb,
    du_lieu_nguon jsonb NOT NULL DEFAULT '{}'::jsonb,
    truy_cap_mo boolean NOT NULL DEFAULT false,
    url_trang_goc text,
    url_pdf text,
    ngay_tao timestamptz NOT NULL DEFAULT now(),
    ngay_cap_nhat timestamptz NOT NULL DEFAULT now(),
    id_file_pdf uuid REFERENCES file(id) ON DELETE SET NULL,
    CONSTRAINT ck_tai_lieu_nam_xuat_ban
        CHECK (nam_xuat_ban IS NULL OR nam_xuat_ban BETWEEN 1000 AND 9999),
    CONSTRAINT ck_tai_lieu_quartile
        CHECK (quartile IS NULL OR quartile IN ('Q1', 'Q2', 'Q3', 'Q4'))
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_tai_lieu_doi_lower
    ON tai_lieu (lower(doi))
    WHERE doi IS NOT NULL AND btrim(doi) <> '';

CREATE INDEX IF NOT EXISTS ix_tai_lieu_nam_xuat_ban
    ON tai_lieu (nam_xuat_ban);

CREATE INDEX IF NOT EXISTS ix_tai_lieu_tu_khoa_gin
    ON tai_lieu USING gin (tu_khoa);

CREATE INDEX IF NOT EXISTS ix_tai_lieu_tac_gia_gin
    ON tai_lieu USING gin (tac_gia);

CREATE INDEX IF NOT EXISTS ix_tai_lieu_tieu_de_gin
    ON tai_lieu USING gin (
        to_tsvector('simple', coalesce(tieu_de_chuan_hoa, tieu_de, ''))
    );

CREATE TABLE IF NOT EXISTS lan_tim_kiem (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    id_de_tai uuid NOT NULL
        REFERENCES de_tai(id) ON DELETE CASCADE,
    id_nguoi_dung uuid NOT NULL
        REFERENCES nguoi_dung(id) ON DELETE RESTRICT,
    dieu_kien_tim_kiem jsonb NOT NULL DEFAULT '{}'::jsonb,
    lan integer NOT NULL,
    thoi_gian timestamptz NOT NULL DEFAULT now(),
    tong_ket_qua integer,
    so_tai_lieu_moi integer,
    loai_tim_kiem varchar(20) NOT NULL DEFAULT 'thuong',
    yeu_cau_ai text,
    dieu_kien_ai jsonb,
    trang_thai_ai varchar(30),
    ngay_hoan_thanh_ai timestamptz,
    thoi_gian_bat_dau timestamptz NOT NULL DEFAULT now(),
    thoi_gian_hoan_thanh timestamptz,
    thoi_gian_chay_ms bigint,
    thoi_gian_bat_dau_dot_pdf timestamptz,
    yeu_cau_dung_tai_pdf boolean NOT NULL DEFAULT false,
    trang_thai_tai_pdf varchar(30) NOT NULL DEFAULT 'hoan_thanh',
    tong_pdf_can_tai integer NOT NULL DEFAULT 0,
    so_pdf_da_co integer NOT NULL DEFAULT 0,
    so_pdf_tai_thanh_cong integer NOT NULL DEFAULT 0,
    so_pdf_khong_tim_thay integer NOT NULL DEFAULT 0,
    so_pdf_loi integer NOT NULL DEFAULT 0,
    CONSTRAINT uq_lan_tim_kiem_de_tai_lan UNIQUE (id_de_tai, lan),
    CONSTRAINT uq_lan_tim_kiem_id_de_tai UNIQUE (id, id_de_tai),
    CONSTRAINT ck_lan_tim_kiem_lan CHECK (lan > 0),
    CONSTRAINT ck_lan_tim_kiem_tong_ket_qua
        CHECK (tong_ket_qua IS NULL OR tong_ket_qua >= 0),
    CONSTRAINT ck_lan_tim_kiem_so_tai_lieu_moi
        CHECK (so_tai_lieu_moi IS NULL OR so_tai_lieu_moi >= 0),
    CONSTRAINT ck_lan_tim_kiem_loai
        CHECK (loai_tim_kiem IN ('thuong', 'ai')),
    CONSTRAINT ck_lan_tim_kiem_trang_thai_ai
        CHECK (
            trang_thai_ai IS NULL OR trang_thai_ai IN
            ('cho_xu_ly', 'dang_xu_ly', 'hoan_thanh', 'loi_mot_phan', 'loi')
        ),
    CONSTRAINT ck_lan_tim_kiem_trang_thai_tai_pdf
        CHECK (trang_thai_tai_pdf IN ('cho_xu_ly', 'dang_xu_ly', 'tam_dung', 'hoan_thanh', 'loi_mot_phan', 'loi')),
    CONSTRAINT ck_lan_tim_kiem_thoi_gian_chay
        CHECK (thoi_gian_chay_ms IS NULL OR thoi_gian_chay_ms >= 0)
);

CREATE INDEX IF NOT EXISTS ix_lan_tim_kiem_de_tai_thoi_gian
    ON lan_tim_kiem (id_de_tai, thoi_gian DESC);

CREATE TABLE IF NOT EXISTS de_tai_tai_lieu (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    id_de_tai uuid NOT NULL
        REFERENCES de_tai(id) ON DELETE CASCADE,
    id_tai_lieu uuid NOT NULL
        REFERENCES tai_lieu(id) ON DELETE CASCADE,
    ngay_them timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_de_tai_tai_lieu UNIQUE (id_de_tai, id_tai_lieu),
    CONSTRAINT uq_de_tai_tai_lieu_id_de_tai UNIQUE (id, id_de_tai)
);

CREATE INDEX IF NOT EXISTS ix_de_tai_tai_lieu_id_tai_lieu
    ON de_tai_tai_lieu (id_tai_lieu);

CREATE TABLE IF NOT EXISTS de_tai_tai_lieu_lan_tim_kiem (
    id_de_tai uuid NOT NULL,
    id_de_tai_tai_lieu uuid NOT NULL,
    id_lan_tim_kiem uuid NOT NULL,
    ngay_phat_hien timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (id_de_tai_tai_lieu, id_lan_tim_kiem),
    CONSTRAINT fk_ket_qua_tim_kiem_de_tai_tai_lieu
        FOREIGN KEY (id_de_tai_tai_lieu, id_de_tai)
        REFERENCES de_tai_tai_lieu(id, id_de_tai) ON DELETE CASCADE,
    CONSTRAINT fk_ket_qua_tim_kiem_lan_tim_kiem
        FOREIGN KEY (id_lan_tim_kiem, id_de_tai)
        REFERENCES lan_tim_kiem(id, id_de_tai) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_ket_qua_tim_kiem_id_lan_tim_kiem
    ON de_tai_tai_lieu_lan_tim_kiem (id_lan_tim_kiem);

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

CREATE TABLE IF NOT EXISTS trang_thai_tai_lieu (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    id_de_tai_tai_lieu uuid NOT NULL
        REFERENCES de_tai_tai_lieu(id) ON DELETE CASCADE,
    id_trang_thai uuid NOT NULL
        REFERENCES dm_trang_thai_tai_lieu(id) ON DELETE RESTRICT,
    ly_do text,
    thoi_gian timestamptz NOT NULL DEFAULT now(),
    id_nguoi_dung uuid NOT NULL
        REFERENCES nguoi_dung(id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS ix_trang_thai_tai_lieu_hien_tai
    ON trang_thai_tai_lieu (
        id_de_tai_tai_lieu,
        id_nguoi_dung,
        thoi_gian DESC,
        id DESC
    );

CREATE INDEX IF NOT EXISTS ix_trang_thai_tai_lieu_thong_ke
    ON trang_thai_tai_lieu (
        id_de_tai_tai_lieu,
        id_trang_thai,
        thoi_gian DESC
    );

CREATE TABLE IF NOT EXISTS trang_thai_tai_lieu_file (
    id_trang_thai_tai_lieu uuid NOT NULL
        REFERENCES trang_thai_tai_lieu(id) ON DELETE CASCADE,
    id_file uuid NOT NULL
        REFERENCES file(id) ON DELETE CASCADE,
    thu_tu smallint NOT NULL DEFAULT 0,
    PRIMARY KEY (id_trang_thai_tai_lieu, id_file),
    CONSTRAINT ck_trang_thai_tai_lieu_file_thu_tu CHECK (thu_tu >= 0)
);

CREATE OR REPLACE FUNCTION cap_nhat_ngay_cap_nhat()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.ngay_cap_nhat = now();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_nguoi_dung_ngay_cap_nhat ON nguoi_dung;
CREATE TRIGGER trg_nguoi_dung_ngay_cap_nhat
BEFORE UPDATE ON nguoi_dung
FOR EACH ROW EXECUTE FUNCTION cap_nhat_ngay_cap_nhat();

DROP TRIGGER IF EXISTS trg_de_tai_ngay_cap_nhat ON de_tai;
CREATE TRIGGER trg_de_tai_ngay_cap_nhat
BEFORE UPDATE ON de_tai
FOR EACH ROW EXECUTE FUNCTION cap_nhat_ngay_cap_nhat();

DROP TRIGGER IF EXISTS trg_tai_lieu_ngay_cap_nhat ON tai_lieu;
CREATE TRIGGER trg_tai_lieu_ngay_cap_nhat
BEFORE UPDATE ON tai_lieu
FOR EACH ROW EXECUTE FUNCTION cap_nhat_ngay_cap_nhat();

DROP TRIGGER IF EXISTS trg_danh_gia_ai_ngay_cap_nhat ON danh_gia_ai_tai_lieu;
CREATE TRIGGER trg_danh_gia_ai_ngay_cap_nhat
BEFORE UPDATE ON danh_gia_ai_tai_lieu
FOR EACH ROW EXECUTE FUNCTION cap_nhat_ngay_cap_nhat();

DROP TRIGGER IF EXISTS trg_cong_viec_tai_pdf_ngay_cap_nhat ON cong_viec_tai_pdf;
CREATE TRIGGER trg_cong_viec_tai_pdf_ngay_cap_nhat
BEFORE UPDATE ON cong_viec_tai_pdf
FOR EACH ROW EXECUTE FUNCTION cap_nhat_ngay_cap_nhat();

DROP TRIGGER IF EXISTS trg_ket_qua_tra_cuu_ai_ngay_cap_nhat ON ket_qua_tra_cuu_ai;
CREATE TRIGGER trg_ket_qua_tra_cuu_ai_ngay_cap_nhat
BEFORE UPDATE ON ket_qua_tra_cuu_ai
FOR EACH ROW EXECUTE FUNCTION cap_nhat_ngay_cap_nhat();

INSERT INTO dm_trang_thai_de_tai (ten_trang_thai, mau_trang_thai)
VALUES
    ('Đang thực hiện', '#F59E0B'),
    ('Tạm dừng', '#EF4444'),
    ('Hoàn thành', '#22C55E')
ON CONFLICT (ten_trang_thai)
DO UPDATE SET mau_trang_thai = EXCLUDED.mau_trang_thai;

INSERT INTO dm_trang_thai_tai_lieu (ten_trang_thai, mau_trang_thai)
VALUES
    ('Đang đọc', '#F59E0B'),
    ('Chấp nhận', '#22C55E'),
    ('Không chấp nhận', '#EF4444'),
    ('Thảo luận', '#3B82F6')
ON CONFLICT (ten_trang_thai)
DO UPDATE SET mau_trang_thai = EXCLUDED.mau_trang_thai;

CREATE OR REPLACE VIEW vw_trang_thai_tai_lieu_hien_tai AS
SELECT DISTINCT ON (
    tt.id_de_tai_tai_lieu,
    tt.id_nguoi_dung
)
    tt.id,
    tt.id_de_tai_tai_lieu,
    tt.id_nguoi_dung,
    tt.id_trang_thai,
    tt.ly_do,
    tt.thoi_gian
FROM trang_thai_tai_lieu AS tt
ORDER BY
    tt.id_de_tai_tai_lieu,
    tt.id_nguoi_dung,
    tt.thoi_gian DESC,
    tt.id DESC;

CREATE OR REPLACE VIEW vw_thong_ke_trang_thai_tai_lieu AS
SELECT
    ht.id_de_tai_tai_lieu,
    ht.id_trang_thai,
    dm.ten_trang_thai,
    dm.mau_trang_thai,
    count(*)::integer AS so_nguoi,
    array_agg(nd.ten_nguoi_dung ORDER BY nd.ten_nguoi_dung) AS danh_sach_nguoi_dung
FROM vw_trang_thai_tai_lieu_hien_tai AS ht
JOIN dm_trang_thai_tai_lieu AS dm
    ON dm.id = ht.id_trang_thai
JOIN nguoi_dung AS nd
    ON nd.id = ht.id_nguoi_dung
GROUP BY
    ht.id_de_tai_tai_lieu,
    ht.id_trang_thai,
    dm.ten_trang_thai,
    dm.mau_trang_thai;

COMMIT;
