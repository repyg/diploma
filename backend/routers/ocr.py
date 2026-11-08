"""Certificate upload and in-process OCR task endpoints."""

from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from time import monotonic
from uuid import UUID, uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import SessionLocal, get_db
from ..models import Donation, Donor
from ..schemas import OCRTaskResponse
from ..services.ai_ocr import parse_certificate
from .auth import get_current_user

router = APIRouter(prefix="/ocr", tags=["ocr"])
_MAX_BYTES = 10 * 1024 * 1024
_TTL_SECONDS = 3600
_TASKS: dict[UUID, dict] = {}
_TASK_LOCK = Lock()
_EXECUTOR = ThreadPoolExecutor(max_workers=2)
_ALLOWED_TYPES = {"image/jpeg", "image/png", "image/tiff", "application/pdf"}


def _prune() -> None:
    cutoff = monotonic() - _TTL_SECONDS
    with _TASK_LOCK:
        for task_id in [key for key, value in _TASKS.items() if value["created"] < cutoff]:
            _TASKS.pop(task_id, None)


def _process(task_id: UUID, content: bytes) -> None:
    try:
        result = parse_certificate(content)
        extracted = result.get("extracted_data") or {}
        donor_id = extracted.get("student_id")
        donation_date = extracted.get("donation_date")
        duplicate = False
        if donor_id and donation_date:
            try:
                donor_uuid = UUID(str(donor_id))
                from datetime import date
                parsed_date = date.fromisoformat(str(donation_date))
                with SessionLocal() as db:
                    donor = db.get(Donor, donor_uuid)
                    if donor:
                        duplicate = db.scalar(select(Donation.id).where(Donation.donor_id == donor_uuid, Donation.donation_date == parsed_date)) is not None
            except (ValueError, TypeError):
                pass
        result["is_duplicate"] = duplicate
        with _TASK_LOCK:
            if task_id in _TASKS:
                _TASKS[task_id].update(status="success", result=result, error=None)
    except Exception as exc:
        with _TASK_LOCK:
            if task_id in _TASKS:
                _TASKS[task_id].update(status="failed", error=str(exc), result=None)


@router.post("/parse-certificate", response_model=OCRTaskResponse, status_code=202)
def parse_certificate_upload(background_tasks: BackgroundTasks, file: UploadFile = File(...), _: str = Depends(get_current_user)) -> OCRTaskResponse:
    _prune()
    if file.content_type not in _ALLOWED_TYPES:
        raise HTTPException(status_code=415, detail="Upload a JPEG, PNG, TIFF, or PDF file")
    content = file.file.read(_MAX_BYTES + 1)
    if not content:
        raise HTTPException(status_code=422, detail="Uploaded file is empty")
    if len(content) > _MAX_BYTES:
        raise HTTPException(status_code=413, detail="File exceeds 10 MB limit")
    task_id = uuid4()
    with _TASK_LOCK:
        if len(_TASKS) >= 1000:
            raise HTTPException(status_code=503, detail="OCR task registry is full; retry later")
        _TASKS[task_id] = {"status": "queued", "result": None, "error": None, "created": monotonic()}
    _EXECUTOR.submit(_process, task_id, content)
    return OCRTaskResponse(task_id=task_id, status="queued")


@router.get("/tasks/{task_id}", response_model=OCRTaskResponse)
def get_ocr_task(task_id: UUID, _: str = Depends(get_current_user)) -> OCRTaskResponse:
    _prune()
    with _TASK_LOCK:
        task = _TASKS.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="OCR task not found or expired")
        return OCRTaskResponse(task_id=task_id, status=task["status"], result=task["result"], error=task["error"])
