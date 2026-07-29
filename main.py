import os
from pathlib import Path
from dotenv import load_dotenv
import logging
from typing import Optional
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Depends, Form
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Field, SQLModel, Session, create_engine, select
from datetime import datetime, timedelta


# ---------------------------------------------------------------------------
# 1. SETUP & CONFIG
# ---------------------------------------------------------------------------
# Load environment variables
BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
load_dotenv(dotenv_path=ENV_PATH)

# Retrieve token
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

if not TELEGRAM_BOT_TOKEN:
    print("WARNING: TELEGRAM_BOT_TOKEN is missing!")

# Diagnostic Prints
print("--------------------------------------------------")
print("1. Checking path:", ENV_PATH)
print("2. Does file exist?:", ENV_PATH.is_file())
print("3. DEBUG TOKEN:", TELEGRAM_BOT_TOKEN)
print("--------------------------------------------------")

if not TELEGRAM_BOT_TOKEN:
    logging.warning(
        "TELEGRAM_BOT_TOKEN is not set! Create a .env file locally with "
        "TELEGRAM_BOT_TOKEN=your_token, or set it in Render/Railway's Environment Variables."
    )

DATABASE_URL = f"sqlite:///{BASE_DIR / 'melkegna.db'}"
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})


# ---------------------------------------------------------------------------
# 2. MODELS
# ---------------------------------------------------------------------------
class Service(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    price: float
    duration_min: int


class Staff(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    role: Optional[str] = "Stylist"
    phone: Optional[str] = None


class Appointment(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    telegram_id: Optional[int] = None
    customer_name: str
    customer_phone: str
    service_id: int
    service_name: str
    staff_id: int
    staff_name: str
    appointment_time: str
    status: str = "PENDING"
    booking_source: str = "TELEGRAM"


SQLModel.metadata.create_all(engine)


def get_session():
    with Session(engine) as session:
        yield session


# ---------------------------------------------------------------------------
# 3. FASTAPI APP
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    yield

app = FastAPI(lifespan=lifespan, debug=True)
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


# ---------------------------------------------------------------------------
# 4. DASHBOARD ROUTES
# ---------------------------------------------------------------------------
@app.get("/dashboard", response_class=HTMLResponse)
def get_dashboard(request: Request, phone_search: Optional[str] = None, session: Session = Depends(get_session)):
    services = session.exec(select(Service)).all()
    staff = session.exec(select(Staff)).all()

    query = select(Appointment)
    if phone_search:
        query = query.where(Appointment.customer_phone.contains(phone_search.strip()))
    appointments = session.exec(query).all()

    today_str = datetime.now().strftime("%Y-%m-%d")
    start_of_week = (datetime.now() - timedelta(days=datetime.now().weekday())).strftime("%Y-%m-%d")
    current_month_str = datetime.now().strftime("%Y-%m")

    daily_revenue = 0.0
    weekly_revenue = 0.0
    monthly_revenue = 0.0
    daily_completed_count = 0
    weekly_completed_count = 0
    monthly_completed_count = 0

    services_map = {s.id: s for s in services}

    all_appointments = session.exec(select(Appointment)).all()
    for appt in all_appointments:
        if appt.status == "COMPLETED":
            service_price = services_map[appt.service_id].price if appt.service_id in services_map else 0.0
            appt_date = appt.appointment_time.split(" ")[0]

            if appt_date == today_str:
                daily_revenue += service_price
                daily_completed_count += 1
            if appt_date >= start_of_week:
                weekly_revenue += service_price
                weekly_completed_count += 1
            if appt_date.startswith(current_month_str):
                monthly_revenue += service_price
                monthly_completed_count += 1

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "services": services,
            "staff": staff,
            "appointments": appointments,
            "phone_search": phone_search or "",
            "services_map": {s.id: s.name for s in services},
            "staff_map": {st.id: st.name for st in staff},
            "daily_revenue": daily_revenue,
            "weekly_revenue": weekly_revenue,
            "monthly_revenue": monthly_revenue,
            "daily_completed_count": daily_completed_count,
            "weekly_completed_count": weekly_completed_count,
            "monthly_completed_count": monthly_completed_count,
            "today_date": today_str
        },
    )


@app.post("/appointments/add-manual")
def add_manual_appointment(
    customer_name: str = Form(...),
    customer_phone: str = Form(...),
    service_id: int = Form(...),
    staff_id: int = Form(...),
    appointment_date: str = Form(...),
    appointment_time_slot: str = Form(...),
    booking_source: str = Form(...),
    session: Session = Depends(get_session)
):
    srv = session.get(Service, service_id)
    stf = session.get(Staff, staff_id)

    full_appt_time = f"{appointment_date} {appointment_time_slot}"

    appt = Appointment(
        customer_name=customer_name,
        customer_phone=customer_phone,
        service_id=service_id,
        service_name=srv.name if srv else "Unknown",
        staff_id=staff_id,
        staff_name=stf.name if stf else "Unassigned",
        appointment_time=full_appt_time,
        status="CONFIRMED",
        booking_source=booking_source
    )
    session.add(appt)
    session.commit()
    return RedirectResponse(url="/dashboard", status_code=303)


@app.post("/services/add")
def add_service(
    name: str = Form(...),
    price: float = Form(...),
    duration_min: int = Form(...),
    session: Session = Depends(get_session),
):
    session.add(Service(name=name, price=price, duration_min=duration_min))
    session.commit()
    return RedirectResponse(url="/dashboard", status_code=303)


@app.post("/services/update/{service_id}")
def update_service(
    service_id: int,
    name: str = Form(...),
    price: float = Form(...),
    duration_min: int = Form(...),
    session: Session = Depends(get_session),
):
    srv = session.get(Service, service_id)
    if srv:
        srv.name = name
        srv.price = price
        srv.duration_min = duration_min
        session.commit()
    return RedirectResponse(url="/dashboard", status_code=303)


@app.post("/services/delete/{service_id}")
def delete_service(service_id: int, session: Session = Depends(get_session)):
    srv = session.get(Service, service_id)
    if srv:
        session.delete(srv)
        session.commit()
    return RedirectResponse(url="/dashboard", status_code=303)


@app.post("/staff/add")
def add_staff(
    name: str = Form(...),
    role: str = Form(...),
    phone: str = Form(None),
    session: Session = Depends(get_session),
):
    session.add(Staff(name=name, role=role, phone=phone))
    session.commit()
    return RedirectResponse(url="/dashboard", status_code=303)


@app.post("/staff/update/{staff_id}")
def update_staff(
    staff_id: int,
    name: str = Form(...),
    role: str = Form(...),
    phone: str = Form(None),
    session: Session = Depends(get_session),
):
    st = session.get(Staff, staff_id)
    if st:
        st.name = name
        st.role = role
        st.phone = phone
        session.commit()
    return RedirectResponse(url="/dashboard", status_code=303)


@app.post("/staff/delete/{staff_id}")
def delete_staff(staff_id: int, session: Session = Depends(get_session)):
    st = session.get(Staff, staff_id)
    if st:
        session.delete(st)
        session.commit()
    return RedirectResponse(url="/dashboard", status_code=303)


@app.post("/appointments/update-status/{appt_id}")
def update_status(
    appt_id: int, status: str = Form(...), session: Session = Depends(get_session)
):
    appt = session.get(Appointment, appt_id)
    if appt:
        appt.status = status
        session.commit()
    return RedirectResponse(url="/dashboard", status_code=303)


@app.post("/appointments/delete/{appt_id}")
def delete_appointment(appt_id: int, session: Session = Depends(get_session)):
    appt = session.get(Appointment, appt_id)
    if appt:
        session.delete(appt)
        session.commit()
    return RedirectResponse(url="/dashboard", status_code=303)