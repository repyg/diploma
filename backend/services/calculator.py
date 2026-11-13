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

# Award thresholds are expressed in qualifying equivalents; platelet donations
# count as whole-blood donations, while plasma donations count as half.
HONORARY_DONOR_THRESHOLDS: dict[str, int] = {
    "moscow": 20,
    "russia": 40,
}
COMPONENT_EQUIVALENT = 0.5

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


def count_whole_blood_donations(
    donations: Iterable[Donation], year: int
) -> int:
    """Count whole-blood donations in the given calendar year."""
    return sum(
        donation.donation_type == DonationType.WHOLE_BLOOD
        and donation.donation_date.year == year
        for donation in donations
    )


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
    """Calculate the first date allowed by intervals and annual blood limit.

    When the annual whole-blood limit has been reached, eligibility is moved
    to January 1 of the next year. The medical interval is then applied too.
    """
    records = tuple(donations)
    if not records:
        return on_date or date.today()

    latest = max(records, key=lambda donation: donation.donation_date)
    eligible = latest.donation_date + timedelta(
        days=required_interval_days(latest.donation_type, next_type)
    )
    reference_date = on_date or date.today()

    if next_type == DonationType.WHOLE_BLOOD:
        limit = annual_whole_blood_limit(gender)
        year = max(reference_date.year, eligible.year)
        while count_whole_blood_donations(records, year) >= limit:
            year += 1
        if year > eligible.year:
            eligible = max(eligible, date(year, 1, 1))

    return max(eligible, reference_date)


def honorary_donor_progress(
    donations: Iterable[Donation], award: str
) -> tuple[float, int, float, bool]:
    """Return qualifying equivalents, target, percent, and achieved state.

    Blood donations count as one equivalent; plasma and platelet donations
    count as half. Award rules are represented by explicit thresholds above.
    """
    try:
        target = HONORARY_DONOR_THRESHOLDS[award.lower()]
    except KeyError as exc:
        raise ValueError("award must be 'moscow' or 'russia'") from exc

    total = sum(
        COMPONENT_EQUIVALENT
        if donation.donation_type == DonationType.PLASMA
        else 1.0
        for donation in donations
    )
    percent = min(total / target * 100, 100.0)
    return total, target, percent, total >= target


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
