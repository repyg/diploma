"""Document generation endpoints."""

from datetime import date
from io import BytesIO
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..database import get_db
from ..models import Donation, DonationType, Donor, DonorStatus
from ..schemas import KPPEntry, KPPRequest, KPPResponse, MemoRequest
from ..services.calculator import calculate_next_eligible_date, honorary_donor_progress
from ..services.doc_generator import generate_memo
from .auth import get_current_user

router = APIRouter(prefix="/documents", tags=["documents"])
DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

_TYPE_LABELS = {
    DonationType.WHOLE_BLOOD: "Цельная кровь",
    DonationType.PLASMA: "Плазма",
    DonationType.PLATELETS: "Тромбоциты",
}


def _register_cyrillic_font() -> str:
    """Try to register a Cyrillic TTF font for ReportLab; fall back to Helvetica."""
    try:
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont

        candidates = (
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/dejavu/DejaVuSans.ttf",
            "C:/Windows/Fonts/arial.ttf",
            "/System/Library/Fonts/Supplemental/Arial.ttf",
        )
        for path in candidates:
            if Path(path).is_file():
                pdfmetrics.registerFont(TTFont("CyrillicFont", path))
                return "CyrillicFont"
    except Exception:
        pass
    return "Helvetica"


@router.post("/memo/generate")
def memo(
    payload: MemoRequest,
    db: Session = Depends(get_db),
    _: str = Depends(get_current_user),
) -> StreamingResponse:
    if payload.start_date > payload.end_date:
        raise HTTPException(status_code=422, detail="start_date must not be later than end_date")

    donors = list(
        db.scalars(
            select(Donor)
            .join(Donor.donations)
            .options(selectinload(Donor.donations))
            .where(
                Donation.donation_date >= payload.start_date,
                Donation.donation_date <= payload.end_date,
                Donation.is_rewarded.is_(False),
            )
            .order_by(Donor.full_name)
        ).unique()
    )
    coordinators = (
        list(db.scalars(select(Donor).where(Donor.id.in_(payload.coordinator_ids))))
        if payload.coordinator_ids
        else []
    )
    if not donors and not coordinators:
        raise HTTPException(status_code=422, detail="За выбранный период нет непоощрённых донаций")

    try:
        buffer = generate_memo(
            donors,
            payload.start_date,
            payload.end_date,
            payload.event_name,
            recipient_name=payload.recipient_name,
            salutation=payload.salutation,
            coordinators=coordinators,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if payload.mark_rewarded:
        for donor in donors:
            for donation in donor.donations:
                if payload.start_date <= donation.donation_date <= payload.end_date and not donation.is_rewarded:
                    donation.is_rewarded = True
        db.commit()

    return StreamingResponse(
        buffer,
        media_type=DOCX_MEDIA_TYPE,
        headers={"Content-Disposition": 'attachment; filename="donor_memo.docx"'},
    )


@router.post("/kpp-list", response_model=KPPResponse)
def kpp_list(
    payload: KPPRequest,
    db: Session = Depends(get_db),
    _: str = Depends(get_current_user),
) -> KPPResponse:
    query = select(Donor).options(selectinload(Donor.donations)).where(Donor.status == DonorStatus.ACTIVE)
    if payload.donor_ids is not None:
        query = query.where(Donor.id.in_(payload.donor_ids))
    entries = []
    for donor in db.scalars(query.order_by(Donor.academic_group, Donor.full_name)).unique():
        allowed = calculate_next_eligible_date(
            donor.donations, payload.donation_type, donor.gender, on_date=payload.on_date
        )
        if allowed <= payload.on_date:
            entries.append(
                KPPEntry(
                    donor_id=donor.id,
                    full_name=donor.full_name,
                    academic_group=donor.academic_group,
                    eligible_from=allowed,
                )
            )
    return KPPResponse(on_date=payload.on_date, entries=entries)


@router.get("/pgas/{donor_id}")
def pgas_portfolio(
    donor_id: UUID,
    db: Session = Depends(get_db),
    _: str = Depends(get_current_user),
) -> StreamingResponse:
    donor = db.scalar(select(Donor).options(selectinload(Donor.donations)).where(Donor.id == donor_id))
    if donor is None:
        raise HTTPException(status_code=404, detail="Donor not found")
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas
    except ImportError:
        raise HTTPException(status_code=501, detail="PDF generation dependency is unavailable") from None

    font_name = _register_cyrillic_font()
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    pdf.setTitle("Портфолио донора для ПГАС")

    if font_name == "CyrillicFont":
        pdf.setFont(font_name, 16)
        pdf.drawString(50, 800, "СПРАВКА-ПОРТФОЛИО ДОНОРА (для ПГАС)")
        pdf.setFont(font_name, 11)
        pdf.drawString(50, 772, f"ФИО обучающегося: {donor.full_name}")
        pdf.drawString(50, 754, f"Учебная группа: {donor.academic_group or '—'}")
        pdf.drawString(
            50,
            736,
            f"Фенотип: {donor.blood_group or '—'} {donor.rh_factor or ''} {donor.kell_status or ''}".strip(),
        )
        moscow_eq, moscow_target, _, _ = honorary_donor_progress(donor.donations, "moscow")
        pdf.drawString(50, 718, f"Всего подтвержденных донаций: {len(donor.donations)} (эквивалент ПДМ: {moscow_eq}/{moscow_target})")
        pdf.drawString(50, 700, f"Дата формирования выписки: {date.today():%d.%m.%Y}")

        y = 665
        pdf.drawString(50, y, "История донаций:")
        y -= 20
        for idx, item in enumerate(sorted(donor.donations, key=lambda x: x.donation_date, reverse=True), start=1):
            if y < 60:
                pdf.showPage()
                pdf.setFont(font_name, 11)
                y = 800
            t_label = _TYPE_LABELS.get(item.donation_type, str(item.donation_type))
            center = item.medical_center or "ОПК"
            cert = f"№ {item.certificate_number}" if item.certificate_number else ""
            pdf.drawString(50, y, f"{idx}. {item.donation_date:%d.%m.%Y} — {t_label} ({center}) {cert}".strip())
            y -= 18
    else:
        pdf.setFont("Helvetica", 14)
        pdf.drawString(50, 800, "Donor portfolio")
        pdf.setFont("Helvetica", 11)
        pdf.drawString(50, 775, f"ID: {donor.id}")
        pdf.drawString(50, 755, f"Donations: {len(donor.donations)}")
        pdf.drawString(50, 735, f"Generated: {date.today().isoformat()}")

    pdf.save()
    buffer.seek(0)
    return StreamingResponse(
        buffer,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="pgas-{donor_id}.pdf"'},
    )