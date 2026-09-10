import csv
import io
import os
import secrets
import time
from collections import defaultdict, deque
from datetime import datetime, date, timedelta
from typing import List, Optional
from urllib.parse import urlencode

import uuid
from pathlib import Path

from fastapi import (
    FastAPI, Request, Depends, Form, WebSocket, WebSocketDisconnect, status,
    UploadFile, File,
)
from fastapi.responses import RedirectResponse, JSONResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database import Base, engine, get_db
from models import (
    Salon, Service, Staff, StaffDayOff, Appointment, Waitlist,
    AppointmentStatus, GalleryImage, Testimonial, MediaAsset,
)
from security import (
    hash_password,
    verify_password,
    create_access_token,
    create_admin_token,
    get_current_salon,
    get_current_admin,
    NotAuthenticatedException,
)

Base.metadata.create_all(bind=engine)


def _ensure_staff_day_hours_column():
    """One-time, idempotent startup migration.

    Base.metadata.create_all() above only creates TABLES that don't exist
    yet — it never alters a table that's already there. Your `staff` table
    already existed before the `day_hours` column was added to the model,
    so that column would never actually appear in the real database no
    matter how many times the app restarts, and every save of per-day
    staff hours would silently do nothing. This checks for the column and
    adds it if it's missing, so existing deployments get patched
    automatically on next boot without a manual migration step.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    try:
        existing_columns = [c["name"] for c in inspector.get_columns("staff")]
    except Exception:
        # Table doesn't exist yet (fresh DB) — create_all() above already
        # created it correctly, with the column, via the model definition.
        return

    if "day_hours" in existing_columns:
        return

    dialect = engine.dialect.name
    col_type = "JSON" if dialect == "postgresql" else "TEXT"

    with engine.begin() as conn:
        conn.execute(text(f"ALTER TABLE staff ADD COLUMN day_hours {col_type}"))


_ensure_staff_day_hours_column()

# ---------------------------------------------------------------------------
# Photo uploads — local disk under static/uploads/
# ---------------------------------------------------------------------------
UPLOAD_DIR = Path("static/uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 5 MB


def _ensure_photo_columns():
    """Idempotent startup migration for photo-related columns.

    create_all() only creates missing TABLES — it never ALTERs existing
    ones. Older deployments already have `salons` and `staff` without
    cover_photo_url / photo_url, so we add those columns if missing.
    GalleryImage is a new table and will be created by create_all().
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    dialect = engine.dialect.name
    str_type = "VARCHAR(500)" if dialect == "postgresql" else "TEXT"

    try:
        staff_cols = [c["name"] for c in inspector.get_columns("staff")]
    except Exception:
        staff_cols = []
    if staff_cols and "photo_url" not in staff_cols:
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE staff ADD COLUMN photo_url {str_type}"))

    try:
        salon_cols = [c["name"] for c in inspector.get_columns("salons")]
    except Exception:
        salon_cols = []
    if salon_cols and "cover_photo_url" not in salon_cols:
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE salons ADD COLUMN cover_photo_url {str_type}"))

    # gallery_images.category
    try:
        gi_cols = [c["name"] for c in inspector.get_columns("gallery_images")]
    except Exception:
        gi_cols = []
    if gi_cols and "category" not in gi_cols:
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE gallery_images ADD COLUMN category {str_type}"))

    # salon.slug for pretty public URLs /book/my-salon
    try:
        salon_cols = [c["name"] for c in inspector.get_columns("salons")]
    except Exception:
        salon_cols = []
    if salon_cols and "slug" not in salon_cols:
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE salons ADD COLUMN slug {str_type}"))
            try:
                conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_salons_slug ON salons (slug)"))
            except Exception:
                pass

    # Salon deposit settings
    try:
        salon_cols = [c["name"] for c in inspector.get_columns("salons")]
    except Exception:
        salon_cols = []
    if salon_cols and "deposit_enabled" not in salon_cols:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE salons ADD COLUMN deposit_enabled INTEGER DEFAULT 0"))
    if salon_cols and "payment_methods" not in salon_cols:
        with engine.begin() as conn:
            dialect = engine.dialect.name
            jtype = "JSON" if dialect == "postgresql" else "TEXT"
            conn.execute(text(f"ALTER TABLE salons ADD COLUMN payment_methods {jtype}"))

    # Service deposit_amount
    try:
        svc_cols = [c["name"] for c in inspector.get_columns("services")]
    except Exception:
        svc_cols = []
    if svc_cols and "deposit_amount" not in svc_cols:
        with engine.begin() as conn:
            dialect = engine.dialect.name
            ntype = "NUMERIC(10,2)" if dialect == "postgresql" else "REAL"
            conn.execute(text(f"ALTER TABLE services ADD COLUMN deposit_amount {ntype} DEFAULT 0"))

    # Appointment payment fields
    try:
        appt_cols = [c["name"] for c in inspector.get_columns("appointments")]
    except Exception:
        appt_cols = []
    for col, coltype in [
        ("deposit_amount", "NUMERIC(10,2)" if engine.dialect.name == "postgresql" else "REAL"),
        ("payment_method", str_type),
        ("payment_screenshot_url", str_type),
        ("payment_reviewed", "INTEGER DEFAULT 0"),
    ]:
        if appt_cols and col not in appt_cols:
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE appointments ADD COLUMN {col} {coltype}"))

    # Convert appointments.status from PostgreSQL ENUM to VARCHAR so new
    # statuses (Pending Payment, etc.) never require ALTER TYPE and never 500.
    if engine.dialect.name == "postgresql":
        try:
            with engine.begin() as conn:
                # Only alter if still an enum-backed column
                row = conn.execute(text(
                    """
                    SELECT data_type, udt_name
                    FROM information_schema.columns
                    WHERE table_name = 'appointments' AND column_name = 'status'
                    """
                )).fetchone()
                if row and (row[0] == 'USER-DEFINED' or (row[1] and 'appointment' in str(row[1]).lower())):
                    conn.execute(text(
                        "ALTER TABLE appointments "
                        "ALTER COLUMN status TYPE VARCHAR(30) "
                        "USING status::text"
                    ))
        except Exception:
            pass


_ensure_photo_columns()


def _content_type_for_ext(ext: str) -> str:
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }.get(ext, "application/octet-stream")


def _save_upload(file: UploadFile, subfolder: str = "", salon_id: Optional[int] = None) -> str:
    """Persist an uploaded image in the database (survives redeploys).

    Also writes a disk cache under static/uploads/ when possible.
    Returns a stable public URL: /media/{id}

    Raises ValueError on invalid type / size.
    """
    import base64
    from database import SessionLocal

    if not file or not file.filename:
        raise ValueError("No file provided")

    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        raise ValueError("Invalid image type. Allowed: jpg, jpeg, png, webp, gif")

    data = file.file.read()
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("Image too large (max 5 MB)")

    asset_id = uuid.uuid4().hex
    content_type = _content_type_for_ext(ext)
    b64 = base64.b64encode(data).decode("ascii")

    # Primary store: database
    db = SessionLocal()
    try:
        db.add(MediaAsset(
            id=asset_id,
            salon_id=salon_id,
            content_type=content_type,
            data=b64,
        ))
        db.commit()
    finally:
        db.close()

    # Best-effort disk cache (optional; may vanish on redeploy)
    try:
        dest_dir = UPLOAD_DIR / subfolder if subfolder else UPLOAD_DIR
        dest_dir.mkdir(parents=True, exist_ok=True)
        with open(dest_dir / f"{asset_id}{ext}", "wb") as f:
            f.write(data)
    except Exception:
        pass

    return f"/media/{asset_id}"


app = FastAPI(title="Melkegna Salon Platform")
templates = Jinja2Templates(directory="templates")

# Ensure static root exists before mounting (Railway / fresh deploys)
Path("static").mkdir(parents=True, exist_ok=True)
Path("static/uploads").mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/media/{asset_id}")
def serve_media(asset_id: str, db: Session = Depends(get_db)):
    """Serve an image stored in the database (survives redeploys)."""
    import base64
    from fastapi.responses import Response

    asset = db.query(MediaAsset).filter(MediaAsset.id == asset_id).first()
    if not asset:
        return Response(status_code=404, content=b"Not found")
    try:
        raw = base64.b64decode(asset.data)
    except Exception:
        return Response(status_code=500, content=b"Corrupt image")
    return Response(
        content=raw,
        media_type=asset.content_type or "image/jpeg",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


ADMIN_SECRET_KEY = os.environ.get("ADMIN_SECRET_KEY", "change-me-set-ADMIN_SECRET_KEY-in-railway")
SLOT_STEP_MINUTES = 15

# ---------------------------------------------------------------------------
# Very simple in-memory rate limiting for login endpoints.
# Good enough for a single-instance deployment; resets on restart and does
# NOT share state across multiple server processes/replicas. If you scale
# to multiple Railway instances, move this to Redis or a DB table instead.
# ---------------------------------------------------------------------------
LOGIN_MAX_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 5 * 60  # 5 minutes

_login_attempts: dict[str, deque] = defaultdict(deque)


def _is_rate_limited(bucket_key: str) -> bool:
    now = time.time()
    attempts = _login_attempts[bucket_key]
    while attempts and now - attempts[0] > LOGIN_WINDOW_SECONDS:
        attempts.popleft()
    return len(attempts) >= LOGIN_MAX_ATTEMPTS


def _record_attempt(bucket_key: str) -> None:
    _login_attempts[bucket_key].append(time.time())


def _valid_admin_key(key: Optional[str]) -> bool:
    if not key:
        return False
    return secrets.compare_digest(key, ADMIN_SECRET_KEY)


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


# ---------------------------------------------------------------------------
# Day-of-week names (Amharic short form + English full form), used anywhere
# we want to show "which day" a date falls on rather than just the date
# itself — e.g. the home dashboard header and customer search results.
# Index is Python's date.weekday() (0=Monday ... 6=Sunday).
# ---------------------------------------------------------------------------
_DAY_NAMES_AM = ["ሰኞ", "ማክሰኞ", "ረቡዕ", "ሐሙስ", "ዓርብ", "ቅዳሜ", "እሁድ"]
_DAY_NAMES_EN = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _day_names(d: Optional[date]):
    """Returns (amharic_name, english_name) for a given date, or (None, None)."""
    if not d:
        return None, None
    idx = d.weekday()
    return _DAY_NAMES_AM[idx], _DAY_NAMES_EN[idx]


# ---------------------------------------------------------------------------
# FIX: flexible appointment-datetime parser.
#
# The "book appointment" datetime string is produced by JS in
# dashboard.html / public_booking.html (the Ethiopian scroll-wheel time
# picker). That JS always appends seconds, e.g.:
#
#     hidden.value = bookDate + 'T' + wheelValue + ':00';
#     // -> "2026-09-10T09:00:00"
#
# But every backend route used to parse it with:
#
#     datetime.strptime(appointment_time, "%Y-%m-%dT%H:%M")
#
# ...a format with NO seconds. strptime doesn't ignore trailing text, so
# that mismatch throws:
#
#     ValueError: unconverted data remains: :00
#
# which FastAPI turns into a 500 Internal Server Error. This happened on
# every booking attempt through the wheel picker (not just the
# staff-unavailable path) — the "next available slot" quick-book buttons
# happened to work because THAT value is built server-side without
# seconds ("%Y-%m-%dT%H:%M"), so the two formats were silently
# inconsistent depending on which UI path produced the string.
#
# This helper accepts BOTH formats so it doesn't matter which one a given
# form field happens to send, now or in the future.
# ---------------------------------------------------------------------------
def _parse_appt_datetime(value: str) -> datetime:
    value = (value or "").strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    raise ValueError(f"Unrecognized appointment datetime format: {value!r}")


@app.exception_handler(NotAuthenticatedException)
async def not_authenticated_handler(request: Request, exc: NotAuthenticatedException):
    # Admin routes redirect to the admin login; everything else to salon login.
    if request.url.path.startswith("/admin"):
        return RedirectResponse(url="/admin/login", status_code=status.HTTP_303_SEE_OTHER)
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
# Small helpers — slug / public booking URL
# ---------------------------------------------------------------------------

def _slugify_name(name: str) -> str:
    """URL slug from salon name. Keeps Latin + Ethiopic letters so Amharic names work.

    Examples:
      "Beauty Salon"     -> "beauty-salon"
      "ሳሎን መልከኛ"       -> "ሳሎን-መልከኛ"
      "My Salon!! 2024"  -> "my-salon-2024"
    """
    import re
    text = (name or "").strip().lower()
    # spaces -> hyphen
    text = re.sub(r"\s+", "-", text)
    # keep word chars (unicode letters/digits, including Ethiopic) and hyphens
    text = re.sub(r"[^\w\-]", "", text, flags=re.UNICODE)
    text = re.sub(r"-+", "-", text).strip("-")
    return text[:120] if text else ""


def _unique_slug(db: Session, name: str, salon_id: Optional[int] = None) -> str:
    """Return a unique slug for this salon name.

    First salon named "Beauty" gets "beauty".
    Second gets "beauty-2", third "beauty-3", etc.
    If the name produces an empty slug (rare), fall back to "salon-{id}".
    """
    base = _slugify_name(name)
    if not base:
        base = f"salon-{salon_id or 'new'}"
    candidate = base
    n = 2
    while True:
        q = db.query(Salon).filter(Salon.slug == candidate)
        if salon_id:
            q = q.filter(Salon.id != salon_id)
        if not q.first():
            return candidate
        candidate = f"{base}-{n}"
        n += 1


def _resolve_salon(db: Session, salon_ref: str):
    """Resolve /book/{ref} by slug first, then numeric id (legacy links)."""
    salon_ref = (salon_ref or "").strip()
    if not salon_ref:
        return None
    salon = db.query(Salon).filter(Salon.slug == salon_ref).first()
    if salon:
        return salon
    if salon_ref.isdigit():
        return db.query(Salon).filter(Salon.id == int(salon_ref)).first()
    return None


def _ensure_salon_slugs():
    """Assign missing or placeholder slugs so /book/{name} works for existing salons.

    Runs once at startup. Any salon with NULL/empty slug, or a placeholder
    like "salon-12", gets a proper unique slug derived from its name.
    """
    from database import SessionLocal
    db = SessionLocal()
    try:
        salons = db.query(Salon).all()
        changed = False
        for s in salons:
            current = (getattr(s, "slug", None) or "").strip()
            desired = _slugify_name(s.name)
            needs = (
                not current
                or current.startswith("salon-")
                or (desired and current != desired and not current.startswith(desired + "-") and current != desired)
            )
            # Only rewrite when clearly broken / missing. Do not thrash
            # existing unique suffixes like "beauty-2".
            if not current or current.startswith("salon-"):
                s.slug = _unique_slug(db, s.name, s.id)
                changed = True
            elif desired and not current:
                s.slug = _unique_slug(db, s.name, s.id)
                changed = True
        if changed:
            db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


# Run once at import / startup so existing DBs get pretty URLs immediately.
_ensure_salon_slugs()


def _parse_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def _parse_time_hhmm(value: Optional[str]):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%H:%M").time()
    except (ValueError, TypeError):
        return None


def _revenue_between(db: Session, salon_id: int, start_dt: datetime, end_dt: datetime) -> float:
    """Sum service prices for appointments in range that count as revenue.

    Counts Confirmed + Completed (excludes Cancelled / No-Show). Salon owners
    expect today's booked amount to show as soon as a booking is confirmed,
    not only after marking Complete.
    """
    total = (
        db.query(func.coalesce(func.sum(Service.price), 0))
        .join(Appointment, Appointment.service_id == Service.id)
        .filter(
            Appointment.salon_id == salon_id,
            Appointment.status.in_([
                AppointmentStatus.completed,
                AppointmentStatus.confirmed,
                "Completed",
                "Confirmed",
            ]),
            Appointment.appointment_datetime >= start_dt,
            Appointment.appointment_datetime < end_dt,
        )
        .scalar()
    )
    return float(total or 0)


def _booking_count_between(db: Session, salon_id: int, start_dt: datetime, end_dt: datetime) -> int:
    return int(
        db.query(func.count(Appointment.id))
        .filter(
            Appointment.salon_id == salon_id,
            Appointment.status.in_([
                AppointmentStatus.completed,
                AppointmentStatus.confirmed,
                "Completed",
                "Confirmed",
            ]),
            Appointment.appointment_datetime >= start_dt,
            Appointment.appointment_datetime < end_dt,
        )
        .scalar()
        or 0
    )


def _revenue_details_between(db: Session, salon_id: int, start_dt: datetime, end_dt: datetime):
    """Every completed appointment in the given window, oldest first — the
    itemized, customer-by-customer breakdown behind the revenue totals.
    Used by both the dashboard's revenue tab and the CSV export, so the
    on-screen table and the downloaded file always agree."""
    return (
        db.query(Appointment)
        .filter(
            Appointment.salon_id == salon_id,
            Appointment.status.in_([
                AppointmentStatus.completed,
                AppointmentStatus.confirmed,
                "Completed",
                "Confirmed",
            ]),
            Appointment.appointment_datetime >= start_dt,
            Appointment.appointment_datetime < end_dt,
        )
        .order_by(Appointment.appointment_datetime)
        .all()
    )


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


def _staff_hours_for_day(staff: Staff, salon: Salon, d: date):
    """Open/close time for this staff member on this specific date.

    Priority:
      1. A per-day override saved via "Custom days / half-day per day"
         (staff.day_hours[str(weekday)]) — this is what lets a staff
         member work e.g. only 2 hours or 6 hours on a given day.
      2. The staff member's own overall custom hours (staff.opening_time /
         staff.closing_time), if set.
      3. The salon's default hours.
    """
    day_hours = staff.day_hours or {}
    override = day_hours.get(str(d.weekday()))
    if override and override.get("open") and override.get("close"):
        o = _parse_time_hhmm(override["open"])
        c = _parse_time_hhmm(override["close"])
        if o and c:
            return o, c
    return staff.effective_hours(salon)


def _within_staff_hours(db: Session, salon: Salon, staff: Staff, appt_dt: datetime, end_dt: datetime) -> bool:
    """Checks salon hours AND this staff member's own working days/hours
    (including any per-day override) AND that they aren't marked off that
    specific day."""
    if not _within_business_hours(salon, appt_dt, end_dt):
        return False

    day = appt_dt.date()
    if day.weekday() not in staff.effective_working_days(salon):
        return False

    if _staff_is_off(db, staff.id, day):
        return False

    open_t, close_t = _staff_hours_for_day(staff, salon, day)
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
    hours/days (including any per-day override), skipping if they're off
    that day."""
    day = requested_dt.date()
    if day.weekday() not in staff.effective_working_days(salon):
        return None
    if _staff_is_off(db, staff.id, day):
        return None

    open_t, close_t = _staff_hours_for_day(staff, salon, day)
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
def admin_login(request: Request, password: str = Form(...)):
    bucket_key = f"admin:{request.client.host if request.client else 'unknown'}"
    if _is_rate_limited(bucket_key):
        return RedirectResponse(
            url="/admin/login?error=Too many attempts, please wait a few minutes",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    if not _valid_admin_key(password):
        _record_attempt(bucket_key)
        return RedirectResponse(
            url="/admin/login?error=የተሳሳተ ቁልፍ (Invalid admin key)",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    redirect = RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)
    redirect.set_cookie(
        key="admin_token",
        value=create_admin_token(),
        httponly=True,
        samesite="lax",
        secure=True,
        max_age=8 * 60 * 60,
    )
    return redirect


@app.get("/admin/logout")
def admin_logout():
    redirect = RedirectResponse(url="/admin/login", status_code=status.HTTP_303_SEE_OTHER)
    redirect.delete_cookie("admin_token")
    return redirect


@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(
    request: Request,
    _: bool = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    salons = db.query(Salon).order_by(Salon.id.desc()).all()
    return templates.TemplateResponse(request, "admin.html", {"salons": salons})


@app.post("/admin/approve/{salon_id}")
def admin_approve(
    salon_id: int,
    days: int = Form(...),
    _: bool = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    salon = db.query(Salon).filter(Salon.id == salon_id).first()
    if salon:
        now = datetime.utcnow()
        base = salon.subscription_expires_at if (salon.subscription_expires_at and salon.subscription_expires_at > now) else now
        salon.subscription_expires_at = base + timedelta(days=days)
        salon.status = "active"
        # Ensure a pretty public URL exists as soon as the salon goes live
        if not (salon.slug or "").strip() or (salon.slug or "").startswith("salon-"):
            salon.slug = _unique_slug(db, salon.name, salon.id)
        db.commit()

    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/admin/suspend/{salon_id}")
def admin_suspend(
    salon_id: int,
    _: bool = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    salon = db.query(Salon).filter(Salon.id == salon_id).first()
    if salon:
        salon.status = "suspended"
        db.commit()

    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


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
    confirm_password: str = Form(...),
    opening_time: str = Form("08:00"),
    closing_time: str = Form("20:00"),
    working_days: List[str] = Form(default=[]),
    db: Session = Depends(get_db),
):
    phone_clean = _normalize_phone(phone)
    if len(phone_clean) < 9:
        return RedirectResponse(url="/register?error=invalid_phone", status_code=status.HTTP_303_SEE_OTHER)

    # Password checks happen before the DB lookup below, on purpose:
    # it's cheaper, and it avoids leaking "this phone number exists"
    # to someone just probing the form with garbage passwords.
    if len(password) < 8:
        return RedirectResponse(url="/register?error=weak_password", status_code=status.HTTP_303_SEE_OTHER)

    if password != confirm_password:
        return RedirectResponse(url="/register?error=password_mismatch", status_code=status.HTTP_303_SEE_OTHER)

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
    db.flush()  # get salon.id
    salon.slug = _unique_slug(db, name, salon.id)
    db.commit()

    return RedirectResponse(url="/login?registered=1", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, error: Optional[str] = None, registered: Optional[str] = None):
    return templates.TemplateResponse(
        request, "login.html", {"error": error, "registered": registered}
    )


@app.post("/login")
def login(
    request: Request,
    phone: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    phone_clean = _normalize_phone(phone)
    bucket_key = f"login:{phone_clean}"
    if _is_rate_limited(bucket_key):
        return RedirectResponse(url="/login?error=too_many_attempts", status_code=status.HTTP_303_SEE_OTHER)

    salon = db.query(Salon).filter(Salon.phone == phone_clean).first()
    if not salon or not verify_password(password, salon.hashed_password):
        _record_attempt(bucket_key)
        return RedirectResponse(url="/login?error=invalid", status_code=status.HTTP_303_SEE_OTHER)

    token = create_access_token({"sub": str(salon.id)})
    redirect = RedirectResponse(url="/dashboard?tab=home", status_code=status.HTTP_303_SEE_OTHER)
    redirect.set_cookie(
        key="access_token",
        value=token,
        httponly=True,
        samesite="lax",
        secure=True,
        max_age=60 * 60,
    )
    return redirect


@app.get("/logout")
def logout():
    redirect = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    redirect.delete_cookie("access_token")
    return redirect


# ---------------------------------------------------------------------------
# Privacy Policy
# ---------------------------------------------------------------------------
@app.get("/privacy", response_class=HTMLResponse)
def privacy_page(request: Request):
    return templates.TemplateResponse(request, "privacy.html", {})


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

    # Keep public booking URL pretty and unique for this salon.
    # - Missing / empty / placeholder "salon-12" → assign from name
    # - Does NOT rewrite an existing unique suffix like "beauty-2"
    try:
        current = (getattr(salon, "slug", None) or "").strip()
        if not current or current.startswith("salon-"):
            salon.slug = _unique_slug(db, salon.name, salon.id)
            db.commit()
            db.refresh(salon)
    except Exception:
        db.rollback()

    day_start = datetime.combine(today, datetime.min.time())
    day_end = day_start + timedelta(days=1)

    services = db.query(Service).filter(Service.salon_id == salon.id).order_by(Service.name).all()
    staff_members = db.query(Staff).filter(Staff.salon_id == salon.id).order_by(Staff.name).all()

    daily_rev = _revenue_between(db, salon.id, day_start, day_end)
    week_start = day_start - timedelta(days=day_start.weekday())
    week_end = week_start + timedelta(days=7)
    month_start_dt = day_start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if month_start_dt.month == 12:
        month_end_dt = month_start_dt.replace(year=month_start_dt.year + 1, month=1)
    else:
        month_end_dt = month_start_dt.replace(month=month_start_dt.month + 1)
    weekly_rev = _revenue_between(db, salon.id, week_start, week_end)
    monthly_rev = _revenue_between(db, salon.id, month_start_dt, month_end_dt)
    today_bookings_rev = _booking_count_between(db, salon.id, day_start, day_end)
    avg_booking_today = (daily_rev / today_bookings_rev) if today_bookings_rev else 0.0

    # Appointments for selected day (default today)
    view_date = _parse_date(selected_date) or today
    view_start = datetime.combine(view_date, datetime.min.time())
    view_end = view_start + timedelta(days=1)
    appointments = (
        db.query(Appointment)
        .filter(
            Appointment.salon_id == salon.id,
            Appointment.appointment_datetime >= view_start,
            Appointment.appointment_datetime < view_end,
        )
        .order_by(Appointment.appointment_datetime)
        .all()
    )

    # Waitlist
    waitlist = (
        db.query(Waitlist)
        .filter(Waitlist.salon_id == salon.id)
        .order_by(Waitlist.preferred_date, Waitlist.id)
        .all()
    )

    # Revenue tab range
    rev_start = _parse_date(start_date) or (today - timedelta(days=30))
    rev_end = _parse_date(end_date) or today
    if rev_end < rev_start:
        rev_start, rev_end = rev_end, rev_start
    rev_start_dt = datetime.combine(rev_start, datetime.min.time())
    rev_end_dt = datetime.combine(rev_end + timedelta(days=1), datetime.min.time())
    revenue_rows = _revenue_details_between(db, salon.id, rev_start_dt, rev_end_dt)
    revenue_total = _revenue_between(db, salon.id, rev_start_dt, rev_end_dt)

    # Gallery + testimonials for settings
    gallery = (
        db.query(GalleryImage)
        .filter(GalleryImage.salon_id == salon.id)
        .order_by(GalleryImage.id.desc())
        .all()
    )
    testimonials = (
        db.query(Testimonial)
        .filter(Testimonial.salon_id == salon.id)
        .order_by(Testimonial.is_pinned.desc(), Testimonial.id.desc())
        .all()
    )

    # Pending payment proofs on home
    pending_payments = (
        db.query(Appointment)
        .filter(
            Appointment.salon_id == salon.id,
            Appointment.payment_screenshot_url.isnot(None),
            Appointment.payment_reviewed == 0,
        )
        .order_by(Appointment.created_at.desc())
        .limit(20)
        .all()
    )
    pending_payment_count = len(pending_payments)

    day_am, day_en = _day_names(view_date)

    # Conflict recovery context (same as public booking)
    context = {
        "request": request,
        "salon": salon,
        "active_tab": tab,
        "services": services,
        "staff_members": staff_members,
        "appointments": appointments,
        "waitlist": waitlist,
        "current_date": current_date,
        "selected_date": view_date.isoformat(),
        "selected_day_am": day_am,
        "selected_day_en": day_en,
        "daily_rev": daily_rev,
        "weekly_rev": weekly_rev,
        "monthly_rev": monthly_rev,
        "today_bookings": today_bookings_rev,
        "avg_booking_today": avg_booking_today,
        "revenue_rows": revenue_rows,
        "revenue_total": revenue_total,
        "rev_start": rev_start.isoformat(),
        "rev_end": rev_end.isoformat(),
        "gallery": gallery,
        "testimonials": testimonials,
        "pending_payments": pending_payments,
        "pending_payment_count": pending_payment_count,
        "error": error,
        "hours_label": salon.hours_label,
        "days_label": salon.working_days_label,
        # Public booking link always uses the slug (name-based name)
        "booking_path": salon.slug or str(salon.id),
        # Full absolute URL shown on the dashboard (copy button)
        "booking_url": str(request.base_url).rstrip("/") + "/book/" + (salon.slug or str(salon.id)),
    }

    if error == "conflict" and conflict_time and conflict_service and conflict_staff:
        try:
            conflict_dt = _parse_appt_datetime(conflict_time)
        except ValueError:
            conflict_dt = None
        if conflict_dt:
            c_duration = _service_duration(db, conflict_service)
            c_end = conflict_dt + timedelta(minutes=c_duration)
            conflict_staff_obj = db.query(Staff).filter(Staff.id == conflict_staff).first()
            alt_staff = _available_staff_for_slot(
                db, salon, conflict_dt, c_end, exclude_staff_id=conflict_staff
            )
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

    return templates.TemplateResponse(request, "dashboard.html", context)


@app.get("/dashboard/export-revenue")
def export_revenue_csv(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    salon: Salon = Depends(get_active_salon),
    db: Session = Depends(get_db),
):
    today = date.today()
    rev_start = _parse_date(start_date) or (today - timedelta(days=30))
    rev_end = _parse_date(end_date) or today
    if rev_end < rev_start:
        rev_start, rev_end = rev_end, rev_start
    rev_start_dt = datetime.combine(rev_start, datetime.min.time())
    rev_end_dt = datetime.combine(rev_end + timedelta(days=1), datetime.min.time())
    rows = _revenue_details_between(db, salon.id, rev_start_dt, rev_end_dt)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "Date", "Time (Eth)", "Customer", "Phone", "Service", "Price (ETB)",
        "Staff", "Status", "Source",
    ])
    for a in rows:
        writer.writerow([
            a.appointment_datetime.date().isoformat(),
            a.appointment_time,
            a.customer_name,
            a.customer_phone,
            a.service_name,
            a.service_price,
            a.staff_name,
            getattr(a.status, "value", str(a.status)),
            a.source or "",
        ])
    buf.seek(0)
    filename = f"revenue_{salon.slug or salon.id}_{rev_start}_{rev_end}.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/update-salon-hours")
def update_salon_hours(
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
    day_open_0: Optional[str] = Form(None),
    day_close_0: Optional[str] = Form(None),
    day_open_1: Optional[str] = Form(None),
    day_close_1: Optional[str] = Form(None),
    day_open_2: Optional[str] = Form(None),
    day_close_2: Optional[str] = Form(None),
    day_open_3: Optional[str] = Form(None),
    day_close_3: Optional[str] = Form(None),
    day_open_4: Optional[str] = Form(None),
    day_close_4: Optional[str] = Form(None),
    day_open_5: Optional[str] = Form(None),
    day_close_5: Optional[str] = Form(None),
    day_open_6: Optional[str] = Form(None),
    day_close_6: Optional[str] = Form(None),
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

        day_open_by_index = {
            0: day_open_0, 1: day_open_1, 2: day_open_2, 3: day_open_3,
            4: day_open_4, 5: day_open_5, 6: day_open_6,
        }
        day_close_by_index = {
            0: day_close_0, 1: day_close_1, 2: day_close_2, 3: day_close_3,
            4: day_close_4, 5: day_close_5, 6: day_close_6,
        }

        day_hours = {}
        for i in day_ints:
            o = _parse_time_hhmm(day_open_by_index.get(i))
            c = _parse_time_hhmm(day_close_by_index.get(i))
            if o and c and c > o:
                day_hours[str(i)] = {
                    "open": day_open_by_index[i],
                    "close": day_close_by_index[i],
                }

        staff.day_hours = day_hours or None
    else:
        staff.working_days = None
        staff.day_hours = None

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
        appt_date = a.appointment_datetime.date()
        day_am, day_en = _day_names(appt_date)
        results.append({
            "customer_name": a.customer_name,
            "customer_phone": a.customer_phone,
            "service_name": a.service_name,
            "appointment_date": appt_date.isoformat(),
            "appointment_time": a.appointment_time,
            "day_am": day_am,
            "day_en": day_en,
            "status": a.status.value if hasattr(a.status, "value") else str(a.status),
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
    try:
        appt_dt = _parse_appt_datetime(appointment_time)
    except ValueError:
        params = urlencode({"tab": "home", "error": "invalid_time"})
        return RedirectResponse(url=f"/dashboard?{params}", status_code=status.HTTP_303_SEE_OTHER)

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

    try:
        appt_dt = _parse_appt_datetime(appointment_time)
    except ValueError:
        return RedirectResponse(url="/dashboard?tab=reserve&error=invalid_time", status_code=status.HTTP_303_SEE_OTHER)

    staff_id = entry.staff_id or db.query(Staff.id).filter(Staff.salon_id == salon.id).limit(1).scalar()
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
async def add_staff(
    name: str = Form(...),
    photo: Optional[UploadFile] = File(None),
    salon: Salon = Depends(get_active_salon),
    db: Session = Depends(get_db),
):
    photo_url = None
    if photo and photo.filename:
        try:
            photo_url = _save_upload(photo, subfolder=f"staff/{salon.id}", salon_id=salon.id)
        except ValueError:
            photo_url = None

    try:
        db.add(Staff(salon_id=salon.id, name=name, photo_url=photo_url))
        db.commit()
    except IntegrityError:
        db.rollback()
        redirect = RedirectResponse(url="/login?error=session_expired", status_code=status.HTTP_303_SEE_OTHER)
        redirect.delete_cookie("access_token")
        return redirect

    return RedirectResponse(url="/dashboard?tab=staff", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/update-staff-photo")
async def update_staff_photo(
    staff_id: int = Form(...),
    photo: UploadFile = File(...),
    salon: Salon = Depends(get_active_salon),
    db: Session = Depends(get_db),
):
    staff = db.query(Staff).filter(Staff.id == staff_id, Staff.salon_id == salon.id).first()
    if not staff:
        return RedirectResponse(url="/dashboard?tab=staff", status_code=status.HTTP_303_SEE_OTHER)

    try:
        staff.photo_url = _save_upload(photo, subfolder=f"staff/{salon.id}", salon_id=salon.id)
        db.commit()
    except ValueError:
        return RedirectResponse(url="/dashboard?tab=staff&error=invalid_image", status_code=status.HTTP_303_SEE_OTHER)

    return RedirectResponse(url="/dashboard?tab=staff", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/upload-cover-photo")
async def upload_cover_photo(
    photo: UploadFile = File(...),
    salon: Salon = Depends(get_active_salon),
    db: Session = Depends(get_db),
):
    try:
        url = _save_upload(photo, subfolder=f"cover/{salon.id}", salon_id=salon.id)
    except ValueError:
        return RedirectResponse(url="/dashboard?tab=settings&error=invalid_image", status_code=status.HTTP_303_SEE_OTHER)

    salon.cover_photo_url = url
    db.commit()
    return RedirectResponse(url="/dashboard?tab=settings", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/upload-gallery-photo")
async def upload_gallery_photo(
    photo: UploadFile = File(...),
    caption: Optional[str] = Form(None),
    category: Optional[str] = Form("other"),
    salon: Salon = Depends(get_active_salon),
    db: Session = Depends(get_db),
):
    try:
        url = _save_upload(photo, subfolder=f"gallery/{salon.id}", salon_id=salon.id)
    except ValueError:
        return RedirectResponse(url="/dashboard?tab=settings&error=invalid_image", status_code=status.HTTP_303_SEE_OTHER)

    allowed = {"nails", "manicure", "pedicure", "massage", "facial", "hair", "other"}
    cat = (category or "other").strip().lower()
    if cat not in allowed:
        cat = "other"

    db.add(GalleryImage(
        salon_id=salon.id,
        image_url=url,
        caption=(caption or "").strip() or None,
        category=cat,
    ))
    db.commit()
    return RedirectResponse(url="/dashboard?tab=settings", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/add-testimonial")
async def add_testimonial(
    client_name: str = Form(...),
    comment: str = Form(...),
    rating: int = Form(5),
    service_label: Optional[str] = Form(None),
    is_pinned: Optional[str] = Form(None),
    is_celebrity: Optional[str] = Form(None),
    client_photo: Optional[UploadFile] = File(None),
    salon: Salon = Depends(get_active_salon),
    db: Session = Depends(get_db),
):
    photo_url = None
    if client_photo and client_photo.filename:
        try:
            photo_url = _save_upload(client_photo, subfolder=f"testimonials/{salon.id}", salon_id=salon.id)
        except ValueError:
            photo_url = None

    r = max(1, min(5, int(rating or 5)))
    db.add(Testimonial(
        salon_id=salon.id,
        client_name=client_name.strip(),
        comment=comment.strip(),
        rating=r,
        service_label=(service_label or "").strip() or None,
        client_photo_url=photo_url,
        is_pinned=1 if is_pinned else 0,
        is_celebrity=1 if is_celebrity else 0,
    ))
    db.commit()
    return RedirectResponse(url="/dashboard?tab=settings", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/toggle-testimonial-pin")
def toggle_testimonial_pin(
    testimonial_id: int = Form(...),
    salon: Salon = Depends(get_active_salon),
    db: Session = Depends(get_db),
):
    t = db.query(Testimonial).filter(Testimonial.id == testimonial_id, Testimonial.salon_id == salon.id).first()
    if t:
        t.is_pinned = 0 if t.is_pinned else 1
        db.commit()
    return RedirectResponse(url="/dashboard?tab=settings", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/delete-testimonial")
def delete_testimonial(
    testimonial_id: int = Form(...),
    salon: Salon = Depends(get_active_salon),
    db: Session = Depends(get_db),
):
    t = db.query(Testimonial).filter(Testimonial.id == testimonial_id, Testimonial.salon_id == salon.id).first()
    if t:
        db.delete(t)
        db.commit()
    return RedirectResponse(url="/dashboard?tab=settings", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/delete-gallery-photo")
def delete_gallery_photo(
    image_id: int = Form(...),
    salon: Salon = Depends(get_active_salon),
    db: Session = Depends(get_db),
):
    img = (
        db.query(GalleryImage)
        .filter(GalleryImage.id == image_id, GalleryImage.salon_id == salon.id)
        .first()
    )
    if img:
        try:
            if img.image_url and img.image_url.startswith("/static/"):
                disk_path = Path(img.image_url[len("/static/"):])
                full = Path("static") / disk_path
                if full.is_file():
                    full.unlink()
        except Exception:
            pass
        db.delete(img)
        db.commit()
    return RedirectResponse(url="/dashboard?tab=settings", status_code=status.HTTP_303_SEE_OTHER)


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
# Deposit / payment settings (salon owner)
# ---------------------------------------------------------------------------
@app.post("/settings/deposit")
async def settings_deposit(
    deposit_enabled: Optional[str] = Form(None),
    salon: Salon = Depends(get_active_salon),
    db: Session = Depends(get_db),
):
    salon.deposit_enabled = 1 if deposit_enabled in ("1", "on", "true", "yes") else 0
    db.commit()
    return RedirectResponse(url="/dashboard?tab=settings", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/settings/payment-methods")
async def settings_payment_methods(
    method_names: List[str] = Form(default=[]),
    method_accounts: List[str] = Form(default=[]),
    method_notes: List[str] = Form(default=[]),
    salon: Salon = Depends(get_active_salon),
    db: Session = Depends(get_db),
):
    methods = []
    for i, name in enumerate(method_names):
        name = (name or "").strip()
        if not name:
            continue
        account = method_accounts[i].strip() if i < len(method_accounts) else ""
        notes = method_notes[i].strip() if i < len(method_notes) else ""
        methods.append({"name": name, "account": account, "instructions": notes})
    salon.payment_methods = methods
    db.commit()
    return RedirectResponse(url="/dashboard?tab=settings", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/update-service-deposit")
def update_service_deposit(
    service_id: int = Form(...),
    deposit_amount: float = Form(0),
    salon: Salon = Depends(get_active_salon),
    db: Session = Depends(get_db),
):
    svc = db.query(Service).filter(Service.id == service_id, Service.salon_id == salon.id).first()
    if svc:
        svc.deposit_amount = max(0, float(deposit_amount or 0))
        db.commit()
    return RedirectResponse(url="/dashboard?tab=services", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/dismiss-payment-proof")
def dismiss_payment_proof(
    appointment_id: int = Form(...),
    salon: Salon = Depends(get_active_salon),
    db: Session = Depends(get_db),
):
    """Remove a deposit screenshot from the Home review list (booking stays confirmed)."""
    appt = (
        db.query(Appointment)
        .filter(Appointment.id == appointment_id, Appointment.salon_id == salon.id)
        .first()
    )
    if appt:
        appt.payment_reviewed = 1
        db.commit()
    return RedirectResponse(url="/dashboard?tab=home", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/confirm-deposit-payment")
def confirm_deposit_payment(
    appointment_id: int = Form(...),
    salon: Salon = Depends(get_active_salon),
    db: Session = Depends(get_db),
):
    """Owner marks a pending-payment booking as confirmed after verifying screenshot."""
    appt = (
        db.query(Appointment)
        .filter(Appointment.id == appointment_id, Appointment.salon_id == salon.id)
        .first()
    )
    if appt:
        st = getattr(appt.status, "value", str(appt.status))
        if st in ("Pending Payment", "pending_payment", AppointmentStatus.pending_payment.value):
            appt.status = AppointmentStatus.confirmed
            db.commit()
            try:
                import asyncio
                asyncio.get_event_loop().create_task(manager.broadcast(salon.id, {
                    "event": "payment_confirmed",
                    "appointment_id": appt.id,
                }))
            except Exception:
                pass
    return RedirectResponse(url="/dashboard?tab=home", status_code=status.HTTP_303_SEE_OTHER)


# ---------------------------------------------------------------------------
# Public customer-facing booking page  —  /book/{slug-or-id}
# ---------------------------------------------------------------------------
@app.get("/book/{salon_ref}", response_class=HTMLResponse)
def public_booking_page(
    request: Request,
    salon_ref: str,
    error: Optional[str] = None,
    success: Optional[str] = None,
    waitlisted: Optional[str] = None,
    appt_id: Optional[int] = None,
    conflict_name: Optional[str] = None,
    conflict_phone: Optional[str] = None,
    conflict_service: Optional[int] = None,
    conflict_staff: Optional[int] = None,
    conflict_time: Optional[str] = None,
    db: Session = Depends(get_db),
):
    salon = _resolve_salon(db, salon_ref)
    if not salon:
        return HTMLResponse("Salon not found", status_code=404)

    # Prefer the pretty slug in all redirects / links from this point on
    public_path = (salon.slug or "").strip() or str(salon.id)

    services = db.query(Service).filter(Service.salon_id == salon.id).order_by(Service.name).all()
    staff_members = db.query(Staff).filter(Staff.salon_id == salon.id).order_by(Staff.name).all()

    testimonials = (
        db.query(Testimonial)
        .filter(Testimonial.salon_id == salon.id)
        .order_by(Testimonial.is_pinned.desc(), Testimonial.id.desc())
        .limit(20)
        .all()
    )

    context = {
        "salon": salon,
        "salon_ref": public_path,
        "services": services,
        "staff_members": staff_members,
        "current_date": date.today().isoformat(),
        "error": error,
        "success": success,
        "waitlisted": waitlisted,
        "hours_label": salon.hours_label,
        "days_label": salon.working_days_label,
        "testimonials": testimonials,
        "appt_id": appt_id,
    }

    if error == "conflict" and conflict_time and conflict_service and conflict_staff:
        try:
            conflict_dt = _parse_appt_datetime(conflict_time)
        except ValueError:
            conflict_dt = None
        if conflict_dt:
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


@app.post("/book/{salon_ref}")
async def public_booking_submit(
    salon_ref: str,
    customer_name: str = Form(...),
    customer_phone: str = Form(...),
    service_id: int = Form(...),
    staff_id: int = Form(...),
    appointment_time: str = Form(...),
    payment_method: Optional[str] = Form(None),
    payment_screenshot: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db),
):
    salon = _resolve_salon(db, salon_ref)
    if not salon:
        return HTMLResponse("Salon not found", status_code=404)
    public_path = (salon.slug or "").strip() or str(salon.id)

    try:
        appt_dt = _parse_appt_datetime(appointment_time)
    except ValueError:
        params = urlencode({
            "error": "invalid_time",
            "conflict_name": customer_name,
            "conflict_phone": customer_phone,
            "conflict_service": service_id,
            "conflict_staff": staff_id,
            "conflict_time": appointment_time,
        })
        return RedirectResponse(url=f"/book/{public_path}?{params}", status_code=status.HTTP_303_SEE_OTHER)

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
        return RedirectResponse(url=f"/book/{public_path}?{params}", status_code=status.HTTP_303_SEE_OTHER)

    if _staff_has_overlap(db, salon.id, staff_id, appt_dt, end_dt):
        params = urlencode({
            "error": "conflict",
            "conflict_name": customer_name,
            "conflict_phone": customer_phone,
            "conflict_service": service_id,
            "conflict_staff": staff_id,
            "conflict_time": appointment_time,
        })
        return RedirectResponse(url=f"/book/{public_path}?{params}", status_code=status.HTTP_303_SEE_OTHER)

    svc = db.query(Service).filter(Service.id == service_id, Service.salon_id == salon.id).first()
    deposit_amt = float(svc.deposit_amount or 0) if svc else 0.0
    needs_deposit = bool(salon.deposit_enabled) and deposit_amt > 0

    screenshot_url = None
    pay_method = (payment_method or "").strip() or None
    if needs_deposit:
        if not payment_screenshot or not payment_screenshot.filename:
            params = urlencode({
                "error": "deposit_required",
                "conflict_name": customer_name,
                "conflict_phone": customer_phone,
                "conflict_service": service_id,
                "conflict_staff": staff_id,
                "conflict_time": appointment_time,
            })
            return RedirectResponse(url=f"/book/{public_path}?{params}", status_code=status.HTTP_303_SEE_OTHER)
        try:
            screenshot_url = _save_upload(
                payment_screenshot,
                subfolder=f"payments/{salon.id}",
                salon_id=salon.id,
            )
        except ValueError:
            params = urlencode({"error": "invalid_image"})
            return RedirectResponse(url=f"/book/{public_path}?{params}", status_code=status.HTTP_303_SEE_OTHER)

    # Deposit bookings are confirmed immediately so the slot is reserved.
    # The salon still sees the screenshot on the dashboard home and can cancel
    # if the proof looks wrong.
    appt_status = AppointmentStatus.confirmed

    appt = Appointment(
        salon_id=salon.id,
        customer_name=customer_name,
        customer_phone=customer_phone,
        service_id=service_id,
        staff_id=staff_id,
        appointment_datetime=appt_dt,
        status=appt_status,
        source="online",
        deposit_amount=deposit_amt if needs_deposit else 0,
        payment_method=pay_method,
        payment_screenshot_url=screenshot_url,
    )
    db.add(appt)
    try:
        db.commit()
        db.refresh(appt)
    except Exception as exc:
        db.rollback()
        import logging
        logging.getLogger("melkegna").warning("booking status insert failed: %s", exc)
        appt = Appointment(
            salon_id=salon.id,
            customer_name=customer_name,
            customer_phone=customer_phone,
            service_id=service_id,
            staff_id=staff_id,
            appointment_datetime=appt_dt,
            status=AppointmentStatus.confirmed,
            source="online",
            deposit_amount=deposit_amt if needs_deposit else 0,
            payment_method=pay_method,
            payment_screenshot_url=screenshot_url,
        )
        db.add(appt)
        try:
            db.commit()
            db.refresh(appt)
        except Exception:
            db.rollback()
            params = urlencode({"error": "booking_failed"})
            return RedirectResponse(
                url=f"/book/{public_path}?{params}",
                status_code=status.HTTP_303_SEE_OTHER,
            )

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
            "deposit_amount": float(appt.deposit_amount or 0),
        },
    })

    return RedirectResponse(
        url=f"/book/{public_path}?success=1&appt_id={appt.id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.get("/book/{salon_ref}/status/{appointment_id}")
def public_booking_status(salon_ref: str, appointment_id: int, db: Session = Depends(get_db)):
    """Customer polls this after deposit upload until the salon confirms payment."""
    salon = _resolve_salon(db, salon_ref)
    if not salon:
        return JSONResponse({"ok": False, "status": "not_found"}, status_code=404)
    appt = (
        db.query(Appointment)
        .filter(Appointment.id == appointment_id, Appointment.salon_id == salon.id)
        .first()
    )
    if not appt:
        return JSONResponse({"ok": False, "status": "not_found"}, status_code=404)
    st = getattr(appt.status, "value", str(appt.status))
    return JSONResponse({
        "ok": True,
        "status": st,
        "confirmed": st == "Confirmed" or st == AppointmentStatus.confirmed.value,
        "customer_name": appt.customer_name,
        "appointment_time": appt.appointment_time,
        "service_name": appt.service_name,
    })


@app.post("/book/{salon_ref}/waitlist")
def public_join_waitlist(
    salon_ref: str,
    customer_name: str = Form(...),
    customer_phone: str = Form(...),
    service_id: int = Form(...),
    staff_id: Optional[int] = Form(None),
    preferred_date: str = Form(...),
    db: Session = Depends(get_db),
):
    salon = _resolve_salon(db, salon_ref)
    if not salon:
        return HTMLResponse("Salon not found", status_code=404)
    public_path = (salon.slug or "").strip() or str(salon.id)

    db.add(Waitlist(
        salon_id=salon.id,
        customer_name=customer_name,
        customer_phone=customer_phone,
        service_id=service_id,
        staff_id=staff_id or None,
        preferred_date=_parse_date(preferred_date) or date.today(),
    ))
    db.commit()

    return RedirectResponse(url=f"/book/{public_path}?waitlisted=1", status_code=status.HTTP_303_SEE_OTHER)


# ---------------------------------------------------------------------------
# Local / Railway entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)