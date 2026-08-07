import asyncio
import datetime
import hmac
import os
from contextlib import asynccontextmanager
from typing import Optional, List

import jwt
from fastapi import Depends, FastAPI, Form, HTTPException, Request, WebSocket, WebSocketDisconnect, status
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlmodel import Field, Session, SQLModel, create_engine, select

from pwdlib import PasswordHash
from pwdlib.hashers.bcrypt import BcryptHasher

# ==============================================================================
# 1. SECURITY & CONFIGURATION
# ==============================================================================

SECRET_KEY = os.getenv(
    "SECRET_KEY",
    "SUPER_SECRET_MELKEGNA_KEY_CHANGE_THIS_IN_PRODUCTION"
)

ADMIN_SECRET_KEY = os.getenv(
    "ADMIN_SECRET_KEY",
    "MELKEGNA_ADMIN_2026"
)

ALGORITHM = "HS256"
TOKEN_EXPIRE_HOURS = 24

password_hash = PasswordHash((BcryptHasher(),))


def hash_password(password: str) -> str:
    return password_hash.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return password_hash.verify(plain_password, hashed_password)


def create_access_token(salon_id: int) -> str:
    payload = {
        "sub": str(salon_id),
        "exp": datetime.datetime.now(datetime.timezone.utc)
        + datetime.timedelta(hours=TOKEN_EXPIRE_HOURS),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def get_current_salon_id(request: Request) -> Optional[int]:
    token = request.cookies.get("access_token")
    if not token:
        return None

    try:
        if token.startswith("Bearer "):
            token = token[7:]

        payload = jwt.decode(
            token,
            SECRET_KEY,
            algorithms=[ALGORITHM],
        )
        return int(payload["sub"])
    except Exception:
        return None


# ==============================================================================
# 2. DATABASE MODELS & DYNAMIC ENGINE
# ==============================================================================

class Salon(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    owner_name: str
    phone: str = Field(unique=True, index=True)
    password_hash: str
    status: str = Field(default="pending")  # pending, active, suspended, rejected
    subscription_expires_at: Optional[datetime.date] = None
    created_at: datetime.date = Field(default_factory=datetime.date.today)


class Service(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    salon_id: int = Field(foreign_key="salon.id", index=True)
    name: str
    price: float
    duration_minutes: int = Field(default=60)


class Staff(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    salon_id: int = Field(foreign_key="salon.id", index=True)
    name: str


class Appointment(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    salon_id: int = Field(foreign_key="salon.id", index=True)
    customer_name: str
    customer_phone: str
    service_id: Optional[int] = Field(default=None, foreign_key="service.id")
    staff_id: Optional[int] = Field(default=None, foreign_key="staff.id")
    appointment_time: str
    appointment_date: datetime.date = Field(default_factory=datetime.date.today)
    status: str = Field(default="Confirmed")  # Confirmed, Completed, Cancelled, No-Show


class Waitlist(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    salon_id: int = Field(foreign_key="salon.id", index=True)
    customer_name: str = Field(default="")
    customer_phone: str
    service_id: Optional[int] = Field(default=None, foreign_key="service.id")
    staff_id: Optional[int] = Field(default=None, foreign_key="staff.id")
    preferred_date: datetime.date = Field(default_factory=datetime.date.today)
    note: Optional[str] = None
    status: str = Field(default="Waiting")  # Waiting, Converted, Cancelled
    created_at: datetime.datetime = Field(default_factory=datetime.datetime.utcnow)


DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///melkegna.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

is_sqlite = "sqlite" in DATABASE_URL
connect_args = {"check_same_thread": False} if is_sqlite else {}
engine_kwargs = {"connect_args": connect_args}

if not is_sqlite:
    engine_kwargs.update({
        "pool_pre_ping": True,
        "pool_recycle": 300,
        "pool_size": 10,
        "max_overflow": 20,
    })

engine = create_engine(DATABASE_URL, **engine_kwargs)


def get_session():
    with Session(engine) as session:
        yield session


def run_light_migrations():
    statements = [
        "ALTER TABLE service ADD COLUMN duration_minutes INTEGER DEFAULT 60",
        "ALTER TABLE waitlist ADD COLUMN customer_name VARCHAR DEFAULT ''",
    ]
    with engine.connect() as conn:
        for stmt in statements:
            try:
                conn.execute(text(stmt))
                conn.commit()
            except Exception:
                conn.rollback()


# ==============================================================================
# 2.5 REAL-TIME WEBSOCKET ENGINE
# ==============================================================================

class ConnectionManager:
    def __init__(self):
        self.active_connections: dict[int, List[WebSocket]] = {}

    async def connect(self, salon_id: int, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.setdefault(salon_id, []).append(websocket)

    def disconnect(self, salon_id: int, websocket: WebSocket):
        conns = self.active_connections.get(salon_id)
        if conns and websocket in conns:
            conns.remove(websocket)
            if not conns:
                del self.active_connections[salon_id]

    async def broadcast(self, salon_id: int, message: dict):
        conns = list(self.active_connections.get(salon_id, []))
        for ws in conns:
            try:
                await ws.send_json(message)
            except Exception:
                self.disconnect(salon_id, ws)


manager = ConnectionManager()
main_event_loop: Optional[asyncio.AbstractEventLoop] = None


def broadcast_new_booking(salon_id: int, payload: dict):
    if main_event_loop is None:
        return
    message = {"event": "new_booking", "appointment": payload}
    asyncio.run_coroutine_threadsafe(manager.broadcast(salon_id, message), main_event_loop)


# ==============================================================================
# 3. APPLICATION & LIFESPAN MANAGEMENT
# ==============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    global main_event_loop
    main_event_loop = asyncio.get_running_loop()
    SQLModel.metadata.create_all(engine)
    run_light_migrations()
    yield


app = FastAPI(title="Melkegna Platform", lifespan=lifespan)
templates = Jinja2Templates(directory="templates")


def get_active_salon(request: Request, db: Session = Depends(get_session)) -> Salon:
    salon_id = get_current_salon_id(request)
    if not salon_id:
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})

    salon = db.get(Salon, salon_id)
    if not salon:
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})

    today = datetime.date.today()
    if salon.subscription_expires_at and salon.subscription_expires_at < today and salon.status == "active":
        salon.status = "suspended"
        db.add(salon)
        db.commit()
        db.refresh(salon)

    if salon.status != "active":
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/pending"})

    return salon


# ==============================================================================
# 3.5 BOOKING / DOUBLE-BOOKING GUARD HELPERS
# ==============================================================================

BLOCKING_STATUSES = ("Confirmed", "Completed")


def parse_appt_datetime(fallback_date: datetime.date, time_str: str) -> datetime.datetime:
    if not time_str:
        return datetime.datetime.combine(fallback_date, datetime.time(0, 0))

    if "T" in time_str:
        try:
            return datetime.datetime.strptime(time_str, "%Y-%m-%dT%H:%M")
        except ValueError:
            pass

    time_part = time_str.split("T")[-1]
    try:
        t = datetime.datetime.strptime(time_part, "%H:%M").time()
    except ValueError:
        t = datetime.time(0, 0)
    return datetime.datetime.combine(fallback_date, t)


def find_conflict(
    db: Session,
    salon_id: int,
    staff_id: Optional[int],
    appt_date: datetime.date,
    start_dt: datetime.datetime,
    duration_minutes: int,
    service_map: dict,
    exclude_appointment_id: Optional[int] = None,
) -> Optional[Appointment]:
    if not staff_id:
        return None

    end_dt = start_dt + datetime.timedelta(minutes=duration_minutes)

    same_day = db.exec(
        select(Appointment)
        .where(Appointment.salon_id == salon_id)
        .where(Appointment.staff_id == staff_id)
        .where(Appointment.appointment_date == appt_date)
    ).all()

    for appt in same_day:
        if exclude_appointment_id and appt.id == exclude_appointment_id:
            continue
        if appt.status not in BLOCKING_STATUSES:
            continue

        existing_start = parse_appt_datetime(appt_date, appt.appointment_time)
        existing_service = service_map.get(appt.service_id)
        existing_duration = existing_service.duration_minutes if existing_service else 60
        existing_end = existing_start + datetime.timedelta(minutes=existing_duration)

        if start_dt < existing_end and existing_start < end_dt:
            return appt

    return None


# ==============================================================================
# 4. AUTHENTICATION & STATUS ROUTES
# ==============================================================================

@app.get("/", response_class=HTMLResponse)
def root(request: Request):
    if get_current_salon_id(request):
        return RedirectResponse(url="/dashboard", status_code=status.HTTP_303_SEE_OTHER)
    return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/login", response_class=HTMLResponse)
def get_login(request: Request):
    if get_current_salon_id(request):
        return RedirectResponse(url="/dashboard", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(request=request, name="auth.html", context={"mode": "login"})


@app.post("/login")
def post_login(
    request: Request,
    phone: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_session),
):
    salon = db.exec(select(Salon).where(Salon.phone == phone)).first()

    if salon is None or not verify_password(password, salon.password_hash):
        return templates.TemplateResponse(
            request=request,
            name="auth.html",
            context={
                "mode": "login",
                "error": "የስልክ ቁጥር ወይም የይለፍ ቃል ተሳስቷል (Invalid phone or password)",
            },
        )

    token = create_access_token(salon.id)
    response = RedirectResponse(url="/dashboard", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        key="access_token",
        value=f"Bearer {token}",
        httponly=True,
        samesite="lax",
    )
    return response


@app.get("/signup", response_class=HTMLResponse)
def get_signup(request: Request):
    if get_current_salon_id(request):
        return RedirectResponse(url="/dashboard", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(request=request, name="auth.html", context={"mode": "signup"})


@app.post("/signup")
def post_signup(
    request: Request,
    salon_name: str = Form(...),
    owner_name: str = Form(...),
    phone: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_session),
):
    existing = db.exec(select(Salon).where(Salon.phone == phone)).first()
    if existing:
        return templates.TemplateResponse(
            request=request,
            name="auth.html",
            context={
                "mode": "signup",
                "error": "ይህ ስልክ ቁጥር ቀደም ሲል ተመዝግቧል (Phone number already registered)",
            },
        )

    try:
        new_salon = Salon(
            name=salon_name,
            owner_name=owner_name,
            phone=phone,
            password_hash=hash_password(password),
            status="pending",
        )
        db.add(new_salon)
        db.commit()
        db.refresh(new_salon)

        default_service = Service(
            salon_id=new_salon.id,
            name="Hair Styling / የፀጉር ስራ",
            price=500.0,
            duration_minutes=60,
        )
        default_staff = Staff(salon_id=new_salon.id, name="General Staff / ሰራተኛ")
        db.add(default_service)
        db.add(default_staff)
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(status_code=500, detail="Error establishing salon profile")

    token = create_access_token(new_salon.id)
    response = RedirectResponse(url="/pending", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        key="access_token",
        value=f"Bearer {token}",
        httponly=True,
        samesite="lax",
    )
    return response


@app.get("/pending", response_class=HTMLResponse)
def pending_page(request: Request, db: Session = Depends(get_session)):
    salon_id = get_current_salon_id(request)
    if not salon_id:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    salon = db.get(Salon, salon_id)
    if not salon:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    if salon.status == "active":
        return RedirectResponse(url="/dashboard", status_code=status.HTTP_303_SEE_OTHER)

    return templates.TemplateResponse(request=request, name="pending.html", context={"salon": salon})


@app.get("/logout")
def logout():
    response = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(key="access_token")
    return response


# ==============================================================================
# 5. DASHBOARD & WEBSOCKET ROUTES
# ==============================================================================

@app.websocket("/ws/dashboard")
async def websocket_dashboard(websocket: WebSocket, db: Session = Depends(get_session)):
    salon_id = get_current_salon_id(websocket)
    if not salon_id:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await manager.connect(salon_id, websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(salon_id, websocket)


@app.get("/dashboard", response_class=HTMLResponse)
def get_dashboard(
    request: Request,
    tab: str = "home",
    selected_date: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    error: Optional[str] = None,
    db: Session = Depends(get_session),
    salon: Salon = Depends(get_active_salon),
):
    salon_id = salon.id
    today = datetime.date.today()

    target_date = today
    if selected_date:
        try:
            target_date = datetime.datetime.strptime(selected_date, "%Y-%m-%d").date()
        except ValueError:
            target_date = today

    start_of_week = today - datetime.timedelta(days=today.weekday())
    start_of_month = today.replace(day=1)

    services = db.exec(select(Service).where(Service.salon_id == salon_id)).all()
    staff_members = db.exec(select(Staff).where(Staff.salon_id == salon_id)).all()

    service_map = {s.id: s for s in services}
    staff_map = {st.id: st for st in staff_members}

    schedule_appts = db.exec(
        select(Appointment)
        .where(Appointment.salon_id == salon_id)
        .where(Appointment.appointment_date == target_date)
    ).all()

    formatted_appts = []
    for appt in schedule_appts:
        srv = service_map.get(appt.service_id) if appt.service_id else None
        stf = staff_map.get(appt.staff_id) if appt.staff_id else None
        formatted_appts.append({
            "id": appt.id,
            "appointment_time": appt.appointment_time,
            "customer_name": appt.customer_name,
            "customer_phone": appt.customer_phone,
            "service_name": srv.name if srv else "N/A",
            "service_price": srv.price if srv else 0.0,
            "staff_name": stf.name if stf else "N/A",
            "status": appt.status,
        })

    completed_appts = db.exec(
        select(Appointment)
        .where(Appointment.salon_id == salon_id)
        .where(Appointment.status == "Completed")
    ).all()

    daily_rev = sum(
        service_map[a.service_id].price
        for a in completed_appts
        if a.service_id and a.service_id in service_map and a.appointment_date == today
    )
    weekly_rev = sum(
        service_map[a.service_id].price
        for a in completed_appts
        if a.service_id and a.service_id in service_map and a.appointment_date >= start_of_week
    )
    monthly_rev = sum(
        service_map[a.service_id].price
        for a in completed_appts
        if a.service_id and a.service_id in service_map and a.appointment_date >= start_of_month
    )

    custom_rev = None
    if start_date and end_date:
        try:
            s_date = datetime.datetime.strptime(start_date, "%Y-%m-%d").date()
            e_date = datetime.datetime.strptime(end_date, "%Y-%m-%d").date()
            custom_rev = sum(
                service_map[a.service_id].price
                for a in completed_appts
                if a.service_id and a.service_id in service_map and s_date <= a.appointment_date <= e_date
            )
        except ValueError:
            custom_rev = 0.0

    all_appts = db.exec(select(Appointment).where(Appointment.salon_id == salon_id)).all()
    unique_customers = len(set(a.customer_phone for a in all_appts))
    no_show_count_today = len([a for a in schedule_appts if a.status == "No-Show"])

    waitlist_rows = db.exec(
        select(Waitlist)
        .where(Waitlist.salon_id == salon_id)
        .where(Waitlist.status == "Waiting")
        .order_by(Waitlist.preferred_date)
    ).all()

    formatted_waitlist = []
    for w in waitlist_rows:
        srv = service_map.get(w.service_id) if w.service_id else None
        stf = staff_map.get(w.staff_id) if w.staff_id else None
        formatted_waitlist.append({
            "id": w.id,
            "customer_name": w.customer_name,
            "customer_phone": w.customer_phone,
            "service_name": srv.name if srv else "N/A",
            "staff_name": stf.name if stf else "ማንኛውም (Any)",
            "preferred_date": w.preferred_date.strftime("%Y-%m-%d"),
            "note": w.note,
        })

    forwarded_scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("host", request.url.netloc)
    booking_url = f"{forwarded_scheme}://{host}/book/{salon.id}"

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "salon": salon,
            "booking_url": booking_url,
            "active_tab": tab,
            "daily_rev": daily_rev,
            "weekly_rev": weekly_rev,
            "monthly_rev": monthly_rev,
            "today_appt_count": len(schedule_appts),
            "total_customers": unique_customers,
            "no_show_count_today": no_show_count_today,
            "waitlist_count": len(formatted_waitlist),
            "appointments": formatted_appts,
            "services": services,
            "staff_members": staff_members,
            "waitlist_entries": formatted_waitlist,
            "selected_date": target_date.strftime("%Y-%m-%d"),
            "current_date": today.strftime("%Y-%m-%d"),
            "start_date": start_date or "",
            "end_date": end_date or "",
            "custom_rev": custom_rev,
            "error": error,
        },
    )


# ==============================================================================
# 6. ACTION ROUTES
# ==============================================================================

@app.post("/book-appointment")
def book_appointment(
    request: Request,
    customer_name: str = Form(...),
    customer_phone: str = Form(...),
    service_id: int = Form(...),
    staff_id: int = Form(...),
    appointment_time: str = Form(...),
    db: Session = Depends(get_session),
    salon: Salon = Depends(get_active_salon),
):
    appt_date = datetime.date.today()
    if "T" in appointment_time:
        try:
            date_part = appointment_time.split("T")[0]
            appt_date = datetime.datetime.strptime(date_part, "%Y-%m-%d").date()
        except ValueError:
            pass

    services = db.exec(select(Service).where(Service.salon_id == salon.id)).all()
    service_map = {s.id: s for s in services}
    service = service_map.get(service_id)
    duration = service.duration_minutes if service else 60

    start_dt = parse_appt_datetime(appt_date, appointment_time)
    conflict = find_conflict(db, salon.id, staff_id, appt_date, start_dt, duration, service_map)
    if conflict:
        return RedirectResponse(
            url=f"/dashboard?tab=home&selected_date={appt_date.strftime('%Y-%m-%d')}&error=conflict",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    new_appt = Appointment(
        salon_id=salon.id,
        customer_name=customer_name,
        customer_phone=customer_phone,
        service_id=service_id,
        staff_id=staff_id,
        appointment_time=appointment_time,
        appointment_date=appt_date,
        status="Confirmed",
    )
    db.add(new_appt)
    db.commit()

    return RedirectResponse(url="/dashboard?tab=home", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/update-appointment-status")
def update_status(
    request: Request,
    appointment_id: int = Form(...),
    status_val: str = Form(..., alias="status"),
    db: Session = Depends(get_session),
    salon: Salon = Depends(get_active_salon),
):
    appt = db.get(Appointment, appointment_id)
    accept = request.headers.get("accept", "")
    is_ajax = "application/json" in accept or request.headers.get("x-requested-with") == "XMLHttpRequest"

    if not appt or appt.salon_id != salon.id:
        if is_ajax:
            return JSONResponse({"success": False, "error": "Not found"}, status_code=404)
        return RedirectResponse(url="/dashboard?tab=home", status_code=status.HTTP_303_SEE_OTHER)

    appt.status = status_val
    db.add(appt)
    db.commit()

    if is_ajax:
        return JSONResponse({"success": True, "status": status_val, "id": appt.id})

    return RedirectResponse(url="/dashboard?tab=home", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/add-service")
def add_service(
    request: Request,
    name: str = Form(...),
    price: float = Form(...),
    duration_minutes: int = Form(60),
    db: Session = Depends(get_session),
    salon: Salon = Depends(get_active_salon),
):
    new_service = Service(
        salon_id=salon.id,
        name=name,
        price=price,
        duration_minutes=duration_minutes,
    )
    db.add(new_service)
    db.commit()
    return RedirectResponse(url="/dashboard?tab=services", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/delete-service")
def delete_service(
    request: Request,
    service_id: int = Form(...),
    db: Session = Depends(get_session),
    salon: Salon = Depends(get_active_salon),
):
    srv = db.get(Service, service_id)
    if srv and srv.salon_id == salon.id:
        linked_appts = db.exec(select(Appointment).where(Appointment.service_id == service_id)).all()
        for appt in linked_appts:
            appt.service_id = None
            db.add(appt)

        db.delete(srv)
        db.commit()

    return RedirectResponse(url="/dashboard?tab=services", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/add-staff")
def add_staff(
    request: Request,
    name: str = Form(...),
    db: Session = Depends(get_session),
    salon: Salon = Depends(get_active_salon),
):
    new_staff = Staff(salon_id=salon.id, name=name)
    db.add(new_staff)
    db.commit()
    return RedirectResponse(url="/dashboard?tab=staff", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/delete-staff")
def delete_staff(
    request: Request,
    staff_id: int = Form(...),
    db: Session = Depends(get_session),
    salon: Salon = Depends(get_active_salon),
):
    stf = db.get(Staff, staff_id)
    if stf and stf.salon_id == salon.id:
        linked_appts = db.exec(select(Appointment).where(Appointment.staff_id == staff_id)).all()
        for appt in linked_appts:
            appt.staff_id = None
            db.add(appt)

        db.delete(stf)
        db.commit()

    return RedirectResponse(url="/dashboard?tab=staff", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/add-waitlist")
def add_waitlist(
    request: Request,
    customer_name: str = Form(...),
    customer_phone: str = Form(...),
    service_id: int = Form(...),
    staff_id: Optional[str] = Form(None),
    preferred_date: str = Form(...),
    note: Optional[str] = Form(None),
    db: Session = Depends(get_session),
    salon: Salon = Depends(get_active_salon),
):
    staff_id_val = int(staff_id) if staff_id else None
    try:
        pref_date = datetime.datetime.strptime(preferred_date, "%Y-%m-%d").date()
    except ValueError:
        pref_date = datetime.date.today()

    entry = Waitlist(
        salon_id=salon.id,
        customer_name=customer_name,
        customer_phone=customer_phone,
        service_id=service_id,
        staff_id=staff_id_val,
        preferred_date=pref_date,
        note=note,
        status="Waiting",
    )
    db.add(entry)
    db.commit()

    return RedirectResponse(url="/dashboard?tab=reserve", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/convert-waitlist/{waitlist_id}")
def convert_waitlist(
    waitlist_id: int,
    appointment_time: str = Form(...),
    db: Session = Depends(get_session),
    salon: Salon = Depends(get_active_salon),
):
    entry = db.get(Waitlist, waitlist_id)
    if not entry or entry.salon_id != salon.id:
        raise HTTPException(status_code=404, detail="Waitlist entry not found")

    appt_date = entry.preferred_date
    if "T" in appointment_time:
        try:
            appt_date = datetime.datetime.strptime(appointment_time.split("T")[0], "%Y-%m-%d").date()
        except ValueError:
            pass

    services = db.exec(select(Service).where(Service.salon_id == salon.id)).all()
    service_map = {s.id: s for s in services}
    service = service_map.get(entry.service_id)
    duration = service.duration_minutes if service else 60

    if entry.staff_id:
        start_dt = parse_appt_datetime(appt_date, appointment_time)
        conflict = find_conflict(db, salon.id, entry.staff_id, appt_date, start_dt, duration, service_map)
        if conflict:
            return RedirectResponse(
                url="/dashboard?tab=reserve&error=conflict",
                status_code=status.HTTP_303_SEE_OTHER,
            )

    new_appt = Appointment(
        salon_id=salon.id,
        customer_name=entry.customer_name,
        customer_phone=entry.customer_phone,
        service_id=entry.service_id,
        staff_id=entry.staff_id,
        appointment_time=appointment_time,
        appointment_date=appt_date,
        status="Confirmed",
    )
    db.add(new_appt)

    entry.status = "Converted"
    db.add(entry)
    db.commit()

    return RedirectResponse(
        url=f"/dashboard?tab=home&selected_date={appt_date.strftime('%Y-%m-%d')}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post("/delete-waitlist")
def delete_waitlist(
    waitlist_id: int = Form(...),
    db: Session = Depends(get_session),
    salon: Salon = Depends(get_active_salon),
):
    entry = db.get(Waitlist, waitlist_id)
    if entry and entry.salon_id == salon.id:
        db.delete(entry)
        db.commit()

    return RedirectResponse(url="/dashboard?tab=reserve", status_code=status.HTTP_303_SEE_OTHER)


# ==============================================================================
# 7. SUPER ADMIN PORTAL
# ==============================================================================

@app.get("/admin/login", response_class=HTMLResponse)
def get_admin_login(request: Request):
    return templates.TemplateResponse(request=request, name="admin_login.html")


@app.post("/admin/login")
def post_admin_login(request: Request, password: str = Form(...)):
    if not hmac.compare_digest(password, ADMIN_SECRET_KEY):
        return templates.TemplateResponse(
            request=request,
            name="admin_login.html",
            context={"error": "የተሳሳተ የይለፍ ቃል (Invalid Admin Password)"},
        )

    response = RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(key="admin_auth", value=ADMIN_SECRET_KEY, httponly=True, samesite="lax")
    return response


@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(request: Request, db: Session = Depends(get_session)):
    admin_cookie = request.cookies.get("admin_auth")
    if not admin_cookie or not hmac.compare_digest(admin_cookie, ADMIN_SECRET_KEY):
        return RedirectResponse(url="/admin/login", status_code=status.HTTP_303_SEE_OTHER)

    salons = db.exec(select(Salon)).all()
    return templates.TemplateResponse(request=request, name="admin.html", context={"salons": salons})


@app.post("/admin/approve/{salon_id}")
def approve_salon_admin(
    request: Request,
    salon_id: int,
    days: int = Form(30),
    db: Session = Depends(get_session),
):
    admin_cookie = request.cookies.get("admin_auth")
    if not admin_cookie or not hmac.compare_digest(admin_cookie, ADMIN_SECRET_KEY):
        raise HTTPException(status_code=401, detail="Unauthorized")

    salon = db.get(Salon, salon_id)
    if salon:
        salon.status = "active"
        salon.subscription_expires_at = datetime.date.today() + datetime.timedelta(days=days)
        db.add(salon)
        db.commit()

    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)