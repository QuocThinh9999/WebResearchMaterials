from __future__ import annotations


def format_elapsed_ms(milliseconds: int | float | None) -> str:
    """Định dạng thời gian chạy theo tổng số giờ, phút và giây."""
    total_seconds = max(0, int((milliseconds or 0) // 1000))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d} giờ {minutes:02d} phút {seconds:02d} giây"
