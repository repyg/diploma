"""Donation verification and deletion endpoints."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Donation, Donor
from ..schemas import DonationListRead, DonationRead, DonationVerify
from .auth import get_current_user

router = APIRouter(prefix="/donations", tags=["donations"])


@router.get("", response_model=list[DonationListRead])
def list_donations(db: Session = Depends(get_db), _: str = Depends(get_current_user)) -> list[dict]:
    rows = db.execute(
        select(Donation, Donor)
        .join(Donor, Donation.donor_id == Donor.id)
        .order_by(Donation.donation_date.desc())
    ).all()
    return [
        {
            "id": donation.id,
            "donor_id": donor.id,
            "donor_full_name": donor.full_name,
            "academic_group": donor.academic_group,
            "donor_status": donor.status,
            "donation_date": donation.donation_date,
            "donation_type": donation.donation_type,
            "is_rewarded": donation.is_rewarded,
            "medical_center": getattr(donation, "medical_center", None),
            "certificate_number": getattr(donation, "certificate_number", None),
            "created_at": donation.created_at,
        }
        for donation, donor in rows
    ]


@router.put("/{donation_id}/verify", response_model=DonationRead)
def verify_donation(donation_id: UUID, payload: DonationVerify, db: Session = Depends(get_db), _: str = Depends(get_current_user)) -> Donation:
    if db.get(Donor, payload.donor_id) is None:
        raise HTTPException(status_code=404, detail="Donor not found")
    donation = db.get(Donation, donation_id)
    duplicate = db.scalar(select(Donation).where(Donation.donor_id == payload.donor_id, Donation.donation_date == payload.donation_date, Donation.id != donation_id))
    if duplicate:
        raise HTTPException(status_code=409, detail="Donation already exists for this donor and date")
    if donation is None:
        donation = Donation(
            id=donation_id,
            donor_id=payload.donor_id,
            donation_date=payload.donation_date,
            donation_type=payload.donation_type,
        )
        if hasattr(donation, "medical_center"):
            donation.medical_center = payload.medical_center
        if hasattr(donation, "certificate_number"):
            donation.certificate_number = payload.certificate_number
        db.add(donation)
    else:
        donation.donor_id = payload.donor_id
        donation.donation_date = payload.donation_date
        donation.donation_type = payload.donation_type
        if hasattr(donation, "medical_center"):
            donation.medical_center = payload.medical_center
        if hasattr(donation, "certificate_number"):
            donation.certificate_number = payload.certificate_number
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Donation conflicts with an existing record") from None
    db.refresh(donation)
    return donation


@router.delete("/{donation_id}", status_code=204)
def delete_donation(donation_id: UUID, db: Session = Depends(get_db), _: str = Depends(get_current_user)) -> None:
    donation = db.get(Donation, donation_id)
    if donation is None:
        raise HTTPException(status_code=404, detail="Donation not found")
    db.delete(donation)
    db.commit()
