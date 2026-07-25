from database import SessionLocal
from models import Service

db = SessionLocal()

services = db.query(Service).all()

for s in services:
    print(
        "Service ID:", s.id,
        "Salon ID:", s.salon_id,
        "Name:", s.service_name
    )

db.close()