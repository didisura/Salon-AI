import os
from datetime import datetime, date, timedelta
from typing import List, Optional
from urllib.parse import urlencode

from fastapi import (
    FastAPI, Request, Depends, Form, WebSocket, WebSocketDisconnect, status
)
from fastapi.responses import RedirectResponse, JSONResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database import Base, engine, get_db
from models import Salon, Service, Staff, StaffDayOff, Appointment, Waitlist, AppointmentStatus
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

ADMIN_SECRET_KEY = os.environ.get("ADMIN_SECRET_KEY", "change-me-set-ADMIN_SECRET_KEY-in-railway")
SLOT_STEP_MINUTES = 15


def _valid_admin_key(key: Optional[str]) -> bool:
    return bool(key) and key == ADMIN_SECRET_KEY


def _eth_display(dt: Optional[datetime]) -> Optional[str]:
    """Format a datetime in Ethiopian time, e.g. 'ጧት 3:15 ሰዓት'."""
    if not dt:
        return None
    total = (dt.hour * 60 + dt.minute - 360) % 1440
    eh, em = total // 60, total % 60
    if eh < 6:
        p, h = "ጧት", 12 if eh == 0 else eh
    elif eh < 12:
        p, h = "ቀን", eh
    elif eh < 18:
        p, h = "ማታ", 12 if eh == 12 else eh - 12
    else:
        p, h = "ለሊት", eh - 12
    return f"{p} {h}:{em:02d} ሰዓት"


@app.exception_handler(NotAuthenticatedException)
async def not_authenticated_handler(request: Request, exc: NotAuthenticatedException):
    return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)


class SalonNotActiveException(Exception):
    def __init__(self, salon: Salon):
        self.salon = salon


@app.exception_handler(SalonNotActiveException)
async def salon_not_active_handler(request: Request, exc: SalonNotActiveException):
    return templates.TemplateResponse(
        request, "pending.html", {"salon": exc.salon}, status_code=status.HTTP_403_FORBIDDEN
    )


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
            await websocket.receive_text()
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


def _parse_time_hhmm(value: str):
    try:
        return datetime.strptime(value, "%H:%M").time()
    except (ValueError, TypeError):
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


def _service_duration(db: Session, service_id: int) -> int:
    duration = db.query(Service.duration_minutes).filter(Service.id == service_id).scalar()
    return duration or 30


# ---------------------------------------------------------------------------
# Working hours / working days — salon level + per-staff overrides + day-offs
# ---------------------------------------------------------------------------
def _is_working_day(salon: Salon, d: date) -> bool:
    return d.weekday() in salon.working_days_set


def _within_business_hours(salon: Salon, appt_dt: datetime, end_dt: datetime) -> bool:
    if not _is_working_day(salon, appt_dt.date()):
        return False
    day = appt_dt.date()
    open_dt = datetime.combine(day, salon.opening_time)
    close_dt = datetime.combine(day, salon.closing_time)
    return open_dt <= appt_dt and end_dt <= close_dt


def _staff_is_off(db: Session, staff_id: int, d: date) -> bool:
    return db.query(StaffDayOff.id).filter(
        StaffDayOff.staff_id == staff_id, StaffDayOff.off_date == d
    ).first() is not None


def _within_staff_hours(db: Session, salon: Salon, staff: Staff, appt_dt: datetime, end_dt: datetime) -> bool:
    """Checks salon hours AND this staff member's own working days/hours
    AND that they aren't marked off that specific day."""
    if not _within_business_hours(salon, appt_dt, end_dt):
        return False

    day = appt_dt.date()
    if day.weekday() not in staff.effective_working_days(salon):
        return False

    if _staff_is_off(db, staff.id, day):
        return False

    open_t, close_t = staff.effective_hours(salon)
    open_dt = datetime.combine(day, open_t)
    close_dt = datetime.combine(day, close_t)
    return open_dt <= appt_dt and end_dt <= close_dt


def _staff_has_overlap(
    db: Session,
    salon_id: int,
    staff_id: int,
    start_dt: datetime,
    end_dt: datetime,
    exclude_appointment_id: Optional[int] = None,
) -> bool:
    day_start = datetime.combine(start_dt.date(), datetime.min.time())
    day_end = day_start + timedelta(days=1)

    q = (
        db.query(Appointment)
        .filter(
            Appointment.salon_id == salon_id,
            Appointment.staff_id == staff_id,
            Appointment.status.notin_([AppointmentStatus.cancelled, AppointmentStatus.no_show]),
            Appointment.appointment_datetime >= day_start,
            Appointment.appointment_datetime < day_end,
        )
    )
    if exclude_appointment_id:
        q = q.filter(Appointment.id != exclude_appointment_id)

    for existing in q.all():
        existing_start = existing.appointment_datetime
        existing_duration = existing.service.duration_minutes if existing.service else 30
        existing_end = existing_start + timedelta(minutes=existing_duration)
        if existing_start < end_dt and existing_end > start_dt:
            return True
    return False


def _available_staff_for_slot(
    db: Session,
    salon: Salon,
    start_dt: datetime,
    end_dt: datetime,
    exclude_staff_id: Optional[int] = None,
):
    """Every staff member at this salon who is working that day/hours,
    not marked off, and free for the whole [start_dt, end_dt) window."""
    staff_list = db.query(Staff).filter(Staff.salon_id == salon.id).order_by(Staff.name).all()
    return [
        st for st in staff_list
        if st.id != exclude_staff_id
        and _within_staff_hours(db, salon, st, start_dt, end_dt)
        and not _staff_has_overlap(db, salon.id, st.id, start_dt, end_dt)
    ]


def _next_available_slot(
    db: Session,
    salon: Salon,
    staff: Staff,
    duration_minutes: int,
    requested_dt: datetime,
) -> Optional[datetime]:
    """Search forward same-day, within THIS staff member's effective
    hours/days, skipping if they're off that day."""
    day = requested_dt.date()
    if day.weekday() not in staff.effective_working_days(salon):
        return None
    if _staff_is_off(db, staff.id, day):
        return None

    open_t, close_t = staff.effective_hours(salon)
    business_end = datetime.combine(day, close_t)

    slot_start = requested_dt + timedelta(minutes=SLOT_STEP_MINUTES)
    while slot_start + timedelta(minutes=duration_minutes) <= business_end:
        slot_end = slot_start + timedelta(minutes=duration_minutes)
        if not _staff_has_overlap(db, salon.id, staff.id, slot_start, slot_end):
            return slot_start
        slot_start += timedelta(minutes=SLOT_STEP_MINUTES)
    return None


def _normalize_phone(raw: str) -> str:
    raw = raw.strip()
    digits = "".join(ch for ch in raw if ch.isdigit())
    return digits


def get_active_salon(
    salon: Salon = Depends(get_current_salon),
    db: Session = Depends(get_db),
) -> Salon:
    now = datetime.utcnow()

    if salon.status == "active" and salon.subscription_expires_at and salon.subscription_expires_at < now:
        salon.status = "expired"
        db.commit()

    if salon.status != "active":
        raise SalonNotActiveException(salon)

    return salon


# ---------------------------------------------------------------------------
# Super Admin
# ---------------------------------------------------------------------------
@app.get("/admin/login", response_class=HTMLResponse)
def admin_login_page(request: Request, error: Optional[str] = None):
    return templates.TemplateResponse(request, "admin_login.html", {"error": error})


@app.post("/admin/login")
def admin_login(password: str = Form(...)):
    if not _valid_admin_key(password):
        return RedirectResponse(
            url="/admin/login?error=የተሳሳተ ቁልፍ (Invalid admin key)",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    return RedirectResponse(url=f"/admin?key={password}", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(request: Request, key: Optional[str] = None, db: Session = Depends(get_db)):
    if not _valid_admin_key(key):
        return RedirectResponse(
            url="/admin/login?error=Session expired, please log in again",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    salons = db.query(Salon).order_by(Salon.id.desc()).all()
    return templates.TemplateResponse(request, "admin.html", {"salons": salons, "admin_key": key})


@app.post("/admin/approve/{salon_id}")
def admin_approve(
    salon_id: int,
    key: str = Form(...),
    days: int = Form(...),
    db: Session = Depends(get_db),
):
    if not _valid_admin_key(key):
        return RedirectResponse(
            url="/admin/login?error=Session expired, please log in again",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    salon = db.query(Salon).filter(Salon.id == salon_id).first()
    if salon:
        now = datetime.utcnow()
        base = salon.subscription_expires_at if (salon.subscription_expires_at and salon.subscription_expires_at > now) else now
        salon.subscription_expires_at = base + timedelta(days=days)
        salon.status = "active"
        db.commit()

    return RedirectResponse(url=f"/admin?key={key}", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/admin/suspend/{salon_id}")
def admin_suspend(
    salon_id: int,
    key: str = Form(...),
    db: Session = Depends(get_db),
):
    if not _valid_admin_key(key):
        return RedirectResponse(
            url="/admin/login?error=Session expired, please log in again",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    salon = db.query(Salon).filter(Salon.id == salon_id).first()
    if salon:
        salon.status = "suspended"
        db.commit()

    return RedirectResponse(url=f"/admin?key={key}", status_code=status.HTTP_303_SEE_OTHER)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/login")


@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request, error: Optional[str] = None):
    return templates.TemplateResponse(request, "register.html", {"error": error})


@app.post("/register")
def register_salon(
    name: str = Form(...),
    owner_name: str = Form(...),
    phone: str = Form(...),
    password: str = Form(...),
    opening_time: str = Form("08:00"),
    closing_time: str = Form("20:00"),
    working_days: List[str] = Form(default=[]),
    db: Session = Depends(get_db),
):
    phone_clean = _normalize_phone(phone)
    if len(phone_clean) < 9:
        return RedirectResponse(url="/register?error=invalid_phone", status_code=status.HTTP_303_SEE_OTHER)

    if db.query(Salon.id).filter(Salon.phone == phone_clean).first():
        return RedirectResponse(url="/register?error=exists", status_code=status.HTTP_303_SEE_OTHER)

    open_t = _parse_time_hhmm(opening_time)
    close_t = _parse_time_hhmm(closing_time)
    if not open_t or not close_t or close_t <= open_t:
        return RedirectResponse(url="/register?error=invalid_hours", status_code=status.HTTP_303_SEE_OTHER)

    day_ints = sorted({int(d) for d in working_days if d.isdigit() and 0 <= int(d) <= 6})
    if not day_ints:
        return RedirectResponse(url="/register?error=invalid_days", status_code=status.HTTP_303_SEE_OTHER)

    salon = Salon(
        name=name,
        owner_name=owner_name,
        phone=phone_clean,
        hashed_password=hash_password(password),
        status="pending",
        subscription_expires_at=None,
        opening_time=open_t,
        closing_time=close_t,
        working_days=",".join(str(d) for d in day_ints),
    )
    db.add(salon)
    db.commit()

    return RedirectResponse(url="/login?registered=1", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, error: Optional[str] = None, registered: Optional[str] = None):
    return templates.TemplateResponse(
        request, "login.html", {"error": error, "registered": registered}
    )


@app.post("/login")
def login(
    phone: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    phone_clean = _normalize_phone(phone)
    salon = db.query(Salon).filter(Salon.phone == phone_clean).first()
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
    )
    return redirect


@app.get("/logout")
def logout():
    redirect = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    redirect.delete_cookie("access_token")
    return redirect


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(
    request: Request,
    tab: str = "home",
    selected_date: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    error: Optional[str] = None,
    conflict_name: Optional[str] = None,
    conflict_phone: Optional[str] = None,
    conflict_service: Optional[int] = None,
    conflict_staff: Optional[int] = None,
    conflict_time: Optional[str] = None,
    salon: Salon = Depends(get_active_salon),
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

        if error == "conflict" and conflict_time and conflict_service and conflict_staff:
            conflict_dt = datetime.strptime(conflict_time, "%Y-%m-%dT%H:%M")
            c_duration = _service_duration(db, conflict_service)
            c_end = conflict_dt + timedelta(minutes=c_duration)

            conflict_staff_obj = db.query(Staff).filter(Staff.id == conflict_staff).first()
            alt_staff = _available_staff_for_slot(db, salon, conflict_dt, c_end, exclude_staff_id=conflict_staff)
            next_slot = (
                _next_available_slot(db, salon, conflict_staff_obj, c_duration, conflict_dt)
                if conflict_staff_obj else None
            )

            context.update({
                "conflict_name": conflict_name,
                "conflict_phone": conflict_phone,
                "conflict_service": conflict_service,
                "conflict_staff": conflict_staff,
                "conflict_staff_name": conflict_staff_obj.name if conflict_staff_obj else "",
                "conflict_time": conflict_time,
                "conflict_date": conflict_dt.date().isoformat(),
                "alt_staff": alt_staff,
                "next_slot": next_slot.strftime("%Y-%m-%dT%H:%M") if next_slot else None,
                "next_slot_display": _eth_display(next_slot),
            })

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

    return templates.TemplateResponse(request, "dashboard.html", context)


# ---------------------------------------------------------------------------
# Salon-level working hours
# ---------------------------------------------------------------------------
@app.post("/update-hours")
def update_hours(
    opening_time: str = Form(...),
    closing_time: str = Form(...),
    working_days: List[str] = Form(default=[]),
    address: Optional[str] = Form(None),
    salon: Salon = Depends(get_active_salon),
    db: Session = Depends(get_db),
):
    open_t = _parse_time_hhmm(opening_time)
    close_t = _parse_time_hhmm(closing_time)
    if not open_t or not close_t or close_t <= open_t:
        return RedirectResponse(url="/dashboard?tab=home&error=invalid_hours", status_code=status.HTTP_303_SEE_OTHER)

    day_ints = sorted({int(d) for d in working_days if d.isdigit() and 0 <= int(d) <= 6})
    if not day_ints:
        return RedirectResponse(url="/dashboard?tab=home&error=invalid_days", status_code=status.HTTP_303_SEE_OTHER)

    salon.opening_time = open_t
    salon.closing_time = close_t
    salon.working_days = ",".join(str(d) for d in day_ints)
    if address is not None:
        salon.address = address.strip() or None
    db.commit()

    return RedirectResponse(url="/dashboard?tab=home", status_code=status.HTTP_303_SEE_OTHER)


# ---------------------------------------------------------------------------
# Staff-level schedule overrides + day-offs
# ---------------------------------------------------------------------------
@app.post("/update-staff-schedule")
def update_staff_schedule(
    staff_id: int = Form(...),
    use_custom_hours: Optional[str] = Form(None),
    opening_time: Optional[str] = Form(None),
    closing_time: Optional[str] = Form(None),
    use_custom_days: Optional[str] = Form(None),
    working_days: List[str] = Form(default=[]),
    salon: Salon = Depends(get_active_salon),
    db: Session = Depends(get_db),
):
    staff = db.query(Staff).filter(Staff.id == staff_id, Staff.salon_id == salon.id).first()
    if not staff:
        return RedirectResponse(url="/dashboard?tab=staff", status_code=status.HTTP_303_SEE_OTHER)

    if use_custom_hours:
        open_t = _parse_time_hhmm(opening_time)
        close_t = _parse_time_hhmm(closing_time)
        if not open_t or not close_t or close_t <= open_t:
            return RedirectResponse(url="/dashboard?tab=staff&error=invalid_hours", status_code=status.HTTP_303_SEE_OTHER)
        staff.opening_time = open_t
        staff.closing_time = close_t
    else:
        staff.opening_time = None
        staff.closing_time = None

    if use_custom_days:
        day_ints = sorted({int(d) for d in working_days if d.isdigit() and 0 <= int(d) <= 6})
        if not day_ints:
            return RedirectResponse(url="/dashboard?tab=staff&error=invalid_days", status_code=status.HTTP_303_SEE_OTHER)
        staff.working_days = ",".join(str(d) for d in day_ints)
    else:
        staff.working_days = None

    db.commit()
    return RedirectResponse(url="/dashboard?tab=staff", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/add-staff-dayoff")
def add_staff_dayoff(
    staff_id: int = Form(...),
    off_date: str = Form(...),
    salon: Salon = Depends(get_active_salon),
    db: Session = Depends(get_db),
):
    staff = db.query(Staff).filter(Staff.id == staff_id, Staff.salon_id == salon.id).first()
    d = _parse_date(off_date)
    if staff and d:
        exists = db.query(StaffDayOff.id).filter(
            StaffDayOff.staff_id == staff.id, StaffDayOff.off_date == d
        ).first()
        if not exists:
            db.add(StaffDayOff(staff_id=staff.id, off_date=d))
            db.commit()
    return RedirectResponse(url="/dashboard?tab=staff", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/delete-staff-dayoff")
def delete_staff_dayoff(
    dayoff_id: int = Form(...),
    salon: Salon = Depends(get_active_salon),
    db: Session = Depends(get_db),
):
    db.query(StaffDayOff).filter(
        StaffDayOff.id == dayoff_id,
        StaffDayOff.staff_id.in_(db.query(Staff.id).filter(Staff.salon_id == salon.id))
    ).delete(synchronize_session=False)
    db.commit()
    return RedirectResponse(url="/dashboard?tab=staff", status_code=status.HTTP_303_SEE_OTHER)


# ---------------------------------------------------------------------------
# Customer search
# ---------------------------------------------------------------------------
@app.get("/search-customer")
def search_customer(
    q: str,
    salon: Salon = Depends(get_active_salon),
    db: Session = Depends(get_db),
):
    q = q.strip()
    if not q:
        return JSONResponse({"results": []})

    q_digits = _normalize_phone(q)
    filters = [Appointment.customer_name.ilike(f"%{q}%")]
    if q_digits:
        filters.append(Appointment.customer_phone.ilike(f"%{q_digits}%"))
    else:
        filters.append(Appointment.customer_phone.ilike(f"%{q}%"))

    matches = (
        db.query(Appointment)
        .filter(Appointment.salon_id == salon.id, or_(*filters))
        .order_by(Appointment.appointment_datetime.desc())
        .limit(25)
        .all()
    )

    seen_phones = set()
    results = []
    for a in matches:
        if a.customer_phone in seen_phones:
            continue
        seen_phones.add(a.customer_phone)
        results.append({
            "customer_name": a.customer_name,
            "customer_phone": a.customer_phone,
            "service_name": a.service_name,
            "appointment_time": a.appointment_time,
            "status": a.status.value,
        })

    return JSONResponse({"results": results})


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
    salon: Salon = Depends(get_active_salon),
    db: Session = Depends(get_db),
):
    appt_dt = datetime.strptime(appointment_time, "%Y-%m-%dT%H:%M")
    duration = _service_duration(db, service_id)
    end_dt = appt_dt + timedelta(minutes=duration)

    staff_obj = db.query(Staff).filter(Staff.id == staff_id, Staff.salon_id == salon.id).first()

    if not staff_obj or not _within_staff_hours(db, salon, staff_obj, appt_dt, end_dt):
        params = urlencode({
            "tab": "home",
            "error": "outside_hours",
            "selected_date": appt_dt.date().isoformat(),
        })
        return RedirectResponse(url=f"/dashboard?{params}", status_code=status.HTTP_303_SEE_OTHER)

    if _staff_has_overlap(db, salon.id, staff_id, appt_dt, end_dt):
        params = urlencode({
            "tab": "home",
            "error": "conflict",
            "selected_date": appt_dt.date().isoformat(),
            "conflict_name": customer_name,
            "conflict_phone": customer_phone,
            "conflict_service": service_id,
            "conflict_staff": staff_id,
            "conflict_time": appointment_time,
        })
        return RedirectResponse(url=f"/dashboard?{params}", status_code=status.HTTP_303_SEE_OTHER)

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
    salon: Salon = Depends(get_active_salon),
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
    salon: Salon = Depends(get_active_salon),
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
    salon: Salon = Depends(get_active_salon),
    db: Session = Depends(get_db),
):
    db.query(Waitlist).filter(Waitlist.id == waitlist_id, Waitlist.salon_id == salon.id).delete()
    db.commit()
    return RedirectResponse(url="/dashboard?tab=reserve", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/convert-waitlist/{waitlist_id}")
def convert_waitlist(
    waitlist_id: int,
    appointment_time: str = Form(...),
    salon: Salon = Depends(get_active_salon),
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
    staff_obj = db.query(Staff).filter(Staff.id == staff_id, Staff.salon_id == salon.id).first()
    c_duration = _service_duration(db, entry.service_id)
    c_end = appt_dt + timedelta(minutes=c_duration)

    if not staff_obj or not _within_staff_hours(db, salon, staff_obj, appt_dt, c_end):
        return RedirectResponse(url="/dashboard?tab=reserve&error=outside_hours", status_code=status.HTTP_303_SEE_OTHER)

    if _staff_has_overlap(db, salon.id, staff_id, appt_dt, c_end):
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
    salon: Salon = Depends(get_active_salon),
    db: Session = Depends(get_db),
):
    db.add(Service(salon_id=salon.id, name=name, price=price, duration_minutes=duration_minutes))
    db.commit()
    return RedirectResponse(url="/dashboard?tab=services", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/delete-service")
def delete_service(
    service_id: int = Form(...),
    salon: Salon = Depends(get_active_salon),
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
    salon: Salon = Depends(get_active_salon),
    db: Session = Depends(get_db),
):
    try:
        db.add(Staff(salon_id=salon.id, name=name))
        db.commit()
    except IntegrityError:
        db.rollback()
        redirect = RedirectResponse(url="/login?error=session_expired", status_code=status.HTTP_303_SEE_OTHER)
        redirect.delete_cookie("access_token")
        return redirect

    return RedirectResponse(url="/dashboard?tab=staff", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/delete-staff")
def delete_staff(
    staff_id: int = Form(...),
    salon: Salon = Depends(get_active_salon),
    db: Session = Depends(get_db),
):
    db.query(Staff).filter(Staff.id == staff_id, Staff.salon_id == salon.id).delete()
    db.commit()
    return RedirectResponse(url="/dashboard?tab=staff", status_code=status.HTTP_303_SEE_OTHER)


# ---------------------------------------------------------------------------
# Public customer-facing booking page
# ---------------------------------------------------------------------------
@app.get("/book/{salon_id}", response_class=HTMLResponse)
def public_booking_page(
    request: Request,
    salon_id: int,
    error: Optional[str] = None,
    success: Optional[str] = None,
    waitlisted: Optional[str] = None,
    conflict_name: Optional[str] = None,
    conflict_phone: Optional[str] = None,
    conflict_service: Optional[int] = None,
    conflict_staff: Optional[int] = None,
    conflict_time: Optional[str] = None,
    db: Session = Depends(get_db),
):
    salon = db.query(Salon).filter(Salon.id == salon_id).first()
    if not salon:
        return HTMLResponse("Salon not found", status_code=404)

    services = db.query(Service).filter(Service.salon_id == salon.id).order_by(Service.name).all()
    staff_members = db.query(Staff).filter(Staff.salon_id == salon.id).order_by(Staff.name).all()

    context = {
        "salon": salon,
        "services": services,
        "staff_members": staff_members,
        "current_date": date.today().isoformat(),
        "error": error,
        "success": success,
        "waitlisted": waitlisted,
        "hours_label": salon.hours_label,
        "days_label": salon.working_days_label,
    }

    if error == "conflict" and conflict_time and conflict_service and conflict_staff:
        conflict_dt = datetime.strptime(conflict_time, "%Y-%m-%dT%H:%M")
        c_duration = _service_duration(db, conflict_service)
        c_end = conflict_dt + timedelta(minutes=c_duration)

        conflict_staff_obj = db.query(Staff).filter(Staff.id == conflict_staff).first()
        alt_staff = _available_staff_for_slot(db, salon, conflict_dt, c_end, exclude_staff_id=conflict_staff)
        next_slot = (
            _next_available_slot(db, salon, conflict_staff_obj, c_duration, conflict_dt)
            if conflict_staff_obj else None
        )

        context.update({
            "conflict_name": conflict_name,
            "conflict_phone": conflict_phone,
            "conflict_service": conflict_service,
            "conflict_staff": conflict_staff,
            "conflict_staff_name": conflict_staff_obj.name if conflict_staff_obj else "",
            "conflict_time": conflict_time,
            "conflict_date": conflict_dt.date().isoformat(),
            "alt_staff": alt_staff,
            "next_slot": next_slot.strftime("%Y-%m-%dT%H:%M") if next_slot else None,
            "next_slot_display": _eth_display(next_slot),
        })

    return templates.TemplateResponse(request, "public_booking.html", context)


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
    duration = _service_duration(db, service_id)
    end_dt = appt_dt + timedelta(minutes=duration)

    staff_obj = db.query(Staff).filter(Staff.id == staff_id, Staff.salon_id == salon.id).first()

    if not staff_obj or not _within_staff_hours(db, salon, staff_obj, appt_dt, end_dt):
        params = urlencode({
            "error": "outside_hours",
            "conflict_name": customer_name,
            "conflict_phone": customer_phone,
            "conflict_service": service_id,
            "conflict_staff": staff_id,
            "conflict_time": appointment_time,
        })
        return RedirectResponse(url=f"/book/{salon_id}?{params}", status_code=status.HTTP_303_SEE_OTHER)

    if _staff_has_overlap(db, salon.id, staff_id, appt_dt, end_dt):
        params = urlencode({
            "error": "conflict",
            "conflict_name": customer_name,
            "conflict_phone": customer_phone,
            "conflict_service": service_id,
            "conflict_staff": staff_id,
            "conflict_time": appointment_time,
        })
        return RedirectResponse(url=f"/book/{salon_id}?{params}", status_code=status.HTTP_303_SEE_OTHER)

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
            "source": appt.source,
        },
    })

    return RedirectResponse(url=f"/book/{salon_id}?success=1", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/book/{salon_id}/waitlist")
def public_join_waitlist(
    salon_id: int,
    customer_name: str = Form(...),
    customer_phone: str = Form(...),
    service_id: int = Form(...),
    staff_id: Optional[int] = Form(None),
    preferred_date: str = Form(...),
    db: Session = Depends(get_db),
):
    salon = db.query(Salon).filter(Salon.id == salon_id).first()
    if not salon:
        return HTMLResponse("Salon not found", status_code=404)

    db.add(Waitlist(
        salon_id=salon.id,
        customer_name=customer_name,
        customer_phone=customer_phone,
        service_id=service_id,
        staff_id=staff_id or None,
        preferred_date=_parse_date(preferred_date) or date.today(),
    ))
    db.commit()

    return RedirectResponse(url=f"/book/{salon_id}?waitlisted=1", status_code=status.HTTP_303_SEE_OTHER)
