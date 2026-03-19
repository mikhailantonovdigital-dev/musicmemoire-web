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
    storage_backend: str = "local"
    storage_bucket: str | None = None
    storage_key: str | None = None


class StorageError(RuntimeError):
    pass


def ensure_storage_dirs() -> None:
    base = Path(settings.UPLOADS_DIR)
    (base / "voice").mkdir(parents=True, exist_ok=True)
    (base / "songs").mkdir(parents=True, exist_ok=True)
    (base / "tmp").mkdir(parents=True, exist_ok=True)


def object_storage_enabled() -> bool:
    return bool(
        (settings.OBJECT_STORAGE_BUCKET or "").strip()
        and (settings.OBJECT_STORAGE_ACCESS_KEY_ID or "").strip()
        and (settings.OBJECT_STORAGE_SECRET_ACCESS_KEY or "").strip()
    )


def _get_upload_size_bytes(upload: UploadFile) -> int:
    upload.file.seek(0, os.SEEK_END)
    size = upload.file.tell()
    upload.file.seek(0)
    return size


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


def resolve_storage_path(relative_path: str) -> Path:
    base = Path(settings.UPLOADS_DIR).resolve()
    candidate = (base / relative_path).resolve()
    if base != candidate and base not in candidate.parents:
        raise StorageError("Некорректный путь к файлу.")
    return candidate


def _object_storage_prefix() -> str:
    return (settings.OBJECT_STORAGE_PREFIX or "").strip("/")


def build_object_storage_key(relative_path: str) -> str:
    relative = relative_path.strip().lstrip("/")
    prefix = _object_storage_prefix()
    return f"{prefix}/{relative}" if prefix else relative


def _create_s3_client():
    try:
        import boto3
        from botocore.config import Config
    except Exception as exc:  # noqa: BLE001
        raise StorageError("Не установлен boto3 для object storage.") from exc

    addressing_style = "path" if settings.OBJECT_STORAGE_FORCE_PATH_STYLE else "virtual"
    config = Config(
        signature_version="s3v4",
        s3={"addressing_style": addressing_style},
    )
    return boto3.client(
        "s3",
        endpoint_url=(settings.OBJECT_STORAGE_ENDPOINT_URL or None),
        region_name=(settings.OBJECT_STORAGE_REGION or None),
        aws_access_key_id=settings.OBJECT_STORAGE_ACCESS_KEY_ID,
        aws_secret_access_key=settings.OBJECT_STORAGE_SECRET_ACCESS_KEY,
        config=config,
    )


def _upload_local_file_to_object_storage(
    *,
    local_path: Path,
    relative_path: str,
    content_type: str,
) -> tuple[str, str]:
    if not object_storage_enabled():
        raise StorageError("Object storage не настроен.")

    bucket = (settings.OBJECT_STORAGE_BUCKET or "").strip()
    storage_key = build_object_storage_key(relative_path)
    client = _create_s3_client()

    extra_args = {"ContentType": (content_type or "application/octet-stream").split(";", 1)[0].strip()}

    try:
        with local_path.open("rb") as file_obj:
            client.upload_fileobj(file_obj, bucket, storage_key, ExtraArgs=extra_args)
    except Exception as exc:  # noqa: BLE001
        raise StorageError(f"Не удалось загрузить файл в object storage: {exc}") from exc

    return bucket, storage_key


def _download_object_to_local_cache(
    *,
    storage_key: str,
    relative_path: str,
    max_bytes: int,
) -> Path:
    if not object_storage_enabled():
        raise StorageError("Object storage не настроен.")

    bucket = (settings.OBJECT_STORAGE_BUCKET or "").strip()
    client = _create_s3_client()
    target_path = resolve_storage_path(relative_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        response = client.get_object(Bucket=bucket, Key=storage_key)
    except Exception as exc:  # noqa: BLE001
        raise StorageError(f"Не удалось скачать файл из object storage: {exc}") from exc

    body = response.get("Body")
    content_length = int(response.get("ContentLength") or 0)
    if content_length and content_length > max_bytes:
        raise StorageError("Файл в object storage превышает допустимый размер.")

    size_bytes = 0
    try:
        with target_path.open("wb") as out_file:
            while True:
                chunk = body.read(1024 * 256)
                if not chunk:
                    break
                size_bytes += len(chunk)
                if size_bytes > max_bytes:
                    out_file.close()
                    target_path.unlink(missing_ok=True)
                    raise StorageError("Файл в object storage превышает допустимый размер.")
                out_file.write(chunk)
    finally:
        try:
            body.close()
        except Exception:  # noqa: BLE001
            pass

    return target_path


def ensure_local_cache_from_object_storage(
    *,
    storage_key: str | None,
    relative_path: str,
    max_bytes: int,
) -> Path:
    if not storage_key:
        raise StorageError("Для файла не сохранён object storage key.")

    local_path = resolve_storage_path(relative_path)
    if local_path.exists():
        return local_path

    return _download_object_to_local_cache(
        storage_key=storage_key,
        relative_path=relative_path,
        max_bytes=max_bytes,
    )


def ensure_voice_input_local_path(voice_input) -> Path:
    local_path = Path(voice_input.storage_path)
    if local_path.exists():
        return local_path

    backend = (getattr(voice_input, "storage_backend", None) or "local").strip() or "local"
    if backend != "s3":
        raise StorageError("Файл голосового не найден на сервере.")

    return ensure_local_cache_from_object_storage(
        storage_key=(getattr(voice_input, "storage_key", None) or "").strip() or None,
        relative_path=voice_input.relative_path,
        max_bytes=settings.MAX_VOICE_FILE_MB * 1024 * 1024,
    )


def _save_local_upload(upload: UploadFile) -> StoredFile:
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
        storage_backend="local",
        storage_bucket=None,
        storage_key=None,
    )


def save_voice_file(upload: UploadFile) -> StoredFile:
    stored = _save_local_upload(upload)

    if not object_storage_enabled():
        return stored

    bucket, storage_key = _upload_local_file_to_object_storage(
        local_path=Path(stored.absolute_path),
        relative_path=stored.relative_path,
        content_type=stored.content_type,
    )
    stored.storage_backend = "s3"
    stored.storage_bucket = bucket
    stored.storage_key = storage_key
    return stored


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

    stored = StoredFile(
        original_filename=f"{safe_order_number}-track-{track_index + 1}{ext}",
        content_type=(content_type or "audio/mpeg").split(";", 1)[0].strip() or "audio/mpeg",
        size_bytes=size_bytes,
        absolute_path=str(absolute_path),
        relative_path=relative_path.as_posix(),
        storage_backend="local",
        storage_bucket=None,
        storage_key=None,
    )

    if object_storage_enabled():
        bucket, storage_key = _upload_local_file_to_object_storage(
            local_path=absolute_path,
            relative_path=stored.relative_path,
            content_type=stored.content_type,
        )
        stored.storage_backend = "s3"
        stored.storage_bucket = bucket
        stored.storage_key = storage_key

    return stored
