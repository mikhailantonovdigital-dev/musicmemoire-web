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


async def _cleanup_transcript_text(client: AsyncOpenAI, text: str) -> str:
    model = (settings.OPENAI_MODEL or "").strip()
    if not model:
        return text

    try:
        response = await client.chat.completions.create(
            model=model,
            temperature=0.1,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Ты редактор расшифровок. Приведи текст в аккуратный читаемый вид: "
                        "исправь явные оговорки, орфографию, пунктуацию и разбиение на абзацы. "
                        "Не добавляй новые факты, не сокращай смысл и не выдумывай детали. "
                        "Верни только готовый очищенный текст без пояснений."
                    ),
                },
                {"role": "user", "content": text},
            ],
        )
    except Exception:
        return text

    cleaned = (response.choices[0].message.content or "").strip() if getattr(response, "choices", None) else ""
    return cleaned or text


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

        text = (getattr(response, "text", "") or "").strip()
        if not text:
            raise TranscriptionServiceError(
                "Распознавание завершилось без текста. Попробуй запись получше или перезапусти расшифровку."
            )

        text = await _cleanup_transcript_text(client, text)
        language = getattr(response, "language", None)
    except TranscriptionServiceError:
        raise
    except Exception as exc:
        raise TranscriptionServiceError(
            "Не удалось распознать голосовое. Попробуй ещё раз чуть позже."
        ) from exc
    finally:
        await client.close()

    return TranscriptionResult(
        text=text,
        model=settings.OPENAI_TRANSCRIBE_MODEL,
        language=language,
    )
