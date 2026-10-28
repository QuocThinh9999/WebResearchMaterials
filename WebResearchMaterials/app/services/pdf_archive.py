from __future__ import annotations

import re
from tempfile import SpooledTemporaryFile
from typing import BinaryIO
from zipfile import ZIP_STORED, ZipFile

from app.models import TepTin
from app.services.files import resolve_stored_path


def _pdf_name(title: str, used: set[str]) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", title).strip(" .")[:180].rstrip(" .") or "Tài liệu"
    if re.fullmatch(r"CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]", name.split(".", 1)[0], re.IGNORECASE):
        name = f"_{name}"
    candidate = name
    number = 2
    while candidate.casefold() in used:
        candidate = f"{name[:170].rstrip()} ({number})"
        number += 1
    used.add(candidate.casefold())
    return f"PDF/{candidate}.pdf"


def create_pdf_archive(files: list[tuple[str, TepTin]]) -> BinaryIO | None:
    archive = SpooledTemporaryFile(max_size=8 * 1024 * 1024)
    used: set[str] = set()
    with ZipFile(archive, "w", compression=ZIP_STORED, allowZip64=True) as zipped:
        for title, record in files:
            if record.mime_type != "application/pdf":
                continue
            try:
                path = resolve_stored_path(record)
            except FileNotFoundError:
                continue
            zipped.write(path, _pdf_name(title, used))
    if not used:
        archive.close()
        return None
    archive.seek(0)
    return archive
