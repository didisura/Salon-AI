"""
Seeds a demo salon with a couple of services and a staff member so you
have something to click through in dev. Matches the real models in
models.py — NOT the old sqlmodel/schemas.py shape.

Run locally with: python seed.py

IMPORTANT:
- This is a DEV convenience script. Never run it against your production
  database — it's not idempotent-safe for a live system and the demo
  password below is public (it's sitting in your repo).
- If you ever do want a demo/test salon in production for support
  purposes, generate a random password with `secrets.token_urlsafe(16)`
  and store it somewhere private (not in this file), then change it
  after first login.
"""
from datetime import datetime, timedelta, time

from sqlalchemy.orm import sessionmaker

from database import engine, Base
from models import Salon, Service, Staff
from security import hash_password

Base.metadata.create_all(bind=engine)
SessionLocal = sessionmaker(bind=engine)

DEMO_PHONE = "0911000000"
DEMO_PASSWORD = "ChangeThisPassword123!"  # dev-only — rotate before any real use


def main() -> None:
    with SessionLocal() as session:
        existing = session.query(Salon).filter(Salon.phone == DEMO_PHONE).first()
        if existing:
            print(f"Demo salon already exists (id={existing.id}), skipping seed.")
            return

        salon = Salon(
            name="Demo Salon",
            owner_name="Demo Owner",
            phone=DEMO_PHONE,
            hashed_password=hash_password(DEMO_PASSWORD),
            status="active",
            subscription_expires_at=datetime.utcnow() + timedelta(days=365),
            opening_time=time(8, 0),
            closing_time=time(20, 0),
            working_days="0,1,2,3,4,5",  # Mon–Sat, matches register.html default
        )
        session.add(salon)
        session.commit()
        session.refresh(salon)

        session.add_all([
            Service(salon_id=salon.id, name="Haircut & Styling", price=350.0, duration_minutes=45),
            Service(salon_id=salon.id, name="Manicure", price=250.0, duration_minutes=30),
        ])
        session.add(Staff(salon_id=salon.id, name="Abebe"))
        session.commit()

        print(f"Seeded demo salon (id={salon.id}, phone={DEMO_PHONE}).")
        print(f"Dev login password: {DEMO_PASSWORD}  (change this before any shared/staging use)")


if __name__ == "__main__":
    main()