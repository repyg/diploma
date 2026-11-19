"""Certificate upload and in-process OCR task endpoints."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from difflib import SequenceMatcher
from pathlib import Path
import re
from threading import Lock
from time import monotonic
from uuid import UUID, uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..database import SessionLocal
from ..models import Donation, Donor
from ..schemas import OCRTaskResponse
from ..services.ai_ocr import build_llm_certificate_normalizer, parse_certificate
from .auth import get_current_user

router = APIRouter(prefix="/ocr", tags=["ocr"])
_MAX_BYTES = 10 * 1024 * 1024
_TTL_SECONDS = 3600
_TASKS: dict[UUID, dict] = {}
_TASK_LOCK = Lock()
_EXECUTOR = ThreadPoolExecutor(max_workers=2)
_ALLOWED_TYPES = {"image/jpeg", "image/png", "image/tiff", "application/pdf"}
_UPLOAD_DIR = Path(__file__).resolve().parents[1] / "uploads"
_EXTENSIONS = {"image/jpeg": ".jpg", "image/png": ".png", "image/tiff": ".tiff", "application/pdf": ".pdf"}


def _prune() -> None:
    cutoff = monotonic() - _TTL_SECONDS
    with _TASK_LOCK:
        for task_id in [key for key, value in _TASKS.items() if value["created"] < cutoff]:
            _TASKS.pop(task_id, None)


def _normalize_name(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().casefold().replace("ё", "е"))


def _stem_word(word: str) -> str:
    """Убираем падежные окончания (включая дательный и родительный падежи в справках)."""
    if len(word) <= 4:
        return word
    return re.sub(
        r"(?:овичу|евичу|овне|евне|ичне|овой|евой|иной|скому|цкому|ому|ему|ович|евич|овна|евна|ична|ичу|ой|ей|ом|ем|ам|ям|ах|ях|ая|яя|ий|ый|ии|ие|у|ю|е|а|я)$",
        "",
        word,
    )


def _name_similarity(ocr_name: str, db_name: str) -> float:
    words_ocr = _normalize_name(ocr_name).split()
    words_db = _normalize_name(db_name).split()
    if len(words_ocr) < 2 or len(words_db) < 2:
        return 0.0

    stems_ocr = [_stem_word(w) for w in words_ocr]
    stems_db = [_stem_word(w) for w in words_db]

    surname_score = max(
        SequenceMatcher(None, stems_ocr[0], stems_db[0]).ratio(),
        SequenceMatcher(None, stems_ocr[0], stems_db[-1]).ratio(),
    )
    if surname_score < 0.80:
        return 0.0

    full_ocr = " ".join(sorted(stems_ocr))
    full_db = " ".join(sorted(stems_db))
    return SequenceMatcher(None, full_ocr, full_db).ratio()


def _find_donor(db: Session, extracted: dict, raw_text: str = "") -> Donor | None:
    donor_id = extracted.get("student_id")
    if donor_id:
        try:
            donor = db.scalar(
                select(Donor).options(selectinload(Donor.donations)).where(Donor.id == UUID(str(donor_id)))
            )
        except (ValueError, TypeError):
            donor = None
        if donor is not None:
            return donor

    name = extracted.get("full_name")
    if not name:
        return None

    all_donors = list(db.scalars(select(Donor).options(selectinload(Donor.donations))).unique().all())
    if not all_donors:
        return None

    scored = [(_name_similarity(name, d.full_name), d) for d in all_donors]
    scored.sort(key=lambda item: item[0], reverse=True)
    if scored and scored[0][0] >= 0.84:
        return scored[0][1]

    return None


def _process(task_id: UUID, content: bytes, api_key: str | None = None) -> None:
    try:
        llm_normalizer = build_llm_certificate_normalizer(api_key)
        result = parse_certificate(content, llm_normalizer=llm_normalizer)
        raw_text = result.get("raw_text", "")
        extracted = result.get("extracted_data") or {}
        donation_date = extracted.get("donation_date")
        duplicate = False

        with SessionLocal() as db:
            donor = _find_donor(db, extracted, raw_text)
            if donor is not None:
                extracted["student_id"] = str(donor.id)
                extracted["full_name"] = donor.full_name
                if not extracted.get("academic_group"):
                    extracted["academic_group"] = donor.academic_group
                extracted["donation_sequence_number"] = len(donor.donations) + 1

                if donation_date:
                    try:
                        parsed_date = date.fromisoformat(str(donation_date))
                    except (ValueError, TypeError):
                        parsed_date = None
                    if parsed_date is not None:
                        duplicate = db.scalar(
                            select(Donation.id).where(
                                Donation.donor_id == donor.id,
                                Donation.donation_date == parsed_date,
                            )
                        ) is not None

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