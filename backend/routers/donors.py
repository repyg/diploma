"""Donor registry and eligibility endpoints."""

from datetime import date, datetime, timedelta
from io import BytesIO, StringIO
import csv
import json
import re
import zipfile
from uuid import UUID, uuid4
from xml.etree import ElementTree as ET

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import ValidationError
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, selectinload

from ..database import get_db
from ..models import Donation, DonationType, Donor, DonorGender, DonorStatus
from ..schemas import (CoursePromotionBatchResult, CoursePromotionRequest, CoursePromotionResult, DonorCreate, DonorPage, DonorRead, DonorUpdate, DonationIntervalResult, HonoraryDonorProgress, HonoraryDonorProgressResult, IntervalCalendar)
from ..services.calculator import (annual_whole_blood_limit, calculate_next_eligible_date, count_whole_blood_donations, honorary_donor_progress, promote_donors_for_september, required_interval_days)
from .auth import get_current_user

router = APIRouter(prefix="/donors", tags=["donors"])


def _normalize_name(value: str) -> str:
    value = re.sub(r"\s*\([^)]*\)\s*", " ", value or "")
    value = re.sub(r"\s+", " ", value.strip().strip(","))
    words = [word[:1].upper() + word[1:].lower() for word in value.split()]
    return " ".join(word.lower() if word in {"Оглы", "Кызы"} else word for word in words)


def _valid_student_name(value: str) -> bool:
    words = value.split()
    if len(words) < 2 or any(not re.search(r"[А-Яа-яЁё]", word) for word in words):
        return False
    normalized = value.casefold()
    junk = ("донор", "женщина номер", "девочка с", "человек со дня", "января ", "сентября ")
    return not any(term in normalized for term in junk) and not re.match(r"^\d", value)


def _dedupe_key(full_name: str, academic_group: str | None = None) -> str:
    return re.sub(r"\s+", " ", full_name.strip()).casefold().replace("ё", "е")


def _normalize_group(value: str | None) -> str | None:
    if not value:
        return None
    group = value.strip().replace("0-", "О-", 1).replace("H-", "Н-").replace("h-", "Н-")
    group = re.sub(r"\s*-\s*", "-", group)
    group = re.sub(r"\s+", "-", group)
    match = re.match(r"^([А-Яа-яЁёA-Za-z])-(.+)$", group)
    return f"{match.group(1).upper()}-{match.group(2)}" if match else group


def _infer_gender(full_name: str) -> str | None:
    parts = full_name.strip().casefold().split()
    patronymic = parts[1] if len(parts) > 1 else ""
    surname = parts[0] if parts else ""
    if re.search(r"(ич|оглы|улы)$", patronymic) or re.search(r"(ов|ев|ин|ын|ский|цкий)$", surname):
        return "male"
    if re.search(r"(на|кызы)$", patronymic) or re.search(r"(ова|ева|ина|ына|ская|цкая)$", surname):
        return "female"
    return None


def _xlsx_rows(content: bytes) -> list[list[str]]:
    with zipfile.ZipFile(BytesIO(content)) as archive:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            ns = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
            for item in root.findall("x:si", ns):
                shared.append("".join(node.text or "" for node in item.iter() if node.tag.endswith("}t")))
        sheet_name = "xl/worksheets/sheet1.xml"
        if sheet_name not in archive.namelist():
            raise ValueError("XLSX не содержит первый лист")
        root = ET.fromstring(archive.read(sheet_name))
        ns = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        rows: list[list[str]] = []
        for row in root.findall(".//x:sheetData/x:row", ns):
            values: dict[int, str] = {}
            for cell in row.findall("x:c", ns):
                ref = cell.attrib.get("r", "A1")
                column = 0
                for char in re.match(r"[A-Z]+", ref).group(0):
                    column = column * 26 + ord(char) - 64
                column -= 1
                value = cell.find("x:v", ns)
                text = value.text if value is not None and value.text else ""
                if cell.attrib.get("t") == "s" and text.isdigit():
                    text = shared[int(text)] if int(text) < len(shared) else ""
                values[column] = text
            if values:
                rows.append([values.get(i, "") for i in range(max(values) + 1)])
        return rows


def _table_payload(filename: str, content: bytes) -> list[dict[str, str]]:
    suffix = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if suffix == "xlsx":
        rows = _xlsx_rows(content)
    else:
        delimiter = "\t" if suffix == "tsv" else None
        text = content.decode("utf-8-sig")
        try:
            detected = delimiter or csv.Sniffer().sniff(text[:4096]).delimiter
        except csv.Error:
            detected = ","
        rows = list(csv.reader(StringIO(text), delimiter=detected))
    payload: list[dict[str, str]] = []
    headers: list[str] = []
    for row in rows:
        cells = [str(value).strip() for value in row]
        normalized = [re.sub(r"[^a-zа-яё0-9]", "", value.casefold()) for value in cells]
        if not headers:
            headers = cells
            continue
        if any(value in {"фио", "full_name"} for value in normalized) and any("контакт" in value.casefold() or "статус" in value.casefold() or "дата последней сдачи" in value.casefold() for value in cells):
            headers = cells
            continue
        if any(re.search(r"\d{1,2}\s+[а-я]+\s+в\s+\d{1,2}:\d{2}", value.casefold()) for value in cells):
            continue
        if any(value in {"фио", "full_name"} for value in normalized) and any("дата" in value.casefold() or "вид донации" in value.casefold() for value in cells):
            headers = cells
            continue
        if cells and any(value in {"фио", "full_name"} for value in normalized):
            headers = cells
            continue
        if any(value for value in cells):
            payload.append(dict(zip(headers, cells)))
    return payload


@router.post("/parse-import")
async def parse_import(request: Request, _: str = Depends(get_current_user)) -> dict:
    form = await request.form()
    upload = form.get("file")
    if upload is None or not hasattr(upload, "filename"):
        raise HTTPException(status_code=422, detail="Передайте файл в поле file")
    filename = str(upload.filename or "")
    if filename.lower().rsplit(".", 1)[-1] not in {"csv", "tsv", "xlsx"}:
        raise HTTPException(status_code=415, detail="Поддерживаются CSV, TSV и XLSX")
    try:
        rows = _table_payload(filename, await upload.read())
    except (UnicodeDecodeError, zipfile.BadZipFile, ET.ParseError, csv.Error, ValueError) as exc:
        raise HTTPException(status_code=422, detail="Не удалось прочитать файл импорта") from exc
    headers = list(rows[0].keys()) if rows else []
    return {"headers": headers, "rows": rows, "total": len(rows)}


def _row_value(row: dict[str, object], *names: str) -> str | None:
    normalized = {re.sub(r"[^a-zа-яё0-9]", "", key.casefold()): value for key, value in row.items()}
    for name in names:
        value = normalized.get(re.sub(r"[^a-zа-яё0-9]", "", name.casefold()))
        if value is not None:
            text = str(value).strip()
            if text:
                return text
    return None


def _parse_import_date(value: object) -> date | None:
    if value is None or not str(value).strip():
        return None
    text = str(value).strip()
    try:
        serial = float(text.replace(",", "."))
    except ValueError:
        serial = None
    if serial is not None and 25000 <= serial <= 65000:
        return date(1899, 12, 30) + timedelta(days=int(serial))
    for pattern in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    return None


def _import_donation_type(value: str | None) -> DonationType | None:
    if not value:
        return None
    normalized = re.sub(r"[^a-zа-яё0-9]", "", value.casefold())
    if normalized in {"организатор", "-"}:
        return None
    if any(term in normalized for term in ("плазм", "plasma")):
        return DonationType.PLASMA
    if any(term in normalized for term in ("тромбоц", "platelet")):
        return DonationType.PLATELETS
    if any(term in normalized for term in ("цельнаякровь", "кровь", "wholeblood", "донорствокрови")):
        return DonationType.WHOLE_BLOOD
    return None


def _import_rewarded(value: str | bool | None) -> bool:
    if isinstance(value, bool):
        return value
    return bool(value and value.strip().casefold() in {"1", "true", "yes", "да", "поощрена", "поощрено", "приказ сделан", "отправлено сз"})


@router.post("/bulk-import")
async def bulk_import(request: Request, db: Session = Depends(get_db), _: str = Depends(get_current_user)) -> dict:
    try:
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            raw = json.loads((await request.body()).decode("utf-8"))
            replace_all = bool(raw.get("replace_all", False)) if isinstance(raw, dict) else False
            raw_rows = raw.get("rows", []) if isinstance(raw, dict) else raw
            if not isinstance(raw_rows, list):
                raise HTTPException(status_code=422, detail="Ожидается массив доноров")
            rows = [item if isinstance(item, dict) else {} for item in raw_rows]
        else:
            form = await request.form()
            upload = form.get("file")
            if upload is None or not hasattr(upload, "filename"):
                raise HTTPException(status_code=422, detail="Передайте файл в поле file")
            filename = str(upload.filename or "")
            if filename.lower().rsplit(".", 1)[-1] not in {"csv", "tsv", "xlsx"}:
                raise HTTPException(status_code=415, detail="Поддерживаются CSV, TSV и XLSX")
            rows = _table_payload(filename, await upload.read())
            replace_all = str(form.get("replace_all", "false")).lower() in {"1", "true", "yes", "on"}

        if replace_all:
            db.execute(delete(Donation))
            db.execute(delete(Donor))
            db.flush()
        existing = list(db.scalars(select(Donor)))
        donors_by_name: dict[str, Donor] = {_dedupe_key(d.full_name): d for d in existing}
        seen_donations: set[tuple[UUID, date]] = {
            (donor_id, donation_date)
            for donor_id, donation_date in db.execute(
                select(Donation.donor_id, Donation.donation_date)
            )
        }
        result = {"created": 0, "updated": 0, "skipped": 0, "donations_created": 0, "donations_skipped": 0, "items": []}
        for index, row in enumerate(rows, start=2):
            try:
                full_name = _row_value(row, "full_name", "фио", "студент", "донор", "name") or ""
                full_name = _normalize_name(full_name)
                if not _valid_student_name(full_name):
                    result["skipped"] += 1
                    continue
                contacts = _row_value(row, "контакты", "contacts") or ""
                gender_value = _row_value(row, "gender", "пол", "sex")
                gender = (gender_value or _infer_gender(full_name) or DonorGender.MALE)
                if str(gender).casefold() in {"м", "муж", "мужской"}:
                    gender = DonorGender.MALE
                elif str(gender).casefold() in {"ж", "жен", "женский"}:
                    gender = DonorGender.FEMALE
                payload = DonorCreate(
                    full_name=full_name,
                    gender=gender,
                    academic_group=_normalize_group(_row_value(row, "academic_group", "группа", "group", "шифр")),
                    blood_group=_row_value(row, "blood_group", "группа крови", "abo", "кровь"),
                    rh_factor=_row_value(row, "rh_factor", "резус", "rh"),
                    kell_status="Kell+" if "kell+" in contacts.casefold() else _row_value(row, "kell_status", "kell", "келл"),
                )
            except (ValidationError, ValueError) as exc:
                result["skipped"] += 1
                result["items"].append({"row": index, "status": "skipped", "reason": str(exc)})
                continue
            key = _dedupe_key(payload.full_name)
            donor = donors_by_name.get(key)
            if donor is None:
                donor = Donor(id=uuid4(), **payload.model_dump())
                donors_by_name[key] = donor
                db.add(donor)
                db.flush()
                result["created"] += 1
                outcome = "created"
            else:
                donor_changed = False
                if payload.academic_group and donor.academic_group != payload.academic_group:
                    donor.academic_group = payload.academic_group
                    donor_changed = True
                changes = payload.model_dump(exclude_unset=True)
                for field in ("blood_group", "rh_factor", "kell_status"):
                    value = changes[field]
                    if value is not None and getattr(donor, field) != value:
                        setattr(donor, field, value)
                        donor_changed = True
                if donor_changed:
                    result["updated"] += 1
                    outcome = "updated"
                else:
                    outcome = "existing"
            item_result = {"row": index, "status": outcome, "donor_id": str(donor.id)}
            result["items"].append(item_result)

            donation_rows = [row]
            nested_donations = row.get("donations", [])
            if isinstance(nested_donations, list):
                donation_rows.extend(item for item in nested_donations if isinstance(item, dict))
            for donation_row in donation_rows:
                donation_date = _parse_import_date(_row_value(donation_row, "donation_date", "дата донации", "дата сдачи", "дата кроводачи", "date"))
                raw_type = _row_value(donation_row, "donation_type", "тип донации", "вид донации", "тип", "вид")
                donation_type = None if raw_type and raw_type.strip().casefold() in {"организатор", "-"} else _import_donation_type(raw_type)
                if donation_date is not None and donation_type is not None:
                    donation_key = (donor.id, donation_date)
                    if donation_key in seen_donations:
                        result["donations_skipped"] += 1
                        item_result.setdefault("donations", []).append("skipped_duplicate")
                        continue
                    seen_donations.add(donation_key)
                    donation = Donation(
                        donor_id=donor.id,
                        donation_date=donation_date,
                        donation_type=donation_type,
                        is_rewarded=_import_rewarded(_row_value(donation_row, "is_rewarded", "rewarded", "поощрена", "поощрено", "благодарность")),
                        medical_center=_row_value(donation_row, "medical_center", "медицинский центр", "место", "опк", "центр"),
                        certificate_number=_row_value(donation_row, "certificate_number", "номер справки", "справка", "номер"),
                    )
                    db.add(donation)
                    db.flush()
                    result["donations_created"] += 1
                    item_result.setdefault("donations", []).append("created")
                elif any(_row_value(donation_row, name) for name in ("donation_date", "дата донации", "дата сдачи", "дата кроводачи", "date")):
                    result["donations_skipped"] += 1
                    item_result.setdefault("donations", []).append("skipped_invalid_date_or_type")
        db.commit()
        return result
    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("", response_model=DonorPage)
def list_donors(limit: int = Query(1000, ge=1, le=1000), offset: int = Query(0, ge=0), blood_group: str | None = None, rh_factor: str | None = None, kell_status: str | None = None, status: DonorStatus | None = None, available_on: date | None = None, donation_type: DonationType = DonationType.WHOLE_BLOOD, db: Session = Depends(get_db), _: str = Depends(get_current_user)) -> DonorPage:
    query = select(Donor).options(selectinload(Donor.donations))
    filters = []
    if blood_group:
        filters.append(Donor.blood_group == blood_group)
    if rh_factor:
        filters.append(Donor.rh_factor == rh_factor)
    if kell_status:
        filters.append(Donor.kell_status == kell_status)
    if status:
        filters.append(Donor.status == status)
    if filters:
        query = query.where(*filters)
    donors = list(db.scalars(query.order_by(Donor.full_name)).unique())
    if available_on is not None:
        donors = [d for d in donors if d.status == DonorStatus.ACTIVE and calculate_next_eligible_date(d.donations, donation_type, d.gender, on_date=available_on) <= available_on]
    total = len(donors)
    donors = donors[offset:offset + limit]
    return DonorPage(items=[DonorRead.model_validate(d) for d in donors], total=total, limit=limit, offset=offset)


@router.post("", response_model=DonorRead, status_code=201)
def create_donor(payload: DonorCreate, db: Session = Depends(get_db), _: str = Depends(get_current_user)) -> Donor:
    donor = Donor(**payload.model_dump())
    db.add(donor)
    db.commit()
    db.refresh(donor)
    return donor


@router.post("/promote-courses", response_model=CoursePromotionBatchResult)
def promote_courses(payload: CoursePromotionRequest, db: Session = Depends(get_db), _: str = Depends(get_current_user)) -> CoursePromotionBatchResult:
    donors = list(db.scalars(select(Donor).order_by(Donor.id)))
    try:
        proposals = promote_donors_for_september(donors, effective_date=payload.effective_date)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    results = []
    for donor, proposal in zip(donors, proposals):
        previous_group, previous_status = donor.academic_group, donor.status
        donor.academic_group, donor.status = proposal.academic_group, proposal.status
        results.append(CoursePromotionResult(donor_id=donor.id, previous_group=previous_group, new_group=proposal.academic_group, previous_status=previous_status, new_status=proposal.status, promoted=proposal.promoted, archived=proposal.archived))
    db.commit()
    return CoursePromotionBatchResult(effective_date=payload.effective_date, processed=len(donors), promoted=sum(x.promoted for x in results), archived=sum(x.archived for x in results), unchanged=sum(not x.promoted and not x.archived for x in results), results=results)

@router.patch("/{donor_id}", response_model=DonorRead)
def update_donor(donor_id: UUID, payload: DonorUpdate, db: Session = Depends(get_db), _: str = Depends(get_current_user)) -> Donor:
    donor = db.get(Donor, donor_id)
    if donor is None:
        raise HTTPException(status_code=404, detail="Donor not found")
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=422, detail="At least one field must be provided")
    for field, value in changes.items():
        if value is None and field in {"full_name", "gender"}:
            raise HTTPException(status_code=422, detail=f"{field} cannot be null")
        setattr(donor, field, value)
    db.commit()
    db.refresh(donor)
    return donor


@router.get("/{donor_id}/intervals", response_model=IntervalCalendar)
def donor_intervals(donor_id: UUID, on_date: date | None = None, db: Session = Depends(get_db), _: str = Depends(get_current_user)) -> IntervalCalendar:
    donor = db.scalar(select(Donor).options(selectinload(Donor.donations)).where(Donor.id == donor_id))
    if donor is None:
        raise HTTPException(status_code=404, detail="Donor not found")
    as_of = on_date or date.today()
    history = list(donor.donations)
    latest = max(history, key=lambda item: item.donation_date, default=None)
    whole_count = count_whole_blood_donations(history, as_of.year)
    intervals = []
    for next_type in DonationType:
        allowed = calculate_next_eligible_date(history, next_type, donor.gender, on_date=as_of)
        days = required_interval_days(latest.donation_type if latest else None, next_type)
        intervals.append(DonationIntervalResult(previous_donation_date=latest.donation_date if latest else None, previous_donation_type=latest.donation_type if latest else None, next_donation_type=next_type, interval_days=days, eligible_from=allowed, whole_blood_donations_this_year=whole_count, annual_whole_blood_limit=annual_whole_blood_limit(donor.gender), annual_limit_reached=next_type == DonationType.WHOLE_BLOOD and whole_count >= annual_whole_blood_limit(donor.gender)))
    def progress(award: str, label: str) -> HonoraryDonorProgress:
        total, target, percent, achieved = honorary_donor_progress(history, award)
        return HonoraryDonorProgress(award_name=label, qualifying_donations=total, target_donations=target, progress_percent=percent, achieved=achieved)
    honorary = HonoraryDonorProgressResult(moscow=progress("moscow", "Почетный донор Москвы"), russia=progress("russia", "Почетный донор России"))
    return IntervalCalendar(donor=DonorRead.model_validate(donor), intervals=intervals, honorary_progress=honorary)
