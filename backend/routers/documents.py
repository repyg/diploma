"""Document generation endpoints."""

from datetime import date
from io import BytesIO
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..database import get_db
from ..models import Donation, Donor, DonorStatus
from ..schemas import KPPEntry, KPPRequest, KPPResponse, MemoRequest
from ..services.calculator import calculate_next_eligible_date
from ..services.doc_generator import generate_memo
from .auth import get_current_user

router = APIRouter(prefix="/documents", tags=["documents"])
DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@router.post("/memo/generate")
def memo(payload: MemoRequest, db: Session = Depends(get_db), _: str = Depends(get_current_user)) -> StreamingResponse:
    if payload.start_date > payload.end_date:
        raise HTTPException(status_code=422, detail="start_date must not be later than end_date")
    donors = list(db.scalars(select(Donor).options(selectinload(Donor.donations)).order_by(Donor.full_name)).unique())
    included = [d for d in donors if any(payload.start_date <= x.donation_date <= payload.end_date and not x.is_rewarded for x in d.donations)]
    coordinators = list(db.scalars(select(Donor).where(Donor.id.in_(payload.coordinator_ids)))) if payload.coordinator_ids else []
    try:
        buffer = generate_memo(included, payload.start_date, payload.end_date, payload.event_name, recipient_name=payload.recipient_name, coordinators=coordinators)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    for donor in included:
        for donation in donor.donations:
            if payload.start_date <= donation.donation_date <= payload.end_date and not donation.is_rewarded:
                donation.is_rewarded = True
    db.commit()
    return StreamingResponse(buffer, media_type=DOCX_MEDIA_TYPE, headers={"Content-Disposition": 'attachment; filename="donor_memo.docx"'})


@router.post("/kpp-list", response_model=KPPResponse)
def kpp_list(payload: KPPRequest, db: Session = Depends(get_db), _: str = Depends(get_current_user)) -> KPPResponse:
    query = select(Donor).options(selectinload(Donor.donations)).where(Donor.status == DonorStatus.ACTIVE)
    if payload.donor_ids is not None:
        query = query.where(Donor.id.in_(payload.donor_ids))
    entries = []
    for donor in db.scalars(query.order_by(Donor.academic_group, Donor.full_name)).unique():
        allowed = calculate_next_eligible_date(donor.donations, payload.donation_type, donor.gender, on_date=payload.on_date)
        if allowed <= payload.on_date:
            entries.append(KPPEntry(donor_id=donor.id, full_name=donor.full_name, academic_group=donor.academic_group, eligible_from=allowed))
    return KPPResponse(on_date=payload.on_date, entries=entries)


@router.get("/pgas/{donor_id}")
def pgas_portfolio(donor_id: UUID, db: Session = Depends(get_db), _: str = Depends(get_current_user)) -> StreamingResponse:
    donor = db.scalar(select(Donor).options(selectinload(Donor.donations)).where(Donor.id == donor_id))
    if donor is None:
        raise HTTPException(status_code=404, detail="Donor not found")
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas
    except ImportError:
        raise HTTPException(status_code=501, detail="PDF generation dependency is unavailable") from None
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    pdf.setTitle("Портфолио донора")
    pdf.setFont("Helvetica", 14)
    pdf.drawString(50, 800, "Donor portfolio")
    pdf.setFont("Helvetica", 11)
    pdf.drawString(50, 775, f"ID: {donor.id}")
    pdf.drawString(50, 755, f"Donations: {len(donor.donations)}")
    pdf.drawString(50, 735, f"Generated: {date.today().isoformat()}")
    pdf.save()
    buffer.seek(0)
    return StreamingResponse(buffer, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="pgas-{donor_id}.pdf"'})
