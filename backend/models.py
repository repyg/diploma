"""SQLAlchemy models for donors and donation records."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import Boolean, Date, DateTime, Enum, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


class DonorGender(StrEnum):
    """Gender values used by annual whole-blood limits."""

    MALE = "male"
    FEMALE = "female"


class DonationType(StrEnum):
    """Donation types covered by Order No. 1166n calculations."""

    WHOLE_BLOOD = "whole_blood"
    PLASMA = "plasma"
    PLATELETS = "platelets"


class DonorStatus(StrEnum):
    """Current status of a student in the donor registry."""

    ACTIVE = "active"
    GRADUATE = "graduate"
    ARCHIVED = "archived"


class Donor(Base):
    """Person registered in the university donor registry."""

    __tablename__ = "donors"

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    gender: Mapped[DonorGender] = mapped_column(
        Enum(DonorGender, name="donor_gender"), nullable=False
    )
    academic_group: Mapped[str | None] = mapped_column(String(32), nullable=True)
    blood_group: Mapped[str | None] = mapped_column(String(8), nullable=True)
    rh_factor: Mapped[str | None] = mapped_column(String(4), nullable=True)
    kell_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    status: Mapped[DonorStatus] = mapped_column(
        Enum(DonorStatus, name="donor_status"),
        default=DonorStatus.ACTIVE,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, nullable=False
    )

    donations: Mapped[list[Donation]] = relationship(
        back_populates="donor",
        cascade="all, delete-orphan",
        order_by="Donation.donation_date",
    )


class Donation(Base):
    """Verified fact of a donor's donation."""

    __tablename__ = "donations"
    __table_args__ = (
        UniqueConstraint("donor_id", "donation_date", name="uq_donation_donor_date"),
        Index("ix_donations_donor_date", "donor_id", "donation_date"),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    donor_id: Mapped[UUID] = mapped_column(
        ForeignKey("donors.id", ondelete="CASCADE"), nullable=False
    )
    donation_date: Mapped[date] = mapped_column(Date, nullable=False)
    donation_type: Mapped[DonationType] = mapped_column(
        Enum(DonationType, name="donation_type"), nullable=False
    )
    is_rewarded: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    medical_center: Mapped[str | None] = mapped_column(String(255), nullable=True)
    certificate_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, nullable=False
    )

    donor: Mapped[Donor] = relationship(back_populates="donations")


__all__: Sequence[str] = (
    "Donation",
    "DonationType",
    "Donor",
    "DonorGender",
    "DonorStatus",
)
