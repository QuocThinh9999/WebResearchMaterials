from __future__ import annotations

import argparse
import getpass
import sys

from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError

from app.database import SessionLocal
from app.models import NguoiDung
from app.services.security import hash_password
from app.services.ai.client import AIConfigurationError
from app.services.ai.worker import run_worker


def create_account() -> int:
    username = input("Tên đăng nhập: ").strip()
    full_name = input("Tên người dùng: ").strip()
    password = getpass.getpass("Mật khẩu: ")
    confirmation = getpass.getpass("Nhập lại mật khẩu: ")
    if not username or not full_name:
        print("Tên đăng nhập và tên người dùng không được để trống.")
        return 1
    if password != confirmation:
        print("Hai lần nhập mật khẩu không khớp.")
        return 1
    try:
        hashed = hash_password(password)
        with SessionLocal() as db:
            existing = db.scalar(select(NguoiDung).where(func.lower(NguoiDung.ten_dang_nhap) == username.lower()))
            if existing:
                print("Tên đăng nhập đã tồn tại.")
                return 1
            db.add(NguoiDung(ten_dang_nhap=username, ten_nguoi_dung=full_name, mat_khau_hash=hashed))
            db.commit()
    except (ValueError, SQLAlchemyError) as exc:
        message = str(exc) if isinstance(exc, ValueError) else "Không thể tạo tài khoản. Hãy kiểm tra kết nối PostgreSQL."
        print(message)
        return 1
    print("Đã tạo tài khoản đầu tiên.")
    return 0


def check_connection() -> int:
    try:
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
    except SQLAlchemyError:
        print("Không thể kết nối PostgreSQL. Hãy kiểm tra các biến PG* trong .env.")
        return 1
    print("Kết nối PostgreSQL thành công.")
    return 0


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Công cụ quản trị ứng dụng")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("tao-tai-khoan", help="Tạo tài khoản đăng nhập")
    subparsers.add_parser("kiem-tra-ket-noi", help="Kiểm tra kết nối PostgreSQL")
    background_parser = subparsers.add_parser("xu-ly-nen", help="Chạy worker tải PDF và tra cứu bằng AI")
    background_parser.add_argument("--lien-tuc", action="store_true", help="Tiếp tục chờ công việc mới")
    ai_parser = subparsers.add_parser("xu-ly-ai", help="Lệnh tương thích, chạy cùng worker nền")
    ai_parser.add_argument("--lien-tuc", action="store_true", help="Tiếp tục chờ công việc mới")
    args = parser.parse_args()
    if args.command == "tao-tai-khoan":
        return create_account()
    if args.command in {"xu-ly-nen", "xu-ly-ai"}:
        try:
            processed = run_worker(args.lien_tuc)
            if not args.lien_tuc:
                print(f"Đã xử lý {processed} công việc nền.")
            return 0
        except AIConfigurationError as exc:
            print(str(exc))
            return 1
    return check_connection()


if __name__ == "__main__":
    sys.exit(main())
