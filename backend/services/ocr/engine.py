"""Local OCR engine: PaddleOCR primary (CPU), single-pass Tesseract fallback, PDF support."""

from __future__ import annotations

from dataclasses import dataclass
from threading import Lock
from typing import Any

import numpy as np

from .preprocess import preprocess_variants

_PADDLE_LOCK = Lock()
_PADDLE_INSTANCE: Any = None
_PADDLE_INIT_FAILED = False


@dataclass(frozen=True, slots=True)
class OCRLine:
    text: str
    score: float
    y_center: float
    x_left: float
    height: float


def _get_paddle_ocr():
    global _PADDLE_INSTANCE, _PADDLE_INIT_FAILED
    if _PADDLE_INIT_FAILED:
        return None
    if _PADDLE_INSTANCE is not None:
        return _PADDLE_INSTANCE

    with _PADDLE_LOCK:
        if _PADDLE_INSTANCE is not None or _PADDLE_INIT_FAILED:
            return _PADDLE_INSTANCE
        try:
            from paddleocr import PaddleOCR

            try:
                # PaddleOCR 3.x API
                _PADDLE_INSTANCE = PaddleOCR(
                    lang="ru",
                    use_doc_orientation_classify=True,
                    use_doc_unwarping=False,
                    use_textline_orientation=True,
                )
            except TypeError:
                # Совместимость с PaddleOCR 2.x
                _PADDLE_INSTANCE = PaddleOCR(use_angle_cls=True, lang="ru", show_log=False)
        except Exception:
            _PADDLE_INIT_FAILED = True
            return None
    return _PADDLE_INSTANCE


def _group_boxes_to_lines(items: list[OCRLine]) -> tuple[str, float | None]:
    if not items:
        return "", None

    heights = [item.height for item in items if item.height > 0]
    y_tol = (float(np.median(heights)) * 0.6) if heights else 14.0

    sorted_items = sorted(items, key=lambda it: (it.y_center, it.x_left))
    rows: list[list[OCRLine]] = []
    for item in sorted_items:
        if not rows:
            rows.append([item])
            continue
        current_y = float(np.mean([x.y_center for x in rows[-1]]))
        if abs(item.y_center - current_y) <= y_tol:
            rows[-1].append(item)
        else:
            rows.append([item])

    lines_out: list[str] = []
    scores: list[float] = []
    for row in rows:
        row.sort(key=lambda it: it.x_left)
        line_text = " ".join(part.text.strip() for part in row if part.text.strip())
        if line_text:
            lines_out.append(line_text)
            scores.extend(part.score for part in row if part.score >= 0)

    mean_conf = round(sum(scores) / len(scores), 2) if scores else None
    return "\n".join(lines_out), mean_conf


def _run_paddle_on_bgr(image_bgr: np.ndarray) -> tuple[str, float | None] | None:
    ocr = _get_paddle_ocr()
    if ocr is None:
        return None

    items: list[OCRLine] = []
    with _PADDLE_LOCK:
        if hasattr(ocr, "predict"):
            results = list(ocr.predict(image_bgr))
            for res in results:
                data = res if isinstance(res, dict) else getattr(res, "json", {}).get("res", res)
                texts = data.get("rec_texts", []) if isinstance(data, dict) else []
                scores = data.get("rec_scores", []) if isinstance(data, dict) else []
                polys = data.get("rec_polys", []) if isinstance(data, dict) else []
                for idx, text in enumerate(texts):
                    if not text or not str(text).strip():
                        continue
                    score = float(scores[idx]) if idx < len(scores) else 0.0
                    poly = np.asarray(polys[idx]) if idx < len(polys) else np.zeros((4, 2))
                    y_vals = poly[:, 1] if poly.ndim == 2 and poly.shape[1] >= 2 else np.array([idx * 20.0])
                    x_vals = poly[:, 0] if poly.ndim == 2 and poly.shape[1] >= 2 else np.array([0.0])
                    items.append(
                        OCRLine(
                            text=str(text),
                            score=score,
                            y_center=float(np.mean(y_vals)),
                            x_left=float(np.min(x_vals)),
                            height=float(max(1.0, np.max(y_vals) - np.min(y_vals))),
                        )
                    )
        else:
            raw = ocr.ocr(image_bgr, cls=True) or []
            for page in raw:
                if not page:
                    continue
                for box, (text, score) in page:
                    poly = np.asarray(box)
                    items.append(
                        OCRLine(
                            text=str(text),
                            score=float(score),
                            y_center=float(np.mean(poly[:, 1])),
                            x_left=float(np.min(poly[:, 0])),
                            height=float(max(1.0, np.max(poly[:, 1]) - np.min(poly[:, 1]))),
                        )
                    )

    return _group_boxes_to_lines(items)


def _run_tesseract_single_pass(image_bgr: np.ndarray) -> tuple[str, float | None]:
    import cv2
    import pytesseract
    from PIL import Image
    from pytesseract import Output

    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
    data = pytesseract.image_to_data(
        Image.fromarray(gray),
        lang="rus+eng",
        config="--oem 3 --psm 6",
        output_type=Output.DICT,
    )

    lines_map: dict[tuple[int, int, int], list[str]] = {}
    confs: list[float] = []
    n = len(data.get("text", []))
    for i in range(n):
        word = (data["text"][i] or "").strip()
        conf_raw = str(data["conf"][i])
        if word:
            key = (int(data["block_num"][i]), int(data["par_num"][i]), int(data["line_num"][i]))
            lines_map.setdefault(key, []).append(word)
        if conf_raw.lstrip("-").isdigit() and int(conf_raw) >= 0:
            confs.append(float(conf_raw) / 100.0)

    text = "\n".join(" ".join(words) for _, words in sorted(lines_map.items()))
    mean_conf = round(sum(confs) / len(confs), 2) if confs else None
    return text, mean_conf


def run_ocr_on_bgr(image_bgr: np.ndarray) -> tuple[str, float | None]:
    """Run PaddleOCR on BGR array, falling back to single-pass Tesseract."""
    paddle_res = _run_paddle_on_bgr(image_bgr)
    if paddle_res is not None and paddle_res[0].strip():
        return paddle_res
    return _run_tesseract_single_pass(image_bgr)


def extract_pdf_text_and_confidence(pdf_bytes: bytes) -> tuple[str, float | None]:
    try:
        import fitz
    except ImportError as exc:
        raise RuntimeError("PDF OCR requires PyMuPDF (install pymupdf)") from exc

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        embedded = "\n".join(page.get_text("text") for page in doc).strip()
        if embedded:
            return embedded, 0.99

        texts: list[str] = []
        confs: list[float] = []
        for page in doc:
            pix = page.get_pixmap(dpi=250, alpha=False)
            std_bgr, _ = preprocess_variants(pix.tobytes("png"))
            page_text, page_conf = run_ocr_on_bgr(std_bgr)
            if page_text.strip():
                texts.append(page_text)
            if page_conf is not None:
                confs.append(page_conf)
        mean_conf = round(sum(confs) / len(confs), 2) if confs else None
        return "\n".join(texts), mean_conf
    finally:
        doc.close()