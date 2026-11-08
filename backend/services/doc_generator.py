"""Generate official donor reward memos as DOCX documents."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import date
from io import BytesIO

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Cm, Pt

from ..models import DonationType, Donor


DONATION_REASONS = {
    DonationType.WHOLE_BLOOD: "Осуществление донации цельной крови в добровольном порядке",
    DonationType.PLASMA: "Осуществление донации плазмы в добровольном порядке",
    DonationType.PLATELETS: "Осуществление донации тромбоцитов в добровольном порядке",
}


def generate_memo(
    donors: Iterable[Donor],
    start_date: date,
    end_date: date,
    event_name: str,
    *,
    recipient_name: str = "Лина Александровна",
    coordinators: Iterable[Donor] = (),
) -> BytesIO:
    """Build a memo for un-rewarded donations in the requested date range.

    The returned buffer is positioned at its beginning and can be returned
    directly by a FastAPI endpoint using the DOCX media type.
    """
    if start_date > end_date:
        raise ValueError("start_date must not be later than end_date")
    if not event_name.strip():
        raise ValueError("event_name must not be empty")

    grouped: dict[DonationType, dict[str, tuple[Donor, int]]] = defaultdict(dict)
    for donor in donors:
        for donation in donor.donations:
            if (
                start_date <= donation.donation_date <= end_date
                and not donation.is_rewarded
            ):
                grouped[donation.donation_type].setdefault(
                    str(donor.id), (donor, len(grouped[donation.donation_type]) + 1)
                )

    document = Document()
    section = document.sections[0]
    section.top_margin = Cm(2)
    section.bottom_margin = Cm(2)
    section.left_margin = Cm(2.5)
    section.right_margin = Cm(1.5)
    normal = document.styles["Normal"]
    normal.font.name = "Times New Roman"
    normal.font.size = Pt(12)

    recipient = document.add_paragraph()
    recipient.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    recipient.add_run(f"Уважаемая {recipient_name}!")

    document.add_paragraph(
        f"Просим Вас объявить благодарность за помощь в проведении мероприятия «{event_name}» "
        f"в период с {start_date:%d.%m.%Y} по {end_date:%d.%m.%Y}."
    )

    section_number = 1
    for donation_type in (
        DonationType.WHOLE_BLOOD,
        DonationType.PLASMA,
        DonationType.PLATELETS,
    ):
        participants = grouped.get(donation_type, {})
        if not participants:
            continue
        document.add_paragraph(
            f"{section_number}. Просим Вас объявить благодарность студентам за участие в мероприятии:"
        )
        _add_participant_table(
            document,
            [
                (number, donor.full_name, donor.academic_group or "—", DONATION_REASONS[donation_type])
                for donor, number in participants.values()
            ],
        )
        section_number += 1

    coordinator_rows = [
        (number, donor.full_name, donor.academic_group or "—",
         "Координация и сопровождение группы доноров на всех этапах")
        for number, donor in enumerate(coordinators, start=1)
    ]
    if coordinator_rows:
        document.add_paragraph(
            f"{section_number}. Просим Вас объявить благодарность за организацию и проведение мероприятия:"
        )
        _add_participant_table(document, coordinator_rows)

    output = BytesIO()
    document.save(output)
    output.seek(0)
    return output


def _add_participant_table(
    document: Document, rows: list[tuple[int, str, str, str]]
) -> None:
    table = document.add_table(rows=1, cols=4)
    table.style = "Table Grid"
    for cell, value in zip(table.rows[0].cells, ("№", "ФИО", "Группа", "Обоснование")):
        cell.text = value
        for run in cell.paragraphs[0].runs:
            run.bold = True
    for number, full_name, group, reason in rows:
        cells = table.add_row().cells
        for cell, value in zip(cells, (str(number), full_name, group, reason)):
            cell.text = value


__all__ = ["generate_memo"]
