from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from openai import AsyncOpenAI

from app.core.config import settings

logger = logging.getLogger(__name__)


class LyricsGenerationError(RuntimeError):
    pass


class ProviderLyricsGenerationError(LyricsGenerationError):
    def __init__(
        self,
        slot_label: str,
        user_message: str,
        technical_message: str,
    ) -> None:
        super().__init__(user_message)
        self.slot_label = slot_label
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


def build_variant_1_prompt(story_text: str) -> str:
    return f"""
Ты — сильный русскоязычный автор песенных текстов.

Задача:
написать персональный текст песни на основе истории клиента.

Это Вариант 1.
Подход этой версии:
- эмоционально
- образно
- цепляюще
- трогательно
- сильный и запоминающийся припев
- больше красивых формулировок и образов

Очень важно:
- выдай текст СТРОГО в такой структуре:
[Куплет 1]
...
[Припев]
...
[Куплет 2]
...
[Припев]
...
[Бридж]
...
[Финальный припев]
...

- ограничения по длине секций:
  - [Куплет 1] — ровно восемь строк
  - [Припев] — ровно восемь строк: сначала четыре строки, затем повтор этих же четырёх строк
  - [Куплет 2] — ровно восемь строк
  - [Бридж] — ровно четыре строки
  - [Финальный припев] — ровно восемь строк: сначала четыре строки, затем повтор этих же четырёх строк

- суммарно должно быть тридцать шесть строк текста (без учёта заголовков секций)
- цель по длительности: примерно три–четыре минуты, без затянутых длинных куплетов

Правила:
- только текст песни
- без пояснений
- без markdown-блоков
- без лишних комментариев
- русский язык
- любые цифры (числа, годы, даты) пиши словами, не цифрами
- названия брендов, аббревиатуры и англоязычные названия пиши так, как они должны произноситься по-русски
- используй реальные детали из истории
- избегай банальностей и избитых штампов
- строки должны быть удобны для вокального исполнения

История клиента:
{story_text}
""".strip()


def build_variant_2_prompt(story_text: str) -> str:
    return f"""
Ты — опытный русскоязычный сонграйтер.

Нужно написать персональный текст песни на основе истории клиента.

Это Вариант 2.
Подход этой версии:
- более структурно
- более чётко
- проще и музыкальнее
- ясные формулировки
- хороший ритм фраз
- текст должен лучше подходить для последующей генерации песни

Очень важно:
- выдай текст СТРОГО в такой структуре:
[Куплет 1]
...
[Припев]
...
[Куплет 2]
...
[Припев]
...
[Бридж]
...
[Финальный припев]
...

- ограничения по длине секций:
  - [Куплет 1] — ровно восемь строк
  - [Припев] — ровно восемь строк: сначала четыре строки, затем повтор этих же четырёх строк
  - [Куплет 2] — ровно восемь строк
  - [Бридж] — ровно четыре строки
  - [Финальный припев] — ровно восемь строк: сначала четыре строки, затем повтор этих же четырёх строк

- суммарно должно быть тридцать шесть строк текста (без учёта заголовков секций)
- цель по длительности: примерно три–четыре минуты, без затянутых длинных куплетов

Правила:
- только текст песни
- без пояснений
- без markdown-блоков
- без лишних комментариев
- русский язык
- любые цифры (числа, годы, даты) пиши словами, не цифрами
- названия брендов, аббревиатуры и англоязычные названия пиши так, как они должны произноситься по-русски
- опирайся на реальные детали истории
- делай припев сильным, но понятным
- текст должен легко ложиться на музыку

История клиента:
{story_text}
""".strip()


async def generate_openai_variant(
    *,
    slot_label: str,
    prompt_text: str,
    angle_label: str,
) -> GeneratedLyricsVersion:
    if not settings.OPENAI_API_KEY:
        raise ProviderLyricsGenerationError(
            slot_label=slot_label,
            user_message=f"Не удалось сгенерировать {angle_label.lower()}.",
            technical_message="OPENAI_API_KEY is not configured.",
        )

    if not settings.OPENAI_MODEL:
        raise ProviderLyricsGenerationError(
            slot_label=slot_label,
            user_message=f"Не удалось сгенерировать {angle_label.lower()}.",
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
        logger.exception("Lyrics generation failed for %s", slot_label)
        raise ProviderLyricsGenerationError(
            slot_label=slot_label,
            user_message=f"Не удалось сгенерировать {angle_label.lower()}.",
            technical_message=technical,
        ) from exc
    finally:
        await client.close()

    text = normalize_lyrics_text(response.output_text or "")
    if not text:
        raise ProviderLyricsGenerationError(
            slot_label=slot_label,
            user_message=f"{angle_label} получился пустым.",
            technical_message=f"{slot_label}: OpenAI response.output_text is empty.",
        )

    return GeneratedLyricsVersion(
        provider="openai",
        model_name=settings.OPENAI_MODEL,
        angle_label=angle_label,
        prompt_text=prompt_text,
        lyrics_text=text,
    )


async def generate_dual_lyrics_versions(story_text: str) -> DualGenerationResult:
    source_text = (story_text or "").strip()
    if not source_text:
        raise LyricsGenerationError("Сначала нужно заполнить историю для песни.")

    prompt_1 = build_variant_1_prompt(source_text)
    prompt_2 = build_variant_2_prompt(source_text)

    results = await asyncio.gather(
        generate_openai_variant(
            slot_label="variant_1",
            prompt_text=prompt_1,
            angle_label="Вариант 1",
        ),
        generate_openai_variant(
            slot_label="variant_2",
            prompt_text=prompt_2,
            angle_label="Вариант 2",
        ),
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
                slot_label="unknown",
                user_message="Одна из версий текста не смогла сгенерироваться.",
                technical_message=technical,
            )
        )

    if not versions:
        joined = " ".join(item.user_message for item in errors) or "Не удалось сгенерировать ни одной версии текста."
        raise LyricsGenerationError(joined)

    order_map = {"Вариант 1": 0, "Вариант 2": 1}
    versions.sort(key=lambda item: order_map.get(item.angle_label, 999))

    return DualGenerationResult(
        versions=versions,
        errors=errors,
    )
