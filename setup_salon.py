from sqlmodel import Session, select, SQLModel
from database import engine
from models import User, Salon, Service, Staff

def setup_real_salon():
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        # Check if salon exists or create new
        salon = session.exec(select(Salon)).first()
        if not salon:
            salon = Salon(
                name="Melkegna Beauty Salon",
                location="Bole, Addis Ababa",
                phone_number="0911002233"
            )
            session.add(salon)
            session.commit()
            session.refresh(salon)

        # Clear existing services/staff if updating
        print(f"Configured Salon: {salon.name} (ID: {salon.id})")

        # 1. Add Real Services (Modify prices & names to match the salon)
        services_data = [
            {"name": "Haircut & Styling (የወንዶች/የሴቶች ቁርጥ)", "price_etb": 400.0, "duration_min": 30},
            {"name": "Hair Coloring / Dye (ቀለም)", "price_etb": 1200.0, "duration_min": 90},
            {"name": "Manicure & Pedicure (እጅና እግር ጥፍር)", "price_etb": 600.0, "duration_min": 45},
            {"name": "Facial Treatment (የፊት እንክብካቤ)", "price_etb": 1000.0, "duration_min": 60},
        ]

        for s in services_data:
            existing = session.exec(
                select(Service).where(Service.salon_id == salon.id, Service.name == s["name"])
            ).first()
            if not existing:
                session.add(Service(salon_id=salon.id, **s))

        # 2. Add Real Staff Members
        staff_data = [
            {"name": "Selam", "role": "Hair Specialist"},
            {"name": "Tigist", "role": "Nail Artist"},
            {"name": "Dawit", "role": "Barber"},
        ]

        for st in staff_data:
            existing = session.exec(
                select(Staff).where(Staff.salon_id == salon.id, Staff.name == st["name"])
            ).first()
            if not existing:
                session.add(Staff(salon_id=salon.id, **st))

        session.commit()
        print("✅ Real Salon Services and Staff successfully loaded!")

if __name__ == "__main__":
    setup_real_salon()