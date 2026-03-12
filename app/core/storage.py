from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from fastapi import UploadFile

from app.core.config import settings

ALLOWED_AUDIO_EXTENSIONS = {
    ".mp3",
    ".wav",
    ".m4a",
    ".aac",
    ".ogg",
    ".oga",
    ".opus",
    ".webm",
    ".mp4",
}


@dataclass(slots=True)
class StoredFile:
    original_filename: str | None
    content_type: str
    size_bytes: int
    absolute_path: str
    relative_path: str


def ensure_storage_dirs() -> None:
    base = Path(settings.UPLOADS_DIR)
    (base / "voice").mkdir(parents=True, exist_ok=True)


def _guess_extension(upload: UploadFile) -> str:
    suffix = Path(upload.filename or "").suffix.lower()
    if suffix in ALLOWED_AUDIO_EXTENSIONS:
        return suffix

    content_type = (upload.content_type or "").lower()
    mapping = {
        "audio/mpeg": ".mp3",
        "audio/mp3": ".mp3",
        "audio/wav": ".wav",
        "audio/x-wav": ".wav",
        "audio/webm": ".webm",
        "audio/ogg": ".ogg",
        "audio/opus": ".opus",
        "audio/mp4": ".m4a",
        "audio/aac": ".aac",
    }
    return mapping.get(content_type, ".bin")


def _get_upload_size_bytes(upload: UploadFile) -> int:
    upload.file.seek(0, os.SEEK_END)
    size = upload.file.tell()
    upload.file.seek(0)
    return size


def save_voice_file(upload: UploadFile) -> StoredFile:
    if not upload.filename:
        raise ValueError("Выбери аудиофайл.")

    content_type = (upload.content_type or "").lower()
    ext = _guess_extension(upload)
    size_bytes = _get_upload_size_bytes(upload)

    if not content_type.startswith("audio/") and ext not in ALLOWED_AUDIO_EXTENSIONS:
        raise ValueError("Поддерживаются только аудиофайлы.")

    max_bytes = settings.MAX_VOICE_FILE_MB * 1024 * 1024
    if size_bytes > max_bytes:
        raise ValueError(
            f"Файл слишком большой. Максимум — {settings.MAX_VOICE_FILE_MB} МБ."
        )

    now = datetime.utcnow()
    relative_path = (
        Path("voice")
        / str(now.year)
        / f"{now.month:02d}"
        / f"{uuid4().hex}{ext}"
    )

    absolute_path = Path(settings.UPLOADS_DIR) / relative_path
    absolute_path.parent.mkdir(parents=True, exist_ok=True)

    with absolute_path.open("wb") as out_file:
        shutil.copyfileobj(upload.file, out_file)

    return StoredFile(
        original_filename=upload.filename,
        content_type=content_type or "application/octet-stream",
        size_bytes=size_bytes,
        absolute_path=str(absolute_path),
        relative_path=relative_path.as_posix(),
    )
