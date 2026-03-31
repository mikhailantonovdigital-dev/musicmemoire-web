from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4

from app.core.config import settings
from app.core.security import utcnow


class SunoServiceError(RuntimeError):
    pass


@dataclass(slots=True)
class SongStartResult:
    external_job_id: str
    status: str
    raw: dict[str, Any]


@dataclass(slots=True)
class SongSyncResult:
    status: str
    audio_url: str | None
    result_tracks: list[dict[str, Any]]
    error_message: str | None
    raw: dict[str, Any]


@dataclass(slots=True)
class SongCallbackResult:
    external_job_id: str | None
    callback_type: str | None
    sync_result: SongSyncResult


STYLE_MAP = {
    "pop": "Modern chart pop, big chorus, polished topline, radio-ready production",
    "rap": "Modern melodic rap, catchy flow, streaming-ready hooks, crisp low end",
    "rock": "Modern pop rock, anthemic chorus, emotional guitars, stadium energy",
    "chanson": (
        "Russian chanson, heartfelt storytelling, expressive lead vocal, acoustic guitar and accordion, "
        "intimate tavern atmosphere, emotional chorus, realistic live arrangement, no glossy pop production"
    ),
    "indie": "Modern indie pop, atmospheric texture, intimate vocals, tasteful hook",
}

MOOD_MAP = {
    "romantic": "romantic, warm, intimate, in love",
    "uplifting": "uplifting, inspiring, bright, hopeful",
    "nostalgic": "nostalgic, heartfelt, bittersweet, reflective",
    "dramatic": "dramatic, emotionally powerful, cinematic, intense",
    "party": "celebratory, energetic, feel-good, danceable",
}

BRAND_PRONUNCIATION_MAP = {
    "kfc": "кей эф си",
    "bmw": "би эм дабл-ю",
    "mcdonalds": "макдоналдс",
    "mcdonald's": "макдоналдс",
    "nike": "найки",
    "iphone": "айфон",
}

DATE_PATTERN = re.compile(r"\b(0?[1-9]|[12]\d|3[01])[.\-/](0?[1-9]|1[0-2])[.\-/](\d{4})\b")
NUMBER_PATTERN = re.compile(r"(?<![\w])\d{1,6}(?![\w-])")
HEADER_LINE_PATTERN = re.compile(r"^\s*\[[^\]]+\]\s*$")

MONTHS_RU_GENITIVE = {
    1: "января",
    2: "февраля",
    3: "марта",
    4: "апреля",
    5: "мая",
    6: "июня",
    7: "июля",
    8: "августа",
    9: "сентября",
    10: "октября",
    11: "ноября",
    12: "декабря",
}

DAY_ORDINALS = {
    1: "первое",
    2: "второе",
    3: "третье",
    4: "четвёртое",
    5: "пятое",
    6: "шестое",
    7: "седьмое",
    8: "восьмое",
    9: "девятое",
    10: "десятое",
    11: "одиннадцатое",
    12: "двенадцатое",
    13: "тринадцатое",
    14: "четырнадцатое",
    15: "пятнадцатое",
    16: "шестнадцатое",
    17: "семнадцатое",
    18: "восемнадцатое",
    19: "девятнадцатое",
    20: "двадцатое",
    21: "двадцать первое",
    22: "двадцать второе",
    23: "двадцать третье",
    24: "двадцать четвёртое",
    25: "двадцать пятое",
    26: "двадцать шестое",
    27: "двадцать седьмое",
    28: "двадцать восьмое",
    29: "двадцать девятое",
    30: "тридцатое",
    31: "тридцать первое",
}


def _safe_json_loads(raw_text: str) -> dict[str, Any] | list[Any] | None:
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        return None


def _ensure_api_ready() -> None:
    if not settings.SUNO_API_KEY:
        raise SunoServiceError("Suno API key не настроен. Заполни SUNO_API_KEY и выключи SUNO_STUB_MODE.")


def _base_url() -> str:
    return (settings.SUNO_API_BASE_URL or "https://api.sunoapi.org").rstrip("/")


def _build_request_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.SUNO_API_KEY}",
        "Accept": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/145.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "Cache-Control": "no-cache",
    }


def _humanize_http_error(exc: HTTPError, raw_text: str) -> str:
    parsed = _safe_json_loads(raw_text)
    if isinstance(parsed, dict):
        error_name = str(parsed.get("error_name") or "").strip().lower()
        detail = str(parsed.get("detail") or parsed.get("message") or parsed.get("msg") or "").strip()
        if exc.code == 403 and error_name == "browser_signature_banned":
            return (
                "Сервис генерации отклонил запрос через защиту Cloudflare (403). "
                "Похоже, провайдер блокирует текущую сигнатуру HTTP-клиента."
            )
        if detail:
            return detail
    return raw_text.strip() or str(exc)


def _api_request(method: str, path: str, *, payload: dict[str, Any] | None = None, query: dict[str, str] | None = None) -> dict[str, Any]:
    _ensure_api_ready()

    url = f"{_base_url()}{path}"
    if query:
        url = f"{url}?{urlencode(query)}"

    body = None
    headers = _build_request_headers()

    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = Request(url, data=body, headers=headers, method=method.upper())

    try:
        with urlopen(req, timeout=max(5, int(settings.SUNO_REQUEST_TIMEOUT_SECONDS))) as response:
            raw_text = response.read().decode("utf-8")
    except HTTPError as exc:
        raw_text = exc.read().decode("utf-8", errors="replace")
        msg = _humanize_http_error(exc, raw_text)
        raise SunoServiceError(f"Ошибка сервиса генерации ({exc.code}): {msg}") from exc
    except URLError as exc:
        raise SunoServiceError(f"Не удалось связаться с сервисом генерации: {exc.reason}") from exc
    except Exception as exc:  # noqa: BLE001
        raise SunoServiceError(f"Не удалось выполнить запрос к сервису генерации: {exc}") from exc

    parsed = _safe_json_loads(raw_text)
    if not isinstance(parsed, dict):
        raise SunoServiceError("Сервис генерации вернул невалидный JSON.")

    code = parsed.get("code")
    if code != 200:
        msg = str(parsed.get("msg") or parsed.get("message") or "Неизвестная ошибка сервиса генерации.")
        raise SunoServiceError(msg)

    return parsed


def _number_to_words_ru(value: int) -> str:
    units = ["ноль", "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять"]
    units_feminine = ["ноль", "одна", "две", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять"]
    teens = {
        10: "десять",
        11: "одиннадцать",
        12: "двенадцать",
        13: "тринадцать",
        14: "четырнадцать",
        15: "пятнадцать",
        16: "шестнадцать",
        17: "семнадцать",
        18: "восемнадцать",
        19: "девятнадцать",
    }
    tens = {
        2: "двадцать",
        3: "тридцать",
        4: "сорок",
        5: "пятьдесят",
        6: "шестьдесят",
        7: "семьдесят",
        8: "восемьдесят",
        9: "девяносто",
    }
    hundreds = {
        1: "сто",
        2: "двести",
        3: "триста",
        4: "четыреста",
        5: "пятьсот",
        6: "шестьсот",
        7: "семьсот",
        8: "восемьсот",
        9: "девятьсот",
    }

    def _under_thousand(num: int, *, feminine: bool = False) -> str:
        if num == 0:
            return ""
        words: list[str] = []
        h = num // 100
        rest = num % 100
        if h:
            words.append(hundreds[h])
        if 10 <= rest <= 19:
            words.append(teens[rest])
            return " ".join(words)
        t = rest // 10
        u = rest % 10
        if t:
            words.append(tens[t])
        if u:
            source = units_feminine if feminine else units
            words.append(source[u])
        return " ".join(words)

    if value < 0:
        return f"минус {_number_to_words_ru(abs(value))}"
    if value == 0:
        return units[0]
    if value < 1000:
        return _under_thousand(value)
    if value < 1_000_000:
        thousands = value // 1000
        remainder = value % 1000
        thousand_words = _under_thousand(thousands, feminine=True)
        last_two = thousands % 100
        last = thousands % 10
        if 11 <= last_two <= 14:
            form = "тысяч"
        elif last == 1:
            form = "тысяча"
        elif 2 <= last <= 4:
            form = "тысячи"
        else:
            form = "тысяч"
        remainder_words = _under_thousand(remainder)
        parts = [thousand_words, form, remainder_words]
        return " ".join(part for part in parts if part).strip()
    return str(value)


def _replace_date_with_words(match: re.Match[str]) -> str:
    day = int(match.group(1))
    month = int(match.group(2))
    year = int(match.group(3))
    day_words = DAY_ORDINALS.get(day)
    month_words = MONTHS_RU_GENITIVE.get(month)
    year_words = _number_to_words_ru(year)
    if not day_words or not month_words:
        return match.group(0)
    return f"{day_words} {month_words} {year_words} года"


def normalize_lyrics_for_suno(lyrics_text: str) -> str:
    text = (lyrics_text or "").strip()
    if not text:
        return ""

    lines = text.splitlines()
    normalized_lines: list[str] = []
    for line in lines:
        if HEADER_LINE_PATTERN.match(line):
            normalized_lines.append(line)
            continue

        updated = line
        updated = DATE_PATTERN.sub(_replace_date_with_words, updated)
        for brand, spoken in BRAND_PRONUNCIATION_MAP.items():
            updated = re.sub(rf"\b{re.escape(brand)}\b", spoken, updated, flags=re.IGNORECASE)
        updated = NUMBER_PATTERN.sub(lambda m: _number_to_words_ru(int(m.group(0))), updated)
        normalized_lines.append(updated)
    return "\n".join(normalized_lines).strip()


def build_song_style(
    *,
    song_style: str | None,
    song_style_custom: str | None,
    singer_gender: str | None = None,
    song_mood: str | None = None,
) -> str:
    style_code = (song_style or "").strip().lower()
    style_custom = (song_style_custom or "").strip()

    mood_hint = MOOD_MAP.get((song_mood or "").strip().lower())

    vocal_hint_map = {
        "male": "Male lead vocal",
        "female": "Female lead vocal",
    }
    vocal_hint = vocal_hint_map.get((singer_gender or "").strip().lower())

    if style_code == "multi" and style_custom:
        base_style = f"Mixed styles: {style_custom}"
        parts = [base_style]
        if vocal_hint:
            parts.append(vocal_hint)
        if mood_hint:
            parts.append(f"Mood: {mood_hint}")
        return ". ".join(parts)

    if style_code == "custom" and style_custom:
        base_style = style_custom
    elif style_code in STYLE_MAP:
        base_style = STYLE_MAP[style_code]
    elif style_custom:
        base_style = style_custom
    else:
        base_style = "Modern chart pop, emotional personalized song, memorable hook"

    parts = [base_style]
    if vocal_hint:
        parts.append(vocal_hint)
    if mood_hint:
        parts.append(f"Mood: {mood_hint}")
    return ". ".join(parts)


def build_song_title(order_number: str) -> str:
    title = f"Magic Music {order_number}".strip()
    return title[:100]


def build_vocal_gender(singer_gender: str | None) -> str | None:
    gender = (singer_gender or "").strip().lower()
    if gender == "male":
        return "m"
    if gender == "female":
        return "f"
    return None


def build_callback_url() -> str:
    base = settings.BASE_URL.rstrip("/")
    url = f"{base}/songs/callback/suno"
    token = (settings.SUNO_CALLBACK_TOKEN or "").strip()
    if token:
        url = f"{url}?token={token}"
    return url


def _normalize_track(item: dict[str, Any], index: int) -> dict[str, Any]:
    audio_url = item.get("audioUrl") or item.get("audio_url")
    stream_audio_url = item.get("streamAudioUrl") or item.get("stream_audio_url")
    return {
        "index": index,
        "track_id": item.get("id") or item.get("musicId") or item.get("audioId"),
        "title": item.get("title") or f"Вариант {index + 1}",
        "audio_url": audio_url,
        "stream_audio_url": stream_audio_url,
        "image_url": item.get("imageUrl") or item.get("image_url"),
        "prompt": item.get("prompt"),
        "tags": item.get("tags"),
        "duration": item.get("duration"),
        "model_name": item.get("modelName") or item.get("model_name"),
    }


def _extract_tracks(raw_items: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_items, list):
        return []

    tracks: list[dict[str, Any]] = []
    for index, item in enumerate(raw_items):
        if isinstance(item, dict):
            tracks.append(_normalize_track(item, index))
    return tracks


def _pick_audio_url(tracks: list[dict[str, Any]]) -> str | None:
    for track in tracks:
        url = (track.get("audio_url") or "").strip()
        if url:
            return url
    for track in tracks:
        url = (track.get("stream_audio_url") or "").strip()
        if url:
            return url
    return None


def _map_remote_status(remote_status: str | None) -> str:
    normalized = (remote_status or "").strip().upper()
    if normalized in {"SUCCESS"}:
        return "succeeded"
    if normalized in {"CREATE_TASK_FAILED", "GENERATE_AUDIO_FAILED", "CALLBACK_EXCEPTION", "SENSITIVE_WORD_ERROR"}:
        return "failed"
    return "processing"


def _build_sync_result(*, raw: dict[str, Any], remote_status: str | None, raw_tracks: Any, error_message: str | None = None) -> SongSyncResult:
    tracks = _extract_tracks(raw_tracks)
    status = _map_remote_status(remote_status)
    if status == "succeeded" and not tracks:
        status = "processing"
    return SongSyncResult(
        status=status,
        audio_url=_pick_audio_url(tracks),
        result_tracks=tracks,
        error_message=error_message,
        raw=raw,
    )


def start_song_generation(
    *,
    order_number: str,
    lyrics_text: str,
    song_style: str | None = None,
    song_style_custom: str | None = None,
    singer_gender: str | None = None,
    song_mood: str | None = None,
) -> SongStartResult:
    style_text = build_song_style(
        song_style=song_style,
        song_style_custom=song_style_custom,
        singer_gender=singer_gender,
        song_mood=song_mood,
    )
    lyrics = normalize_lyrics_for_suno(lyrics_text)

    if len(lyrics) > 5000:
        raise SunoServiceError("Финальный текст слишком длинный для генерации песни. Сократи его до 5000 символов.")

    if settings.SUNO_STUB_MODE:
        stub_job_id = f"stub-{uuid4().hex[:12]}"
        return SongStartResult(
            external_job_id=stub_job_id,
            status="processing",
            raw={
                "mode": "stub",
                "order_number": order_number,
                "lyrics_chars": len(lyrics),
                "job_id": stub_job_id,
                "song_style": song_style,
                "song_style_custom": song_style_custom,
                "singer_gender": singer_gender,
                "song_mood": song_mood,
                "style_text": style_text,
            },
        )

    payload: dict[str, Any] = {
        "customMode": True,
        "instrumental": False,
        "model": (settings.SUNO_MODEL or "V5").strip(),
        "callBackUrl": build_callback_url(),
        "prompt": lyrics,
        "style": style_text[:1000],
        "title": build_song_title(order_number),
    }

    vocal_gender = build_vocal_gender(singer_gender)
    if vocal_gender:
        payload["vocalGender"] = vocal_gender

    response_json = _api_request("POST", "/api/v1/generate", payload=payload)
    data = response_json.get("data") or {}
    task_id = (data.get("taskId") or "").strip()
    if not task_id:
        raise SunoServiceError("Сервис генерации не вернул taskId.")

    return SongStartResult(
        external_job_id=task_id,
        status="processing",
        raw={
            "provider": "sunoapi",
            "request": payload,
            "response": response_json,
        },
    )


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
                result_tracks=[],
                error_message=None,
                raw={
                    "mode": "stub",
                    "job_id": external_job_id,
                    "elapsed_seconds": elapsed,
                    "eta_seconds": max(0, settings.SUNO_STUB_DELAY_SECONDS - elapsed),
                },
            )

        stub_url = settings.SUNO_STUB_AUDIO_URL
        stub_tracks = []
        if stub_url:
            stub_tracks = [
                {
                    "index": 0,
                    "track_id": f"{external_job_id}-1",
                    "title": "Вариант 1",
                    "audio_url": stub_url,
                    "stream_audio_url": stub_url,
                    "image_url": None,
                    "prompt": None,
                    "tags": None,
                    "duration": None,
                    "model_name": "stub",
                },
                {
                    "index": 1,
                    "track_id": f"{external_job_id}-2",
                    "title": "Вариант 2",
                    "audio_url": stub_url,
                    "stream_audio_url": stub_url,
                    "image_url": None,
                    "prompt": None,
                    "tags": None,
                    "duration": None,
                    "model_name": "stub",
                },
            ]

        return SongSyncResult(
            status="succeeded",
            audio_url=stub_url,
            result_tracks=stub_tracks,
            error_message=None,
            raw={
                "mode": "stub",
                "job_id": external_job_id,
                "elapsed_seconds": elapsed,
                "audio_url": stub_url,
                "result_tracks": stub_tracks,
            },
        )

    if not external_job_id:
        raise SunoServiceError("У задачи генерации нет внешнего taskId.")

    response_json = _api_request(
        "GET",
        "/api/v1/generate/record-info",
        query={"taskId": external_job_id},
    )
    data = response_json.get("data") or {}
    response_data = data.get("response") or {}
    raw_tracks = response_data.get("sunoData")
    remote_status = data.get("status")
    error_message = data.get("errorMessage") or response_json.get("msg")

    return _build_sync_result(
        raw={
            "provider": "sunoapi",
            "response": response_json,
        },
        remote_status=remote_status,
        raw_tracks=raw_tracks,
        error_message=error_message if _map_remote_status(remote_status) == "failed" else None,
    )


def parse_song_callback(payload: dict[str, Any]) -> SongCallbackResult:
    callback_data = payload.get("data") or {}
    task_id = callback_data.get("task_id") or callback_data.get("taskId")
    callback_type = callback_data.get("callbackType")
    code = payload.get("code")
    msg = str(payload.get("msg") or "").strip() or None
    raw_tracks = callback_data.get("data")

    if code == 200 and callback_type == "complete":
        sync_result = _build_sync_result(
            raw={"provider": "sunoapi", "callback": payload},
            remote_status="SUCCESS",
            raw_tracks=raw_tracks,
            error_message=None,
        )
    elif code == 200 and callback_type in {"text", "first"}:
        sync_result = _build_sync_result(
            raw={"provider": "sunoapi", "callback": payload},
            remote_status="FIRST_SUCCESS" if callback_type == "first" else "PENDING",
            raw_tracks=raw_tracks,
            error_message=None,
        )
    else:
        sync_result = SongSyncResult(
            status="failed",
            audio_url=None,
            result_tracks=_extract_tracks(raw_tracks),
            error_message=msg or "Генерация песни завершилась ошибкой.",
            raw={"provider": "sunoapi", "callback": payload},
        )

    return SongCallbackResult(
        external_job_id=(task_id or "").strip() or None,
        callback_type=(callback_type or "").strip() or None,
        sync_result=sync_result,
    )
