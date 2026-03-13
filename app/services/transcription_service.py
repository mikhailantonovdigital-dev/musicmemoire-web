from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from openai import AsyncOpenAI

from app.core.config import settings


class TranscriptionServiceError(RuntimeError):
    pass


@dataclass(slots=True)
class TranscriptionResult:
    text: str
    model: str
    language: str | None = None


async def transcribe_audio_file(file_path: str) -> TranscriptionResult:
    if not settings.OPENAI_API_KEY:
        raise TranscriptionServiceError(
            "Не настроен OPENAI_API_KEY для авторасшифровки."
        )

    path = Path(file_path)
    if not path.exists():
        raise TranscriptionServiceError("Файл голосового не найден на сервере.")

    client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)

    try:
        with path.open("rb") as audio_file:
            response = await client.audio.transcriptions.create(
                file=audio_file,
                model=settings.OPENAI_TRANSCRIBE_MODEL,
                language=settings.AUDIO_TRANSCRIBE_LANGUAGE or None,
                response_format="json",
            )
    except Exception as exc:
        raise TranscriptionServiceError(
            "Не удалось распознать голосовое. Попробуй ещё раз чуть позже."
        ) from exc
    finally:
        await client.close()

    text = (getattr(response, "text", "") or "").strip()
    if not text:
        raise TranscriptionServiceError(
            "Распознавание завершилось без текста. Попробуй запись получше или перезапусти расшифровку."
        )

    language = getattr(response, "language", None)

    return TranscriptionResult(
        text=text,
        model=settings.OPENAI_TRANSCRIBE_MODEL,
        language=language,
    )
