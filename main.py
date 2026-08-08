import os
from datetime import datetime, date, timedelta
from typing import Optional
from urllib.parse import urlencode

from fastapi import (
    FastAPI, Request, Depends, Form, WebSocket, WebSocketDisconnect, status
)
from fastapi.responses import RedirectResponse, JSONResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_
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

# Set this in Railway's environment variables (Project -> Variables).
# Whoever knows this string can approve/suspend any salon, so keep it long
# and random, and never commit a real value to source control.
ADMIN_SECRET_KEY = os.environ.get("ADMIN_SECRET_KEY", "change-me-set-ADMIN_SECRET_KEY-in-railway")


def _valid_admin_key(key: Optional[str]) -> bool:
    return bool(key) and key == ADMIN_SECRET_KEY


@app.exception_handler(NotAuthenticatedException)
async def not_authenticated_handler(request: Request, exc: NotAuthenticatedException):
    return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)


class SalonNotActiveException(Exception):
    """Raised when a logged-in salon isn't approved / has an expired
    subscription. Lets every protected route redirect to the same
    account-status page without repeating the check everywhere."""
    def __init__(self, salon: Salon):
        self.salon = salon


@app.exception_handler(SalonNotActiveException)
async def salon_not_active_handler(request: Request, exc: SalonNotActiveException):
    return templates.TemplateResponse(
        request, "account_status.html", {"salon": exc.salon}, status_code=status.HTTP_403_FORBIDDEN
    )


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
    """Deprecated: exact-timestamp check kept only for reference.
    Use _staff_has_overlap instead, which accounts for service duration."""
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


# How long the salon is assumed to be open for slot-suggestion purposes.
# (No per-salon business-hours model exists yet, so this is a sane default.)
BUSINESS_START_HOUR = 8
BUSINESS_END_HOUR = 20
SLOT_STEP_MINUTES = 15


def _service_duration(db: Session, service_id: int) -> int:
    duration = db.query(Service.duration_minutes).filter(Service.id == service_id).scalar()
    return duration or 30


def _staff_has_overlap(
    db: Session,
    salon_id: int,
    staff_id: int,
    start_dt: datetime,
    end_dt: datetime,
    exclude_appointment_id: Optional[int] = None,
) -> bool:
    """True if this staff member already has an appointment (of any
    duration) whose time range overlaps [start_dt, end_dt)."""
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
    salon_id: int,
    start_dt: datetime,
    end_dt: datetime,
    exclude_staff_id: Optional[int] = None,
):
    """Every staff member at this salon who is free for the whole
    [start_dt, end_dt) window, excluding the one already tried."""
    staff_list = db.query(Staff).filter(Staff.salon_id == salon_id).order_by(Staff.name).all()
    return [
        st for st in staff_list
        if st.id != exclude_staff_id and not _staff_has_overlap(db, salon_id, st.id, start_dt, end_dt)
    ]


def _next_available_slot(
    db: Session,
    salon_id: int,
    staff_id: int,
    duration_minutes: int,
    requested_dt: datetime,
) -> Optional[datetime]:
    """Search forward in SLOT_STEP_MINUTES increments, same day only,
    within business hours, for the next free slot for this staff member."""
    day = requested_dt.date()
    business_end = datetime.combine(day, datetime.min.time()) + timedelta(hours=BUSINESS_END_HOUR)

    slot_start = requested_dt + timedelta(minutes=SLOT_STEP_MINUTES)
    while slot_start + timedelta(minutes=duration_minutes) <= business_end:
        slot_end = slot_start + timedelta(minutes=duration_minutes)
        if not _staff_has_overlap(db, salon_id, staff_id, slot_start, slot_end):
            return slot_start
        slot_start += timedelta(minutes=SLOT_STEP_MINUTES)
    return None


def _normalize_phone(raw: str) -> str:
    """Keep only digits and a leading +, so '09 11 22 33 44' and
    '0911223344' are treated as the same login/lookup key."""
    raw = raw.strip()
    digits = "".join(ch for ch in raw if ch.isdigit())
    return digits


def get_active_salon(
    salon: Salon = Depends(get_current_salon),
    db: Session = Depends(get_db),
) -> Salon:
    """Use this instead of get_current_salon on every route a salon
    shouldn't reach until a super admin has approved them and their
    subscription is still current."""
    now = datetime.utcnow()

    # Auto-expire: if the paid period has quietly run out, flip an
    # "active" salon to "expired" so they lose dashboard access without
    # an admin having to manually suspend them every month.
    if salon.status == "active" and salon.subscription_expires_at and salon.subscription_expires_at < now:
        salon.status = "expired"
        db.commit()

    if salon.status != "active":
        raise SalonNotActiveException(salon)

    return salon


# ---------------------------------------------------------------------------
# Super Admin — approve new salons, extend/suspend subscriptions.
# Auth here is a single shared secret (ADMIN_SECRET_KEY), not a salon
# login. After /admin/login succeeds it's carried forward as `key` in
# the URL and in a hidden form field on every approve/suspend button,
# matching how admin_salons.html is already built. Anyone who obtains
# that key has full admin access, so keep it private and rotate it in
# Railway if you ever suspect it's leaked.
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
    return templates.TemplateResponse(request, "admin_salons.html", {"salons": salons, "admin_key": key})


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
        # Extend from the current expiry if it's still in the future
        # (renewing early doesn't lose the days already paid for);
        # otherwise start the new period from today.
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
    db: Session = Depends(get_db),
):
    phone_clean = _normalize_phone(phone)
    if len(phone_clean) < 9:
        return RedirectResponse(url="/register?error=invalid_phone", status_code=status.HTTP_303_SEE_OTHER)

    if db.query(Salon.id).filter(Salon.phone == phone_clean).first():
        return RedirectResponse(url="/register?error=exists", status_code=status.HTTP_303_SEE_OTHER)

    salon = Salon(
        name=name,
        owner_name=owner_name,
        phone=phone_clean,
        hashed_password=hash_password(password),
        status="pending",
        subscription_expires_at=None,
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

            alt_staff = _available_staff_for_slot(db, salon.id, conflict_dt, c_end, exclude_staff_id=conflict_staff)
            next_slot = _next_available_slot(db, salon.id, conflict_staff, c_duration, conflict_dt)
            conflict_staff_obj = db.query(Staff).filter(Staff.id == conflict_staff).first()

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
                "next_slot_display": next_slot.strftime("%I:%M %p") if next_slot else None,
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
# Customer search (by phone number or name) — used by the dashboard's
# search modal so reception can quickly find someone to call/text back.
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

    # Collapse to one card per phone number (most recent visit first) so
    # reception isn't scrolling through the same customer's whole history.
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
    c_duration = _service_duration(db, entry.service_id)
    c_end = appt_dt + timedelta(minutes=c_duration)

    if staff_id and _staff_has_overlap(db, salon.id, staff_id, appt_dt, c_end):
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
    db.add(Staff(salon_id=salon.id, name=name))
    db.commit()
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
# Public customer-facing booking page (the {{ booking_url }} link)
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
    }

    if error == "conflict" and conflict_time and conflict_service and conflict_staff:
        conflict_dt = datetime.strptime(conflict_time, "%Y-%m-%dT%H:%M")
        c_duration = _service_duration(db, conflict_service)
        c_end = conflict_dt + timedelta(minutes=c_duration)

        alt_staff = _available_staff_for_slot(db, salon.id, conflict_dt, c_end, exclude_staff_id=conflict_staff)
        next_slot = _next_available_slot(db, salon.id, conflict_staff, c_duration, conflict_dt)
        conflict_staff_obj = db.query(Staff).filter(Staff.id == conflict_staff).first()

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
            "next_slot_display": next_slot.strftime("%I:%M %p") if next_slot else None,
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
    """Lets a customer join today's waitlist directly from the public
    booking page when their preferred slot is unavailable — no login
    required, unlike the admin /add-waitlist route."""
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