from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from google import genai
from openai import AsyncOpenAI

from app.core.config import settings

logger = logging.getLogger(__name__)


class LyricsGenerationError(RuntimeError):
    pass


class ProviderLyricsGenerationError(LyricsGenerationError):
    def __init__(
        self,
        provider: str,
        user_message: str,
        technical_message: str,
    ) -> None:
        super().__init__(user_message)
        self.provider = provider
        self.user_message = user_message
        self.technical_message = technical_message


@dataclass(slots=True)
class GeneratedLyricsVersion:
    provider: str
    model_name: str
    angle_label: str
    prompt_text: str
    lyrics_text: str


@dataclass(slots=True)
class DualGenerationResult:
    versions: list[GeneratedLyricsVersion]
    errors: list[ProviderLyricsGenerationError]


def normalize_lyrics_text(text: str) -> str:
    value = (text or "").strip()

    if value.startswith("```"):
        value = value.strip("`").strip()

    prefixes = [
        "Вот текст песни:",
        "Вот версия текста:",
        "Вот вариант текста:",
        "Конечно, вот текст песни:",
        "Конечно! Вот текст песни:",
    ]
    for prefix in prefixes:
        if value.startswith(prefix):
            value = value[len(prefix):].strip()

    return value.strip()


def build_openai_prompt(story_text: str) -> str:
    return f"""
Ты — сильный русскоязычный автор песенных текстов.

Задача:
написать персональный текст песни на основе истории клиента.

Подход этой версии:
- эмоционально
- ярко
- современно
- с сильными образами
- с цепляющим припевом
- без банальных штампов

Правила:
- пиши только текст песни
- без объяснений, без комментариев, без markdown
- не добавляй заголовок и служебные пометки вроде "Куплет 1"
- текст должен быть естественным для вокального исполнения
- используй конкретные детали из истории
- русский язык

История клиента:
{story_text}
""".strip()


def build_gemini_prompt(story_text: str) -> str:
    return f"""
Ты — опытный русскоязычный сонграйтер.

Нужно написать персональный текст песни на основе истории клиента.

Подход этой версии:
- более структурно
- цельно
- музыкально
- понятные куплеты и сильный припев
- хороший баланс эмоции и ясности
- без перегруза и без лишнего пафоса

Правила:
- выдай только текст песни
- не пиши пояснений
- не используй markdown
- не добавляй заголовок
- текст должен легко ложиться на музыку
- опирайся на реальные детали из истории
- русский язык

История клиента:
{story_text}
""".strip()


async def generate_openai_lyrics(prompt_text: str) -> GeneratedLyricsVersion:
    if not settings.OPENAI_API_KEY:
        raise ProviderLyricsGenerationError(
            provider="openai",
            user_message="OpenAI временно недоступен.",
            technical_message="OPENAI_API_KEY is not configured.",
        )
    if not settings.OPENAI_MODEL:
        raise ProviderLyricsGenerationError(
            provider="openai",
            user_message="OpenAI временно недоступен.",
            technical_message="OPENAI_MODEL is not configured.",
        )

    client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)

    try:
        response = await client.responses.create(
            model=settings.OPENAI_MODEL,
            input=prompt_text,
        )
    except Exception as exc:
        technical = f"{exc.__class__.__name__}: {exc}"
        logger.exception("OpenAI lyrics generation failed")
        raise ProviderLyricsGenerationError(
            provider="openai",
            user_message="OpenAI не смог сгенерировать первую версию текста.",
            technical_message=technical,
        ) from exc
    finally:
        await client.close()

    text = normalize_lyrics_text(response.output_text or "")
    if not text:
        raise ProviderLyricsGenerationError(
            provider="openai",
            user_message="OpenAI вернул пустую версию текста.",
            technical_message="OpenAI response.output_text is empty.",
        )

    return GeneratedLyricsVersion(
        provider="openai",
        model_name=settings.OPENAI_MODEL,
        angle_label="Эмоциональная версия",
        prompt_text=prompt_text,
        lyrics_text=text,
    )


def generate_gemini_lyrics_sync(prompt_text: str) -> GeneratedLyricsVersion:
    if not settings.GEMINI_API_KEY:
        raise ProviderLyricsGenerationError(
            provider="gemini",
            user_message="Gemini временно недоступен.",
            technical_message="GEMINI_API_KEY is not configured.",
        )
    if not settings.GEMINI_MODEL_PRIMARY:
        raise ProviderLyricsGenerationError(
            provider="gemini",
            user_message="Gemini временно недоступен.",
            technical_message="GEMINI_MODEL_PRIMARY is not configured.",
        )

    client = genai.Client(api_key=settings.GEMINI_API_KEY)

    try:
        response = client.models.generate_content(
            model=settings.GEMINI_MODEL_PRIMARY,
            contents=prompt_text,
        )
    except Exception as exc:
        technical = f"{exc.__class__.__name__}: {exc}"
        logger.exception("Gemini lyrics generation failed")
        raise ProviderLyricsGenerationError(
            provider="gemini",
            user_message="Gemini не смог сгенерировать вторую версию текста.",
            technical_message=technical,
        ) from exc

    text = normalize_lyrics_text(response.text or "")
    if not text:
        raise ProviderLyricsGenerationError(
            provider="gemini",
            user_message="Gemini вернул пустую версию текста.",
            technical_message="Gemini response.text is empty.",
        )

    return GeneratedLyricsVersion(
        provider="gemini",
        model_name=settings.GEMINI_MODEL_PRIMARY,
        angle_label="Структурная версия",
        prompt_text=prompt_text,
        lyrics_text=text,
    )


async def generate_dual_lyrics_versions(story_text: str) -> DualGenerationResult:
    source_text = (story_text or "").strip()
    if not source_text:
        raise LyricsGenerationError("Сначала нужно заполнить историю для песни.")

    openai_prompt = build_openai_prompt(source_text)
    gemini_prompt = build_gemini_prompt(source_text)

    results = await asyncio.gather(
        generate_openai_lyrics(openai_prompt),
        asyncio.to_thread(generate_gemini_lyrics_sync, gemini_prompt),
        return_exceptions=True,
    )

    versions: list[GeneratedLyricsVersion] = []
    errors: list[ProviderLyricsGenerationError] = []

    for result in results:
        if isinstance(result, GeneratedLyricsVersion):
            versions.append(result)
            continue

        if isinstance(result, ProviderLyricsGenerationError):
            errors.append(result)
            continue

        technical = f"{result.__class__.__name__}: {result}"
        logger.exception("Unknown lyrics generation error", exc_info=result)
        errors.append(
            ProviderLyricsGenerationError(
                provider="unknown",
                user_message="Одна из моделей не смогла сгенерировать текст.",
                technical_message=technical,
            )
        )

    if not versions:
        joined = " ".join(item.user_message for item in errors) or "Не удалось сгенерировать ни одной версии текста."
        raise LyricsGenerationError(joined)

    versions.sort(key=lambda item: 0 if item.provider == "openai" else 1)
    return DualGenerationResult(
        versions=versions,
        errors=errors,
    )
