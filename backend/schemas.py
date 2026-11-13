"""Pydantic schemas for backend requests and responses."""

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .models import DonationType, DonorGender, DonorStatus


def normalize_donation_type(value):
    if isinstance(value, DonationType):
        return value
    if isinstance(value, str):
        normalized = value.strip().casefold()
        aliases = {
            "цельная кровь": DonationType.WHOLE_BLOOD,
            "кровь": DonationType.WHOLE_BLOOD,
            "whole_blood": DonationType.WHOLE_BLOOD,
            "плазма": DonationType.PLASMA,
            "плазмаферез": DonationType.PLASMA,
            "plasma": DonationType.PLASMA,
            "тромбоциты": DonationType.PLATELETS,
            "тромбоцитаферез": DonationType.PLATELETS,
            "platelets": DonationType.PLATELETS,
        }
        return aliases.get(normalized, value)
    return value


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class DonationCreate(BaseModel):
    donation_date: date
    donation_type: DonationType

    @field_validator("donation_type", mode="before")
    @classmethod
    def normalize_type(cls, value):
        return normalize_donation_type(value)


class DonationRead(ORMModel):
    id: UUID
    donor_id: UUID
    donor_full_name: str | None = None
    academic_group: str | None = None
    donor_status: DonorStatus | None = None
    donation_date: date
    donation_type: DonationType
    is_rewarded: bool
    medical_center: str | None = None
    certificate_number: str | None = None
    file_url: str | None = None
    created_at: datetime


class DonorCreate(BaseModel):
    full_name: str = Field(min_length=2, max_length=255)
    gender: DonorGender = DonorGender.MALE
    academic_group: str | None = Field(default=None, max_length=32)
    blood_group: str | None = Field(default=None, max_length=8)
    rh_factor: str | None = Field(default=None, max_length=4)
    kell_status: str | None = Field(default=None, max_length=16)

    @field_validator("gender", mode="before")
    @classmethod
    def blank_gender_defaults_to_male(cls, value):
        return DonorGender.MALE if isinstance(value, str) and not value.strip() else value

    @field_validator("academic_group", "blood_group", "rh_factor", "kell_status", mode="before")
    @classmethod
    def empty_strings_to_none(cls, value):
        return None if isinstance(value, str) and not value.strip() else value


class ImportedDonationItem(BaseModel):
    donation_date: date
    donation_type: DonationType
    is_rewarded: bool = False
    medical_center: str | None = Field(default=None, max_length=255)
    certificate_number: str | None = Field(default=None, max_length=128)

    @field_validator("donation_type", mode="before")
    @classmethod
    def normalize_type(cls, value):
        return normalize_donation_type(value)


class ImportedDonorItem(BaseModel):
    full_name: str = Field(min_length=2, max_length=255)
    gender: DonorGender = DonorGender.MALE
    academic_group: str | None = Field(default=None, max_length=32)
    blood_group: str | None = Field(default=None, max_length=8)
    rh_factor: str | None = Field(default=None, max_length=4)
    kell_status: str | None = Field(default=None, max_length=16)
    donation_date: date | None = None
    donation_type: DonationType | None = None
    is_rewarded: bool = False
    medical_center: str | None = Field(default=None, max_length=255)
    certificate_number: str | None = Field(default=None, max_length=128)
    donations: list[ImportedDonationItem] = Field(default_factory=list)

    @field_validator("donation_type", mode="before")
    @classmethod
    def normalize_type(cls, value):
        return normalize_donation_type(value) if value is not None else None


class DonorUpdate(BaseModel):
    full_name: str | None = Field(default=None, min_length=2, max_length=255)
    gender: DonorGender | None = None
    academic_group: str | None = Field(default=None, max_length=32)
    blood_group: str | None = Field(default=None, max_length=8)
    rh_factor: str | None = Field(default=None, max_length=4)
    kell_status: str | None = Field(default=None, max_length=16)
    status: DonorStatus | None = None

    @field_validator("*", mode="before")
    @classmethod
    def empty_strings_to_none(cls, value):
        return None if isinstance(value, str) and not value.strip() else value


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
    donations: list[DonationRead] = Field(default_factory=list)


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
    task_id: UUID | None = None
    file_url: str | None = None
    donation_date: date
    donation_type: DonationType
    medical_center: str | None = None
    certificate_number: str | None = None

    @field_validator("donation_type", mode="before")
    @classmethod
    def normalize_type(cls, value):
        return normalize_donation_type(value)


class DonationListRead(ORMModel):
    id: UUID
    donor_id: UUID
    donor_full_name: str
    academic_group: str | None
    donor_status: DonorStatus
    donation_date: date
    donation_type: DonationType
    is_rewarded: bool
    medical_center: str | None = None
    certificate_number: str | None = None
    file_url: str | None = None
    created_at: datetime


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

    @field_validator("donation_type", mode="before")
    @classmethod
    def normalize_type(cls, value):
        return normalize_donation_type(value)


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
