"""Seed the local PostgreSQL database with ten fictional RKhTU students.

Run from the project root with ``python seed.py`` after configuring DATABASE_URL.
The command is idempotent and never removes or overwrites existing records.
"""

from datetime import date, timedelta

from sqlalchemy import select, text

from backend.database import Base, SessionLocal, engine
from backend.models import Donation, DonationType, Donor, DonorGender


def seed() -> None:
    Base.metadata.create_all(bind=engine)
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE donations ADD COLUMN IF NOT EXISTS medical_center VARCHAR(255)"))
        connection.execute(text("ALTER TABLE donations ADD COLUMN IF NOT EXISTS certificate_number VARCHAR(64)"))
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
