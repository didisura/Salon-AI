from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.requests import Request
from sqlmodel import SQLModel, Field, Session, create_engine, select
from pydantic import BaseModel
from typing import Optional

# -------------------------------------------------------------------
# Database Setup
# -------------------------------------------------------------------
sqlite_file_name = "melkegna.db"
sqlite_url = f"sqlite:///{sqlite_file_name}"

engine = create_engine(sqlite_url, echo=True)

# -------------------------------------------------------------------
# Models
# -------------------------------------------------------------------
class Salon(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    address: str
    phone: str

class Service(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    salon_id: int
    name: str
    price_etb: float
    duration_min: int

class Staff(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    salon_id: int
    name: str
    role: str

class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    telegram_id: Optional[int] = Field(default=None, unique=True)
    full_name: str
    phone_number: str
    role: str = "customer"

class Appointment(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int
    salon_id: int
    service_id: int
    staff_id: Optional[int] = None
    appointment_time: str
    status: str = "confirmed"  # confirmed, completed, cancelled
    booking_channel: str = "Telegram"  # Telegram or Manual

# -------------------------------------------------------------------
# FastAPI App Initialization
# -------------------------------------------------------------------
app = FastAPI(title="Melkegna API", version="1.0")
templates = Jinja2Templates(directory="templates")

def create_db_and_tables():
    SQLModel.metadata.create_all(engine)
    # Seed default salon & services if empty
    with Session(engine) as session:
        salon = session.exec(select(Salon)).first()
        if not salon:
            default_salon = Salon(id=1, name="Melkegna Beauty Salon", address="Bole, Addis Ababa", phone="0911000000")
            session.add(default_salon)
            session.commit()

            # Seed Services
            services = [
                Service(salon_id=1, name="Haircut & Styling", price_etb=350, duration_min=45),
                Service(salon_id=1, name="Manicure & Pedicure", price_etb=500, duration_min=60),
                Service(salon_id=1, name="Facial Treatment", price_etb=800, duration_min=50),
            ]
            for s in services:
                session.add(s)

            # Seed Staff
            staff_members = [
                Staff(salon_id=1, name="Dawit", role="Senior Stylist"),
                Staff(salon_id=1, name="Hiwot", role="Aesthetician"),
            ]
            for st in staff_members:
                session.add(st)
            session.commit()

@app.on_event("startup")
def on_startup():
    create_db_and_tables()

# -------------------------------------------------------------------
# Frontend Route
# -------------------------------------------------------------------
@app.get("/dashboard", response_class=HTMLResponse)
async def get_dashboard(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={}
    )

# -------------------------------------------------------------------
# Reception API Endpoints
# -------------------------------------------------------------------
@app.get("/api/reception/salons/{salon_id}/details")
def get_salon_details(salon_id: int):
    with Session(engine) as session:
        salon = session.get(Salon, salon_id)
        if not salon:
            raise HTTPException(status_code=404, detail="Salon not found")
        
        services = session.exec(select(Service).where(Service.salon_id == salon_id)).all()
        staff = session.exec(select(Staff).where(Staff.salon_id == salon_id)).all()
        
        return {
            "salon_name": salon.name,
            "services": services,
            "staff": staff
        }

@app.get("/api/reception/salons/{salon_id}/today-schedule")
def get_today_schedule(salon_id: int):
    with Session(engine) as session:
        appointments = session.exec(select(Appointment).where(Appointment.salon_id == salon_id)).all()
        
        schedule_list = []
        for appt in appointments:
            user = session.get(User, appt.user_id)
            service = session.get(Service, appt.service_id)
            staff = session.get(Staff, appt.staff_id) if appt.staff_id else None
            
            schedule_list.append({
                "appointment_id": appt.id,
                "customer_name": user.full_name if user else "Unknown",
                "customer_phone": user.phone_number if user else "N/A",
                "service_name": service.name if service else "General Service",
                "price_etb": service.price_etb if service else 0,
                "duration_min": service.duration_min if service else 30,
                "assigned_staff": staff.name if staff else "Any Available",
                "appointment_time": appt.appointment_time,
                "status": appt.status,
                "booking_channel": appt.booking_channel
            })
            
        return {"schedule": schedule_list}

class ManualBookingPayload(BaseModel):
    customer_name: str
    customer_phone: str
    salon_id: int
    service_id: int
    staff_id: Optional[int] = None
    appointment_time: str
    booking_source: str = "Manual"

@app.post("/api/reception/bookings/manual")
def create_manual_booking(payload: ManualBookingPayload):
    with Session(engine) as session:
        # 1. Find or create user by phone number
        user = session.exec(select(User).where(User.phone_number == payload.customer_phone)).first()
        if not user:
            user = User(
                full_name=payload.customer_name,
                phone_number=payload.customer_phone,
                role="customer"
            )
            session.add(user)
            session.commit()
            session.refresh(user)
        else:
            user.full_name = payload.customer_name
            session.add(user)
            session.commit()

        # 2. Create appointment
        appointment = Appointment(
            user_id=user.id,
            salon_id=payload.salon_id,
            service_id=payload.service_id,
            staff_id=payload.staff_id,
            appointment_time=payload.appointment_time,
            status="confirmed",
            booking_channel="Manual"
        )
        session.add(appointment)
        session.commit()
        session.refresh(appointment)

        return {"status": "success", "appointment_id": appointment.id}

class StatusUpdatePayload(BaseModel):
    status: str

@app.patch("/api/reception/appointments/{appointment_id}/status")
def update_appointment_status(appointment_id: int, payload: StatusUpdatePayload):
    with Session(engine) as session:
        appt = session.get(Appointment, appointment_id)
        if not appt:
            raise HTTPException(status_code=404, detail="Appointment not found")
        
        appt.status = payload.status
        session.add(appt)
        session.commit()
        return {"status": "success"}