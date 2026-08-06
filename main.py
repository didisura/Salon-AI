import os
import json
import asyncio
from datetime import datetime, date, timedelta
from typing import Optional, List, Dict

from fastapi import (
    FastAPI,
    Request,
    Response,
    Form,
    Depends,
    HTTPException,
    status,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from sqlmodel import SQLModel, Field, Session, select, create_engine, Relationship
from pydantic import BaseModel
import jwt

# ==========================================
# DATABASE & SYSTEM CONFIGURATION
# ==========================================
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./melkegna.db")
# Adjust Postgres URL for SQLAlchemy compatibility if hosted on Render/Railway
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

SECRET_KEY = os.getenv("SECRET_KEY", "melkegna-super-secret-key-change-in-prod")
ALGORITHM = "HS256"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {},
)

app = FastAPI(title="Melkegna Platform", version="2.0.0")

# Mount Static and Templates
os.makedirs("static", exist_ok=True)
os.makedirs("templates", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


# ==========================================
# WEBSOCKET CONNECTION MANAGER
# ==========================================
class ConnectionManager:
    def __init__(self):
        # Maps salon_id -> List[WebSocket]
        self.active_connections: Dict[int, List[WebSocket]] = {}

    async def connect(self, websocket: WebSocket, salon_id: int):
        await websocket.accept()
        if salon_id not in self.active_connections:
            self.active_connections[salon_id] = []
        self.active_connections[salon_id].append(websocket)

    def disconnect(self, websocket: WebSocket, salon_id: int):
        if salon_id in self.active_connections:
            if websocket in self.active_connections[salon_id]:
                self.active_connections[salon_id].remove(websocket)
            if not self.active_connections[salon_id]:
                del self.active_connections[salon_id]

    async def broadcast_to_salon(self, salon_id: int, message: dict):
        if salon_id in self.active_connections:
            for connection in self.active_connections[salon_id]:
                try:
                    await connection.send_json(message)
                except Exception:
                    # Connection might be dead, handled on disconnect
                    pass


manager = ConnectionManager()

# ==========================================
# SQLMODEL DATABASE SCHEMAS
# ==========================================
class Salon(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    owner_name: str
    phone: str = Field(unique=True, index=True)
    password_hash: str
    created_at: datetime = Field(default_factory=datetime.utcnow)

    services: List["Service"] = Relationship(back_populates="salon")
    staff: List["Staff"] = Relationship(back_populates="salon")
    appointments: List["Appointment"] = Relationship(back_populates="salon")
    waitlist: List["Waitlist"] = Relationship(back_populates="salon")


class Service(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    salon_id: int = Field(foreign_key="salon.id", index=True)
    name: str
    price: float
    duration_minutes: int

    salon: Optional[Salon] = Relationship(back_populates="services")


class Staff(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    salon_id: int = Field(foreign_key="salon.id", index=True)
    name: str

    salon: Optional[Salon] = Relationship(back_populates="staff")


class Appointment(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    salon_id: int = Field(foreign_key="salon.id", index=True)
    service_id: int = Field(foreign_key="service.id")
    staff_id: int = Field(foreign_key="staff.id")
    customer_name: str
    customer_phone: str
    appointment_time: datetime = Field(index=True)
    status: str = Field(default="Confirmed")  # Confirmed, Completed, No-Show, Cancelled
    created_at: datetime = Field(default_factory=datetime.utcnow)

    salon: Optional[Salon] = Relationship(back_populates="appointments")


class Waitlist(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    salon_id: int = Field(foreign_key="salon.id", index=True)
    customer_name: str
    customer_phone: str
    service_id: int = Field(foreign_key="service.id")
    staff_id: int = Field(foreign_key="staff.id")
    preferred_date: date
    created_at: datetime = Field(default_factory=datetime.utcnow)

    salon: Optional[Salon] = Relationship(back_populates="waitlist")


def init_db():
    SQLModel.metadata.create_all(engine)


@app.on_event("startup")
def on_startup():
    init_db()


def get_db():
    with Session(engine) as session:
        yield session


# ==========================================
# AUTHENTICATION HELPERS
# ==========================================
def create_access_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(days=30)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def get_current_salon(request: Request, db: Session = Depends(get_db)) -> Optional[Salon]:
    token = request.cookies.get("access_token")
    if not token:
        return None
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        salon_id: int = payload.get("sub")
        if salon_id is None:
            return None
    except jwt.PyJWTError:
        return None

    salon = db.get(Salon, salon_id)
    return salon


# ==========================================
# ROUTES & ENDPOINTS
# ==========================================
@app.get("/", response_class=HTMLResponse)
def index_redirect(request: Request, db: Session = Depends(get_db)):
    salon = get_current_salon(request, db)
    if salon:
        return RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)
    return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(
    "index.html",                        # 1st arg: template file name (string)
    {"request": request, "user": user}   # 2nd arg: context dictionary
)


@app.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    phone: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    statement = select(Salon).where(Salon.phone == phone)
    salon = db.exec(statement).first()

    # Note: In production, use passlib/bcrypt for password hashing verification
    if not salon or salon.password_hash != password:
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "የስልክ ቁጥር ወይም የፉልቃል ስህተት ነው!"},
        )

    token = create_access_token({"sub": salon.id})
    response = RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)
    response.set_cookie(key="access_token", value=token, httponly=True)
    return response


@app.get("/logout")
def logout():
    response = RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    response.delete_cookie("access_token")
    return response


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(
    request: Request,
    tab: str = "home",
    selected_date: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    error: Optional[str] = None,
    db: Session = Depends(get_db),
):
    salon = get_current_salon(request, db)
    if not salon:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)

    today_str = date.today().isoformat()
    filter_date_str = selected_date if selected_date else today_str
    try:
        target_date = date.fromisoformat(filter_date_str)
    except ValueError:
        target_date = date.today()

    # Base Query for Appointments
    all_appts = db.exec(
        select(Appointment).where(Appointment.salon_id == salon.id)
    ).all()

    # Calculate Revenue Metrics
    daily_rev = sum(
        db.get(Service, a.service_id).price
        for a in all_appts
        if a.appointment_time.date() == date.today() and a.status == "Completed"
    )

    week_ago = date.today() - timedelta(days=7)
    weekly_rev = sum(
        db.get(Service, a.service_id).price
        for a in all_appts
        if a.appointment_time.date() >= week_ago and a.status == "Completed"
    )

    month_start = date.today().replace(day=1)
    monthly_rev = sum(
        db.get(Service, a.service_id).price
        for a in all_appts
        if a.appointment_time.date() >= month_start and a.status == "Completed"
    )

    # Custom Revenue Filter Calculation
    custom_rev = None
    if start_date and end_date:
        try:
            s_date = date.fromisoformat(start_date)
            e_date = date.fromisoformat(end_date)
            custom_rev = sum(
                db.get(Service, a.service_id).price
                for a in all_appts
                if s_date <= a.appointment_time.date() <= e_date
                and a.status == "Completed"
            )
        except ValueError:
            custom_rev = 0.0

    # Appointments for display table
    target_appts = [
        a for a in all_appts if a.appointment_time.date() == target_date
    ]
    target_appts.sort(key=lambda x: x.appointment_time)

    # Format appointment dictionaries for Jinja rendering
    formatted_appointments = []
    for a in target_appts:
        srv = db.get(Service, a.service_id)
        stf = db.get(Staff, a.staff_id)
        formatted_appointments.append(
            {
                "id": a.id,
                "customer_name": a.customer_name,
                "customer_phone": a.customer_phone,
                "appointment_time": a.appointment_time.strftime("%I:%M %p"),
                "service_name": srv.name if srv else "Unknown",
                "service_price": f"{srv.price:,.2f}" if srv else "0.00",
                "staff_name": stf.name if stf else "Unknown",
                "status": a.status,
            }
        )

    # Counts
    today_appt_count = len(
        [a for a in all_appts if a.appointment_time.date() == date.today()]
    )
    no_show_count_today = len(
        [
            a
            for a in all_appts
            if a.appointment_time.date() == date.today() and a.status == "No-Show"
        ]
    )
    total_customers = len(set(a.customer_phone for a in all_appts))

    # Waitlist Data
    raw_waitlist = db.exec(
        select(Waitlist).where(Waitlist.salon_id == salon.id)
    ).all()
    formatted_waitlist = []
    for w in raw_waitlist:
        srv = db.get(Service, w.service_id)
        stf = db.get(Staff, w.staff_id)
        formatted_waitlist.append(
            {
                "id": w.id,
                "customer_name": w.customer_name,
                "customer_phone": w.customer_phone,
                "service_name": srv.name if srv else "Unknown",
                "staff_name": stf.name if stf else "Unknown",
                "preferred_date": w.preferred_date.isoformat(),
            }
        )

    services = db.exec(select(Service).where(Service.salon_id == salon.id)).all()
    staff_members = db.exec(select(Staff).where(Staff.salon_id == salon.id)).all()

    base_url = str(request.base_url).rstrip("/")
    booking_url = f"{base_url}/book/{salon.id}"

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "salon": salon,
            "active_tab": tab,
            "selected_date": filter_date_str,
            "current_date": today_str,
            "start_date": start_date or "",
            "end_date": end_date or "",
            "error": error,
            "daily_rev": daily_rev,
            "weekly_rev": weekly_rev,
            "monthly_rev": monthly_rev,
            "custom_rev": custom_rev,
            "today_appt_count": today_appt_count,
            "no_show_count_today": no_show_count_today,
            "total_customers": total_customers,
            "appointments": formatted_appointments,
            "waitlist_entries": formatted_waitlist,
            "services": services,
            "staff_members": staff_members,
            "booking_url": booking_url,
        },
    )


# ==========================================
# ACTION HANDLERS (POST ENDPOINTS)
# ==========================================
@app.post("/book-appointment")
async def book_appointment(
    request: Request,
    customer_name: str = Form(...),
    customer_phone: str = Form(...),
    service_id: int = Form(...),
    staff_id: int = Form(...),
    appointment_time: str = Form(...),
    db: Session = Depends(get_db),
):
    salon = get_current_salon(request, db)
    if not salon:
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        dt = datetime.fromisoformat(appointment_time)
    except ValueError:
        return RedirectResponse(
            url="/dashboard?tab=home&error=invalid_date",
            status_code=status.HTTP_302_FOUND,
        )

    # Double Booking Prevention Logic
    existing = db.exec(
        select(Appointment).where(
            Appointment.salon_id == salon.id,
            Appointment.staff_id == staff_id,
            Appointment.appointment_time == dt,
            Appointment.status != "Cancelled",
        )
    ).first()

    if existing:
        return RedirectResponse(
            url="/dashboard?tab=home&error=conflict", status_code=status.HTTP_302_FOUND
        )

    new_appt = Appointment(
        salon_id=salon.id,
        service_id=service_id,
        staff_id=staff_id,
        customer_name=customer_name,
        customer_phone=customer_phone,
        appointment_time=dt,
        status="Confirmed",
    )
    db.add(new_appt)
    db.commit()
    db.refresh(new_appt)

    srv = db.get(Service, service_id)
    stf = db.get(Staff, staff_id)

    # Broadcast to Connected WebSockets
    await manager.broadcast_to_salon(
        salon.id,
        {
            "event": "new_booking",
            "appointment": {
                "id": new_appt.id,
                "customer_name": new_appt.customer_name,
                "customer_phone": new_appt.customer_phone,
                "appointment_time": new_appt.appointment_time.strftime("%I:%M %p"),
                "service_name": srv.name if srv else "Unknown",
                "service_price": f"{srv.price:,.2f}" if srv else "0.00",
                "staff_name": stf.name if stf else "Unknown",
                "status": new_appt.status,
            },
        },
    )

    return RedirectResponse(url="/dashboard?tab=home", status_code=status.HTTP_302_FOUND)


@app.post("/update-appointment-status")
async def update_appointment_status(
    request: Request,
    appointment_id: int = Form(...),
    status: str = Form(...),
    db: Session = Depends(get_db),
):
    salon = get_current_salon(request, db)
    if not salon:
        return JSONResponse(status_code=401, content={"success": False, "error": "Unauthorized"})

    appt = db.get(Appointment, appointment_id)
    if appt and appt.salon_id == salon.id:
        appt.status = status
        db.add(appt)
        db.commit()
        return JSONResponse(content={"success": True, "status": status})

    return JSONResponse(status_code=400, content={"success": False, "error": "Not Found"})


@app.post("/add-service")
def add_service(
    request: Request,
    name: str = Form(...),
    price: float = Form(...),
    duration_minutes: int = Form(...),
    db: Session = Depends(get_db),
):
    salon = get_current_salon(request, db)
    if salon:
        srv = Service(
            salon_id=salon.id,
            name=name,
            price=price,
            duration_minutes=duration_minutes,
        )
        db.add(srv)
        db.commit()
    return RedirectResponse(url="/dashboard?tab=services", status_code=status.HTTP_302_FOUND)


@app.post("/delete-service")
def delete_service(
    request: Request, service_id: int = Form(...), db: Session = Depends(get_db)
):
    salon = get_current_salon(request, db)
    if salon:
        srv = db.get(Service, service_id)
        if srv and srv.salon_id == salon.id:
            db.delete(srv)
            db.commit()
    return RedirectResponse(url="/dashboard?tab=services", status_code=status.HTTP_302_FOUND)


@app.post("/add-staff")
def add_staff(
    request: Request, name: str = Form(...), db: Session = Depends(get_db)
):
    salon = get_current_salon(request, db)
    if salon:
        stf = Staff(salon_id=salon.id, name=name)
        db.add(stf)
        db.commit()
    return RedirectResponse(url="/dashboard?tab=staff", status_code=status.HTTP_302_FOUND)


@app.post("/delete-staff")
def delete_staff(
    request: Request, staff_id: int = Form(...), db: Session = Depends(get_db)
):
    salon = get_current_salon(request, db)
    if salon:
        stf = db.get(Staff, staff_id)
        if stf and stf.salon_id == salon.id:
            db.delete(stf)
            db.commit()
    return RedirectResponse(url="/dashboard?tab=staff", status_code=status.HTTP_302_FOUND)


@app.post("/add-waitlist")
def add_waitlist(
    request: Request,
    customer_name: str = Form(...),
    customer_phone: str = Form(...),
    service_id: int = Form(...),
    staff_id: int = Form(...),
    preferred_date: str = Form(...),
    db: Session = Depends(get_db),
):
    salon = get_current_salon(request, db)
    if salon:
        p_date = date.fromisoformat(preferred_date)
        w = Waitlist(
            salon_id=salon.id,
            customer_name=customer_name,
            customer_phone=customer_phone,
            service_id=service_id,
            staff_id=staff_id,
            preferred_date=p_date,
        )
        db.add(w)
        db.commit()
    return RedirectResponse(url="/dashboard?tab=reserve", status_code=status.HTTP_302_FOUND)


@app.post("/convert-waitlist/{waitlist_id}")
def convert_waitlist(
    waitlist_id: int,
    request: Request,
    appointment_time: str = Form(...),
    db: Session = Depends(get_db),
):
    salon = get_current_salon(request, db)
    if not salon:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)

    w = db.get(Waitlist, waitlist_id)
    if w and w.salon_id == salon.id:
        dt = datetime.fromisoformat(appointment_time)
        appt = Appointment(
            salon_id=salon.id,
            service_id=w.service_id,
            staff_id=w.staff_id,
            customer_name=w.customer_name,
            customer_phone=w.customer_phone,
            appointment_time=dt,
            status="Confirmed",
        )
        db.add(appt)
        db.delete(w)
        db.commit()

    return RedirectResponse(url="/dashboard?tab=home", status_code=status.HTTP_302_FOUND)


@app.post("/delete-waitlist")
def delete_waitlist(
    request: Request, waitlist_id: int = Form(...), db: Session = Depends(get_db)
):
    salon = get_current_salon(request, db)
    if salon:
        w = db.get(Waitlist, waitlist_id)
        if w and w.salon_id == salon.id:
            db.delete(w)
            db.commit()
    return RedirectResponse(url="/dashboard?tab=reserve", status_code=status.HTTP_302_FOUND)


# ==========================================
# WEBSOCKET ENDPOINT
# ==========================================
@app.websocket("/ws/salon/{salon_id}")
async def websocket_endpoint(websocket: WebSocket, salon_id: int):
    await manager.connect(websocket, salon_id)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket, salon_id)