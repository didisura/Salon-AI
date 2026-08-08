from datetime import datetime, date, timedelta
from typing import Optional

from fastapi import (
    FastAPI, Request, Depends, Form, WebSocket, WebSocketDisconnect, status
)
from fastapi.responses import RedirectResponse, JSONResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session

from database import Base, engine, get_db
from models import Salon, Service, Staff, Appointment, Waitlist, AppointmentStatus
from security import (
    hash_password,
    verify_password,
    create_access_token,
    get_current_salon,
    NotAuthenticatedException,
)

Base.metadata.create_all(bind=engine)

app = FastAPI(title="Melkegna Salon Platform")
templates = Jinja2Templates(directory="templates")


@app.exception_handler(NotAuthenticatedException)
async def not_authenticated_handler(request: Request, exc: NotAuthenticatedException):
    return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)


# ---------------------------------------------------------------------------
# Live WebSocket updates (one connection pool per salon)
# ---------------------------------------------------------------------------
class ConnectionManager:
    def __init__(self):
        self.active: dict[int, list[WebSocket]] = {}

    async def connect(self, salon_id: int, ws: WebSocket):
        await ws.accept()
        self.active.setdefault(salon_id, []).append(ws)

    def disconnect(self, salon_id: int, ws: WebSocket):
        if ws in self.active.get(salon_id, []):
            self.active[salon_id].remove(ws)

    async def broadcast(self, salon_id: int, payload: dict):
        for ws in list(self.active.get(salon_id, [])):
            try:
                await ws.send_json(payload)
            except Exception:
                self.disconnect(salon_id, ws)


manager = ConnectionManager()


@app.websocket("/ws/salon/{salon_id}")
async def ws_salon(websocket: WebSocket, salon_id: int):
    await manager.connect(salon_id, websocket)
    try:
        while True:
            await websocket.receive_text()  # keep-alive; frontend doesn't send data
    except WebSocketDisconnect:
        manager.disconnect(salon_id, websocket)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _parse_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def _revenue_between(db: Session, salon_id: int, start_dt: datetime, end_dt: datetime) -> float:
    total = (
        db.query(func.coalesce(func.sum(Service.price), 0))
        .join(Appointment, Appointment.service_id == Service.id)
        .filter(
            Appointment.salon_id == salon_id,
            Appointment.status == AppointmentStatus.completed,
            Appointment.appointment_datetime >= start_dt,
            Appointment.appointment_datetime < end_dt,
        )
        .scalar()
    )
    return float(total or 0)


def _has_conflict(db: Session, salon_id: int, staff_id: int, appt_dt: datetime) -> bool:
    return (
        db.query(Appointment.id)
        .filter(
            Appointment.salon_id == salon_id,
            Appointment.staff_id == staff_id,
            Appointment.appointment_datetime == appt_dt,
            Appointment.status.notin_([AppointmentStatus.cancelled, AppointmentStatus.no_show]),
        )
        .first()
        is not None
    )


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/login")


@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request, error: Optional[str] = None):
    return templates.TemplateResponse("register.html", {"request": request, "error": error})


@app.post("/register")
def register_salon(
    name: str = Form(...),
    owner_name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    if db.query(Salon.id).filter(Salon.email == email).first():
        return RedirectResponse(url="/register?error=exists", status_code=status.HTTP_303_SEE_OTHER)

    salon = Salon(
        name=name,
        owner_name=owner_name,
        email=email,
        hashed_password=hash_password(password),
    )
    db.add(salon)
    db.commit()

    return RedirectResponse(url="/login?registered=1", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, error: Optional[str] = None, registered: Optional[str] = None):
    return templates.TemplateResponse(
        "login.html", {"request": request, "error": error, "registered": registered}
    )


@app.post("/login")
def login(
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    salon = db.query(Salon).filter(Salon.email == email).first()
    if not salon or not verify_password(password, salon.hashed_password):
        return RedirectResponse(url="/login?error=invalid", status_code=status.HTTP_303_SEE_OTHER)

    token = create_access_token({"sub": str(salon.id)})
    redirect = RedirectResponse(url="/dashboard?tab=home", status_code=status.HTTP_303_SEE_OTHER)
    redirect.set_cookie(
        key="access_token",
        value=token,
        httponly=True,
        samesite="lax",
        max_age=60 * 60,
        # secure=True,  # turn on once you're serving over HTTPS
    )
    return redirect


@app.get("/logout")
def logout():
    redirect = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    redirect.delete_cookie("access_token")
    return redirect


# ---------------------------------------------------------------------------
# Dashboard (all 5 tabs live behind this one route, like the template expects)
# ---------------------------------------------------------------------------
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(
    request: Request,
    tab: str = "home",
    selected_date: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    error: Optional[str] = None,
    salon: Salon = Depends(get_current_salon),
    db: Session = Depends(get_db),
):
    today = date.today()
    current_date = today.isoformat()
    day_start = datetime.combine(today, datetime.min.time())
    day_end = day_start + timedelta(days=1)

    services = db.query(Service).filter(Service.salon_id == salon.id).order_by(Service.name).all()
    staff_members = db.query(Staff).filter(Staff.salon_id == salon.id).order_by(Staff.name).all()

    daily_rev = _revenue_between(db, salon.id, day_start, day_end)

    today_appt_count = (
        db.query(func.count(Appointment.id))
        .filter(
            Appointment.salon_id == salon.id,
            Appointment.appointment_datetime >= day_start,
            Appointment.appointment_datetime < day_end,
        )
        .scalar()
        or 0
    )

    total_customers = (
        db.query(func.count(func.distinct(Appointment.customer_phone)))
        .filter(Appointment.salon_id == salon.id)
        .scalar()
        or 0
    )

    no_show_count_today = (
        db.query(func.count(Appointment.id))
        .filter(
            Appointment.salon_id == salon.id,
            Appointment.status == AppointmentStatus.no_show,
            Appointment.appointment_datetime >= day_start,
            Appointment.appointment_datetime < day_end,
        )
        .scalar()
        or 0
    )

    context = {
        "request": request,
        "salon": salon,
        "active_tab": tab,
        "current_date": current_date,
        "services": services,
        "staff_members": staff_members,
        "daily_rev": daily_rev,
        "today_appt_count": today_appt_count,
        "total_customers": total_customers,
        "no_show_count_today": no_show_count_today,
        "error": error,
        "booking_url": str(request.base_url).rstrip("/") + f"/book/{salon.id}",
    }

    if tab == "home":
        sel_date = _parse_date(selected_date) or today
        d_start = datetime.combine(sel_date, datetime.min.time())
        d_end = d_start + timedelta(days=1)
        appointments = (
            db.query(Appointment)
            .filter(
                Appointment.salon_id == salon.id,
                Appointment.appointment_datetime >= d_start,
                Appointment.appointment_datetime < d_end,
            )
            .order_by(Appointment.appointment_datetime)
            .all()
        )
        context["appointments"] = appointments
        context["selected_date"] = sel_date.isoformat()

    elif tab == "reserve":
        context["waitlist_entries"] = (
            db.query(Waitlist)
            .filter(Waitlist.salon_id == salon.id)
            .order_by(Waitlist.preferred_date)
            .all()
        )

    elif tab == "revenue":
        week_start = day_start - timedelta(days=today.weekday())
        month_start = day_start.replace(day=1)

        context["weekly_rev"] = _revenue_between(db, salon.id, week_start, day_end)
        context["monthly_rev"] = _revenue_between(db, salon.id, month_start, day_end)
        context["start_date"] = start_date or current_date
        context["end_date"] = end_date or current_date

        custom_rev = None
        s, e = _parse_date(start_date), _parse_date(end_date)
        if s and e:
            custom_rev = _revenue_between(
                db, salon.id,
                datetime.combine(s, datetime.min.time()),
                datetime.combine(e, datetime.min.time()) + timedelta(days=1),
            )
        context["custom_rev"] = custom_rev

    return templates.TemplateResponse("dashboard.html", context)


# ---------------------------------------------------------------------------
# Appointments (admin / walk-in)
# ---------------------------------------------------------------------------
@app.post("/book-appointment")
def book_appointment(
    customer_name: str = Form(...),
    customer_phone: str = Form(...),
    service_id: int = Form(...),
    staff_id: int = Form(...),
    appointment_time: str = Form(...),
    salon: Salon = Depends(get_current_salon),
    db: Session = Depends(get_db),
):
    appt_dt = datetime.strptime(appointment_time, "%Y-%m-%dT%H:%M")

    if _has_conflict(db, salon.id, staff_id, appt_dt):
        return RedirectResponse(url="/dashboard?tab=home&error=conflict", status_code=status.HTTP_303_SEE_OTHER)

    db.add(Appointment(
        salon_id=salon.id,
        customer_name=customer_name,
        customer_phone=customer_phone,
        service_id=service_id,
        staff_id=staff_id,
        appointment_datetime=appt_dt,
        status=AppointmentStatus.confirmed,
        source="walk-in",
    ))
    db.commit()

    return RedirectResponse(
        url=f"/dashboard?tab=home&selected_date={appt_dt.date().isoformat()}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post("/update-appointment-status")
async def update_appointment_status(
    request: Request,
    appointment_id: int = Form(...),
    status_value: str = Form(..., alias="status"),
    salon: Salon = Depends(get_current_salon),
    db: Session = Depends(get_db),
):
    is_ajax = request.headers.get("x-requested-with") == "XMLHttpRequest"

    appt = (
        db.query(Appointment)
        .filter(Appointment.id == appointment_id, Appointment.salon_id == salon.id)
        .first()
    )
    if not appt:
        if is_ajax:
            return JSONResponse({"success": False, "error": "not_found"}, status_code=404)
        return RedirectResponse(url="/dashboard?tab=home", status_code=status.HTTP_303_SEE_OTHER)

    try:
        appt.status = AppointmentStatus(status_value)
    except ValueError:
        if is_ajax:
            return JSONResponse({"success": False, "error": "invalid_status"}, status_code=400)
        return RedirectResponse(url="/dashboard?tab=home", status_code=status.HTTP_303_SEE_OTHER)

    db.commit()

    if is_ajax:
        return JSONResponse({"success": True, "status": appt.status.value})
    return RedirectResponse(url="/dashboard?tab=home", status_code=status.HTTP_303_SEE_OTHER)


# ---------------------------------------------------------------------------
# Waitlist
# ---------------------------------------------------------------------------
@app.post("/add-waitlist")
def add_waitlist(
    customer_name: str = Form(...),
    customer_phone: str = Form(...),
    service_id: int = Form(...),
    staff_id: Optional[int] = Form(None),
    preferred_date: str = Form(...),
    salon: Salon = Depends(get_current_salon),
    db: Session = Depends(get_db),
):
    db.add(Waitlist(
        salon_id=salon.id,
        customer_name=customer_name,
        customer_phone=customer_phone,
        service_id=service_id,
        staff_id=staff_id or None,
        preferred_date=_parse_date(preferred_date) or date.today(),
    ))
    db.commit()
    return RedirectResponse(url="/dashboard?tab=reserve", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/delete-waitlist")
def delete_waitlist(
    waitlist_id: int = Form(...),
    salon: Salon = Depends(get_current_salon),
    db: Session = Depends(get_db),
):
    db.query(Waitlist).filter(Waitlist.id == waitlist_id, Waitlist.salon_id == salon.id).delete()
    db.commit()
    return RedirectResponse(url="/dashboard?tab=reserve", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/convert-waitlist/{waitlist_id}")
def convert_waitlist(
    waitlist_id: int,
    appointment_time: str = Form(...),
    salon: Salon = Depends(get_current_salon),
    db: Session = Depends(get_db),
):
    entry = (
        db.query(Waitlist)
        .filter(Waitlist.id == waitlist_id, Waitlist.salon_id == salon.id)
        .first()
    )
    if not entry:
        return RedirectResponse(url="/dashboard?tab=reserve", status_code=status.HTTP_303_SEE_OTHER)

    appt_dt = datetime.strptime(appointment_time, "%Y-%m-%dT%H:%M")
    staff_id = entry.staff_id or db.query(Staff.id).filter(Staff.salon_id == salon.id).scalar()

    if staff_id and _has_conflict(db, salon.id, staff_id, appt_dt):
        return RedirectResponse(url="/dashboard?tab=reserve&error=conflict", status_code=status.HTTP_303_SEE_OTHER)

    db.add(Appointment(
        salon_id=salon.id,
        customer_name=entry.customer_name,
        customer_phone=entry.customer_phone,
        service_id=entry.service_id,
        staff_id=staff_id,
        appointment_datetime=appt_dt,
        status=AppointmentStatus.confirmed,
        source="walk-in",
    ))
    db.delete(entry)
    db.commit()

    return RedirectResponse(url="/dashboard?tab=reserve", status_code=status.HTTP_303_SEE_OTHER)


# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------
@app.post("/add-service")
def add_service(
    name: str = Form(...),
    price: float = Form(...),
    duration_minutes: int = Form(...),
    salon: Salon = Depends(get_current_salon),
    db: Session = Depends(get_db),
):
    db.add(Service(salon_id=salon.id, name=name, price=price, duration_minutes=duration_minutes))
    db.commit()
    return RedirectResponse(url="/dashboard?tab=services", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/delete-service")
def delete_service(
    service_id: int = Form(...),
    salon: Salon = Depends(get_current_salon),
    db: Session = Depends(get_db),
):
    db.query(Service).filter(Service.id == service_id, Service.salon_id == salon.id).delete()
    db.commit()
    return RedirectResponse(url="/dashboard?tab=services", status_code=status.HTTP_303_SEE_OTHER)


# ---------------------------------------------------------------------------
# Staff
# ---------------------------------------------------------------------------
@app.post("/add-staff")
def add_staff(
    name: str = Form(...),
    salon: Salon = Depends(get_current_salon),
    db: Session = Depends(get_db),
):
    db.add(Staff(salon_id=salon.id, name=name))
    db.commit()
    return RedirectResponse(url="/dashboard?tab=staff", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/delete-staff")
def delete_staff(
    staff_id: int = Form(...),
    salon: Salon = Depends(get_current_salon),
    db: Session = Depends(get_db),
):
    db.query(Staff).filter(Staff.id == staff_id, Staff.salon_id == salon.id).delete()
    db.commit()
    return RedirectResponse(url="/dashboard?tab=staff", status_code=status.HTTP_303_SEE_OTHER)


# ---------------------------------------------------------------------------
# Public customer-facing booking page (the {{ booking_url }} link)
# ---------------------------------------------------------------------------
@app.get("/book/{salon_id}", response_class=HTMLResponse)
def public_booking_page(
    request: Request,
    salon_id: int,
    error: Optional[str] = None,
    success: Optional[str] = None,
    db: Session = Depends(get_db),
):
    salon = db.query(Salon).filter(Salon.id == salon_id).first()
    if not salon:
        return HTMLResponse("Salon not found", status_code=404)

    services = db.query(Service).filter(Service.salon_id == salon.id).order_by(Service.name).all()
    staff_members = db.query(Staff).filter(Staff.salon_id == salon.id).order_by(Staff.name).all()

    return templates.TemplateResponse(
        "public_booking.html",
        {
            "request": request,
            "salon": salon,
            "services": services,
            "staff_members": staff_members,
            "current_date": date.today().isoformat(),
            "error": error,
            "success": success,
        },
    )


@app.post("/book/{salon_id}")
async def public_booking_submit(
    salon_id: int,
    customer_name: str = Form(...),
    customer_phone: str = Form(...),
    service_id: int = Form(...),
    staff_id: int = Form(...),
    appointment_time: str = Form(...),
    db: Session = Depends(get_db),
):
    salon = db.query(Salon).filter(Salon.id == salon_id).first()
    if not salon:
        return HTMLResponse("Salon not found", status_code=404)

    appt_dt = datetime.strptime(appointment_time, "%Y-%m-%dT%H:%M")

    if _has_conflict(db, salon.id, staff_id, appt_dt):
        return RedirectResponse(url=f"/book/{salon_id}?error=conflict", status_code=status.HTTP_303_SEE_OTHER)

    appt = Appointment(
        salon_id=salon.id,
        customer_name=customer_name,
        customer_phone=customer_phone,
        service_id=service_id,
        staff_id=staff_id,
        appointment_datetime=appt_dt,
        status=AppointmentStatus.confirmed,
        source="online",
    )
    db.add(appt)
    db.commit()
    db.refresh(appt)

    # Push it live onto the owner's dashboard (Home tab) via websocket
    await manager.broadcast(salon.id, {
        "event": "new_booking",
        "appointment": {
            "id": appt.id,
            "appointment_time": appt.appointment_time,
            "customer_name": appt.customer_name,
            "customer_phone": appt.customer_phone,
            "service_name": appt.service_name,
            "service_price": appt.service_price,
            "staff_name": appt.staff_name,
            "status": appt.status.value,
        },
    })

    return RedirectResponse(url=f"/book/{salon_id}?success=1", status_code=status.HTTP_303_SEE_OTHER)