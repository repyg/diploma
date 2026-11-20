"""OpenCV image preprocessing tuned for Russian Form 447/u certificates."""

from __future__ import annotations

from io import BytesIO

import cv2
import numpy as np
from PIL import Image, ImageOps


def load_bgr_with_exif(image_bytes: bytes) -> np.ndarray:
    """Decode image bytes respecting mobile EXIF orientation and return BGR array."""
    pil_img = ImageOps.exif_transpose(Image.open(BytesIO(image_bytes))).convert("RGB")
    rgb = np.asarray(pil_img)
    return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)


def _order_quad_points(pts: np.ndarray) -> np.ndarray:
    rect = np.zeros((4, 2), dtype="float32")
    s = pts.sum(axis=1)
    rect[0] = pts[np.argmin(s)]  # top-left
    rect[2] = pts[np.argmax(s)]  # bottom-right
    diff = np.diff(pts, axis=1)
    rect[1] = pts[np.argmin(diff)]  # top-right
    rect[3] = pts[np.argmax(diff)]  # bottom-left
    return rect


def _try_perspective_crop(bgr: np.ndarray) -> np.ndarray:
    """Crop and flatten a certificate photographed on a contrasting surface."""
    h, w = bgr.shape[:2]
    scale = min(1.0, 900.0 / max(h, w))
    small = cv2.resize(bgr, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)

    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(blurred, 50, 150)
    edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=1)

    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return bgr

    img_area = small.shape[0] * small.shape[1]
    for cnt in sorted(contours, key=cv2.contourArea, reverse=True)[:5]:
        area = cv2.contourArea(cnt)
        # Обрезаем только если лист занимает 30%–92% кадра (плоские сканы не трогаем)
        if not (0.30 * img_area <= area <= 0.92 * img_area):
            continue
        peri = cv2.arcLength(cnt, True)
        approx = cv2.approxPolyDP(cnt, 0.02 * peri, True)
        if len(approx) == 4 and cv2.isContourConvex(approx):
            quad = _order_quad_points(approx.reshape(4, 2).astype("float32")) / scale
            (tl, tr, br, bl) = quad
            dst_w = int(max(np.linalg.norm(br - bl), np.linalg.norm(tr - tl)))
            dst_h = int(max(np.linalg.norm(tr - br), np.linalg.norm(tl - bl)))
            if dst_w < 300 or dst_h < 300:
                continue
            dst = np.array(
                [[0, 0], [dst_w - 1, 0], [dst_w - 1, dst_h - 1], [0, dst_h - 1]],
                dtype="float32",
            )
            matrix = cv2.getPerspectiveTransform(quad, dst)
            return cv2.warpPerspective(bgr, matrix, (dst_w, dst_h))
    return bgr


def _deskew_small_angle(bgr: np.ndarray) -> np.ndarray:
    """Correct small tilt angles (-12..+12 deg) via Hough lines on text edges."""
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 50, 150, apertureSize=3)
    lines = cv2.HoughLinesP(
        edges,
        rho=1,
        theta=np.pi / 180.0,
        threshold=80,
        minLineLength=max(60, bgr.shape[1] // 8),
        maxLineGap=15,
    )
    if lines is None:
        return bgr

    angles: list[float] = []
    for line in lines:
        x1, y1, x2, y2 = line[0]
        if x2 == x1:
            continue
        angle = float(np.degrees(np.arctan2(y2 - y1, x2 - x1)))
        if -12.0 <= angle <= 12.0:
            angles.append(angle)

    if len(angles) < 3:
        return bgr
    median_angle = float(np.median(angles))
    if abs(median_angle) < 0.4:
        return bgr

    h, w = bgr.shape[:2]
    matrix = cv2.getRotationMatrix2D((w / 2.0, h / 2.0), median_angle, 1.0)
    return cv2.warpAffine(
        bgr,
        matrix,
        (w, h),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_REPLICATE,
    )


def _resize_for_ocr(bgr: np.ndarray, target_max_side: int = 2000) -> np.ndarray:
    h, w = bgr.shape[:2]
    max_side = max(h, w)
    if max_side < 1400:
        scale = min(2.0, 1800.0 / max_side)
        return cv2.resize(bgr, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_CUBIC)
    if max_side > target_max_side:
        scale = target_max_side / max_side
        return cv2.resize(bgr, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    return bgr


def _normalize_illumination(bgr: np.ndarray) -> np.ndarray:
    """Remove uneven phone shadows while preserving both black print and blue pen."""
    lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB)
    l_ch, a_ch, b_ch = cv2.split(lab)

    # Оценка фона для выравнивания теней на фото с телефона
    bg = cv2.GaussianBlur(l_ch, (0, 0), sigmaX=35, sigmaY=35)
    norm_l = cv2.divide(l_ch, np.maximum(bg, 1), scale=255)

    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    enhanced_l = clahe.apply(norm_l)
    merged = cv2.merge((enhanced_l, a_ch, b_ch))
    result = cv2.cvtColor(merged, cv2.COLOR_LAB2BGR)
    return cv2.fastNlMeansDenoisingColored(result, None, 3, 3, 7, 15)


def _suppress_blue_stamps(bgr: np.ndarray) -> np.ndarray:
    """Suppress blue/violet OПК stamps using HSV mask + blue channel replacement."""
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    # Диапазон синих/фиолетовых чернил медицинских печатей
    lower_blue = np.array([90, 35, 40], dtype=np.uint8)
    upper_blue = np.array([150, 255, 255], dtype=np.uint8)
    stamp_mask = cv2.inRange(hsv, lower_blue, upper_blue)
    stamp_mask = cv2.dilate(stamp_mask, np.ones((3, 3), np.uint8), iterations=1)

    b_channel, _, _ = cv2.split(bgr)
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)

    # В зоне синей печати подставляем синий канал (в нём синяя печать почти белая, а чёрный текст тёмный)
    cleaned = np.where(stamp_mask > 0, b_channel, gray).astype(np.uint8)

    bg = cv2.GaussianBlur(cleaned, (0, 0), sigmaX=35, sigmaY=35)
    norm = cv2.divide(cleaned, np.maximum(bg, 1), scale=255)
    clahe = cv2.createCLAHE(clipLimit=2.2, tileGridSize=(8, 8))
    enhanced = clahe.apply(norm)
    return cv2.cvtColor(enhanced, cv2.COLOR_GRAY2BGR)


def preprocess_variants(image_bytes: bytes) -> tuple[np.ndarray, np.ndarray]:
    """Return (standard_bgr, stamp_suppressed_bgr) ready for OCR."""
    raw_bgr = load_bgr_with_exif(image_bytes)
    cropped = _try_perspective_crop(raw_bgr)
    deskewed = _deskew_small_angle(cropped)
    resized = _resize_for_ocr(deskewed)

    standard = _normalize_illumination(resized)
    stamp_free = _suppress_blue_stamps(resized)
    return standard, stamp_free