"""Seed the local PostgreSQL database with ten fictional RKhTU students.

Run from the project root with ``python seed.py`` after configuring DATABASE_URL.
The command is idempotent and never removes or overwrites existing records.
"""

from datetime import date, timedelta

from sqlalchemy import select

from backend.database import Base, SessionLocal, engine
from backend.models import Donation, DonationType, Donor, DonorGender

SEED_DONORS = [
    ("Александр Иванович Волков", DonorGender.MALE, "ХТ-11", "O(I)", "Rh-", "Kell-"),
    ("Мария Сергеевна Смирнова", DonorGender.FEMALE, "БТ-12", "A(II)", "Rh+", "Kell-"),
    ("Дмитрий Андреевич Кузнецов", DonorGender.MALE, "Э-13", "B(III)", "Rh+", "Kell+"),
    ("Анна Павловна Попова", DonorGender.FEMALE, "ТМ-21", "AB(IV)", "Rh+", "Kell-"),
    ("Михаил Олегович Соколов", DonorGender.MALE, "ХТ-22", "O(I)", "Rh+", "Kell-"),
    ("Екатерина Ильинична Лебедева", DonorGender.FEMALE, "БТ-23", "A(II)", "Rh-", "Kell-"),
    ("Илья Романович Козлов", DonorGender.MALE, "Э-31", "B(III)", "Rh-", "Kell+"),
    ("София Максимовна Новикова", DonorGender.FEMALE, "ТМ-32", "O(I)", "Rh+", "Kell-"),
    ("Артём Николаевич Морозов", DonorGender.MALE, "ХТ-33", "A(II)", "Rh+", "Kell-"),
    ("Полина Викторовна Павлова", DonorGender.FEMALE, "БТ-34", "AB(IV)", "Rh-", "Kell-"),
]


def seed() -> None:
    Base.metadata.create_all(bind=engine)
    today = date.today()
    with SessionLocal.begin() as db:
        for index, (name, gender, group, blood, rh, kell) in enumerate(SEED_DONORS):
            donor = db.scalar(select(Donor).where(Donor.full_name == name, Donor.academic_group == group))
            if donor is None:
                donor = Donor(full_name=name, gender=gender, academic_group=group, blood_group=blood, rh_factor=rh, kell_status=kell)
                db.add(donor)
                db.flush()
            if not donor.donations:
                history = [
                    (today - timedelta(days=120 + index), DonationType.WHOLE_BLOOD),
                    (today - timedelta(days=250 + index), DonationType.PLASMA if index % 2 else DonationType.WHOLE_BLOOD),
                ]
                for donation_date, donation_type in history:
                    exists = db.scalar(select(Donation.id).where(Donation.donor_id == donor.id, Donation.donation_date == donation_date))
                    if exists is None:
                        db.add(Donation(donor_id=donor.id, donation_date=donation_date, donation_type=donation_type))
    print(f"Seed complete: {len(SEED_DONORS)} fictional RKhTU donors ensured.")


if __name__ == "__main__":
    seed()
