from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from app.core.config import settings
from app.core.security import utcnow


class SunoServiceError(RuntimeError):
    pass


@dataclass(slots=True)
class SongStartResult:
    external_job_id: str
    status: str
    raw: dict


@dataclass(slots=True)
class SongSyncResult:
    status: str
    audio_url: str | None
    error_message: str | None
    raw: dict


def start_song_generation(*, order_number: str, lyrics_text: str) -> SongStartResult:
    if settings.SUNO_STUB_MODE:
        stub_job_id = f"stub-{uuid4().hex[:12]}"
        return SongStartResult(
            external_job_id=stub_job_id,
            status="processing",
            raw={
                "mode": "stub",
                "order_number": order_number,
                "lyrics_chars": len(lyrics_text),
                "job_id": stub_job_id,
            },
        )

    if not settings.SUNO_API_KEY:
        raise SunoServiceError("Suno ещё не настроен. Пока оставь SUNO_STUB_MODE=true.")

    raise SunoServiceError("Реальная интеграция Suno будет добавлена следующим коммитом.")


def sync_song_generation(*, external_job_id: str | None, started_at: datetime | None) -> SongSyncResult:
    if settings.SUNO_STUB_MODE:
        now = utcnow()
        elapsed = 0
        if started_at is not None:
            elapsed = max(0, int((now - started_at).total_seconds()))

        if elapsed < settings.SUNO_STUB_DELAY_SECONDS:
            return SongSyncResult(
                status="processing",
                audio_url=None,
                error_message=None,
                raw={
                    "mode": "stub",
                    "job_id": external_job_id,
                    "elapsed_seconds": elapsed,
                    "eta_seconds": max(0, settings.SUNO_STUB_DELAY_SECONDS - elapsed),
                },
            )

        return SongSyncResult(
            status="succeeded",
            audio_url=settings.SUNO_STUB_AUDIO_URL,
            error_message=None,
            raw={
                "mode": "stub",
                "job_id": external_job_id,
                "elapsed_seconds": elapsed,
                "audio_url": settings.SUNO_STUB_AUDIO_URL,
            },
        )

    if not settings.SUNO_API_KEY:
        raise SunoServiceError("Suno ещё не настроен. Пока оставь SUNO_STUB_MODE=true.")

    raise SunoServiceError("Реальная синхронизация Suno будет добавлена следующим коммитом.")
