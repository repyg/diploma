"""Certificate parsing orchestrator (local CV+PaddleOCR+Natasha+RapidFuzz) and optional LLM helpers."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from copy import deepcopy
from datetime import date, timedelta
from threading import Lock
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import urlparse

from ..models import DonationType
from .calculator import required_interval_days
from .ocr.engine import extract_pdf_text_and_confidence, run_ocr_on_bgr
from .ocr.nlp_extractor import (
    extract_certificate_number,
    extract_donation_type,
    extract_fields_from_text,
)
from .ocr.preprocess import preprocess_variants

USE_MOCK = os.getenv("OCR_USE_MOCK", "false").strip().lower() in {"1", "true", "yes"}

_AI_CONFIG_LOCK = Lock()
_AI_CONFIG: dict[str, str] = {
    "api_key": os.getenv("LLM_API_KEY", os.getenv("GEMINI_API_KEY", os.getenv("OPENAI_API_KEY", ""))).strip(),
    "base_url": os.getenv("LLM_BASE_URL", "https://generativelanguage.googleapis.com/v1beta").strip().rstrip("/"),
    "model": os.getenv("LLM_MODEL", "gemini-2.5-flash").strip(),
}

_ALLOWED_LLM_HOSTS = {
    "generativelanguage.googleapis.com",
    "aiplatform.googleapis.com",
    "api.openai.com",
    "openrouter.ai",
    "api.groq.com",
    "api.deepseek.com",
    "gigachat.devices.sberbank.ru",
}

MOCK_RESPONSE: dict[str, Any] = {
    "status": "success",
    "confidence_score": 0.96,
    "is_duplicate": False,
    "extracted_data": {
        "student_id": "4810291b-8d5c-4e1a-9c12-0a2b3c4d5e6f",
        "full_name": "Князева Милана Игоревна",
        "academic_group": "Э-14",
        "donation_date": "2026-10-07",
        "donation_type_ru": "Тромбоциты",
        "medical_center_short": "НМИЦ Рогачёва",
        "certificate_number": "143117",
        "donation_sequence_number": 2,
    },
    "medical_intervals": {
        "next_whole_blood_allowed": "2026-10-21",
        "status_comment": "Донор доступен для записи с 21.10.2026. Медотвод: 14 дней.",
    },
    "raw_text": "СПРАВКА № 143117\nКнязева Милана Игоревна\n07.10.2026\nТромбоциты",
    "masked_text": "СПРАВКА № 143117\nКнязева Милана Игоревна\n07.10.2026\nТромбоциты",
}

_PII_PATTERNS = (
    (re.compile(r"(?<!\d)\d{3}[- ]?\d{3}[- ]?\d{3}\s?\d{2}(?!\d)"), "[СНИЛС_СКРЫТ_ФЗ152] "),
    (re.compile(r"(?<!\w)(?:\+7|8)[\s(.-]*\d{3}[)\s.-]*\d{3}[\s.-]*\d{2}[\s.-]*\d{2}(?!\d)"), "[ТЕЛЕФОН_СКРЫТ_ФЗ152] "),
    (re.compile(r"(?<!\d)\d{4}\s?\d{6}(?!\d)"), "[ПАСПОРТ_СКРЫТ_ФЗ152] "),
)

LLMNormalizer = Callable[[str], str | dict[str, Any]]
OCRReader = Callable[[bytes], str]


def _validate_base_url(base_url: str) -> str:
    parsed = urlparse(base_url.strip())
    if parsed.scheme != "https" or parsed.hostname not in _ALLOWED_LLM_HOSTS:
        raise ValueError("Недопустимый URL провайдера LLM")
    return base_url.strip().rstrip("/")


def set_ai_config(
    api_key: str,
    base_url: str = "https://generativelanguage.googleapis.com/v1beta",
    model: str = "gemini-2.5-flash",
) -> dict[str, Any]:
    clean_url = _validate_base_url(base_url)
    with _AI_CONFIG_LOCK:
        _AI_CONFIG["api_key"] = api_key.strip()
        _AI_CONFIG["base_url"] = clean_url
        _AI_CONFIG["model"] = model.strip()
    return get_ai_config_status()


def clear_ai_config() -> dict[str, Any]:
    with _AI_CONFIG_LOCK:
        _AI_CONFIG["api_key"] = ""
    return get_ai_config_status()


def get_ai_config_status() -> dict[str, Any]:
    with _AI_CONFIG_LOCK:
        key = _AI_CONFIG["api_key"]
        masked = f"{key[:6]}...{key[-4:]}" if len(key) > 10 else ("***" if key else None)
        return {
            "configured": bool(key),
            "masked_key": masked,
            "base_url": _AI_CONFIG["base_url"],
            "model": _AI_CONFIG["model"],
        }


def mask_personal_data(text: str) -> str:
    masked = text
    for pattern, replacement in _PII_PATTERNS:
        masked = pattern.sub(replacement, masked)
    return masked


def _call_gemini_native(
    system_prompt: str,
    user_prompt: str,
    *,
    api_key: str,
    model: str,
    json_mode: bool = False,
    timeout: float = 18.0,
) -> str:
    gemini_model = model if model.startswith("gemini-") else "gemini-2.5-flash"
    generation_config: dict[str, Any] = {"temperature": 0.1}
    if json_mode:
        generation_config["responseMimeType"] = "application/json"

    payload = {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
        "generationConfig": generation_config,
    }
    data = json.dumps(payload).encode("utf-8")
    endpoints = (
        f"https://generativelanguage.googleapis.com/v1beta/models/{gemini_model}:generateContent",
        f"https://aiplatform.googleapis.com/v1/publishers/google/models/{gemini_model}:generateContent",
    )
    for url in endpoints:
        req = urllib_request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
            method="POST",
        )
        try:
            with urllib_request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                return body["candidates"][0]["content"]["parts"][0]["text"]
        except (urllib_error.URLError, KeyError, IndexError, json.JSONDecodeError):
            continue
    raise RuntimeError("Gemini API request failed (check API key, model, and network)")


def call_openai_compatible_llm(
    system_prompt: str,
    user_prompt: str,
    *,
    api_key: str | None = None,
    json_mode: bool = False,
    timeout: float = 18.0,
) -> str:
    with _AI_CONFIG_LOCK:
        effective_key = (api_key or _AI_CONFIG["api_key"]).strip()
        base_url = _AI_CONFIG["base_url"]
        model = _AI_CONFIG["model"]

    if not effective_key:
        raise RuntimeError("API key is not configured")

    if effective_key.startswith(("AQ.", "AIza")) or "googleapis.com" in base_url:
        return _call_gemini_native(
            system_prompt, user_prompt, api_key=effective_key, model=model, json_mode=json_mode, timeout=timeout
        )

    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.1,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    req = urllib_request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {effective_key}",
            "HTTP-Referer": "https://donorflow.local",
            "X-Title": "DonorFlow AIS",
        },
        method="POST",
    )
    try:
        with urllib_request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return body["choices"][0]["message"]["content"]
    except (urllib_error.URLError, KeyError, IndexError, json.JSONDecodeError) as exc:
        raise RuntimeError("LLM request failed") from exc


def build_llm_certificate_normalizer(api_key: str | None = None) -> LLMNormalizer | None:
    """Certificate parsing now runs 100% locally (CV + PaddleOCR + Natasha + RapidFuzz) per 152-FZ."""
    return None


def _build_medical_intervals(extracted: dict[str, Any]) -> dict[str, Any]:
    donation_date = extracted.get("donation_date")
    donation_type_ru = extracted.get("donation_type_ru")
    if donation_date and donation_type_ru in {"Цельная кровь", "Плазма", "Тромбоциты"}:
        parsed_date = date.fromisoformat(str(donation_date))
        dtype = {
            "Цельная кровь": DonationType.WHOLE_BLOOD,
            "Плазма": DonationType.PLASMA,
            "Тромбоциты": DonationType.PLATELETS,
        }[donation_type_ru]
        interval_days = required_interval_days(dtype, DonationType.WHOLE_BLOOD)
        next_date = parsed_date + timedelta(days=interval_days)
        return {
            "next_whole_blood_allowed": next_date.isoformat(),
            "status_comment": f"Донор доступен для записи с {next_date:%d.%m.%Y}. Медотвод: {interval_days} дней.",
        }
    return {
        "next_whole_blood_allowed": None,
        "status_comment": "Вид донации не указан в справке; медицинский интервал требует проверки.",
    }


def normalize_certificate_text(
    raw_text: str,
    *,
    original_text: str | None = None,
    ocr_confidence: float | None = None,
) -> dict[str, Any]:
    original = (original_text if original_text is not None else raw_text).replace("\xa0", " ")
    safe_text = raw_text.replace("\xa0", " ")
    extracted = extract_fields_from_text(safe_text)

    if not extracted.get("donation_date"):
        raise ValueError("OCR text does not contain a donation date")

    recognized = sum(value is not None for value in extracted.values())
    fallback_conf = round(min(0.95, 0.45 + recognized * 0.08), 2)
    return {
        "status": "success",
        "confidence_score": ocr_confidence if ocr_confidence is not None else fallback_conf,
        "is_duplicate": False,
        "extracted_data": extracted,
        "medical_intervals": _build_medical_intervals(extracted),
        "raw_text": original,
        "masked_text": safe_text,
    }


def extract_text_with_confidence(image_bytes: bytes) -> tuple[str, float | None]:
    if not image_bytes:
        raise ValueError("image_bytes must not be empty")
    if image_bytes.lstrip().startswith(b"%PDF"):
        return extract_pdf_text_and_confidence(image_bytes)
    standard_bgr, _ = preprocess_variants(image_bytes)
    return run_ocr_on_bgr(standard_bgr)


def extract_text(image_bytes: bytes) -> str:
    text, _ = extract_text_with_confidence(image_bytes)
    return text


def _merge_extracted(primary: dict[str, Any], secondary: dict[str, Any]) -> dict[str, Any]:
    merged = dict(primary)
    for key, val in secondary.items():
        if merged.get(key) is None and val is not None:
            merged[key] = val
    return merged


def parse_certificate(
    image_bytes: bytes,
    *,
    llm_normalizer: LLMNormalizer | None = None,
    ocr_reader: OCRReader | None = None,
    use_mock: bool | None = None,
) -> dict[str, Any]:
    """Run two-pass local CV + PaddleOCR + Natasha + RapidFuzz pipeline."""
    mock = USE_MOCK if use_mock is None else use_mock
    if mock:
        return deepcopy(MOCK_RESPONSE)
    if not image_bytes:
        raise ValueError("image_bytes must not be empty")

    # Ветка для тестов (кастомный ocr_reader) или PDF
    if ocr_reader is not None or image_bytes.lstrip().startswith(b"%PDF"):
        if ocr_reader is not None:
            raw_text, ocr_conf = ocr_reader(image_bytes), None
        else:
            raw_text, ocr_conf = extract_pdf_text_and_confidence(image_bytes)
        if not raw_text.strip():
            raise ValueError("OCR did not recognize any text")
        safe_text = mask_personal_data(raw_text)
        return normalize_certificate_text(safe_text, original_text=raw_text, ocr_confidence=ocr_conf)

    # Двухпроходный режим для фото/сканов:
    # Проход 1: выровненное освещение (сохраняет и чёрную печать, и синюю ручку)
    standard_bgr, stamp_free_bgr = preprocess_variants(image_bytes)
    text_1, conf_1 = run_ocr_on_bgr(standard_bgr)
    safe_1 = mask_personal_data(text_1)
    fields_1 = extract_fields_from_text(safe_1) if text_1.strip() else {}

    need_second_pass = (
        not fields_1.get("full_name")
        or not fields_1.get("donation_date")
        or not fields_1.get("donation_type_ru")
        or (conf_1 is not None and conf_1 < 0.78)
    )

    best_raw, best_safe, best_conf, best_fields = text_1, safe_1, conf_1, fields_1
    if need_second_pass:
        # Проход 2: с подавлением синих печатей ОПК
        text_2, conf_2 = run_ocr_on_bgr(stamp_free_bgr)
        if text_2.strip():
            safe_2 = mask_personal_data(text_2)
            fields_2 = extract_fields_from_text(safe_2)
            score_1 = sum(v is not None for v in fields_1.values())
            score_2 = sum(v is not None for v in fields_2.values())
            if score_2 > score_1 or (score_2 == score_1 and (conf_2 or 0.0) > (conf_1 or 0.0)):
                best_raw, best_safe, best_conf = text_2, safe_2, conf_2
                best_fields = _merge_extracted(fields_2, fields_1)
            else:
                best_fields = _merge_extracted(fields_1, fields_2)

    if not best_raw.strip():
        raise ValueError("OCR did not recognize any text")
    if not best_fields.get("donation_date"):
        raise ValueError("OCR text does not contain a donation date")

    recognized = sum(v is not None for v in best_fields.values())
    fallback_conf = round(min(0.95, 0.45 + recognized * 0.08), 2)
    return {
        "status": "success",
        "confidence_score": best_conf if best_conf is not None else fallback_conf,
        "is_duplicate": False,
        "extracted_data": best_fields,
        "medical_intervals": _build_medical_intervals(best_fields),
        "raw_text": best_raw,
        "masked_text": best_safe,
    }


__all__ = [
    "LLMNormalizer",
    "MOCK_RESPONSE",
    "OCRReader",
    "USE_MOCK",
    "build_llm_certificate_normalizer",
    "call_openai_compatible_llm",
    "clear_ai_config",
    "extract_certificate_number",
    "extract_donation_type",
    "extract_text",
    "extract_text_with_confidence",
    "get_ai_config_status",
    "mask_personal_data",
    "normalize_certificate_text",
    "parse_certificate",
    "set_ai_config",
]