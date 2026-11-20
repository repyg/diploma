"""Russian NLP extraction (Natasha + pymorphy3) and RapidFuzz matching for Form 447/u."""

from __future__ import annotations

import re
import sys
from datetime import date
from threading import Lock
from typing import Any

from rapidfuzz import fuzz, process

# Патч совместимости natasha 1.6 с Python 3.12: подменяем pymorphy2 на pymorphy3 до импорта natasha
try:
    import pymorphy3
    import pymorphy3.analyzer
    import pymorphy3.tagset

    sys.modules["pymorphy2"] = pymorphy3
    sys.modules["pymorphy2.analyzer"] = pymorphy3.analyzer
    sys.modules["pymorphy2.tagset"] = pymorphy3.tagset
except ImportError:
    pymorphy3 = None  # type: ignore[assignment]

_NLP_LOCK = Lock()
_NLP_BUNDLE: dict[str, Any] | None = None
_MORPH: Any = None

_UUID_RE = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}"
_DATE_NUMERIC_RE = r"(?<!\d)(\d{1,2}[./-]\d{1,2}[./-]\d{2,4}|\d{4}[./-]\d{1,2}[./-]\d{1,2})(?!\d)"

_RU_MONTHS = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4,
    "ма": 5, "июн": 6, "июл": 7, "август": 8,
    "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
}

# Канонические медцентры Москвы и алиасы для RapidFuzz
_CENTER_ALIASES: dict[str, str] = {
    "рогачева": "НМИЦ Рогачёва",
    "дмитрия рогачева": "НМИЦ Рогачёва",
    "нмиц дгои": "НМИЦ Рогачёва",
    "гаврилова": "Центр крови им. О.К. Гаврилова",
    "центр крови имени гаврилова": "Центр крови им. О.К. Гаврилова",
    "фмба россии": "Центр крови ФМБА России",
    "центр крови фмба": "Центр крови ФМБА России",
    "склифосовского": "НИИ СП им. Н.В. Склифосовского",
    "сеченовского университета": "Центр крови Сеченовского университета",
    "первого мгму": "Центр крови Сеченовского университета",
    "нмиц гематологии": "НМИЦ гематологии",
    "бакулева": "НМИЦ ССХ им. А.Н. Бакулева",
    "блохина": "НМИЦ онкологии им. Н.Н. Блохина",
    "гкб 52": "ГКБ № 52",
    "больница 52": "ГКБ № 52",
}

_DONATION_TYPE_ALIASES: dict[str, str] = {
    "тромбоциты": "Тромбоциты",
    "тромбоцитаферез": "Тромбоциты",
    "тромбоцитоферез": "Тромбоциты",
    "концентрат тромбоцитов": "Тромбоциты",
    "плазма": "Плазма",
    "плазмаферез": "Плазма",
    "плазмоферез": "Плазма",
    "цельная кровь": "Цельная кровь",
    "кроводача": "Цельная кровь",
    "эритроцитная масса": "Цельная кровь",
    "сдача крови": "Цельная кровь",
    "кровь": "Цельная кровь",
}

# Выражения с фамилиями в названиях медучреждений, чтобы NER не принимал их за донора
_INSTITUTION_NAME_RE = re.compile(
    r"(?i)\b(?:имени|им\.?)\s+(?:[А-ЯЁ]\.?\s*[А-ЯЁ]\.?\s+)?[А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё]+)?",
)

_STOP_FIO_WORDS = {
    "российской", "федерации", "минздрава", "министерство", "здравоохранения",
    "справка", "выдана", "дана", "донору", "донор", "центр", "крови", "имени",
    "дмитрия", "рогачева", "рогачёва", "гаврилова", "отделение", "переливания",
    "медицинский", "учреждение", "города", "москвы", "россии", "форма",
    "освобожден", "освобождена", "работы", "учебы", "учёбы", "врач",
    "федеральное", "государственное", "бюджетное", "научный", "главный",
}

_INDECLINABLE_SURNAME_SUFFIXES = ("о", "е", "и", "у", "ю", "ых", "их", "ко", "дзе", "швили", "ян")


def _get_morph():
    global _MORPH
    if _MORPH is None and pymorphy3 is not None:
        _MORPH = pymorphy3.MorphAnalyzer()
    return _MORPH


def _get_natasha_bundle() -> dict[str, Any] | None:
    global _NLP_BUNDLE
    if _NLP_BUNDLE is not None:
        return _NLP_BUNDLE
    with _NLP_LOCK:
        if _NLP_BUNDLE is not None:
            return _NLP_BUNDLE
        try:
            from natasha import (
                MorphVocab,
                NamesExtractor,
                NewsEmbedding,
                NewsMorphTagger,
                NewsNERTagger,
                Segmenter,
            )

            emb = NewsEmbedding()
            morph_vocab = MorphVocab()
            _NLP_BUNDLE = {
                "segmenter": Segmenter(),
                "morph_vocab": morph_vocab,
                "morph_tagger": NewsMorphTagger(emb),
                "ner_tagger": NewsNERTagger(emb),
                "names_extractor": NamesExtractor(morph_vocab),
            }
        except Exception:
            _NLP_BUNDLE = {}
    return _NLP_BUNDLE or None


def _detect_fio_gender(words: list[str]) -> str | None:
    """Detect grammatical gender ('masc' / 'femn') from patronymic or surname."""
    if len(words) >= 3:
        patr = words[2].lower()
        if re.search(r"(?:ич|ичу|ича|ичем|иче|оглы|улы)$", patr):
            return "masc"
        if re.search(r"(?:вна|вне|вну|вной|чна|чне|чну|чной|кызы)$", patr):
            return "femn"
    morph = _get_morph()
    if morph is not None:
        for word in words:
            for p in morph.parse(word):
                if any(tag in p.tag for tag in ("Patr", "Name", "Surn")):
                    if "femn" in p.tag:
                        return "femn"
                    if "masc" in p.tag:
                        return "masc"
    return None


def _to_nominative_word(word: str, target_gender: str | None, preferred_tag: str) -> str:
    clean = word.strip(".,;:«»\"'() ")
    if not clean:
        return ""
    lower = clean.lower()
    if preferred_tag == "Surn" and lower.endswith(_INDECLINABLE_SURNAME_SUFFIXES):
        return clean[:1].upper() + clean[1:].lower()

    morph = _get_morph()
    if morph is None:
        return clean[:1].upper() + clean[1:].lower()

    parses = morph.parse(clean)
    # Ищем разбор, совпадающий по типу (Surn/Name/Patr) и роду
    best = None
    for p in parses:
        if preferred_tag in p.tag and (target_gender is None or target_gender in p.tag):
            best = p
            break
    if best is None:
        for p in parses:
            if any(t in p.tag for t in ("Surn", "Name", "Patr")):
                best = p
                break
    if best is None:
        best = parses[0]

    grammemes = {"nomn", "sing"}
    if target_gender:
        grammemes.add(target_gender)
    inflected = best.inflect(grammemes) or best.inflect({"nomn", "sing"}) or best.inflect({"nomn"})
    result = inflected.word if inflected is not None else best.normal_form
    return result[:1].upper() + result[1:].lower()


def normalize_fio_nominative(raw_fio: str) -> str:
    """Convert Russian FIO from dative/genitive ('Кузнецову Владиславу Валерьевичу') to nominative."""
    words = [w for w in re.split(r"\s+", raw_fio.strip()) if re.search(r"[А-Яа-яЁё]", w)]
    if not words:
        return ""
    gender = _detect_fio_gender(words)
    tags = ("Surn", "Name", "Patr")
    normalized = [
        _to_nominative_word(word, gender, tags[idx] if idx < len(tags) else "Surn")
        for idx, word in enumerate(words[:3])
    ]
    return " ".join(w for w in normalized if w)


def _is_valid_fio_candidate(words: list[str]) -> bool:
    if not (2 <= len(words) <= 3):
        return False
    return not any(w.lower() in _STOP_FIO_WORDS or len(w) < 2 for w in words)


def _value_after_label(text: str, labels: tuple[str, ...]) -> str | None:
    label = "(?:" + "|".join(labels) + r")"
    match = re.search(rf"(?im)^[^\n]*?{label}\s*[:№-]?\s*([^\n]+)", text)
    return match.group(1).strip(" .;,«»\"'") if match else None


def extract_full_name(text: str) -> str | None:
    """Extract donor FIO using anchored rules + Natasha NER + pymorphy3 nominative normalization."""
    # Отрезаем подвал справки с ФИО врача/регистратора
    body = re.split(r"(?i)лицо[,\s]+выдавшее\s+справку|подпись\s+врача|врач\s*[:.]|руководитель", text)[0]
    body = re.sub(r"(?i)выдана\s+для\s+предъявления[^\n]*", "", body)
    # Убираем фамилии в названиях учреждений ("им. Дмитрия Рогачёва", "им. О.К. Гаврилова")
    body_no_inst = _INSTITUTION_NAME_RE.sub(" [МЕДЦЕНТР] ", body)

    word_pat = r"(?:[А-ЯЁ][а-яё]{1,25}|[А-ЯЁ]{2,25})"
    anchored_patterns = (
        rf"(?i:(?:справка\s+)?(?:вы)?дана\s+донору|донору|ф\.?\s*и\.?\s*о\.?|фамилия[,\s]+имя[,\s]+отчество)[^А-ЯЁ\n]{{0,14}}({word_pat}\s+{word_pat}(?:\s+{word_pat})?)",
        rf"(?i:(?:справка\s+)?(?:вы)?дана)\s+({word_pat}\s+{word_pat}(?:\s+{word_pat})?)",
    )
    for pat in anchored_patterns:
        match = re.search(pat, body_no_inst)
        if match:
            candidate = re.sub(r"\s+(?:в\s+том|что|он|она|дата|год|сдал|сдала).*$", "", match.group(1), flags=re.I).strip()
            words = candidate.split()
            if _is_valid_fio_candidate(words):
                return normalize_fio_nominative(candidate)

    # 2. Natasha NER (PER) + NamesExtractor
    bundle = _get_natasha_bundle()
    if bundle is not None:
        from natasha import Doc

        doc = Doc(body_no_inst)
        doc.segment(bundle["segmenter"])
        doc.tag_morph(bundle["morph_tagger"])
        doc.tag_ner(bundle["ner_tagger"])

        per_candidates: list[str] = []
        for span in doc.spans:
            if span.type != "PER":
                continue
            words = [w for w in re.findall(r"[А-ЯЁа-яё-]+", span.text)]
            if _is_valid_fio_candidate(words):
                per_candidates.append(" ".join(words))

        if per_candidates:
            # Предпочитаем полное 3-словное ФИО (Фамилия Имя Отчество)
            per_candidates.sort(key=lambda s: len(s.split()), reverse=True)
            return normalize_fio_nominative(per_candidates[0])

    # 3. Fallback: поиск тройки слов с явным отчеством
    for match in re.finditer(rf"\b({word_pat}\s+{word_pat}\s+{word_pat})\b", body_no_inst):
        words = match.group(1).split()
        if _is_valid_fio_candidate(words) and re.search(
            r"(?:ович|евич|ич|овна|евна|ична|ьич|овичу|евичу|ичу|овне|евне|ичне)$",
            words[2].lower(),
        ):
            return normalize_fio_nominative(match.group(1))

    return None


def _fix_ocr_digits(raw: str) -> str:
    return re.sub(
        r"(?<=[\d./«\"'-])[ОоOoЗзБбIl|]|(?<!\w)[ОоOo](?=\d[./-])",
        lambda m: {"З": "3", "з": "3", "Б": "6", "б": "6", "I": "1", "l": "1", "|": "1"}.get(m.group(0), "0"),
        raw,
    )


def _parse_single_date(raw: str) -> date | None:
    cleaned = _fix_ocr_digits(raw)
    text_match = re.search(
        r"[«\"']?(\d{1,2})[»\"']?\s+([а-яёА-ЯЁ]{3,10})\s+(\d{4})",
        cleaned,
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

    num_match = re.search(_DATE_NUMERIC_RE, cleaned)
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


def extract_donation_date(text: str) -> str | None:
    labeled = _value_after_label(
        text,
        ("дата донации", "дата сдачи", "дата кроводачи", r"сдал\(а\)", "сдала", "сдал", "дата"),
    )
    if labeled:
        parsed = _parse_single_date(labeled)
        if parsed and 2020 <= parsed.year <= date.today().year + 1:
            return parsed.isoformat()

    candidates: list[date] = []
    for line in text.splitlines():
        lower = line.lower()
        if any(bad in lower for bad in ("рожден", "приказ", "972н", "1166н", "402/у", "447/у", "от 14.09", "от 28.10")):
            continue
        parsed = _parse_single_date(line)
        if parsed and 2020 <= parsed.year <= date.today().year + 1:
            if any(k in lower for k in ("донац", "кров", "плазм", "тромбоц", "сдал", "освобожд", "дана")):
                return parsed.isoformat()
            candidates.append(parsed)

    return candidates[0].isoformat() if candidates else None


def extract_medical_center(text: str) -> str | None:
    lower = text.lower().replace("ё", "е")
    # 1. Быстрый fuzzy-поиск по словарю известных центров крови (терпим к опечаткам OCR)
    best_center: str | None = None
    best_score = 0.0
    for line in lower.splitlines():
        clean_line = line.strip()
        if len(clean_line) < 4:
            continue
        match = process.extractOne(clean_line, _CENTER_ALIASES.keys(), scorer=fuzz.partial_ratio)
        if match and match[1] >= 84 and match[1] > best_score:
            best_score = float(match[1])
            best_center = _CENTER_ALIASES[match[0]]
    if best_center:
        return best_center

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


def extract_donation_type(text: str | None) -> str | None:
    if not text:
        return None
    normalized = text.lower().replace("ё", "е")
    if re.search(r"тромбоц", normalized):
        return "Тромбоциты"
    if re.search(r"плазм", normalized):
        return "Плазма"
    if re.search(r"цельн|эритроц|кроводач", normalized):
        return "Цельная кровь"

    # Fuzzy-матчинг на случай OCR-искажений ("тромб0циты", "плазмоферз")
    for line in normalized.splitlines():
        if len(line.strip()) < 5:
            continue
        match = process.extractOne(line, _DONATION_TYPE_ALIASES.keys(), scorer=fuzz.partial_ratio)
        if match and match[1] >= 82:
            return _DONATION_TYPE_ALIASES[match[0]]

    if re.search(r"\bкров", normalized):
        return "Цельная кровь"
    return None


def extract_certificate_number(text: str) -> str | None:
    labeled = _value_after_label(text, (r"справка\s*(?:ф\.?\s*447/у)?", r"сертификат"))
    if labeled:
        match = re.search(r"(?<!\d)(\d{4,12})(?!\d)", _fix_ocr_digits(labeled))
        if match:
            return match.group(1)

    for line in text.splitlines()[:8]:
        if any(skip in line.lower() for skip in ("447/у", "402/у", "приказ", "1166н")):
            continue
        match = re.search(r"(?<!\d)(\d{5,9})(?!\d)", _fix_ocr_digits(line))
        if match:
            return match.group(1)
    return None


def extract_fields_from_text(text: str) -> dict[str, Any]:
    """Extract structured Form 447/u fields from OCR text."""
    clean = text.replace("\xa0", " ")
    extracted: dict[str, Any] = {}

    student_id = re.search(_UUID_RE, clean)
    if student_id:
        extracted["student_id"] = student_id.group(0)

    extracted["full_name"] = extract_full_name(clean)
    extracted["academic_group"] = _value_after_label(
        clean, ("учебная группа", "академическая группа", "группа")
    )
    extracted["donation_date"] = extract_donation_date(clean)

    type_value = _value_after_label(clean, ("вид донации", "тип донации", "компонент", "вид сдачи"))
    extracted["donation_type_ru"] = extract_donation_type(type_value) or extract_donation_type(clean)
    extracted["medical_center_short"] = extract_medical_center(clean)
    extracted["certificate_number"] = extract_certificate_number(clean)

    sequence = _value_after_label(clean, ("номер донации", "порядковый номер", "донация №"))
    seq_match = re.search(r"\d+", sequence or "")
    extracted["donation_sequence_number"] = int(seq_match.group(0)) if seq_match else None
    return extracted