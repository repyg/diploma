"""Certificate upload and in-process OCR task endpoints."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from difflib import SequenceMatcher
from pathlib import Path
import re
from threading import Lock
from time import monotonic
from uuid import UUID, uuid4
from rapidfuzz import fuzz
from sqlalchemy import func

from fastapi import APIRouter, BackgroundTasks, Depends, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..database import SessionLocal
from ..models import Donation, Donor
from ..schemas import OCRTaskResponse
from ..services.ai_ocr import build_llm_certificate_normalizer, parse_certificate
from .auth import get_current_user
from ..services.ocr.nlp_extractor import normalize_fio_nominative

router = APIRouter(prefix="/ocr", tags=["ocr"])
_MAX_BYTES = 10 * 1024 * 1024
_TTL_SECONDS = 3600
_TASKS: dict[UUID, dict] = {}
_TASK_LOCK = Lock()
_EXECUTOR = ThreadPoolExecutor(max_workers=2)
_ALLOWED_TYPES = {"image/jpeg", "image/png", "image/tiff", "application/pdf"}
_UPLOAD_DIR = Path(__file__).resolve().parents[1] / "uploads"
_EXTENSIONS = {"image/jpeg": ".jpg", "image/png": ".png", "image/tiff": ".tiff", "application/pdf": ".pdf"}

def _matches_magic(content: bytes, content_type: str) -> bool:
    if content_type == "image/jpeg":
        return content.startswith(b"\xff\xd8\xff")
    if content_type == "image/png":
        return content.startswith(b"\x89PNG\r\n\x1a\n")
    if content_type == "image/tiff":
        return content.startswith((b"II*\x00", b"MM\x00*"))
    if content_type == "application/pdf":
        return content.lstrip().startswith(b"%PDF-")
    return False


def _prune() -> None:
    cutoff = monotonic() - _TTL_SECONDS
    with _TASK_LOCK:
        for task_id in [key for key, value in _TASKS.items() if value["created"] < cutoff]:
            _TASKS.pop(task_id, None)


def _normalize_name(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().casefold().replace("ё", "е"))

def _match_donors_rapidfuzz(
    db: Session,
    extracted: dict,
) -> tuple[tuple[UUID, str, str | None] | None, list[dict]]:
    donor_id = extracted.get("student_id")
    if donor_id:
        try:
            row = db.execute(
                select(Donor.id, Donor.full_name, Donor.academic_group).where(Donor.id == UUID(str(donor_id)))
            ).first()
            if row is not None:
                return (row[0], row[1], row[2]), [{"id": str(row[0]), "full_name": row[1], "score": 100.0}]
        except (ValueError, TypeError):
            pass

    raw_name = extracted.get("full_name")
    if not raw_name:
        return None, []

    norm_query = _normalize_name(normalize_fio_nominative(str(raw_name)) or str(raw_name))
    all_rows = db.execute(select(Donor.id, Donor.full_name, Donor.academic_group)).all()
    if not all_rows:
        return None, []

    scored: list[tuple[float, tuple[UUID, str, str | None]]] = []
    for d_id, d_name, d_group in all_rows:
        norm_db = _normalize_name(d_name)
        score = float(
            max(
                fuzz.token_sort_ratio(norm_query, norm_db),
                fuzz.token_set_ratio(norm_query, norm_db) - 3.0,
            )
        )
        if score >= 75.0:
            scored.append((round(score, 1), (d_id, d_name, d_group)))

    scored.sort(key=lambda item: item[0], reverse=True)
    candidates = [
        {"id": str(row[0]), "full_name": row[1], "academic_group": row[2], "score": score}
        for score, row in scored[:3]
    ]

    if not scored:
        return None, []

    top_score, top_row = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else 0.0
    # Автоподстановка только при высокой уверенности и отрыве от второго кандидата
    if top_score >= 88.0 and (top_score - second_score >= 5.0 or top_score >= 97.0):
        return top_row, candidates
    return None, candidates


def _process(task_id: UUID, content: bytes, api_key: str | None = None) -> None:
    try:
        result = parse_certificate(content)
        extracted = result.get("extracted_data") or {}
        donation_date = extracted.get("donation_date")
        duplicate = False

        with SessionLocal() as db:
            matched, candidates = _match_donors_rapidfuzz(db, extracted)
            result["matched_candidates"] = candidates
            if matched is not None:
                d_id, d_name, d_group = matched
                extracted["student_id"] = str(d_id)
                extracted["full_name"] = d_name
                if not extracted.get("academic_group"):
                    extracted["academic_group"] = d_group

                don_count = db.scalar(select(func.count(Donation.id)).where(Donation.donor_id == d_id)) or 0
                extracted["donation_sequence_number"] = int(don_count) + 1

                if donation_date:
                    try:
                        parsed_date = date.fromisoformat(str(donation_date))
                    except (ValueError, TypeError):
                        parsed_date = None
                    if parsed_date is not None:
                        duplicate = (
                            db.scalar(
                                select(Donation.id).where(
                                    Donation.donor_id == d_id,
                                    Donation.donation_date == parsed_date,
                                )
                            )
                            is not None
                        )

        result["extracted_data"] = extracted
        result["is_duplicate"] = duplicate
        with _TASK_LOCK:
            if task_id in _TASKS:
                _TASKS[task_id].update(status="success", result=result, error=None)
    except Exception as exc:
        with _TASK_LOCK:
            if task_id in _TASKS:
                _TASKS[task_id].update(status="failed", error=str(exc), result=None)

@router.post("/parse-certificate", response_model=OCRTaskResponse, status_code=202)
def parse_certificate_upload(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    x_llm_api_key: str | None = Header(default=None),
    _: str = Depends(get_current_user),
) -> OCRTaskResponse:
    _prune()
    if file.content_type not in _ALLOWED_TYPES:
        raise HTTPException(status_code=415, detail="Upload a JPEG, PNG, TIFF, or PDF file")
    content = file.file.read(_MAX_BYTES + 1)
    if not content:
        raise HTTPException(status_code=422, detail="Uploaded file is empty")
    if len(content) > _MAX_BYTES:
        raise HTTPException(status_code=413, detail="File exceeds 10 MB limit")
    if not _matches_magic(content, file.content_type):
        raise HTTPException(status_code=415, detail="Содержимое файла не соответствует заявленному типу")
    task_id = uuid4()
    _UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    (_UPLOAD_DIR / f"{task_id}{_EXTENSIONS[file.content_type]}").write_bytes(content)
    with _TASK_LOCK:
        if len(_TASKS) >= 1000:
            raise HTTPException(status_code=503, detail="OCR task registry is full; retry later")
        _TASKS[task_id] = {"status": "queued", "result": None, "error": None, "created": monotonic()}
    _EXECUTOR.submit(_process, task_id, content, x_llm_api_key)
    return OCRTaskResponse(task_id=task_id, status="queued", result={"file_url": f"/api/v1/ocr/tasks/{task_id}/file"})


@router.get("/tasks/{task_id}", response_model=OCRTaskResponse)
def get_ocr_task(task_id: UUID, _: str = Depends(get_current_user)) -> OCRTaskResponse:
    _prune()
    with _TASK_LOCK:
        task = _TASKS.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="OCR task not found or expired")
        return OCRTaskResponse(task_id=task_id, status=task["status"], result=task["result"], error=task["error"])


@router.get("/tasks/{task_id}/file")
def get_ocr_file(task_id: UUID, _: str = Depends(get_current_user)):
    for extension, media_type in (
        (".jpg", "image/jpeg"),
        (".png", "image/png"),
        (".tiff", "image/tiff"),
        (".pdf", "application/pdf"),
    ):
        path = _UPLOAD_DIR / f"{task_id}{extension}"
        if path.is_file():
            return FileResponse(path, media_type=media_type)
    raise HTTPException(status_code=404, detail="Certificate file not found")