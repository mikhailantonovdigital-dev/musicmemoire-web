from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
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


class StorageError(RuntimeError):
    pass


def ensure_storage_dirs() -> None:
    base = Path(settings.UPLOADS_DIR)
    (base / "voice").mkdir(parents=True, exist_ok=True)
    (base / "songs").mkdir(parents=True, exist_ok=True)


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


def _guess_remote_extension(url: str, content_type: str | None) -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in ALLOWED_AUDIO_EXTENSIONS:
        return suffix

    normalized_type = (content_type or "").split(";", 1)[0].strip().lower()
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
        "application/octet-stream": ".mp3",
    }
    return mapping.get(normalized_type, ".mp3")


def _get_upload_size_bytes(upload: UploadFile) -> int:
    upload.file.seek(0, os.SEEK_END)
    size = upload.file.tell()
    upload.file.seek(0)
    return size


def resolve_storage_path(relative_path: str) -> Path:
    base = Path(settings.UPLOADS_DIR).resolve()
    candidate = (base / relative_path).resolve()
    if base != candidate and base not in candidate.parents:
        raise StorageError("Некорректный путь к файлу.")
    return candidate


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


def cache_remote_song_file(remote_url: str, *, order_number: str, song_public_id: str, track_index: int) -> StoredFile:
    url = (remote_url or "").strip()
    if not url:
        raise StorageError("Не передана ссылка на аудиофайл.")

    max_bytes = settings.MAX_SONG_FILE_MB * 1024 * 1024
    now = datetime.utcnow()
    safe_order_number = "".join(ch for ch in (order_number or "song") if ch.isalnum() or ch in {"-", "_"}) or "song"

    req = Request(
        url,
        headers={
            "Accept": "audio/*,application/octet-stream;q=0.9,*/*;q=0.8",
            "User-Agent": f"{settings.APP_NAME}/song-cache",
        },
        method="GET",
    )

    try:
        with urlopen(req, timeout=max(5, int(settings.SUNO_REQUEST_TIMEOUT_SECONDS))) as response:
            content_type = response.headers.get_content_type() or response.headers.get("Content-Type") or "audio/mpeg"
            ext = _guess_remote_extension(url, content_type)
            relative_path = (
                Path("songs")
                / str(now.year)
                / f"{now.month:02d}"
                / safe_order_number
                / f"{song_public_id}-track-{track_index + 1}{ext}"
            )
            absolute_path = Path(settings.UPLOADS_DIR) / relative_path
            absolute_path.parent.mkdir(parents=True, exist_ok=True)

            size_bytes = 0
            with absolute_path.open("wb") as out_file:
                while True:
                    chunk = response.read(1024 * 256)
                    if not chunk:
                        break
                    size_bytes += len(chunk)
                    if size_bytes > max_bytes:
                        out_file.close()
                        try:
                            absolute_path.unlink(missing_ok=True)
                        except OSError:
                            pass
                        raise StorageError(
                            f"Файл песни слишком большой для локального кеша. Максимум — {settings.MAX_SONG_FILE_MB} МБ."
                        )
                    out_file.write(chunk)
    except HTTPError as exc:
        raise StorageError(f"Не удалось скачать аудио песни: HTTP {exc.code}.") from exc
    except URLError as exc:
        raise StorageError(f"Не удалось скачать аудио песни: {exc.reason}.") from exc
    except StorageError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise StorageError(f"Не удалось сохранить аудио песни: {exc}") from exc

    return StoredFile(
        original_filename=f"{safe_order_number}-track-{track_index + 1}{ext}",
        content_type=(content_type or "audio/mpeg").split(";", 1)[0].strip() or "audio/mpeg",
        size_bytes=size_bytes,
        absolute_path=str(absolute_path),
        relative_path=relative_path.as_posix(),
    )
