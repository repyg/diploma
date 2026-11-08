"""Donor registry and eligibility endpoints."""

from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from ..database import get_db
from ..models import DonationType, Donor, DonorStatus
from ..schemas import (CoursePromotionBatchResult, CoursePromotionRequest, CoursePromotionResult, DonorCreate, DonorPage, DonorRead, DonorUpdate, DonationIntervalResult, HonoraryDonorProgress, HonoraryDonorProgressResult, IntervalCalendar)
from ..services.calculator import (annual_whole_blood_limit, calculate_next_eligible_date, count_whole_blood_donations, honorary_donor_progress, promote_donors_for_september, required_interval_days)
from .auth import get_current_user

router = APIRouter(prefix="/donors", tags=["donors"])


@router.get("", response_model=DonorPage)
def list_donors(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), blood_group: str | None = None, rh_factor: str | None = None, kell_status: str | None = None, status: DonorStatus | None = None, available_on: date | None = None, donation_type: DonationType = DonationType.WHOLE_BLOOD, db: Session = Depends(get_db), _: str = Depends(get_current_user)) -> DonorPage:
    query = select(Donor).options(selectinload(Donor.donations))
    count_query = select(func.count()).select_from(Donor)
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
        count_query = count_query.where(*filters)
    donors = list(db.scalars(query.order_by(Donor.full_name).offset(offset).limit(limit)).unique())
    if available_on is not None:
        donors = [d for d in donors if d.status == DonorStatus.ACTIVE and calculate_next_eligible_date(d.donations, donation_type, d.gender, on_date=available_on) <= available_on]
    return DonorPage(items=[DonorRead.model_validate(d) for d in donors], total=db.scalar(count_query) or 0, limit=limit, offset=offset)


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
