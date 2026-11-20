"""Pure business rules for donation intervals, awards, and course promotion."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING

from ..models import DonationType, DonorGender, DonorStatus

if TYPE_CHECKING:
    from ..models import Donation, Donor


# Minimum intervals stated in the project specification for Order No. 1166n.
INTERVAL_DAYS: dict[tuple[DonationType, DonationType], int] = {
    (DonationType.WHOLE_BLOOD, DonationType.WHOLE_BLOOD): 60,
    (DonationType.WHOLE_BLOOD, DonationType.PLASMA): 30,
    (DonationType.WHOLE_BLOOD, DonationType.PLATELETS): 30,
    (DonationType.PLASMA, DonationType.WHOLE_BLOOD): 14,
    (DonationType.PLASMA, DonationType.PLASMA): 14,
    (DonationType.PLASMA, DonationType.PLATELETS): 14,
    (DonationType.PLATELETS, DonationType.WHOLE_BLOOD): 14,
    (DonationType.PLATELETS, DonationType.PLASMA): 14,
    (DonationType.PLATELETS, DonationType.PLATELETS): 14,
}
ANNUAL_WHOLE_BLOOD_LIMITS: dict[DonorGender, int] = {
    DonorGender.MALE: 5,
    DonorGender.FEMALE: 4,
}

COURSE_GROUP_PATTERN = re.compile(
    r"^(?P<prefix>[А-ЯЁа-яё]+)-(?P<course>[1-4])(?P<suffix>\d+)$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class CoursePromotion:
    """Result of evaluating one academic group for September promotion."""

    academic_group: str | None
    status: DonorStatus
    promoted: bool
    archived: bool

def count_whole_blood_donations_rolling(
    donations: Iterable[Donation],
    on_date: date | None = None,
) -> int:
    """Count whole-blood donations within the trailing 365-day window."""
    reference = on_date or date.today()
    window_start = reference - timedelta(days=365)
    return sum(
        d.donation_type == DonationType.WHOLE_BLOOD
        and window_start < d.donation_date <= reference
        for d in donations
    )

def required_interval_days(
    previous_type: DonationType | None,
    next_type: DonationType,
) -> int:
    """Return the minimum interval from the previous donation type."""
    if previous_type is None:
        return 0
    try:
        return INTERVAL_DAYS[(previous_type, next_type)]
    except KeyError as exc:
        raise ValueError("Unsupported donation type combination") from exc


def annual_whole_blood_limit(gender: DonorGender) -> int:
    """Return the annual whole-blood limit for the donor's gender."""
    return ANNUAL_WHOLE_BLOOD_LIMITS[gender]


def calculate_next_eligible_date(
    donations: Iterable[Donation],
    next_type: DonationType,
    gender: DonorGender,
    *,
    on_date: date | None = None,
) -> date:
    reference_date = on_date or date.today()
    records = tuple(donations)
    if not records:
        return reference_date

    # Максимум по ВСЕМ прошлым донациям, а не только по последней
    eligible = max(
        d.donation_date + timedelta(days=required_interval_days(d.donation_type, next_type))
        for d in records
    )

    # Скользящее окно 365 дней для годового лимита цельной крови
    if next_type == DonationType.WHOLE_BLOOD:
        limit = annual_whole_blood_limit(gender)
        wb_dates = sorted(
            d.donation_date for d in records if d.donation_type == DonationType.WHOLE_BLOOD
        )
        candidate = max(eligible, reference_date)
        while True:
            window_start = candidate - timedelta(days=365)
            in_window = [dt for dt in wb_dates if window_start < dt <= candidate]
            if len(in_window) < limit:
                break
            candidate = in_window[0] + timedelta(days=366)
        return candidate

    return max(eligible, reference_date)


def honorary_donor_progress(
    donations: Iterable[Donation], award: str
) -> tuple[float, int, float, bool]:
    key = award.lower()
    if key == "russia":
        blood_target, plasma_target, mix_threshold = 40, 60, 25
    elif key == "moscow":
        blood_target, plasma_target, mix_threshold = 20, 30, 13
    else:
        raise ValueError("award must be 'moscow' or 'russia'")

    blood_like = sum(d.donation_type != DonationType.PLASMA for d in donations)
    plasma = sum(d.donation_type == DonationType.PLASMA for d in donations)
    total = blood_like + plasma

    if blood_like >= mix_threshold:
        target = blood_target
    else:
        target = plasma_target

    achieved = blood_like >= blood_target or total >= target
    effective = min(total, target) if achieved else total
    percent = round(min(effective / target * 100.0, 100.0), 1)
    return float(effective), target, percent, achieved


def promote_academic_group(
    academic_group: str | None,
    status: DonorStatus = DonorStatus.ACTIVE,
) -> CoursePromotion:
    """Increment the course code, archiving fourth-year students."""
    if status != DonorStatus.ACTIVE or academic_group is None:
        return CoursePromotion(academic_group, status, False, False)

    normalized = academic_group.strip()
    match = COURSE_GROUP_PATTERN.fullmatch(normalized)
    if match is None:
        return CoursePromotion(academic_group, status, False, False)

    course = int(match.group("course"))
    if course == 4:
        return CoursePromotion(normalized, DonorStatus.GRADUATE, False, True)

    updated_group = (
        f"{match.group('prefix')}-{course + 1}{match.group('suffix')}"
    )
    return CoursePromotion(updated_group, status, True, False)


def promote_donors_for_september(
    donors: Iterable[Donor], *, effective_date: date
) -> list[CoursePromotion]:
    """Apply course promotion rules to donors for an explicit effective date.

    This pure helper returns proposed changes; persistence is left to the API
    or scheduled job. Calls outside September are rejected.
    """
    if effective_date.month != 9 or effective_date.day != 1:
        raise ValueError("Course promotion is only valid on September 1")

    return [
        promote_academic_group(donor.academic_group, donor.status)
        for donor in donors
    ]
