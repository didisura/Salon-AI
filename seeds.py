from sqlmodel import Session, SQLModel, select
from database import engine
from models import Salon, Service, Staff

def seed_database():
    print("Recreating database tables...")
    # Drop and recreate tables for a clean slate
    SQLModel.metadata.drop_all(engine)
    SQLModel.metadata.create_all(engine)

    with Session(engine) as session:
        # Check using SQLModel's session.exec() syntax
        statement = select(Salon)
        existing_salons = session.exec(statement).all()
        
        if existing_salons:
            print("Database already seeded!")
            return

        print("Seeding database...")

        # 1. Create Salons
        salon1 = Salon(
            name="Bole Beauty Hub",
            location="Bole, Addis Ababa",
            phone_number="+251911123456",
        )
        salon2 = Salon(
            name="Kazanchis Wellness Center",
            location="Kazanchis, Addis Ababa",
            phone_number="+251922654321",
        )

        session.add(salon1)
        session.add(salon2)
        session.commit()
        session.refresh(salon1)
        session.refresh(salon2)

        # 2. Create Services (using duration_min)
        service1 = Service(
            name="Braid Styling / ሽሩባ",
            description="Traditional & modern braiding styles",
            price_etb=500.0,
            duration_min=60,
            salon_id=salon1.id
        )
        service2 = Service(
            name="Haircut & Wash",
            description="Full haircut with wash and blow dry",
            price_etb=350.0,
            duration_min=45,
            salon_id=salon1.id
        )
        service3 = Service(
            name="Manicure & Pedicure",
            description="Nail care and polish",
            price_etb=400.0,
            duration_min=50,
            salon_id=salon2.id
        )

        session.add_all([service1, service2, service3])

        # 3. Create Staff
        staff1 = Staff(
            name="Abebech T.",
            role="Hair Stylist",
            salon_id=salon1.id
        )
        staff2 = Staff(
            name="Tigist M.",
            role="Nail Specialist",
            salon_id=salon2.id
        )

        session.add_all([staff1, staff2])
        session.commit()

        print("✔ Database successfully seeded!")

if __name__ == "__main__":
    seed_database()