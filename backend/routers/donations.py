"""Donation verification and deletion endpoints."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Donation, Donor
from ..schemas import DonationRead, DonationVerify
from .auth import get_current_user

router = APIRouter(prefix="/donations", tags=["donations"])


@router.put("/{donation_id}/verify", response_model=DonationRead)
def verify_donation(donation_id: UUID, payload: DonationVerify, db: Session = Depends(get_db), _: str = Depends(get_current_user)) -> Donation:
    if db.get(Donor, payload.donor_id) is None:
        raise HTTPException(status_code=404, detail="Donor not found")
    donation = db.get(Donation, donation_id)
    duplicate = db.scalar(select(Donation).where(Donation.donor_id == payload.donor_id, Donation.donation_date == payload.donation_date, Donation.id != donation_id))
    if duplicate:
        raise HTTPException(status_code=409, detail="Donation already exists for this donor and date")
    if donation is None:
        donation = Donation(id=donation_id, **payload.model_dump())
        db.add(donation)
    else:
        donation.donor_id = payload.donor_id
        donation.donation_date = payload.donation_date
        donation.donation_type = payload.donation_type
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
