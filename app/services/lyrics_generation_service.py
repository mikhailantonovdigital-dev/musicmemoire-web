from __future__ import annotations

import asyncio
from dataclasses import dataclass

from google import genai
from openai import AsyncOpenAI

from app.core.config import settings


class LyricsGenerationError(RuntimeError):
    pass


@dataclass(slots=True)
class GeneratedLyricsVersion:
    provider: str
    model_name: str
    angle_label: str
    prompt_text: str
    lyrics_text: str


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
        raise LyricsGenerationError("Не настроен OPENAI_API_KEY.")
    if not settings.OPENAI_MODEL:
        raise LyricsGenerationError("Не настроен OPENAI_MODEL.")

    client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)

    try:
        response = await client.responses.create(
            model=settings.OPENAI_MODEL,
            input=prompt_text,
        )
    except Exception as exc:
        raise LyricsGenerationError(
            "OpenAI не смог сгенерировать первую версию текста."
        ) from exc
    finally:
        await client.close()

    text = normalize_lyrics_text(response.output_text or "")
    if not text:
        raise LyricsGenerationError(
            "OpenAI вернул пустой текст."
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
        raise LyricsGenerationError("Не настроен GEMINI_API_KEY.")
    if not settings.GEMINI_MODEL_PRIMARY:
        raise LyricsGenerationError("Не настроен GEMINI_MODEL_PRIMARY.")

    client = genai.Client(api_key=settings.GEMINI_API_KEY)

    try:
        response = client.models.generate_content(
            model=settings.GEMINI_MODEL_PRIMARY,
            contents=prompt_text,
        )
    except Exception as exc:
        raise LyricsGenerationError(
            "Gemini не смог сгенерировать вторую версию текста."
        ) from exc

    text = normalize_lyrics_text(response.text or "")
    if not text:
        raise LyricsGenerationError(
            "Gemini вернул пустой текст."
        )

    return GeneratedLyricsVersion(
        provider="gemini",
        model_name=settings.GEMINI_MODEL_PRIMARY,
        angle_label="Структурная версия",
        prompt_text=prompt_text,
        lyrics_text=text,
    )


async def generate_dual_lyrics_versions(story_text: str) -> list[GeneratedLyricsVersion]:
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

    errors: list[str] = []
    versions: list[GeneratedLyricsVersion] = []

    for result in results:
        if isinstance(result, Exception):
            errors.append(str(result))
        else:
            versions.append(result)

    if errors:
        raise LyricsGenerationError(" ".join(errors))

    versions.sort(key=lambda item: 0 if item.provider == "openai" else 1)
    return versions
