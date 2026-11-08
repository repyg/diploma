"""Pydantic schemas for backend requests and responses."""

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from .models import DonationType, DonorGender, DonorStatus


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class DonationCreate(BaseModel):
    donation_date: date
    donation_type: DonationType


class DonationRead(ORMModel):
    id: UUID
    donor_id: UUID
    donation_date: date
    donation_type: DonationType
    is_rewarded: bool
    created_at: datetime


class DonorCreate(BaseModel):
    full_name: str = Field(min_length=2, max_length=255)
    gender: DonorGender
    academic_group: str | None = Field(default=None, max_length=32)
    blood_group: str | None = Field(default=None, max_length=8)
    rh_factor: str | None = Field(default=None, max_length=4)
    kell_status: str | None = Field(default=None, max_length=16)


class DonorUpdate(BaseModel):
    full_name: str | None = Field(default=None, min_length=2, max_length=255)
    gender: DonorGender | None = None
    academic_group: str | None = Field(default=None, max_length=32)
    blood_group: str | None = Field(default=None, max_length=8)
    rh_factor: str | None = Field(default=None, max_length=4)
    kell_status: str | None = Field(default=None, max_length=16)
    status: DonorStatus | None = None


class DonorRead(ORMModel):
    id: UUID
    full_name: str
    gender: DonorGender
    academic_group: str | None
    blood_group: str | None
    rh_factor: str | None
    kell_status: str | None
    status: DonorStatus
    created_at: datetime


class DonationIntervalResult(BaseModel):
    previous_donation_date: date | None
    previous_donation_type: DonationType | None
    next_donation_type: DonationType
    interval_days: int = Field(ge=0)
    eligible_from: date
    whole_blood_donations_this_year: int = Field(ge=0)
    annual_whole_blood_limit: int = Field(ge=0)
    annual_limit_reached: bool


class HonoraryDonorProgress(BaseModel):
    award_name: str
    qualifying_donations: float = Field(ge=0)
    target_donations: int = Field(gt=0)
    progress_percent: float = Field(ge=0, le=100)
    achieved: bool


class HonoraryDonorProgressResult(BaseModel):
    moscow: HonoraryDonorProgress
    russia: HonoraryDonorProgress


class CoursePromotionResult(BaseModel):
    donor_id: UUID
    previous_group: str | None
    new_group: str | None
    previous_status: DonorStatus
    new_status: DonorStatus
    promoted: bool
    archived: bool


class CoursePromotionBatchResult(BaseModel):
    effective_date: date
    processed: int = Field(ge=0)
    promoted: int = Field(ge=0)
    archived: int = Field(ge=0)
    unchanged: int = Field(ge=0)
    results: list[CoursePromotionResult]


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class DonorPage(BaseModel):
    items: list[DonorRead]
    total: int = Field(ge=0)
    limit: int = Field(gt=0)
    offset: int = Field(ge=0)


class IntervalCalendar(BaseModel):
    donor: DonorRead
    intervals: list[DonationIntervalResult]
    honorary_progress: HonoraryDonorProgressResult


class CoursePromotionRequest(BaseModel):
    effective_date: date


class DonationVerify(BaseModel):
    donor_id: UUID
    donation_date: date
    donation_type: DonationType


class OCRTaskResponse(BaseModel):
    task_id: UUID
    status: str
    result: dict | None = None
    error: str | None = None


class MemoRequest(BaseModel):
    start_date: date
    end_date: date
    event_name: str = Field(min_length=1, max_length=500)
    recipient_name: str = Field(default="Лина Александровна", min_length=1, max_length=255)
    coordinator_ids: list[UUID] = Field(default_factory=list)


class KPPRequest(BaseModel):
    donation_type: DonationType = DonationType.WHOLE_BLOOD
    on_date: date
    donor_ids: list[UUID] | None = None


class KPPEntry(BaseModel):
    donor_id: UUID
    full_name: str
    academic_group: str | None
    eligible_from: date


class KPPResponse(BaseModel):
    on_date: date
    entries: list[KPPEntry]


class DobroRequest(BaseModel):
    event_name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, min_length=300, max_length=3000)
    start_time: str = Field(default="08:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    end_time: str = Field(default="13:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    volunteer_roles: list[str] = Field(default_factory=list, max_length=20)


class DobroResponse(BaseModel):
    event_name: str
    description: str = Field(min_length=300, max_length=3000)
    start_time: str
    end_time: str
    duration_hours: float = Field(ge=0, le=8)
    generated_locally: bool = True
