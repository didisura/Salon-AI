# seed.py
from sqlmodel import Session, create_engine
from main import Service, Staff

engine = create_engine("sqlite:///melkegna.db")

with Session(engine) as session:
    session.add(Service(name="Haircut & Styling", price=350.0, duration_min=45))
    session.add(Service(name="Manicure", price=250.0, duration_min=30))
    session.add(Staff(name="Abebe", role="Senior Stylist", phone="0911000000"))
    session.commit()

print("Database seeded successfully!")