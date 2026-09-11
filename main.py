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
from fastapi.responses import RedirectResponse, JSONResponse, HTMLResponse, StreamingResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database import Base, engine, get_db, SessionLocal
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
    from sqlalchemy import inspect, text
    inspector = inspect(engine)
    try:
        existing_columns = [c["name"] for c in inspector.get_columns("staff")]
    except Exception:
        return
    if "day_hours" in existing_columns:
        return
    dialect = engine.dialect.name
    col_type = "JSON" if dialect == "postgresql" else "TEXT"
    with engine.begin() as conn:
        conn.execute(text(f"ALTER TABLE staff ADD COLUMN day_hours {col_type}"))


_ensure_staff_day_hours_column()

UPLOAD_DIR = Path("static/uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024


def _ensure_photo_columns():
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

    try:
        gi_cols = [c["name"] for c in inspector.get_columns("gallery_images")]
    except Exception:
        gi_cols = []
    if gi_cols and "category" not in gi_cols:
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE gallery_images ADD COLUMN category {str_type}"))

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

    try:
        salon_cols = [c["name"] for c in inspector.get_columns("salons")]
    except Exception:
        salon_cols = []
    if salon_cols and "deposit_enabled" not in salon_cols:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE salons ADD COLUMN deposit_enabled INTEGER DEFAULT 0"))
    if salon_cols and "payment_methods" not in salon_cols:
        with engine.begin() as conn:
            jtype = "JSON" if dialect == "postgresql" else "TEXT"
            conn.execute(text(f"ALTER TABLE salons ADD COLUMN payment_methods {jtype}"))

    try:
        svc_cols = [c["name"] for c in inspector.get_columns("services")]
    except Exception:
        svc_cols = []
    if svc_cols and "deposit_amount" not in svc_cols:
        with engine.begin() as conn:
            ntype = "NUMERIC(10,2)" if dialect == "postgresql" else "REAL"
            conn.execute(text(f"ALTER TABLE services ADD COLUMN deposit_amount {ntype} DEFAULT 0"))

    try:
        appt_cols = [c["name"] for c in inspector.get_columns("appointments")]
    except Exception:
        appt_cols = []
    for col, coltype in [
        ("deposit_amount", "NUMERIC(10,2)" if dialect == "postgresql" else "REAL"),
        ("payment_method", str_type),
        ("payment_screenshot_url", str_type),
        ("payment_reviewed", "INTEGER DEFAULT 0"),
    ]:
        if appt_cols and col not in appt_cols:
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE appointments ADD COLUMN {col} {coltype}"))

    if engine.dialect.name == "postgresql":
        try:
            with engine.begin() as conn:
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


def _ensure_package_columns():
    """Package fields on services + party_size on appointments."""
    from sqlalchemy import inspect, text
    inspector = inspect(engine)
    dialect = engine.dialect.name
    str_type = "VARCHAR(600)" if dialect == "postgresql" else "TEXT"
    int_type = "INTEGER"

    try:
        svc_cols = [c["name"] for c in inspector.get_columns("services")]
    except Exception:
        svc_cols = []
    str500 = "VARCHAR(500)" if dialect == "postgresql" else "TEXT"
    ntype = "NUMERIC(10,2)" if dialect == "postgresql" else "REAL"
    for col, coltype, default in [
        ("is_package", int_type, "0"),
        ("min_people", int_type, "1"),
        ("max_people", int_type, None),
        ("includes_text", str_type, None),
        ("allow_outside_hours", int_type, "0"),
        ("photo_url", str500, None),
        ("extra_person_price", ntype, None),
    ]:
        if svc_cols and col not in svc_cols:
            with engine.begin() as conn:
                if default is not None:
                    conn.execute(text(
                        f"ALTER TABLE services ADD COLUMN {col} {coltype} DEFAULT {default}"
                    ))
                else:
                    conn.execute(text(f"ALTER TABLE services ADD COLUMN {col} {coltype}"))

    try:
        appt_cols = [c["name"] for c in inspector.get_columns("appointments")]
    except Exception:
        appt_cols = []
    if appt_cols and "party_size" not in appt_cols:
        with engine.begin() as conn:
            conn.execute(text(
                "ALTER TABLE appointments ADD COLUMN party_size INTEGER DEFAULT 1"
            ))

    try:
        svc_cols = [c["name"] for c in inspector.get_columns("services")]
    except Exception:
        svc_cols = []
    if svc_cols and "is_active" not in svc_cols:
        with engine.begin() as conn:
            conn.execute(text(
                f"ALTER TABLE services ADD COLUMN is_active {int_type} DEFAULT 1"
            ))

    try:
        appt_cols = [c["name"] for c in inspector.get_columns("appointments")]
    except Exception:
        appt_cols = []
    str120 = "VARCHAR(120)" if dialect == "postgresql" else "TEXT"
    ntype = "NUMERIC(10,2)" if dialect == "postgresql" else "REAL"
    for col, coltype in [
        ("service_name_snap", str120),
        ("service_price_snap", ntype),
    ]:
        if appt_cols and col not in appt_cols:
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE appointments ADD COLUMN {col} {coltype}"))
    if appt_cols and "service_id" in appt_cols:
        try:
            with engine.begin() as conn:
                if dialect == "postgresql":
                    conn.execute(text(
                        "ALTER TABLE appointments ALTER COLUMN service_id DROP NOT NULL"
                    ))
        except Exception:
            pass


_ensure_package_columns()


def _ensure_staff_service_ids_column():
    """Staff.service_ids JSON — which services a staff member offers (null = all)."""
    from sqlalchemy import inspect, text
    inspector = inspect(engine)
    dialect = engine.dialect.name
    jtype = "JSON" if dialect == "postgresql" else "TEXT"
    try:
        cols = [c["name"] for c in inspector.get_columns("staff")]
    except Exception:
        cols = []
    if cols and "service_ids" not in cols:
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE staff ADD COLUMN service_ids {jtype}"))


_ensure_staff_service_ids_column()


def _content_type_for_ext(ext: str) -> str:
    return {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
        ".webp": "image/webp", ".gif": "image/gif",
    }.get(ext, "application/octet-stream")


def _save_upload(file: UploadFile, subfolder: str = "", salon_id: Optional[int] = None) -> str:
    import base64
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
    db = SessionLocal()
    try:
        db.add(MediaAsset(id=asset_id, salon_id=salon_id, content_type=content_type, data=b64))
        db.commit()
    finally:
        db.close()
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
Path("static").mkdir(parents=True, exist_ok=True)
Path("static/uploads").mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/media/{asset_id}")
def serve_media(asset_id: str, db: Session = Depends(get_db)):
    import base64
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

LOGIN_MAX_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 5 * 60
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


def _western_display(dt: Optional[datetime]) -> Optional[str]:
    if not dt:
        return None
    h, m = dt.hour, dt.minute
    suffix = "AM" if h < 12 else "PM"
    h12 = h % 12
    if h12 == 0:
        h12 = 12
    return f"{h12}:{m:02d} {suffix}"


def _dual_time_display(dt: Optional[datetime]) -> Optional[str]:
    """Ethiopian primary · Western secondary (product default)."""
    if not dt:
        return None
    return f"{_eth_display(dt)} · {_western_display(dt)}"


_DAY_NAMES_AM = ["ሰኞ", "ማክሰኞ", "ረቡዕ", "ሐሙስ", "ዓርብ", "ቅዳሜ", "እሁድ"]
_DAY_NAMES_EN = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _day_names(d: Optional[date]):
    if not d:
        return None, None
    idx = d.weekday()
    return _DAY_NAMES_AM[idx], _DAY_NAMES_EN[idx]


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


def _slugify_name(name: str) -> str:
    import re
    text = (name or "").strip().lower()
    text = re.sub(r"\s+", "-", text)
    text = re.sub(r"[^\w\-]", "", text, flags=re.UNICODE)
    text = re.sub(r"-+", "-", text).strip("-")
    return text[:120] if text else ""


def _unique_slug(db: Session, name: str, salon_id: Optional[int] = None) -> str:
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
    salon_ref = (salon_ref or "").strip()
    if not salon_ref:
        return None
    salon = db.query(Salon).filter(Salon.slug == salon_ref).first()
    if salon:
        return salon
    if salon_ref.isdigit():
        return db.query(Salon).filter(Salon.id == int(salon_ref)).first()
    return None


def _public_base_url(request: Request) -> str:
    env = (os.environ.get("PUBLIC_BASE_URL") or "").strip().rstrip("/")
    if env:
        return env
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "https").split(",")[0].strip()
    host = (
        request.headers.get("x-forwarded-host")
        or request.headers.get("host")
        or request.url.netloc
    )
    host = (host or "").split(",")[0].strip()
    if not host:
        return str(request.base_url).rstrip("/")
    return f"{proto}://{host}".rstrip("/")


def _ensure_salon_slugs():
    db = SessionLocal()
    try:
        salons = db.query(Salon).all()
        changed = False
        for s in salons:
            current = (getattr(s, "slug", None) or "").strip()
            if not current or current.startswith("salon-"):
                s.slug = _unique_slug(db, s.name, s.id)
                changed = True
        if changed:
            db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


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


def get_active_salon(request: Request, db: Session = Depends(get_db)) -> Salon:
    """Authenticated salon that is active (not pending)."""
    salon = get_current_salon(request, db)
    if getattr(salon, "status", "active") not in ("active", "Active", None):
        if getattr(salon, "status", "") == "pending":
            raise SalonNotActiveException(salon)
    return salon


# ---------------------------------------------------------------------------
# Availability helpers
# ---------------------------------------------------------------------------

def _service_duration(db: Session, service_id: int) -> int:
    svc = db.query(Service).filter(Service.id == service_id).first()
    return int(svc.duration_minutes) if svc and svc.duration_minutes else 30


def _within_staff_hours(db: Session, salon: Salon, staff: Staff, start: datetime, end: datetime) -> bool:
    """True if [start, end) falls inside staff (or salon) working hours that day."""
    if not staff:
        return False
    off = (
        db.query(StaffDayOff)
        .filter(StaffDayOff.staff_id == staff.id, StaffDayOff.off_date == start.date())
        .first()
    )
    if off:
        return False

    dow = start.weekday()
    days = staff.effective_working_days(salon)
    day_hours = getattr(staff, "day_hours", None) or {}
    if isinstance(day_hours, dict) and str(dow) in day_hours:
        pass
    elif days and dow not in days:
        return False

    open_t, close_t = staff.effective_hours(salon)
    if isinstance(day_hours, dict) and str(dow) in day_hours:
        dh = day_hours[str(dow)] or {}
        if dh.get("open"):
            open_t = _parse_time_hhmm(dh["open"]) or open_t
        if dh.get("close"):
            close_t = _parse_time_hhmm(dh["close"]) or close_t

    start_m = start.hour * 60 + start.minute
    end_m = end.hour * 60 + end.minute
    open_m = open_t.hour * 60 + open_t.minute
    close_m = close_t.hour * 60 + close_t.minute
    return start_m >= open_m and end_m <= close_m


def _staff_has_overlap(db: Session, salon_id: int, staff_id: int, start: datetime, end: datetime) -> bool:
    rows = (
        db.query(Appointment)
        .filter(
            Appointment.salon_id == salon_id,
            Appointment.staff_id == staff_id,
            Appointment.status.in_([
                AppointmentStatus.confirmed, AppointmentStatus.pending_payment,
                "Confirmed", "Pending Payment",
            ]),
        )
        .all()
    )
    for a in rows:
        a_start = a.appointment_datetime
        dur = _service_duration(db, a.service_id) if a.service_id else 30
        a_end = a_start + timedelta(minutes=dur)
        if start < a_end and end > a_start:
            return True
    return False


def _available_staff_for_slot(
    db,
    salon,
    start,
    end,
    service_id=None,
    exclude_staff_id=None,
):
    """Staff free in [start, end). Optionally only those who offer service_id."""
    out = []
    for st in db.query(Staff).filter(Staff.salon_id == salon.id).all():
        if exclude_staff_id is not None and st.id == exclude_staff_id:
            continue
        if service_id is not None and not st.offers_service(service_id):
            continue
        if _within_staff_hours(db, salon, st, start, end) and not _staff_has_overlap(
            db, salon.id, st.id, start, end
        ):
            out.append(st)
    return out


def _next_available_slot(db, salon, staff, duration, after_dt):
    """Scan forward in 15-min steps for next free slot for this staff."""
    cursor = after_dt + timedelta(minutes=SLOT_STEP_MINUTES)
    snap = (cursor.minute // SLOT_STEP_MINUTES) * SLOT_STEP_MINUTES
    cursor = cursor.replace(minute=snap, second=0, microsecond=0)
    for _ in range(96 * 14):
        end = cursor + timedelta(minutes=duration)
        if _within_staff_hours(db, salon, staff, cursor, end) and not _staff_has_overlap(
            db, salon.id, staff.id, cursor, end
        ):
            return cursor
        cursor += timedelta(minutes=SLOT_STEP_MINUTES)
    return None


def _build_conflict_context(
    db,
    salon,
    *,
    conflict_name=None,
    conflict_phone=None,
    conflict_service=None,
    conflict_staff=None,
    conflict_time=None,
    conflict_staff_name=None,
):
    """Shared context for public + dashboard conflict panels."""
    ctx = {
        "conflict_name": conflict_name or "",
        "conflict_phone": conflict_phone or "",
        "conflict_service": conflict_service,
        "conflict_staff": conflict_staff,
        "conflict_staff_name": conflict_staff_name or "",
        "conflict_time": conflict_time or "",
        "conflict_date": None,
        "alt_staff": [],
        "next_slot": None,
        "next_slot_display": None,
    }
    if not conflict_time:
        return ctx
    try:
        conflict_dt = _parse_appt_datetime(conflict_time)
    except ValueError:
        return ctx

    duration = _service_duration(db, conflict_service) if conflict_service else 30
    c_end = conflict_dt + timedelta(minutes=duration)

    conflict_staff_obj = None
    if conflict_staff:
        conflict_staff_obj = (
            db.query(Staff)
            .filter(Staff.id == conflict_staff, Staff.salon_id == salon.id)
            .first()
        )
        if conflict_staff_obj and not ctx["conflict_staff_name"]:
            ctx["conflict_staff_name"] = conflict_staff_obj.name

    alt_staff = _available_staff_for_slot(
        db,
        salon,
        conflict_dt,
        c_end,
        service_id=conflict_service,
        exclude_staff_id=conflict_staff,
    )

    next_slot = None
    if conflict_staff_obj:
        next_slot = _next_available_slot(
            db, salon, conflict_staff_obj, duration, conflict_dt
        )

    ctx.update({
        "conflict_date": conflict_dt.date().isoformat(),
        "alt_staff": alt_staff,
        "next_slot": next_slot.strftime("%Y-%m-%dT%H:%M") if next_slot else None,
        "next_slot_display": (_dual_time_display(next_slot) if next_slot else None),
    })
    return ctx


def _revenue_between(db: Session, salon_id: int, start_dt: datetime, end_dt: datetime) -> float:
    """Revenue counts ONLY completed appointments (not confirmed / pending)."""
    rows = (
        db.query(Appointment)
        .filter(
            Appointment.salon_id == salon_id,
            Appointment.status.in_([
                AppointmentStatus.completed,
                "Completed",
            ]),
            Appointment.appointment_datetime >= start_dt,
            Appointment.appointment_datetime < end_dt,
        )
        .all()
    )
    # Prefer snapshot / package-aware service_price property
    return float(sum(float(a.service_price or 0) for a in rows))


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

@app.get("/")
def root():
    return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, error: Optional[str] = None, registered: Optional[str] = None):
    return templates.TemplateResponse(
        request, "login.html", {"error": error, "registered": registered}
    )


@app.post("/login")
async def login_submit(
    request: Request,
    phone: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    key = f"login:{request.client.host if request.client else 'x'}:{phone}"
    if _is_rate_limited(key):
        return templates.TemplateResponse(
            request, "login.html", {"error": "rate_limited"}, status_code=429
        )
    salon = db.query(Salon).filter(Salon.phone == phone.strip()).first()
    if not salon or not verify_password(password, salon.hashed_password):
        _record_attempt(key)
        return templates.TemplateResponse(
            request, "login.html", {"error": "invalid"}, status_code=401
        )
    token = create_access_token({"sub": str(salon.id)})
    resp = RedirectResponse(url="/dashboard?tab=home", status_code=status.HTTP_303_SEE_OTHER)
    resp.set_cookie("access_token", token, httponly=True, samesite="lax")
    return resp


@app.get("/logout")
def logout():
    resp = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    resp.delete_cookie("access_token")
    return resp


@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request, error: Optional[str] = None):
    return templates.TemplateResponse(request, "register.html", {"error": error})


@app.post("/register")
async def register_submit(
    request: Request,
    name: str = Form(...),
    owner_name: str = Form(...),
    phone: str = Form(...),
    password: str = Form(...),
    address: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    if db.query(Salon).filter(Salon.phone == phone.strip()).first():
        return templates.TemplateResponse(request, "register.html", {"error": "phone_taken"})
    salon = Salon(
        name=name.strip(),
        owner_name=owner_name.strip(),
        phone=phone.strip(),
        address=(address or "").strip() or None,
        hashed_password=hash_password(password),
        status="pending",
        slug=_unique_slug(db, name),
    )
    db.add(salon)
    db.commit()
    return RedirectResponse(url="/login?registered=1", status_code=status.HTTP_303_SEE_OTHER)


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
    salon: Salon = Depends(get_active_salon),
    db: Session = Depends(get_db),
    conflict_name: Optional[str] = None,
    conflict_phone: Optional[str] = None,
    conflict_service: Optional[int] = None,
    conflict_staff: Optional[int] = None,
    conflict_time: Optional[str] = None,
    conflict_staff_name: Optional[str] = None,
    unavailable_staff_name: Optional[str] = None,
    unavailable_time: Optional[str] = None,
):
    today = date.today()
    sel = _parse_date(selected_date) or today
    day_start = datetime.combine(sel, datetime.min.time())
    day_end = day_start + timedelta(days=1)

    services = db.query(Service).filter(Service.salon_id == salon.id).order_by(Service.name).all()
    staff_members = db.query(Staff).filter(Staff.salon_id == salon.id).order_by(Staff.name).all()

    appointments = (
        db.query(Appointment)
        .filter(
            Appointment.salon_id == salon.id,
            Appointment.appointment_datetime >= day_start,
            Appointment.appointment_datetime < day_end,
        )
        .order_by(Appointment.appointment_datetime)
        .all()
    )

    all_from = datetime.combine(today - timedelta(days=7), datetime.min.time())
    all_to = datetime.combine(today + timedelta(days=60), datetime.min.time())
    all_appointments = (
        db.query(Appointment)
        .filter(
            Appointment.salon_id == salon.id,
            Appointment.appointment_datetime >= all_from,
            Appointment.appointment_datetime < all_to,
        )
        .order_by(Appointment.appointment_datetime.asc())
        .limit(300)
        .all()
    )

    waitlist = (
        db.query(Waitlist)
        .filter(Waitlist.salon_id == salon.id)
        .order_by(Waitlist.preferred_date, Waitlist.id)
        .all()
    )

    daily_rev = _revenue_between(db, salon.id, day_start, day_end)
    today_appt_count = (
        db.query(func.count(Appointment.id))
        .filter(
            Appointment.salon_id == salon.id,
            Appointment.appointment_datetime >= datetime.combine(today, datetime.min.time()),
            Appointment.appointment_datetime < datetime.combine(today, datetime.min.time()) + timedelta(days=1),
        )
        .scalar()
    ) or 0

    total_customers = (
        db.query(func.count(func.distinct(Appointment.customer_phone)))
        .filter(Appointment.salon_id == salon.id)
        .scalar()
    ) or 0

    no_show_count_today = (
        db.query(func.count(Appointment.id))
        .filter(
            Appointment.salon_id == salon.id,
            Appointment.appointment_datetime >= datetime.combine(today, datetime.min.time()),
            Appointment.appointment_datetime < datetime.combine(today, datetime.min.time()) + timedelta(days=1),
            Appointment.status.in_([AppointmentStatus.no_show, "No-Show"]),
        )
        .scalar()
    ) or 0

    pending_payment_appts = (
        db.query(Appointment)
        .filter(
            Appointment.salon_id == salon.id,
            Appointment.payment_screenshot_url.isnot(None),
            Appointment.payment_reviewed == 0,
        )
        .order_by(Appointment.appointment_datetime.desc())
        .limit(20)
        .all()
    )
    pending_payment_count = len(pending_payment_appts)

    testimonials = (
        db.query(Testimonial)
        .filter(Testimonial.salon_id == salon.id)
        .order_by(Testimonial.is_pinned.desc(), Testimonial.id.desc())
        .all()
    )

    day_am, day_en = _day_names(sel)
    booking_url = f"{_public_base_url(request)}/book/{salon.slug or salon.id}"

    week_start = today - timedelta(days=today.weekday())
    week_end = week_start + timedelta(days=7)
    month_start_dt = datetime.combine(today.replace(day=1), datetime.min.time())
    if today.month == 12:
        month_end_dt = datetime(today.year + 1, 1, 1)
    else:
        month_end_dt = datetime(today.year, today.month + 1, 1)
    weekly_rev = _revenue_between(
        db, salon.id,
        datetime.combine(week_start, datetime.min.time()),
        datetime.combine(week_end, datetime.min.time()),
    )
    monthly_rev = _revenue_between(db, salon.id, month_start_dt, month_end_dt)

    revenue_trend = []
    for i in range(6, -1, -1):
        d = today - timedelta(days=i)
        amt = _revenue_between(
            db, salon.id,
            datetime.combine(d, datetime.min.time()),
            datetime.combine(d + timedelta(days=1), datetime.min.time()),
        )
        revenue_trend.append({"date": d.isoformat(), "label": d.strftime("%a"), "amount": amt})

    month_appts = (
        db.query(Appointment)
        .filter(
            Appointment.salon_id == salon.id,
            Appointment.appointment_datetime >= month_start_dt,
            Appointment.appointment_datetime < month_end_dt,
            Appointment.status.in_([
                AppointmentStatus.completed,
                "Completed",
            ]),
        )
        .all()
    )
    svc_rev: dict = {}
    staff_cnt: dict = {}
    for a in month_appts:
        sn = a.service_name or "?"
        svc_rev[sn] = svc_rev.get(sn, 0) + float(a.service_price or 0)
        stn = a.staff_name or "?"
        staff_cnt[stn] = staff_cnt.get(stn, 0) + 1
    total_svc_rev = sum(svc_rev.values()) or 1
    top_services = [
        {"name": k, "revenue": v, "pct": int(round(v / total_svc_rev * 100))}
        for k, v in sorted(svc_rev.items(), key=lambda x: -x[1])[:5]
    ]
    top_staff = [
        {"name": k, "count": v}
        for k, v in sorted(staff_cnt.items(), key=lambda x: -x[1])[:5]
    ]

    sd = _parse_date(start_date) or (today - timedelta(days=7))
    ed = _parse_date(end_date) or today
    if ed < sd:
        sd, ed = ed, sd
    custom_rev = _revenue_between(
        db, salon.id,
        datetime.combine(sd, datetime.min.time()),
        datetime.combine(ed + timedelta(days=1), datetime.min.time()),
    )
    custom_details = (
        db.query(Appointment)
        .filter(
            Appointment.salon_id == salon.id,
            Appointment.appointment_datetime >= datetime.combine(sd, datetime.min.time()),
            Appointment.appointment_datetime < datetime.combine(ed + timedelta(days=1), datetime.min.time()),
            Appointment.status.in_([
                AppointmentStatus.completed,
                "Completed",
            ]),
        )
        .order_by(Appointment.appointment_datetime)
        .all()
    )

    new_customers_month = 0
    try:
        phones_before = {
            r[0] for r in db.query(Appointment.customer_phone)
            .filter(
                Appointment.salon_id == salon.id,
                Appointment.appointment_datetime < month_start_dt,
            ).distinct().all()
        }
        phones_month = {
            r[0] for r in db.query(Appointment.customer_phone)
            .filter(
                Appointment.salon_id == salon.id,
                Appointment.appointment_datetime >= month_start_dt,
                Appointment.appointment_datetime < month_end_dt,
            ).distinct().all()
        }
        new_customers_month = len(phones_month - phones_before)
    except Exception:
        new_customers_month = 0

    context = {
        "salon": salon,
        "active_tab": tab,
        "services": services,
        "staff_members": staff_members,
        "appointments": appointments,
        "all_appointments": all_appointments,
        "waitlist": waitlist,
        "current_date": today.isoformat(),
        "selected_date": sel.isoformat(),
        "selected_day_am": day_am,
        "selected_day_en": day_en,
        "daily_rev": daily_rev,
        "today_appt_count": today_appt_count,
        "total_customers": total_customers,
        "new_customers_month": new_customers_month,
        "no_show_count_today": no_show_count_today,
        "pending_payment_appts": pending_payment_appts,
        "pending_payment_count": pending_payment_count,
        "testimonials": testimonials,
        "booking_url": booking_url,
        "error": error,
        "request": request,
        "weekly_rev": weekly_rev,
        "monthly_rev": monthly_rev,
        "revenue_trend": revenue_trend,
        "top_services": top_services,
        "top_staff": top_staff,
        "start_date": sd.isoformat(),
        "end_date": ed.isoformat(),
        "custom_rev": custom_rev,
        "custom_details": custom_details,
    }

    if error == "conflict" and conflict_time:
        context.update(
            _build_conflict_context(
                db,
                salon,
                conflict_name=conflict_name,
                conflict_phone=conflict_phone,
                conflict_service=conflict_service,
                conflict_staff=conflict_staff,
                conflict_time=conflict_time,
                conflict_staff_name=conflict_staff_name,
            )
        )

    return templates.TemplateResponse(request, "dashboard.html", context)


# ---------------------------------------------------------------------------
# Walk-in booking (dashboard)
# ---------------------------------------------------------------------------

@app.post("/book-appointment")
async def book_appointment(
    customer_name: str = Form(...),
    customer_phone: str = Form(...),
    service_id: int = Form(...),
    staff_id: int = Form(...),
    appointment_time: str = Form(...),
    party_size: Optional[int] = Form(1),
    salon: Salon = Depends(get_active_salon),
    db: Session = Depends(get_db),
):
    try:
        appt_dt = _parse_appt_datetime(appointment_time)
    except ValueError:
        return RedirectResponse(url="/dashboard?tab=home&error=invalid_time", status_code=303)

    duration = _service_duration(db, service_id)
    end_dt = appt_dt + timedelta(minutes=duration)
    svc = db.query(Service).filter(Service.id == service_id, Service.salon_id == salon.id).first()
    staff_obj = db.query(Staff).filter(Staff.id == staff_id, Staff.salon_id == salon.id).first()

    party = 1
    try:
        party = max(1, int(party_size or 1))
    except (TypeError, ValueError):
        party = 1

    if staff_obj and svc and not staff_obj.offers_service(svc.id):
        params = urlencode({
            "error": "conflict",
            "conflict_name": customer_name,
            "conflict_phone": customer_phone,
            "conflict_service": service_id,
            "conflict_staff": staff_id,
            "conflict_time": appointment_time,
            "conflict_staff_name": staff_obj.name if staff_obj else "",
            "tab": "home",
        })
        return RedirectResponse(url=f"/dashboard?{params}", status_code=303)

    allow_outside = bool(svc and getattr(svc, "allow_outside_hours", 0))
    if not staff_obj or (not allow_outside and not _within_staff_hours(db, salon, staff_obj, appt_dt, end_dt)):
        params = urlencode({
            "error": "conflict",
            "conflict_name": customer_name,
            "conflict_phone": customer_phone,
            "conflict_service": service_id,
            "conflict_staff": staff_id,
            "conflict_time": appointment_time,
            "conflict_staff_name": staff_obj.name if staff_obj else "",
            "tab": "home",
        })
        return RedirectResponse(url=f"/dashboard?{params}", status_code=303)

    if _staff_has_overlap(db, salon.id, staff_id, appt_dt, end_dt):
        params = urlencode({
            "error": "conflict",
            "conflict_name": customer_name,
            "conflict_phone": customer_phone,
            "conflict_service": service_id,
            "conflict_staff": staff_id,
            "conflict_time": appointment_time,
            "conflict_staff_name": staff_obj.name if staff_obj else "",
            "tab": "home",
        })
        return RedirectResponse(url=f"/dashboard?{params}", status_code=303)

    snap_name = svc.name if svc else None
    snap_price = float(svc.price) if svc else None
    if svc and getattr(svc, "is_package", 0) and getattr(svc, "extra_person_price", None):
        snap_price = svc.package_total(party)

    appt = Appointment(
        salon_id=salon.id,
        customer_name=customer_name.strip(),
        customer_phone=customer_phone.strip(),
        service_id=service_id,
        staff_id=staff_id,
        appointment_datetime=appt_dt,
        status=AppointmentStatus.confirmed,
        source="walk-in",
        party_size=party,
        service_name_snap=snap_name,
        service_price_snap=snap_price,
    )
    db.add(appt)
    db.commit()
    try:
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
                "status": getattr(appt.status, "value", str(appt.status)),
                "source": appt.source,
                "party_size": appt.party_size or 1,
            },
        })
    except Exception:
        pass
    return RedirectResponse(url="/dashboard?tab=home", status_code=303)



@app.post("/reschedule-appointment")
async def reschedule_appointment(
    appointment_id: int = Form(...),
    appointment_time: str = Form(...),
    staff_id: Optional[int] = Form(None),
    salon: Salon = Depends(get_active_salon),
    db: Session = Depends(get_db),
):
    """Move an existing appointment to a new date/time (and optional staff)."""
    appt = (
        db.query(Appointment)
        .filter(Appointment.id == appointment_id, Appointment.salon_id == salon.id)
        .first()
    )
    if not appt:
        return RedirectResponse(url="/dashboard?tab=home&error=not_found", status_code=303)

    st = getattr(appt.status, "value", str(appt.status))
    if st in ("Cancelled", "No-Show", "Completed"):
        return RedirectResponse(url="/dashboard?tab=home&error=cannot_reschedule", status_code=303)

    try:
        appt_dt = _parse_appt_datetime(appointment_time)
    except ValueError:
        return RedirectResponse(url="/dashboard?tab=home&error=invalid_time", status_code=303)

    new_staff_id = int(staff_id) if staff_id else appt.staff_id
    staff_obj = db.query(Staff).filter(Staff.id == new_staff_id, Staff.salon_id == salon.id).first()
    if not staff_obj:
        return RedirectResponse(url="/dashboard?tab=home&error=invalid_staff", status_code=303)

    duration = _service_duration(db, appt.service_id) if appt.service_id else 30
    end_dt = appt_dt + timedelta(minutes=duration)

    svc = None
    if appt.service_id:
        svc = db.query(Service).filter(Service.id == appt.service_id).first()
        if not staff_obj.offers_service(appt.service_id):
            return RedirectResponse(url="/dashboard?tab=home&error=staff_service", status_code=303)

    allow_outside = bool(svc and getattr(svc, "allow_outside_hours", 0))
    if not allow_outside and not _within_staff_hours(db, salon, staff_obj, appt_dt, end_dt):
        return RedirectResponse(url="/dashboard?tab=home&error=outside_hours", status_code=303)

    # Overlap excluding this appointment itself
    rows = (
        db.query(Appointment)
        .filter(
            Appointment.salon_id == salon.id,
            Appointment.staff_id == new_staff_id,
            Appointment.id != appt.id,
            Appointment.status.in_([
                AppointmentStatus.confirmed, AppointmentStatus.pending_payment,
                "Confirmed", "Pending Payment",
            ]),
        )
        .all()
    )
    for a in rows:
        a_start = a.appointment_datetime
        dur = _service_duration(db, a.service_id) if a.service_id else 30
        a_end = a_start + timedelta(minutes=dur)
        if appt_dt < a_end and end_dt > a_start:
            return RedirectResponse(url="/dashboard?tab=home&error=conflict", status_code=303)

    appt.appointment_datetime = appt_dt
    appt.staff_id = new_staff_id
    db.commit()
    return RedirectResponse(url="/dashboard?tab=home&rescheduled=1", status_code=303)


@app.post("/update-appointment-status")
async def update_appointment_status(
    request: Request,
    appointment_id: int = Form(...),
    # Dashboard forms post name="status"; accept both for compatibility
    status: Optional[str] = Form(None),
    status_value: Optional[str] = Form(None),
    salon: Salon = Depends(get_active_salon),
    db: Session = Depends(get_db),
):
    """Mark appointment Confirmed / Completed / No-Show / Cancelled / Pending Payment.

    AJAX (fetch from dashboard) gets JSON. Normal form posts get a redirect.
    """
    raw = (status or status_value or "").strip()
    if not raw:
        wants_json = (
            "application/json" in (request.headers.get("accept") or "").lower()
            or (request.headers.get("x-requested-with") or "").lower() == "xmlhttprequest"
        )
        if wants_json:
            return JSONResponse({"ok": False, "error": "missing_status"}, status_code=422)
        return RedirectResponse(url="/dashboard?tab=home&error=missing_status", status_code=303)

    appt = db.query(Appointment).filter(
        Appointment.id == appointment_id, Appointment.salon_id == salon.id
    ).first()
    if not appt:
        wants_json = (
            "application/json" in (request.headers.get("accept") or "").lower()
            or (request.headers.get("x-requested-with") or "").lower() == "xmlhttprequest"
        )
        if wants_json:
            return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
        return RedirectResponse(url="/dashboard?tab=home", status_code=303)

    resolved = None
    for e in AppointmentStatus:
        if raw == e.value or raw == e.name or raw.lower() == e.name.lower():
            resolved = e
            break
    # Common aliases
    if resolved is None:
        aliases = {
            "complete": AppointmentStatus.completed,
            "completed": AppointmentStatus.completed,
            "no show": AppointmentStatus.no_show,
            "no-show": AppointmentStatus.no_show,
            "noshow": AppointmentStatus.no_show,
            "cancel": AppointmentStatus.cancelled,
            "cancelled": AppointmentStatus.cancelled,
            "canceled": AppointmentStatus.cancelled,
            "confirm": AppointmentStatus.confirmed,
            "confirmed": AppointmentStatus.confirmed,
            "pending": AppointmentStatus.pending_payment,
            "pending payment": AppointmentStatus.pending_payment,
            "pending_payment": AppointmentStatus.pending_payment,
        }
        resolved = aliases.get(raw.lower())

    if resolved is not None:
        appt.status = resolved
    else:
        # Last resort: store as-is (AppointmentStatusType will coerce known values)
        appt.status = raw

    db.commit()
    db.refresh(appt)
    final = getattr(appt.status, "value", str(appt.status))

    wants_json = (
        "application/json" in (request.headers.get("accept") or "").lower()
        or (request.headers.get("x-requested-with") or "").lower() == "xmlhttprequest"
    )
    if wants_json:
        return JSONResponse({"ok": True, "id": appt.id, "status": final})
    return RedirectResponse(url="/dashboard?tab=home", status_code=303)


@app.post("/add-waitlist")
async def add_waitlist(
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
        customer_name=customer_name.strip(),
        customer_phone=customer_phone.strip(),
        service_id=service_id,
        staff_id=staff_id or None,
        preferred_date=_parse_date(preferred_date) or date.today(),
    ))
    db.commit()
    return RedirectResponse(url="/dashboard?tab=reserve", status_code=303)


# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------

@app.post("/add-service")
async def add_service(
    name: str = Form(...),
    price: float = Form(...),
    duration_minutes: int = Form(...),
    deposit_amount: float = Form(0),
    is_package: Optional[str] = Form(None),
    min_people: Optional[int] = Form(1),
    max_people: Optional[int] = Form(None),
    includes_text: Optional[str] = Form(None),
    allow_outside_hours: Optional[str] = Form(None),
    extra_person_price: Optional[float] = Form(None),
    photo: Optional[UploadFile] = File(None),
    salon: Salon = Depends(get_active_salon),
    db: Session = Depends(get_db),
):
    photo_url = None
    if photo and photo.filename:
        try:
            photo_url = _save_upload(photo, subfolder=f"services/{salon.id}", salon_id=salon.id)
        except ValueError:
            photo_url = None
    svc = Service(
        salon_id=salon.id,
        name=name.strip(),
        price=max(0.0, float(price)),
        duration_minutes=max(5, int(duration_minutes)),
        deposit_amount=max(0.0, float(deposit_amount or 0)),
        is_package=1 if is_package in ("1", "on", "true", "yes") else 0,
        min_people=max(1, int(min_people or 1)),
        max_people=int(max_people) if max_people else None,
        includes_text=(includes_text or "").strip() or None,
        allow_outside_hours=1 if allow_outside_hours in ("1", "on", "true", "yes") else 0,
        extra_person_price=float(extra_person_price) if extra_person_price else None,
        photo_url=photo_url,
        is_active=1,
    )
    db.add(svc)
    db.commit()
    return RedirectResponse(url="/dashboard?tab=services", status_code=303)


@app.post("/delete-service")
def delete_service(
    service_id: int = Form(...),
    force: Optional[str] = Form(None),
    salon: Salon = Depends(get_active_salon),
    db: Session = Depends(get_db),
):
    svc = db.query(Service).filter(Service.id == service_id, Service.salon_id == salon.id).first()
    if not svc:
        return RedirectResponse(url="/dashboard?tab=services", status_code=303)

    appt_count = (
        db.query(func.count(Appointment.id))
        .filter(Appointment.service_id == service_id, Appointment.salon_id == salon.id)
        .scalar()
    ) or 0

    if appt_count > 0 and force not in ("1", "on", "true", "yes"):
        return RedirectResponse(
            url="/dashboard?tab=services&error=service_in_use",
            status_code=303,
        )

    if appt_count > 0:
        # Soft-archive + snapshot existing appts
        for a in db.query(Appointment).filter(
            Appointment.service_id == service_id, Appointment.salon_id == salon.id
        ).all():
            if not a.service_name_snap:
                a.service_name_snap = svc.name
            if a.service_price_snap is None:
                a.service_price_snap = svc.price
            a.service_id = None
        svc.is_active = 0
        try:
            db.commit()
            return RedirectResponse(
                url="/dashboard?tab=services&error=service_archived",
                status_code=303,
            )
        except Exception:
            db.rollback()
            return RedirectResponse(
                url="/dashboard?tab=services&error=service_in_use",
                status_code=303,
            )

    try:
        db.delete(svc)
        db.commit()
    except IntegrityError:
        db.rollback()
        svc.is_active = 0
        db.commit()
        return RedirectResponse(
            url="/dashboard?tab=services&error=service_archived",
            status_code=303,
        )
    return RedirectResponse(url="/dashboard?tab=services", status_code=303)


@app.post("/update-service")
async def update_service(
    service_id: int = Form(...),
    name: str = Form(...),
    price: float = Form(...),
    duration_minutes: int = Form(...),
    deposit_amount: float = Form(0),
    salon: Salon = Depends(get_active_salon),
    db: Session = Depends(get_db),
):
    svc = (
        db.query(Service)
        .filter(Service.id == service_id, Service.salon_id == salon.id)
        .first()
    )
    if svc:
        svc.name = (name or "").strip() or svc.name
        try:
            svc.price = max(0.0, float(price))
        except (TypeError, ValueError):
            pass
        try:
            svc.duration_minutes = max(5, int(duration_minutes))
        except (TypeError, ValueError):
            pass
        try:
            svc.deposit_amount = max(0.0, float(deposit_amount or 0))
        except (TypeError, ValueError):
            pass
        db.commit()
    return RedirectResponse(url="/dashboard?tab=services", status_code=303)


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
        redirect = RedirectResponse(url="/login?error=session_expired", status_code=303)
        redirect.delete_cookie("access_token")
        return redirect
    return RedirectResponse(url="/dashboard?tab=staff", status_code=303)


@app.post("/update-staff-photo")
async def update_staff_photo(
    staff_id: int = Form(...),
    photo: UploadFile = File(...),
    salon: Salon = Depends(get_active_salon),
    db: Session = Depends(get_db),
):
    staff = db.query(Staff).filter(Staff.id == staff_id, Staff.salon_id == salon.id).first()
    if not staff:
        return RedirectResponse(url="/dashboard?tab=staff", status_code=303)
    try:
        staff.photo_url = _save_upload(photo, subfolder=f"staff/{salon.id}", salon_id=salon.id)
        db.commit()
    except ValueError:
        return RedirectResponse(url="/dashboard?tab=staff&error=invalid_image", status_code=303)
    return RedirectResponse(url="/dashboard?tab=staff", status_code=303)


@app.post("/delete-staff")
def delete_staff(
    staff_id: int = Form(...),
    salon: Salon = Depends(get_active_salon),
    db: Session = Depends(get_db),
):
    staff = (
        db.query(Staff)
        .filter(Staff.id == staff_id, Staff.salon_id == salon.id)
        .first()
    )
    if not staff:
        return RedirectResponse(url="/dashboard?tab=staff", status_code=303)

    appt_count = (
        db.query(func.count(Appointment.id))
        .filter(Appointment.staff_id == staff_id, Appointment.salon_id == salon.id)
        .scalar()
    ) or 0
    if appt_count > 0:
        return RedirectResponse(
            url="/dashboard?tab=staff&error=staff_in_use",
            status_code=303,
        )

    db.query(Waitlist).filter(
        Waitlist.staff_id == staff_id, Waitlist.salon_id == salon.id
    ).update({Waitlist.staff_id: None}, synchronize_session=False)

    db.query(StaffDayOff).filter(StaffDayOff.staff_id == staff_id).delete(
        synchronize_session=False
    )

    try:
        db.delete(staff)
        db.commit()
    except IntegrityError:
        db.rollback()
        return RedirectResponse(
            url="/dashboard?tab=staff&error=staff_in_use",
            status_code=303,
        )
    return RedirectResponse(url="/dashboard?tab=staff", status_code=303)


@app.post("/update-staff-services")
async def update_staff_services(
    staff_id: int = Form(...),
    service_ids: List[int] = Form(default=[]),
    all_services: Optional[str] = Form(None),
    salon: Salon = Depends(get_active_salon),
    db: Session = Depends(get_db),
):
    staff = db.query(Staff).filter(Staff.id == staff_id, Staff.salon_id == salon.id).first()
    if not staff:
        return RedirectResponse(url="/dashboard?tab=staff", status_code=303)

    if all_services in ("1", "on", "true", "yes") or not service_ids:
        staff.service_ids = None
    else:
        valid = {
            s.id for s in db.query(Service).filter(Service.salon_id == salon.id).all()
        }
        staff.service_ids = [int(i) for i in service_ids if int(i) in valid]

    db.commit()
    return RedirectResponse(url="/dashboard?tab=staff", status_code=303)


@app.post("/update-hours")
async def update_hours(
    opening_time: str = Form(...),
    closing_time: str = Form(...),
    working_days: List[str] = Form(default=[]),
    salon: Salon = Depends(get_active_salon),
    db: Session = Depends(get_db),
):
    open_t = _parse_time_hhmm(opening_time)
    close_t = _parse_time_hhmm(closing_time)
    if not open_t or not close_t:
        return RedirectResponse(url="/dashboard?tab=settings&error=invalid_hours", status_code=303)
    open_m = open_t.hour * 60 + open_t.minute
    close_m = close_t.hour * 60 + close_t.minute
    if close_m <= open_m:
        return RedirectResponse(url="/dashboard?tab=settings&error=invalid_hours", status_code=303)
    days = sorted({int(d) for d in working_days if str(d).isdigit() and 0 <= int(d) <= 6})
    if not days:
        return RedirectResponse(url="/dashboard?tab=settings&error=invalid_days", status_code=303)
    salon.opening_time = open_t
    salon.closing_time = close_t
    salon.working_days = ",".join(str(d) for d in days)
    db.commit()
    return RedirectResponse(url="/dashboard?tab=settings", status_code=303)


@app.post("/update-staff-schedule")
async def update_staff_schedule(
    request: Request,
    staff_id: int = Form(...),
    use_custom_hours: Optional[str] = Form(None),
    use_custom_days: Optional[str] = Form(None),
    opening_time: Optional[str] = Form(None),
    closing_time: Optional[str] = Form(None),
    working_days: List[str] = Form(default=[]),
    salon: Salon = Depends(get_active_salon),
    db: Session = Depends(get_db),
):
    staff = db.query(Staff).filter(Staff.id == staff_id, Staff.salon_id == salon.id).first()
    if not staff:
        return RedirectResponse(url="/dashboard?tab=staff", status_code=303)

    form = await request.form()

    if use_custom_hours in ("1", "on", "true", "yes"):
        staff.opening_time = _parse_time_hhmm(opening_time)
        staff.closing_time = _parse_time_hhmm(closing_time)
    else:
        staff.opening_time = None
        staff.closing_time = None

    if use_custom_days in ("1", "on", "true", "yes"):
        days = sorted({int(d) for d in working_days if str(d).isdigit() and 0 <= int(d) <= 6})
        staff.working_days = ",".join(str(d) for d in days) if days else None
        day_hours = {}
        enabled = set(days)
        for i in range(7):
            if enabled and i not in enabled:
                continue
            o = form.get(f"day_open_{i}")
            c = form.get(f"day_close_{i}")
            if o and c:
                day_hours[str(i)] = {"open": str(o)[:5], "close": str(c)[:5]}
        staff.day_hours = day_hours if day_hours else None
    else:
        staff.working_days = None
        staff.day_hours = None

    db.commit()
    return RedirectResponse(url="/dashboard?tab=staff", status_code=303)


@app.post("/add-staff-dayoff")
async def add_staff_dayoff(
    staff_id: int = Form(...),
    off_date: str = Form(...),
    salon: Salon = Depends(get_active_salon),
    db: Session = Depends(get_db),
):
    staff = db.query(Staff).filter(Staff.id == staff_id, Staff.salon_id == salon.id).first()
    if not staff:
        return RedirectResponse(url="/dashboard?tab=staff", status_code=303)
    d = _parse_date(off_date)
    if not d:
        return RedirectResponse(url="/dashboard?tab=staff", status_code=303)
    exists = (
        db.query(StaffDayOff)
        .filter(StaffDayOff.staff_id == staff_id, StaffDayOff.off_date == d)
        .first()
    )
    if not exists:
        db.add(StaffDayOff(staff_id=staff_id, off_date=d))
        db.commit()
    return RedirectResponse(url="/dashboard?tab=staff", status_code=303)


@app.post("/delete-staff-dayoff")
def delete_staff_dayoff(
    dayoff_id: int = Form(...),
    salon: Salon = Depends(get_active_salon),
    db: Session = Depends(get_db),
):
    row = (
        db.query(StaffDayOff)
        .join(Staff, Staff.id == StaffDayOff.staff_id)
        .filter(StaffDayOff.id == dayoff_id, Staff.salon_id == salon.id)
        .first()
    )
    if row:
        db.delete(row)
        db.commit()
    return RedirectResponse(url="/dashboard?tab=staff", status_code=303)


@app.post("/convert-waitlist/{waitlist_id}")
async def convert_waitlist(
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
        return RedirectResponse(url="/dashboard?tab=reserve", status_code=303)
    try:
        appt_dt = _parse_appt_datetime(appointment_time)
    except ValueError:
        try:
            appt_dt = datetime.combine(entry.preferred_date, datetime.strptime("09:00", "%H:%M").time())
        except Exception:
            return RedirectResponse(url="/dashboard?tab=reserve&error=invalid_time", status_code=303)

    duration = _service_duration(db, entry.service_id)
    end_dt = appt_dt + timedelta(minutes=duration)
    staff_id = entry.staff_id
    if not staff_id:
        free = _available_staff_for_slot(
            db, salon, appt_dt, end_dt, service_id=entry.service_id
        )
        if free:
            staff_id = free[0].id
    if not staff_id:
        return RedirectResponse(url="/dashboard?tab=reserve&error=staff_unavailable", status_code=303)

    if _staff_has_overlap(db, salon.id, staff_id, appt_dt, end_dt):
        return RedirectResponse(url="/dashboard?tab=reserve&error=conflict", status_code=303)

    db.add(Appointment(
        salon_id=salon.id,
        customer_name=entry.customer_name,
        customer_phone=entry.customer_phone,
        service_id=entry.service_id,
        staff_id=staff_id,
        appointment_datetime=appt_dt,
        status=AppointmentStatus.confirmed,
        source="waitlist",
    ))
    db.delete(entry)
    db.commit()
    return RedirectResponse(url="/dashboard?tab=home", status_code=303)


@app.post("/delete-waitlist")
def delete_waitlist(
    waitlist_id: int = Form(...),
    salon: Salon = Depends(get_active_salon),
    db: Session = Depends(get_db),
):
    entry = (
        db.query(Waitlist)
        .filter(Waitlist.id == waitlist_id, Waitlist.salon_id == salon.id)
        .first()
    )
    if entry:
        db.delete(entry)
        db.commit()
    return RedirectResponse(url="/dashboard?tab=reserve", status_code=303)


@app.get("/export-revenue")
def export_revenue(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    salon: Salon = Depends(get_active_salon),
    db: Session = Depends(get_db),
):
    today = date.today()
    sd = _parse_date(start_date) or (today - timedelta(days=7))
    ed = _parse_date(end_date) or today
    if ed < sd:
        sd, ed = ed, sd
    rows = (
        db.query(Appointment)
        .filter(
            Appointment.salon_id == salon.id,
            Appointment.appointment_datetime >= datetime.combine(sd, datetime.min.time()),
            Appointment.appointment_datetime < datetime.combine(ed + timedelta(days=1), datetime.min.time()),
            Appointment.status.in_([
                AppointmentStatus.completed,
                "Completed",
            ]),
        )
        .order_by(Appointment.appointment_datetime)
        .all()
    )
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["date", "time", "customer", "phone", "service", "staff", "price_etb", "party_size", "status"])
    for a in rows:
        w.writerow([
            a.appointment_datetime.date().isoformat(),
            a.appointment_time,
            a.customer_name,
            a.customer_phone,
            a.service_name,
            a.staff_name,
            f"{a.service_price:.2f}",
            a.party_size or 1,
            getattr(a.status, "value", str(a.status)),
        ])
    buf.seek(0)
    filename = f"revenue_{sd.isoformat()}_{ed.isoformat()}.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/upload-cover-photo")
async def upload_cover_photo(
    photo: UploadFile = File(...),
    salon: Salon = Depends(get_active_salon),
    db: Session = Depends(get_db),
):
    try:
        url = _save_upload(photo, subfolder=f"cover/{salon.id}", salon_id=salon.id)
    except ValueError:
        return RedirectResponse(url="/dashboard?tab=settings&error=invalid_image", status_code=303)
    salon.cover_photo_url = url
    db.commit()
    return RedirectResponse(url="/dashboard?tab=settings", status_code=303)


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
        return RedirectResponse(url="/dashboard?tab=settings&error=invalid_image", status_code=303)
    allowed = {"nails", "manicure", "pedicure", "massage", "facial", "hair", "other"}
    cat = (category or "other").strip().lower()
    if cat not in allowed:
        cat = "other"
    db.add(GalleryImage(
        salon_id=salon.id, image_url=url,
        caption=(caption or "").strip() or None, category=cat,
    ))
    db.commit()
    return RedirectResponse(url="/dashboard?tab=settings", status_code=303)


@app.post("/delete-gallery-photo")
def delete_gallery_photo(
    image_id: int = Form(...),
    salon: Salon = Depends(get_active_salon),
    db: Session = Depends(get_db),
):
    img = db.query(GalleryImage).filter(
        GalleryImage.id == image_id, GalleryImage.salon_id == salon.id
    ).first()
    if img:
        db.delete(img)
        db.commit()
    return RedirectResponse(url="/dashboard?tab=settings", status_code=303)


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
    return RedirectResponse(url="/dashboard?tab=settings", status_code=303)


@app.post("/toggle-testimonial-pin")
def toggle_testimonial_pin(
    testimonial_id: int = Form(...),
    salon: Salon = Depends(get_active_salon),
    db: Session = Depends(get_db),
):
    t = db.query(Testimonial).filter(
        Testimonial.id == testimonial_id, Testimonial.salon_id == salon.id
    ).first()
    if t:
        t.is_pinned = 0 if t.is_pinned else 1
        db.commit()
    return RedirectResponse(url="/dashboard?tab=settings", status_code=303)


@app.post("/delete-testimonial")
def delete_testimonial(
    testimonial_id: int = Form(...),
    salon: Salon = Depends(get_active_salon),
    db: Session = Depends(get_db),
):
    t = db.query(Testimonial).filter(
        Testimonial.id == testimonial_id, Testimonial.salon_id == salon.id
    ).first()
    if t:
        db.delete(t)
        db.commit()
    return RedirectResponse(url="/dashboard?tab=settings", status_code=303)


@app.post("/settings/deposit")
async def settings_deposit(
    deposit_enabled: Optional[str] = Form(None),
    salon: Salon = Depends(get_active_salon),
    db: Session = Depends(get_db),
):
    salon.deposit_enabled = 1 if deposit_enabled in ("1", "on", "true", "yes") else 0
    db.commit()
    return RedirectResponse(url="/dashboard?tab=settings", status_code=303)


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
    return RedirectResponse(url="/dashboard?tab=settings", status_code=303)


@app.post("/dismiss-payment-proof")
def dismiss_payment_proof(
    appointment_id: int = Form(...),
    salon: Salon = Depends(get_active_salon),
    db: Session = Depends(get_db),
):
    appt = db.query(Appointment).filter(
        Appointment.id == appointment_id, Appointment.salon_id == salon.id
    ).first()
    if appt:
        appt.payment_reviewed = 1
        db.commit()
    return RedirectResponse(url="/dashboard?tab=home", status_code=303)


@app.post("/confirm-deposit-payment")
def confirm_deposit_payment(
    appointment_id: int = Form(...),
    salon: Salon = Depends(get_active_salon),
    db: Session = Depends(get_db),
):
    appt = db.query(Appointment).filter(
        Appointment.id == appointment_id, Appointment.salon_id == salon.id
    ).first()
    if appt:
        st = getattr(appt.status, "value", str(appt.status))
        if st in ("Pending Payment", "pending_payment", AppointmentStatus.pending_payment.value):
            appt.status = AppointmentStatus.confirmed
            db.commit()
    return RedirectResponse(url="/dashboard?tab=home", status_code=303)


@app.get("/search-customer")
def search_customer(q: str = "", salon: Salon = Depends(get_active_salon), db: Session = Depends(get_db)):
    q = (q or "").strip()
    if len(q) < 2:
        return JSONResponse({"results": []})
    rows = (
        db.query(Appointment)
        .filter(
            Appointment.salon_id == salon.id,
            or_(
                Appointment.customer_name.ilike(f"%{q}%"),
                Appointment.customer_phone.ilike(f"%{q}%"),
            ),
        )
        .order_by(Appointment.appointment_datetime.desc())
        .limit(20)
        .all()
    )
    results = []
    seen = set()
    for a in rows:
        key = (a.customer_phone, a.appointment_datetime.date().isoformat())
        if key in seen:
            continue
        seen.add(key)
        day_am, _ = _day_names(a.appointment_datetime.date())
        results.append({
            "customer_name": a.customer_name,
            "customer_phone": a.customer_phone,
            "appointment_date": a.appointment_datetime.date().isoformat(),
            "day_am": day_am or "",
        })
    return JSONResponse({"results": results})


# ---------------------------------------------------------------------------
# Public booking
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
    public_path = (salon.slug or "").strip() or str(salon.id)
    services = db.query(Service).filter(Service.salon_id == salon.id).order_by(Service.name).all()
    services = [s for s in services if getattr(s, "is_active", 1) != 0]
    staff_members = db.query(Staff).filter(Staff.salon_id == salon.id).order_by(Staff.name).all()
    testimonials = (
        db.query(Testimonial)
        .filter(Testimonial.salon_id == salon.id)
        .order_by(Testimonial.is_pinned.desc(), Testimonial.id.desc())
        .limit(20)
        .all()
    )
    gallery = (
        db.query(GalleryImage)
        .filter(GalleryImage.salon_id == salon.id)
        .order_by(GalleryImage.id.desc())
        .limit(40)
        .all()
    )
    context = {
        "salon": salon,
        "salon_ref": public_path,
        "services": services,
        "staff_members": staff_members,
        "gallery": gallery,
        "current_date": date.today().isoformat(),
        "error": error,
        "success": success,
        "waitlisted": waitlisted,
        "hours_label": salon.hours_label,
        "days_label": salon.working_days_label,
        "testimonials": testimonials,
        "appt_id": appt_id,
    }
    if error == "conflict" and conflict_time:
        context.update(
            _build_conflict_context(
                db,
                salon,
                conflict_name=conflict_name,
                conflict_phone=conflict_phone,
                conflict_service=conflict_service,
                conflict_staff=conflict_staff,
                conflict_time=conflict_time,
            )
        )
    return templates.TemplateResponse(request, "public_booking.html", context)


@app.post("/book/{salon_ref}")
async def public_booking_submit(
    salon_ref: str,
    customer_name: str = Form(...),
    customer_phone: str = Form(...),
    service_id: int = Form(...),
    staff_id: int = Form(...),
    appointment_time: str = Form(...),
    party_size: Optional[int] = Form(1),
    payment_method: Optional[str] = Form(None),
    payment_screenshot: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db),
):
    salon = _resolve_salon(db, salon_ref)
    if not salon:
        return HTMLResponse("Salon not found", status_code=404)
    public_path = (salon.slug or "").strip() or str(salon.id)

    def _conflict_redirect():
        params = urlencode({
            "error": "conflict",
            "conflict_name": customer_name,
            "conflict_phone": customer_phone,
            "conflict_service": service_id,
            "conflict_staff": staff_id if staff_id else 0,
            "conflict_time": appointment_time,
        })
        return RedirectResponse(url=f"/book/{public_path}?{params}", status_code=303)

    try:
        appt_dt = _parse_appt_datetime(appointment_time)
    except ValueError:
        return _conflict_redirect()

    duration = _service_duration(db, service_id)
    end_dt = appt_dt + timedelta(minutes=duration)
    svc = db.query(Service).filter(Service.id == service_id, Service.salon_id == salon.id).first()
    staff_obj = None

    # staff_id == 0 → any available who offers this service
    if not staff_id or int(staff_id) == 0:
        free = _available_staff_for_slot(
            db, salon, appt_dt, end_dt, service_id=service_id
        )
        if not free:
            return _conflict_redirect()
        staff_obj = free[0]
        staff_id = staff_obj.id
    else:
        staff_obj = db.query(Staff).filter(Staff.id == staff_id, Staff.salon_id == salon.id).first()

    party = 1
    try:
        party = max(1, int(party_size or 1))
    except (TypeError, ValueError):
        party = 1
    if svc:
        mn = int(getattr(svc, "min_people", None) or 1)
        mx = getattr(svc, "max_people", None)
        if party < mn:
            party = mn
        if mx:
            party = min(party, int(mx))

    if staff_obj and svc and not staff_obj.offers_service(svc.id):
        return _conflict_redirect()

    allow_outside = bool(svc and getattr(svc, "allow_outside_hours", 0))
    if not staff_obj or (not allow_outside and not _within_staff_hours(db, salon, staff_obj, appt_dt, end_dt)):
        return _conflict_redirect()

    if _staff_has_overlap(db, salon.id, staff_id, appt_dt, end_dt):
        return _conflict_redirect()

    deposit_amt = float(svc.deposit_amount or 0) if svc else 0.0
    needs_deposit = bool(salon.deposit_enabled) and deposit_amt > 0
    screenshot_url = None
    pay_method = (payment_method or "").strip() or None

    # Conflict one-click rebook may lack screenshot → pending payment instead of hard fail
    if needs_deposit:
        if payment_screenshot and payment_screenshot.filename:
            try:
                screenshot_url = _save_upload(
                    payment_screenshot, subfolder=f"payments/{salon.id}", salon_id=salon.id
                )
            except ValueError:
                return RedirectResponse(url=f"/book/{public_path}?error=invalid_image", status_code=303)
        # if no screenshot: still book as pending_payment (better UX on conflict recovery)

    status_val = AppointmentStatus.confirmed
    if needs_deposit and not screenshot_url:
        status_val = AppointmentStatus.pending_payment

    snap_name = svc.name if svc else None
    snap_price = float(svc.price) if svc else None
    if svc and getattr(svc, "is_package", 0):
        snap_price = svc.package_total(party)

    appt = Appointment(
        salon_id=salon.id,
        customer_name=customer_name,
        customer_phone=customer_phone,
        service_id=service_id,
        staff_id=staff_id,
        appointment_datetime=appt_dt,
        status=status_val,
        source="online",
        deposit_amount=deposit_amt if needs_deposit else 0,
        payment_method=pay_method,
        payment_screenshot_url=screenshot_url,
        party_size=party,
        service_name_snap=snap_name,
        service_price_snap=snap_price,
    )
    db.add(appt)
    try:
        db.commit()
        db.refresh(appt)
    except Exception:
        db.rollback()
        return RedirectResponse(url=f"/book/{public_path}?error=booking_failed", status_code=303)

    try:
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
                "status": getattr(appt.status, "value", str(appt.status)),
                "source": appt.source,
                "party_size": appt.party_size or 1,
            },
        })
    except Exception:
        pass

    return RedirectResponse(
        url=f"/book/{public_path}?success=1&appt_id={appt.id}",
        status_code=303,
    )


@app.get("/book/{salon_ref}/my-bookings")
def public_my_bookings(
    salon_ref: str,
    phone: str = "",
    db: Session = Depends(get_db),
):
    salon = _resolve_salon(db, salon_ref)
    if not salon:
        return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)

    phone = (phone or "").strip()
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) < 9:
        return JSONResponse({"ok": False, "error": "phone_short", "results": []})

    tail = digits[-9:]
    rows = (
        db.query(Appointment)
        .filter(
            Appointment.salon_id == salon.id,
            Appointment.customer_phone.isnot(None),
        )
        .order_by(Appointment.appointment_datetime.desc())
        .limit(80)
        .all()
    )
    results = []
    now = datetime.now()
    for a in rows:
        ap_digits = "".join(ch for ch in (a.customer_phone or "") if ch.isdigit())
        if not ap_digits or tail not in ap_digits:
            continue
        st = getattr(a.status, "value", str(a.status))
        is_upcoming = a.appointment_datetime >= now - timedelta(hours=2)
        results.append({
            "id": a.id,
            "customer_name": a.customer_name,
            "date": a.appointment_datetime.strftime("%Y-%m-%d"),
            "time_label": a.appointment_time,
            "time_label_western": getattr(a, "appointment_time_western", None) or _western_display(a.appointment_datetime),
            "time_label_dual": f"{a.appointment_time} · {_western_display(a.appointment_datetime)}",
            "service_name": a.service_name,
            "staff_name": a.staff_name,
            "status": st,
            "party_size": a.party_size or 1,
            "upcoming": bool(is_upcoming and st not in ("Cancelled", "No-Show", "Completed")),
        })
        if len(results) >= 20:
            break

    return JSONResponse({"ok": True, "results": results, "count": len(results)})


@app.get("/book/{salon_ref}/status/{appointment_id}")
def public_booking_status(salon_ref: str, appointment_id: int, db: Session = Depends(get_db)):
    salon = _resolve_salon(db, salon_ref)
    if not salon:
        return JSONResponse({"ok": False, "status": "not_found"}, status_code=404)
    appt = db.query(Appointment).filter(
        Appointment.id == appointment_id, Appointment.salon_id == salon.id
    ).first()
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
    return RedirectResponse(url=f"/book/{public_path}?waitlisted=1", status_code=303)


@app.get("/privacy", response_class=HTMLResponse)
def privacy(request: Request):
    return templates.TemplateResponse(request, "privacy.html", {})


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)