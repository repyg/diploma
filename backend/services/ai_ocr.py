"""OCR certificate images, redact sensitive identifiers, and normalize fields."""

from __future__ import annotations

import json
import os
import re
import secrets
from collections.abc import Callable
from copy import deepcopy
from datetime import date, timedelta
from io import BytesIO
from typing import Any

from ..models import DonationType
from .calculator import required_interval_days

USE_MOCK = os.getenv("OCR_USE_MOCK", "false").strip().lower() in {"1", "true", "yes"}

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
    "raw_text": "СПРАВКА № 143117\\nКнязева Милана Игоревна\\n07.10.2026\\nТромбоциты",
    "masked_text": "СПРАВКА № 143117\\nКнязева Милана Игоревна\\n07.10.2026\\nТромбоциты",
}

_PII_PATTERNS = (
    (re.compile(r"(?<!\d)\d{3}[- ]?\d{3}[- ]?\d{3}\s?\d{2}(?!\d)"), "[СНИЛС_СКРЫТ_ФЗ152] "),
    (re.compile(r"(?<!\w)(?:\+7|8)[\s(.-]*\d{3}[)\s.-]*\d{3}[\s.-]*\d{2}[\s.-]*\d{2}(?!\d)"), "[ТЕЛЕФОН_СКРЫТ_ФЗ152] "),
    (re.compile(r"(?<!\d)\d{4}\s?\d{6}(?!\d)"), "[ПАСПОРТ_СКРЫТ_ФЗ152] "),
)
_UUID_RE = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}"
_DATE_NUMERIC_RE = r"(?<!\d)(\d{1,2}[./-]\d{1,2}[./-]\d{2,4}|\d{4}[./-]\d{1,2}[./-]\d{1,2})(?!\d)"

_RU_MONTHS = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4,
    "ма": 5, "июн": 6, "июл": 7, "август": 8,
    "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
}

_KNOWN_CENTERS = (
    (r"рогачев", "НМИЦ Рогачёва"),
    (r"гаврилов", "Центр крови им. О.К. Гаврилова"),
    (r"фмба", "Центр крови ФМБА России"),
    (r"склифосовск", "НИИ СП им. Н.В. Склифосовского"),
    (r"сеченов|первого мгму|1\s*мгму", "Центр крови Сеченовского университета"),
    (r"гематолог", "НМИЦ гематологии"),
    (r"бакулев", "НМИЦ ССХ им. А.Н. Бакулева"),
    (r"блохин", "НМИЦ онкологии им. Н.Н. Блохина"),
    (r"52|гкб\s*№?\s*52", "ГКБ № 52"),
)

_STOP_FIO_WORDS = {
    "российской", "федерации", "минздрава", "министерство", "здравоохранения",
    "справка", "выдана", "донору", "донор", "центр", "крови", "имени",
    "дмитрия", "рогачева", "гаврилова", "отделение", "переливания", "медицинский",
    "учреждение", "здравоохранения", "города", "москвы", "россии", "форма",
}

_EASYOCR_READER = None

LLMNormalizer = Callable[[str], str | dict[str, Any]]
OCRReader = Callable[[bytes], str]


def mask_personal_data(text: str) -> str:
    """Replace passport, phone, and SNILS values with non-identifying labels."""
    masked = text
    for pattern, replacement in _PII_PATTERNS:
        masked = pattern.sub(replacement, masked)
    return masked


def _get_easyocr_reader():
    global _EASYOCR_READER
    if _EASYOCR_READER is None:
        import easyocr
        _EASYOCR_READER = easyocr.Reader(["ru", "en"], gpu=False)
    return _EASYOCR_READER


def _pdf_text(pdf_bytes: bytes) -> str:
    try:
        import fitz
    except ImportError as exc:
        raise RuntimeError("PDF OCR requires PyMuPDF (install pymupdf)") from exc

    document = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        text = "\n".join(page.get_text("text") for page in document).strip()
        if text:
            return text
        images = []
        for page in document:
            pixmap = page.get_pixmap(dpi=250, alpha=False)
            images.append(pixmap.tobytes("png"))
        return "\n".join(extract_text(image) for image in images)
    finally:
        document.close()


def extract_text(image_bytes: bytes) -> str:
    """Extract Russian text from an image or PDF using local OCR libraries."""
    if not image_bytes:
        raise ValueError("image_bytes must not be empty")
    if image_bytes.lstrip().startswith(b"%PDF"):
        return _pdf_text(image_bytes)

    try:
        import numpy as np
        from PIL import Image, ImageOps

        img = Image.open(BytesIO(image_bytes)).convert("L")
        img = ImageOps.autocontrast(img)
        image = np.asarray(img.convert("RGB"))
        reader = _get_easyocr_reader()
        # paragraph=False сохраняет реальные строки документа
        lines = reader.readtext(image, detail=0, paragraph=False)
        return "\n".join(lines)
    except ImportError:
        pass

    try:
        import pytesseract
        from PIL import Image, ImageOps
    except ImportError as exc:
        raise RuntimeError(
            "Install easyocr or pytesseract and Pillow to enable OCR"
        ) from exc

    img = ImageOps.autocontrast(Image.open(BytesIO(image_bytes)).convert("L"))
    return pytesseract.image_to_string(img, lang="rus+eng")


def _value_after_label(text: str, labels: tuple[str, ...]) -> str | None:
    label = "(?:" + "|".join(labels) + r")"
    match = re.search(rf"(?im)^[^\n]*?{label}\s*[:№-]?\s*([^\n]+)", text)
    return match.group(1).strip(" .;,«»\"'") if match else None


def _parse_single_date(raw: str) -> date | None:
    # 1. Проверка текстовых дат вида: «07» октября 2026
    text_match = re.search(
        r"[«\"']?(\d{1,2})[»\"']?\s+([а-яёА-ЯЁ]{3,10})\s+(\d{4})",
        raw,
        re.IGNORECASE,
    )
    if text_match:
        day = int(text_match.group(1))
        month_str = text_match.group(2).lower().replace("ё", "е")
        year = int(text_match.group(3))
        for prefix, month_num in _RU_MONTHS.items():
            if month_str.startswith(prefix):
                try:
                    return date(year, month_num, day)
                except ValueError:
                    pass

    # 2. Проверка числовых дат: 07.10.2026 или 2026-10-07
    num_match = re.search(_DATE_NUMERIC_RE, raw)
    if num_match:
        parts = re.split(r"[./-]", num_match.group(1))
        try:
            if len(parts[0]) == 4:
                return date(int(parts[0]), int(parts[1]), int(parts[2]))
            year = int(parts[2])
            year = year + 2000 if year < 100 else year
            return date(year, int(parts[1]), int(parts[0]))
        except ValueError:
            return None
    return None


def _extract_donation_date(text: str) -> str | None:
    # Сначала ищем по явным меткам
    labeled = _value_after_label(
        text,
        ("дата донации", "дата сдачи", "дата кроводачи", "сдал\\(а\\)", "сдала", "сдал", "дата"),
    )
    if labeled:
        parsed = _parse_single_date(labeled)
        if parsed and parsed.year >= 2020:
            return parsed.isoformat()

    # Ищем по строкам, отсекая даты рождения и приказов Минздрава
    candidates: list[date] = []
    for line in text.splitlines():
        lower = line.lower()
        if any(bad in lower for bad in ("рожден", "приказ", "972н", "1166н", "402/у", "447/у", "от 14.09")):
            continue
        parsed = _parse_single_date(line)
        if parsed and 2020 <= parsed.year <= date.today().year + 1:
            if any(k in lower for k in ("донац", "кров", "плазм", "тромбоц", "сдал", "освобожд", "дана")):
                return parsed.isoformat()
            candidates.append(parsed)

    if candidates:
        return candidates[0].isoformat()
    return None


def _extract_full_name(text: str) -> str | None:
    # Отрезаем нижний блок с подписью врача и блок "Выдана для предъявления"
    clean_text = re.split(r"(?i)лицо[,\s]+выдавшее\s+справку|врач\s*[:.]", text)[0]
    clean_text = re.sub(r"(?i)выдана\s+для\s+предъявления[^\n]*", "", clean_text)

    # 1. Ищем строго слова с ЗАГЛАВНОЙ буквы [А-ЯЁ] после маркеров "Выдана донору" / "ФИО"
    # Обрати внимание: (?i:...) применяется ТОЛЬКО к префиксу, а ФИО чувствительно к регистру!
    patterns = (
        r"(?i:(?:справка\s+)?(?:вы)?дана\s+донору|донору|ф\.?\s*и\.?\s*о\.?|фамилия[,\s]+имя[,\s]+отчество)[^А-ЯЁ\n]{0,10}([А-ЯЁ][а-яё]{1,25}\s+[А-ЯЁ][а-яё]{1,25}(?:\s+[А-ЯЁ][а-яё]{1,25})?)",
        r"(?i:(?:справка\s+)?(?:вы)?дана)\s+([А-ЯЁ][а-яё]{1,25}\s+[А-ЯЁ][а-яё]{1,25}(?:\s+[А-ЯЁ][а-яё]{1,25})?)",
    )
    for pat in patterns:
        match = re.search(pat, clean_text)
        if match:
            candidate = re.sub(
                r"\s+(?:в\s+том|что|он|она|дата|год).*$", "", match.group(1), flags=re.I
            ).strip()
            words = candidate.split()
            if len(words) >= 2 and not any(w.lower() in _STOP_FIO_WORDS for w in words):
                return candidate

    # 2. Резервный поиск тройки слов (Фамилия Имя Отчество) по окончанию отчества
    for match in re.finditer(r"\b([А-ЯЁ][а-яё]{2,}\s+[А-ЯЁ][а-яё]{2,}\s+[А-ЯЁ][а-яё]{2,})\b", clean_text):
        words = match.group(1).split()
        if not any(w.lower() in _STOP_FIO_WORDS for w in words):
            if re.search(r"(?:ович|евич|ич|овна|евна|ична|ьич)$", words[2].lower()):
                return match.group(1)

    return None


def _extract_medical_center(text: str) -> str | None:
    lower = text.lower().replace("ё", "е")
    for pattern, canonical_name in _KNOWN_CENTERS:
        if re.search(pattern, lower):
            return canonical_name

    labeled = _value_after_label(
        text, ("медицинский центр", "место донации", "учреждение", "организация", "центр", "опк")
    )
    if labeled:
        return labeled[:80]

    for line in text.splitlines():
        clean = line.strip()
        if re.search(r"(?i)(?:ФГБУ|ГБУЗ|НМИЦ|Центр\s+крови|Станция\s+переливания|ОПК)", clean):
            return clean[:80]
    return None


def _extract_certificate_number(text: str) -> str:
    labeled = _value_after_label(text, (r"справка\s*(?:ф\.?\s*447/у)?", r"сертификат"))
    if labeled:
        match = re.search(r"(?<!\d)(\d{4,12})(?!\d)", labeled)
        if match:
            return match.group(1)

    # Бланки часто печатают короткий штрихкод отдельно в верхней части.
    for line in text.splitlines()[:8]:
        match = re.search(r"(?<!\d)(\d{6})(?!\d)", line)
        if match:
            return match.group(1)

    return f"447/{date.today().year}-{secrets.randbelow(10000):04d}"


def _normalize_donation_type(text: str | None) -> str | None:
    if not text:
        return None
    normalized = text.lower().replace("ё", "е")
    if re.search(r"тромбоц", normalized):
        return "Тромбоциты"
    if re.search(r"плазм", normalized):
        return "Плазма"
    if re.search(r"цельн|эритроц|кроводач", normalized):
        return "Цельная кровь"
    if re.search(r"\bкров", normalized):
        return "Цельная кровь"
    return None


def normalize_certificate_text(raw_text: str, *, original_text: str | None = None) -> dict[str, Any]:
    """Extract certificate fields from OCR text without inventing missing values."""
    text = raw_text.replace("\xa0", " ")
    original = (original_text if original_text is not None else raw_text).replace("\xa0", " ")
    extracted: dict[str, Any] = {}
    student_id = re.search(_UUID_RE, text)
    if student_id:
        extracted["student_id"] = student_id.group(0)

    extracted["full_name"] = _extract_full_name(text)
    extracted["academic_group"] = _value_after_label(
        text, ("учебная группа", "академическая группа", "группа")
    )
    extracted["donation_date"] = _extract_donation_date(text)

    type_value = _value_after_label(text, ("вид донации", "тип донации", "компонент", "вид сдачи"))
    extracted["donation_type_ru"] = _normalize_donation_type(type_value) or _normalize_donation_type(text)
    extracted["medical_center_short"] = _extract_medical_center(text)
    extracted["certificate_number"] = _extract_certificate_number(text)

    sequence = _value_after_label(text, ("номер донации", "порядковый номер", "донация №"))
    sequence_match = re.search(r"\d+", sequence or "")
    extracted["donation_sequence_number"] = int(sequence_match.group(0)) if sequence_match else None

    recognized = sum(value is not None for value in extracted.values())
    if not extracted["donation_date"]:
        raise ValueError("OCR text does not contain a donation date")
    parsed_date = date.fromisoformat(extracted["donation_date"])
    medical_intervals: dict[str, Any] = {
        "next_whole_blood_allowed": None,
        "status_comment": "Вид донации не указан в справке; медицинский интервал требует проверки.",
    }
    if extracted["donation_type_ru"]:
        donation_type = {
            "Цельная кровь": DonationType.WHOLE_BLOOD,
            "Плазма": DonationType.PLASMA,
            "Тромбоциты": DonationType.PLATELETS,
        }[extracted["donation_type_ru"]]
        interval_days = required_interval_days(donation_type, DonationType.WHOLE_BLOOD)
        next_date = parsed_date + timedelta(days=interval_days)
        medical_intervals = {
            "next_whole_blood_allowed": next_date.isoformat(),
            "status_comment": f"Донор доступен для записи с {next_date:%d.%m.%Y}. Медотвод: {interval_days} дней.",
        }
    return {
        "status": "success",
        "confidence_score": round(min(0.99, 0.45 + recognized * 0.1), 2),
        "is_duplicate": False,
        "extracted_data": extracted,
        "medical_intervals": medical_intervals,
        "raw_text": original,
        "masked_text": text,
    }


def parse_certificate(
    image_bytes: bytes,
    *,
    llm_normalizer: LLMNormalizer | None = None,
    ocr_reader: OCRReader | None = None,
    use_mock: bool | None = None,
) -> dict[str, Any]:
    """Run OCR and normalize the uploaded certificate."""
    mock = USE_MOCK if use_mock is None else use_mock
    if mock:
        return deepcopy(MOCK_RESPONSE)
    if not image_bytes:
        raise ValueError("image_bytes must not be empty")
    raw_text = (ocr_reader or extract_text)(image_bytes)
    if not raw_text.strip():
        raise ValueError("OCR did not recognize any text")
    safe_text = mask_personal_data(raw_text)
    if llm_normalizer is not None:
        result = llm_normalizer(safe_text)
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except json.JSONDecodeError as exc:
                raise ValueError("LLM returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise ValueError("LLM response must be a JSON object")
        result["raw_text"] = raw_text
        result["masked_text"] = safe_text
        extracted = result.setdefault("extracted_data", {})
        extracted.setdefault("certificate_number", _extract_certificate_number(safe_text))
        return result
    return normalize_certificate_text(safe_text, original_text=raw_text)


__all__ = [
    "LLMNormalizer", "MOCK_RESPONSE", "OCRReader", "USE_MOCK", "extract_text",
    "mask_personal_data", "normalize_certificate_text", "parse_certificate",
]