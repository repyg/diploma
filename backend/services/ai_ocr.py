"""OCR certificate images, mask sensitive identifiers, and normalize fields."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from copy import deepcopy
from io import BytesIO
from typing import Any

USE_MOCK = True

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
        "donation_sequence_number": 2,
    },
    "medical_intervals": {
        "next_whole_blood_allowed": "2026-10-21",
        "status_comment": "Донор доступен для записи с 21.10.2026. Медотвод: 14 дней.",
    },
}

# Russian SNILS with optional separators, Russian phone numbers, and passport
# series/number forms commonly present in OCR text.
_PII_PATTERNS = (
    (re.compile(r"(?<!\d)\d{3}[- ]?\d{3}[- ]?\d{3}\s?\d{2}(?!\d)"), "[СНИЛС] "),
    (re.compile(r"(?<!\w)(?:\+7|8)[\s(.-]*\d{3}[)\s.-]*\d{3}[\s.-]*\d{2}[\s.-]*\d{2}(?!\d)"), "[ТЕЛЕФОН] "),
    (re.compile(r"(?<!\d)\d{4}\s?\d{6}(?!\d)"), "[ПАСПОРТ] "),
)

LLMNormalizer = Callable[[str], str | dict[str, Any]]
OCRReader = Callable[[bytes], str]


def mask_personal_data(text: str) -> str:
    """Replace passport, phone, and SNILS values with non-identifying labels."""
    masked = text
    for pattern, replacement in _PII_PATTERNS:
        masked = pattern.sub(replacement, masked)
    return masked


def extract_text(image_bytes: bytes) -> str:
    """Extract Russian text using EasyOCR, falling back to Tesseract.

    OCR libraries and the Tesseract executable are optional runtime
    dependencies; import them lazily so mock mode needs neither installed.
    """
    if not image_bytes:
        raise ValueError("image_bytes must not be empty")

    try:
        import easyocr
        import numpy as np
        from PIL import Image

        image = np.asarray(Image.open(BytesIO(image_bytes)).convert("RGB"))
        reader = easyocr.Reader(["ru", "en"], gpu=False)
        return "\n".join(reader.readtext(image, detail=0, paragraph=True))
    except ImportError:
        pass

    try:
        import pytesseract
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError(
            "Install easyocr or pytesseract and Pillow to enable OCR"
        ) from exc

    return pytesseract.image_to_string(
        Image.open(BytesIO(image_bytes)), lang="rus+eng"
    )


def parse_certificate(
    image_bytes: bytes,
    *,
    llm_normalizer: LLMNormalizer | None = None,
    ocr_reader: OCRReader | None = None,
    use_mock: bool | None = None,
) -> dict[str, Any]:
    """Run OCR, redact sensitive identifiers, then normalize via an LLM.

    Supply ``llm_normalizer`` in real mode; it receives only masked OCR text
    and must return either a JSON string or a decoded object. The callback
    keeps provider credentials and SDK choice outside this service.
    """
    mock = USE_MOCK if use_mock is None else use_mock
    if mock:
        return deepcopy(MOCK_RESPONSE)
    if not image_bytes:
        raise ValueError("image_bytes must not be empty")
    if llm_normalizer is None:
        raise ValueError("llm_normalizer is required when mock mode is disabled")

    raw_text = (ocr_reader or extract_text)(image_bytes)
    if not raw_text.strip():
        raise ValueError("OCR did not recognize any text")
    safe_text = mask_personal_data(raw_text)
    result = llm_normalizer(safe_text)
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except json.JSONDecodeError as exc:
            raise ValueError("LLM returned invalid JSON") from exc
    if not isinstance(result, dict):
        raise ValueError("LLM response must be a JSON object")
    return result


__all__ = [
    "LLMNormalizer",
    "MOCK_RESPONSE",
    "OCRReader",
    "USE_MOCK",
    "extract_text",
    "mask_personal_data",
    "parse_certificate",
]
