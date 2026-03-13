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


@dataclass(slots=True)
class DualLyricsGenerationResult:
    versions: list[GeneratedLyricsVersion]
    errors: dict[str, str]


def normalize_lyrics_text(text: str) -> str:
    value = (text or "").strip()

    if value.startswith("```"):
        value = value.strip("`").strip()

    prefixes
