"""Ứng dụng quản lý đề tài và tài liệu nghiên cứu."""

import sys


if sys.platform == "win32":
    try:
        import truststore

        truststore.inject_into_ssl()
    except ImportError:
        pass
