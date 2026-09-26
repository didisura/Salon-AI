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
from sqlalchemy.orm.attributes import flag_modified

from database import Base, engine, get_db, SessionLocal
from models import (
    Salon, Service, Staff, StaffDayOff, Appointment, Waitlist,
    AppointmentStatus, GalleryImage, Testimonial, MediaAsset,
    Expense, EXPENSE_CATEGORIES,
)
from security import (
    hash_password,
    verify_password,
    create_access_token,
    create_admin_token,
    get_current_salon,
    get_current_admin,
    get_root_salon,
    get_salon_tree_ids,
    NotAuthenticatedException,
)

Base.metadata.create_all(bind=engine)


# ---------------------------------------------------------------------------
# Schema migrations (idempotent)
# ---------------------------------------------------------------------------

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
                if row and (row[0] == "USER-DEFINED" or (row[1] and "appointment" in str(row[1]).lower())):
                    conn.execute(text(
                        "ALTER TABLE appointments "
                        "ALTER COLUMN status TYPE VARCHAR(30) "
                        "USING status::text"
                    ))
        except Exception:
            pass


_ensure_photo_columns()


def _ensure_package_columns():
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

def _ensure_service_category_columns():
    """Service.category + Salon.service_categories (custom list per salon)."""
    from sqlalchemy import inspect, text
    inspector = inspect(engine)
    dialect = engine.dialect.name
    str_type = "VARCHAR(80)" if dialect == "postgresql" else "TEXT"
    jtype = "JSON" if dialect == "postgresql" else "TEXT"

    try:
        svc_cols = [c["name"] for c in inspector.get_columns("services")]
    except Exception:
        svc_cols = []
    if svc_cols and "category" not in svc_cols:
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE services ADD COLUMN category {str_type}"))

    try:
        salon_cols = [c["name"] for c in inspector.get_columns("salons")]
    except Exception:
        salon_cols = []
    if salon_cols and "service_categories" not in salon_cols:
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE salons ADD COLUMN service_categories {jtype}"))


_ensure_service_category_columns()

def _ensure_waitlist_screenshot_column():
    """Allow payment proof to travel with waitlist entries after a conflict."""
    from sqlalchemy import inspect, text
    inspector = inspect(engine)
    dialect = engine.dialect.name
    str_type = "VARCHAR(500)" if dialect == "postgresql" else "TEXT"
    try:
        cols = [c["name"] for c in inspector.get_columns("waitlist")]
    except Exception:
        cols = []
    if cols and "payment_screenshot_url" not in cols:
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE waitlist ADD COLUMN payment_screenshot_url {str_type}"))
    if cols and "payment_method" not in cols:
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE waitlist ADD COLUMN payment_method {str_type}"))


_ensure_waitlist_screenshot_column()



def _ensure_staff_service_ids_column():
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


def _ensure_branch_columns():
    """Multi-location: parent_id + location_name on salons."""
    from sqlalchemy import inspect, text
    inspector = inspect(engine)
    dialect = engine.dialect.name
    try:
        cols = [c["name"] for c in inspector.get_columns("salons")]
    except Exception:
        return
    with engine.begin() as conn:
        if "parent_id" not in cols:
            # SQLite / Postgres both accept this form
            conn.execute(text("ALTER TABLE salons ADD COLUMN parent_id INTEGER"))
            try:
                if dialect == "postgresql":
                    conn.execute(text(
                        "ALTER TABLE salons ADD CONSTRAINT fk_salons_parent "
                        "FOREIGN KEY (parent_id) REFERENCES salons(id)"
                    ))
            except Exception:
                pass
        if "location_name" not in cols:
            str_type = "VARCHAR(80)" if dialect == "postgresql" else "TEXT"
            conn.execute(text(f"ALTER TABLE salons ADD COLUMN location_name {str_type}"))


_ensure_branch_columns()


def _ensure_admin_audit_table():
    """Idempotent create for admin_audit_log (works on SQLite + Postgres)."""
    from sqlalchemy import inspect, text
    inspector = inspect(engine)
    try:
        tables = inspector.get_table_names()
    except Exception:
        return
    if "admin_audit_log" in tables:
        return
    dialect = engine.dialect.name
    if dialect == "postgresql":
        ddl = """
        CREATE TABLE IF NOT EXISTS admin_audit_log (
            id SERIAL PRIMARY KEY,
            action VARCHAR(80) NOT NULL,
            target_type VARCHAR(40),
            target_id INTEGER,
            target_name VARCHAR(200),
            details TEXT,
            ip VARCHAR(60),
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
        """
    else:
        ddl = """
        CREATE TABLE IF NOT EXISTS admin_audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action VARCHAR(80) NOT NULL,
            target_type VARCHAR(40),
            target_id INTEGER,
            target_name VARCHAR(200),
            details TEXT,
            ip VARCHAR(60),
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    try:
        with engine.begin() as conn:
            conn.execute(text(ddl))
    except Exception:
        pass


_ensure_admin_audit_table()


def _admin_audit(
    db: Session,
    *,
    action: str,
    target_type: Optional[str] = None,
    target_id: Optional[int] = None,
    target_name: Optional[str] = None,
    details: Optional[str] = None,
    ip: Optional[str] = None,
) -> None:
    """Append one admin audit row. Never raises into the request path."""
    from sqlalchemy import text
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO admin_audit_log "
                    "(action, target_type, target_id, target_name, details, ip) "
                    "VALUES (:action, :tt, :tid, :tn, :details, :ip)"
                ),
                {
                    "action": (action or "")[:80],
                    "tt": (target_type or None),
                    "tid": target_id,
                    "tn": (target_name or None) and str(target_name)[:200],
                    "details": details,
                    "ip": (ip or None) and str(ip)[:60],
                },
            )
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass


def _client_ip(request: Optional[Request]) -> Optional[str]:
    if not request:
        return None
    xff = request.headers.get("x-forwarded-for") or ""
    if xff:
        return xff.split(",")[0].strip()[:60]
    if request.client:
        return (request.client.host or "")[:60]
    return None


def _verify_admin_totp(otp: Optional[str]) -> bool:
    """If ADMIN_TOTP_SECRET is set, require a valid TOTP code. Else pass."""
    secret = ADMIN_TOTP_SECRET
    if not secret:
        return True
    code = (otp or "").strip().replace(" ", "")
    if not code.isdigit() or len(code) not in (6, 8):
        return False
    try:
        import hmac
        import struct
        import time
        import base64
        # Minimal TOTP (RFC 6238) — 30s window, SHA1, 6 digits
        key = base64.b32decode(secret.upper().replace(" ", "") + "=" * ((8 - len(secret) % 8) % 8))
        timestep = int(time.time()) // 30
        for w in (0, -1, 1):  # allow ±1 step clock skew
            msg = struct.pack(">Q", timestep + w)
            h = hmac.new(key, msg, "sha1").digest()
            o = h[-1] & 0x0F
            trunc = struct.unpack(">I", h[o : o + 4])[0] & 0x7FFFFFFF
            expected = f"{trunc % (10 ** 6):06d}"
            if hmac.compare_digest(expected, code[-6:].zfill(6)):
                return True
        return False
    except Exception:
        return False


def _valid_admin_credentials(key: Optional[str], otp: Optional[str] = None) -> bool:
    """Accept ADMIN_SECRET_KEY or ADMIN_PASSWORD_HASH; enforce TOTP when configured."""
    if not key:
        return False
    key = key.strip()
    ok = False
    if _valid_admin_key(key):
        ok = True
    elif ADMIN_PASSWORD_HASH:
        try:
            if verify_password(key, ADMIN_PASSWORD_HASH):
                ok = True
        except Exception:
            ok = False
    if not ok:
        return False
    return _verify_admin_totp(otp)



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
# Optional hashed admin password (bcrypt via security.hash_password). If set, login accepts
# either ADMIN_SECRET_KEY or this password. Prefer setting ADMIN_PASSWORD_HASH in production.
ADMIN_PASSWORD_HASH = (os.environ.get("ADMIN_PASSWORD_HASH") or "").strip()
# TOTP secret for optional 2FA (base32). When set, admin login requires `otp` form field.
ADMIN_TOTP_SECRET = (os.environ.get("ADMIN_TOTP_SECRET") or "").strip()

SLOT_STEP_MINUTES = 15

# Short-lived payment proofs when booking hits a conflict then joins waitlist
# key: "{salon_id}:{phone}" → (timestamp, screenshot_url, payment_method)
_pending_payment_proofs: dict = {}
_PENDING_PROOF_TTL = 60 * 60  # 1 hour


def _proof_key(salon_id, phone: str) -> str:
    digits = "".join(c for c in str(phone or "") if c.isdigit())
    return f"{salon_id}:{digits}"


def _store_pending_proof(salon_id, phone: str, screenshot_url: Optional[str], pay_method: Optional[str] = None) -> None:
    if not screenshot_url:
        return
    _pending_payment_proofs[_proof_key(salon_id, phone)] = (time.time(), screenshot_url, pay_method)


def _take_pending_proof(salon_id, phone: str):
    """Pop pending proof if present and not expired."""
    key = _proof_key(salon_id, phone)
    row = _pending_payment_proofs.pop(key, None)
    if not row:
        return None, None
    ts, url, method = row
    if time.time() - ts > _PENDING_PROOF_TTL:
        return None, None
    return url, method



def _cookie_secure() -> bool:
    """True in production HTTPS. Set COOKIE_SECURE=0 for local HTTP dev."""
    explicit = (os.environ.get("COOKIE_SECURE") or "").strip().lower()
    if explicit in ("0", "false", "no", "off"):
        return False
    if explicit in ("1", "true", "yes", "on"):
        return True
    # Auto: secure when PUBLIC_BASE_URL is https
    base = (os.environ.get("PUBLIC_BASE_URL") or "").strip().lower()
    return base.startswith("https://")


def _set_auth_cookie(resp, key: str, value: str, max_age: Optional[int] = None) -> None:
    """HttpOnly + SameSite=Lax + Secure (when HTTPS) for access_token / admin_token."""
    kwargs = dict(
        key=key,
        value=value,
        httponly=True,
        samesite="lax",
        secure=_cookie_secure(),
        path="/",
    )
    if max_age is not None:
        kwargs["max_age"] = max_age
    resp.set_cookie(**kwargs)


def _clear_auth_cookie(resp, key: str) -> None:
    resp.delete_cookie(key, path="/")

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


DEFAULT_SERVICE_CATEGORIES = [
    "Hair",
    "Nails",
    "Makeup",
    "Spa",
    "Massage",
    "Waxing",
    "Skincare",
    "Other",
]


def _salon_service_categories(salon) -> list:
    """Return ordered unique category names for this salon (custom or defaults)."""
    raw = getattr(salon, "service_categories", None)
    if isinstance(raw, str):
        try:
            import json
            raw = json.loads(raw)
        except Exception:
            raw = None
    if isinstance(raw, list) and raw:
        out = []
        for c in raw:
            s = str(c or "").strip()[:80]
            if s and s not in out:
                out.append(s)
        return out if out else list(DEFAULT_SERVICE_CATEGORIES)
    return list(DEFAULT_SERVICE_CATEGORIES)



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
    """Live events for a salon. Requires owner JWT; salon_id must be in their tree."""
    auth = _salon_id_from_ws_cookie(websocket)
    if not auth:
        await websocket.close(code=4401)
        return
    active_id, root_id = auth
    # Allow listening only to own tree (HQ or any branch the owner controls)
    db = SessionLocal()
    try:
        allowed = get_salon_tree_ids(db, root_id)
        if int(salon_id) not in allowed:
            await websocket.close(code=4403)
            return
    finally:
        db.close()

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
    """Ensure every salon has a slug. HQ with a place name gets brand+place URL
    so public booking links are never ambiguous next to branches (/bole, /cmc)."""
    db = SessionLocal()
    try:
        salons = db.query(Salon).all()
        changed = False
        for s in salons:
            current = (getattr(s, "slug", None) or "").strip()
            place = (getattr(s, "location_name", None) or "").strip()
            is_root = getattr(s, "parent_id", None) is None

            # Preferred source for slug
            if is_root and place:
                brand = (s.name or "").split(" — ")[0].strip() or (s.name or f"salon-{s.id}")
                source = f"{brand} {place}"
            elif not is_root:
                source = s.name or f"branch-{s.id}"
            else:
                source = s.name or f"salon-{s.id}"

            desired = _slugify_name(source)
            # Rebuild if missing, generic, or HQ place not reflected in slug
            needs = (
                not current
                or current.startswith("salon-")
                or (is_root and place and place.lower().replace(" ", "-") not in current
                    and _slugify_name(place) not in current)
            )
            if needs and desired:
                s.slug = _unique_slug(db, source, s.id)
                changed = True
            elif not current:
                s.slug = _unique_slug(db, source or f"salon-{s.id}", s.id)
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
    """Authenticated salon that is active (not pending) — returns ACTIVE LOCATION."""
    salon = get_current_salon(request, db)
    # Approval lives on the root business account
    root = salon.root_salon() if hasattr(salon, "root_salon") else salon
    status_val = getattr(root, "status", "active")
    if status_val not in ("active", "Active", None):
        if status_val == "pending":
            raise SalonNotActiveException(root)
    return salon


def _locations_for(db: Session, salon: Salon) -> list:
    root = salon.root_salon() if hasattr(salon, "root_salon") else salon
    rows = (
        db.query(Salon)
        .filter(or_(Salon.id == root.id, Salon.parent_id == root.id))
        .order_by(Salon.parent_id.nullsfirst() if hasattr(Salon.parent_id, "nullsfirst") else Salon.id)
        .all()
    )
    # Fallback sort if nullsfirst not available
    rows = sorted(rows, key=lambda s: (0 if s.parent_id is None else 1, (s.location_name or s.name or "").lower()))
    return rows



# ---------------------------------------------------------------------------
# Ownership / authorization guards (IDOR protection)
# Every resource mutation MUST go through these — never trust client IDs alone.
# ---------------------------------------------------------------------------

class ForbiddenResource(Exception):
    """Raised when a salon tries to touch another salon's row."""
    pass


def _owned_appointment(db: Session, appointment_id: int, salon_id: int):
    """Return appointment only if it belongs to this salon; else None."""
    return (
        db.query(Appointment)
        .filter(Appointment.id == appointment_id, Appointment.salon_id == salon_id)
        .first()
    )


def _owned_staff(db: Session, staff_id: int, salon_id: int):
    return (
        db.query(Staff)
        .filter(Staff.id == staff_id, Staff.salon_id == salon_id)
        .first()
    )


def _owned_service(db: Session, service_id: int, salon_id: int):
    return (
        db.query(Service)
        .filter(Service.id == service_id, Service.salon_id == salon_id)
        .first()
    )


def _owned_waitlist(db: Session, waitlist_id: int, salon_id: int):
    return (
        db.query(Waitlist)
        .filter(Waitlist.id == waitlist_id, Waitlist.salon_id == salon_id)
        .first()
    )


def _owned_gallery(db: Session, image_id: int, salon_id: int):
    return (
        db.query(GalleryImage)
        .filter(GalleryImage.id == image_id, GalleryImage.salon_id == salon_id)
        .first()
    )


def _owned_testimonial(db: Session, testimonial_id: int, salon_id: int):
    return (
        db.query(Testimonial)
        .filter(Testimonial.id == testimonial_id, Testimonial.salon_id == salon_id)
        .first()
    )


def _owned_dayoff(db: Session, dayoff_id: int, salon_id: int):
    """Day-off belongs to staff that belongs to this salon."""
    return (
        db.query(StaffDayOff)
        .join(Staff, Staff.id == StaffDayOff.staff_id)
        .filter(StaffDayOff.id == dayoff_id, Staff.salon_id == salon_id)
        .first()
    )


def _assert_location_in_tree(db: Session, location_id: int, root_id: int) -> bool:
    """True if location_id is the root or a direct branch of root."""
    return int(location_id) in get_salon_tree_ids(db, root_id)


def _salon_id_from_ws_cookie(websocket) -> Optional[int]:
    """Extract active_salon_id (or root sub) from access_token cookie on WS handshake."""
    try:
        cookie_header = websocket.headers.get("cookie") or ""
        token = None
        for part in cookie_header.split(";"):
            part = part.strip()
            if part.startswith("access_token="):
                token = part[len("access_token="):]
                break
        if not token:
            return None
        from security import decode_access_token
        payload = decode_access_token(token)
        if not payload or "sub" not in payload:
            return None
        root_id = int(payload["sub"])
        active_raw = payload.get("active_salon_id", root_id)
        try:
            active_id = int(active_raw)
        except (TypeError, ValueError):
            active_id = root_id
        return active_id, root_id
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Availability helpers
# ---------------------------------------------------------------------------

def _service_duration(db: Session, service_id: int, salon_id: Optional[int] = None) -> int:
    q = db.query(Service).filter(Service.id == service_id)
    if salon_id is not None:
        q = q.filter(Service.salon_id == salon_id)
    svc = q.first()
    return int(svc.duration_minutes) if svc and svc.duration_minutes else 30


def _within_staff_hours(db: Session, salon: Salon, staff: Staff, start: datetime, end: datetime) -> bool:
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


def _available_staff_for_slot(db, salon, start, end, service_id=None, exclude_staff_id=None):
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
        db, salon, conflict_dt, c_end,
        service_id=conflict_service, exclude_staff_id=conflict_staff,
    )

    next_slot = None
    if conflict_staff_obj:
        next_slot = _next_available_slot(db, salon, conflict_staff_obj, duration, conflict_dt)

    ctx.update({
        "conflict_date": conflict_dt.date().isoformat(),
        "alt_staff": alt_staff,
        "next_slot": next_slot.strftime("%Y-%m-%dT%H:%M") if next_slot else None,
        "next_slot_display": (_dual_time_display(next_slot) if next_slot else None),
    })
    return ctx


def _revenue_between(db: Session, salon_id: int, start_dt: datetime, end_dt: datetime) -> float:
    rows = (
        db.query(Appointment)
        .filter(
            Appointment.salon_id == salon_id,
            Appointment.status.in_([AppointmentStatus.completed, "Completed"]),
            Appointment.appointment_datetime >= start_dt,
            Appointment.appointment_datetime < end_dt,
        )
        .all()
    )
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
    # Only roots (or independent salons) should login with real phones.
    # If somehow a branch has a password match, climb to root for identity.
    root = salon.root_salon() if hasattr(salon, "root_salon") else salon
    if salon.parent_id is not None:
        # Branch account — treat login as root, land on this branch
        root = salon.root_salon()
        active_id = salon.id
    else:
        root = salon
        active_id = salon.id

    token = create_access_token({
        "sub": str(root.id),
        "active_salon_id": str(active_id),
    })
    resp = RedirectResponse(url="/dashboard?tab=home", status_code=status.HTTP_303_SEE_OTHER)
    _set_auth_cookie(resp, "access_token", token)
    return resp


@app.get("/logout")
def logout():
    resp = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    _clear_auth_cookie(resp, "access_token")
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
        parent_id=None,
        location_name=None,
    )
    db.add(salon)
    db.commit()
    return RedirectResponse(url="/login?registered=1", status_code=status.HTTP_303_SEE_OTHER)


# ---------------------------------------------------------------------------
# Location switcher + add branch
# ---------------------------------------------------------------------------

@app.post("/switch-location")
async def switch_location(
    request: Request,
    location_id: int = Form(...),
    next: Optional[str] = Form("/dashboard?tab=home"),
    db: Session = Depends(get_db),
):
    token = request.cookies.get("access_token")
    if not token:
        return RedirectResponse(url="/login", status_code=303)
    try:
        from security import decode_access_token
        payload = decode_access_token(token)
        if not payload or "sub" not in payload:
            return RedirectResponse(url="/login", status_code=303)
        root_id = int(payload["sub"])
    except Exception:
        return RedirectResponse(url="/login", status_code=303)

    allowed = get_salon_tree_ids(db, root_id)
    if int(location_id) not in allowed:
        return RedirectResponse(url="/dashboard?tab=home&error=invalid_location", status_code=303)

    new_token = create_access_token({
        "sub": str(root_id),
        "active_salon_id": str(location_id),
    })
    next_url = next or "/dashboard?tab=home"
    if not str(next_url).startswith("/"):
        next_url = "/dashboard?tab=home"

    # Notify admin when owner starts using a branch (not HQ)
    try:
        loc = db.query(Salon).filter(Salon.id == int(location_id)).first()
        root = db.query(Salon).filter(Salon.id == root_id).first()
        if loc and loc.parent_id is not None:
            _admin_audit(
                db,
                action="branch_in_use",
                target_type="salon",
                target_id=loc.id,
                target_name=loc.name or loc.location_name,
                details=(
                    f"Owner switched dashboard to branch "
                    f"'{loc.location_name or loc.name}' "
                    f"(HQ: {root.name if root else root_id}, root_id={root_id})"
                ),
                ip=_client_ip(request),
            )
    except Exception:
        pass

    resp = RedirectResponse(url=str(next_url), status_code=303)
    _set_auth_cookie(resp, "access_token", new_token)
    return resp


@app.post("/add-location")
async def add_location(
    request: Request,
    location_name: str = Form(...),
    address: Optional[str] = Form(None),
    copy_services: Optional[str] = Form(None),
    inherit_payments: Optional[str] = Form(None),
    hq_location_name: Optional[str] = Form(None),
    salon: Salon = Depends(get_active_salon),
    db: Session = Depends(get_db),
):
    """Create a branch. If HQ has no place name yet, set it from hq_location_name."""
    root = salon.root_salon() if hasattr(salon, "root_salon") else salon
    name = (location_name or "").strip()
    if not name:
        return RedirectResponse(url="/dashboard?tab=settings&error=location_name", status_code=303)

    # Ensure HQ always has a place label + location-style public URL
    # so /book/brand-bole is never confused with branches /book/brand-cmc
    brand = (root.name or "").split(" — ")[0].strip() or root.name or "Salon"
    if " — " in (root.name or ""):
        brand = root.name.split(" — ")[0].strip() or brand
    root.name = brand  # keep HQ display name as pure brand

    hq_place = (root.location_name or "").strip()
    if not hq_place:
        hq_place = (hq_location_name or "").strip() or "HQ"
        root.location_name = hq_place
    # Always refresh HQ booking slug to include the place
    try:
        root.slug = _unique_slug(db, f"{brand} {hq_place}", root.id)
    except Exception:
        try:
            root.slug = _unique_slug(db, f"salon-{root.id}-{hq_place}", root.id)
        except Exception:
            pass

    # Synthetic unique phone — branches do not log in with real numbers
    branch_phone = f"branch-{root.id}-{uuid.uuid4().hex[:10]}"
    branch = Salon(
        name=f"{brand} — {name}",
        location_name=name,
        owner_name=root.owner_name,
        phone=branch_phone,
        hashed_password=hash_password(secrets.token_urlsafe(24)),
        address=(address or "").strip() or None,
        status="active",
        parent_id=root.id,
        opening_time=root.opening_time,
        closing_time=root.closing_time,
        working_days=root.working_days,
        slug=_unique_slug(db, f"{brand}-{name}"),
        deposit_enabled=int(root.deposit_enabled or 0) if inherit_payments else 0,
        payment_methods=(list(root.payment_methods or []) if inherit_payments else None),
    )
    db.add(branch)
    db.flush()

    if copy_services in ("1", "on", "true", "yes"):
        for s in db.query(Service).filter(Service.salon_id == root.id).all():
            if getattr(s, "is_active", 1) == 0:
                continue
            db.add(Service(
                salon_id=branch.id,
                name=s.name,
                price=s.price,
                duration_minutes=s.duration_minutes,
                deposit_amount=s.deposit_amount,
                is_package=s.is_package,
                min_people=s.min_people,
                max_people=s.max_people,
                includes_text=s.includes_text,
                allow_outside_hours=s.allow_outside_hours,
                extra_person_price=s.extra_person_price,
                is_active=1,
            ))

    db.commit()

    # Billable signal: owner added a paid branch — surface on admin HQ
    try:
        _admin_audit(
            db,
            action="branch_created",
            target_type="salon",
            target_id=branch.id,
            target_name=branch.name,
            details=(
                f"NEW BRANCH (billable) · place={name} · "
                f"HQ={brand} (id={root.id}) · phone={root.phone} · "
                f"owner={root.owner_name} · "
                f"copy_services={bool(copy_services in ('1','on','true','yes'))} · "
                f"slug={branch.slug}"
            ),
            ip=_client_ip(request),
        )
    except Exception:
        pass

    return RedirectResponse(
        url=f"/dashboard?tab=settings&location_added={branch.id}",
        status_code=303,
    )


@app.post("/update-location")
async def update_location(
    location_id: int = Form(...),
    location_name: str = Form(...),
    address: Optional[str] = Form(None),
    brand_name: Optional[str] = Form(None),
    salon: Salon = Depends(get_active_salon),
    db: Session = Depends(get_db),
):
    """Edit HQ or any branch: place name, address; HQ can also rename the brand.
    Always regenerates a location-style public booking slug so HQ URLs look like
    /book/brand-bole just like branches /book/brand-cmc.
    """
    root = salon.root_salon() if hasattr(salon, "root_salon") else salon
    allowed = get_salon_tree_ids(db, root.id)
    if int(location_id) not in allowed:
        return RedirectResponse(url="/dashboard?tab=settings&error=invalid_location", status_code=303)

    loc = db.query(Salon).filter(Salon.id == location_id).first()
    if not loc:
        return RedirectResponse(url="/dashboard?tab=settings&error=invalid_location", status_code=303)

    place = (location_name or "").strip()
    if not place:
        return RedirectResponse(url="/dashboard?tab=settings&error=location_name", status_code=303)

    loc.location_name = place
    loc.address = (address or "").strip() or None

    if loc.parent_id is None:
        # --- HQ ---
        brand = (brand_name or "").strip()
        if not brand:
            brand = (loc.name or "").strip()
        if " — " in brand:
            brand = brand.split(" — ")[0].strip()
        if not brand:
            brand = "Salon"
        loc.name = brand  # pure brand name on HQ

        # Sync all branch display names: "Brand — Place"
        for b in db.query(Salon).filter(Salon.parent_id == loc.id).all():
            b_place = (b.location_name or "").strip()
            if not b_place and b.name and " — " in (b.name or ""):
                b_place = b.name.split(" — ", 1)[-1].strip()
            b_place = b_place or "Branch"
            b.location_name = b_place
            b.name = f"{brand} — {b_place}"
            try:
                b.slug = _unique_slug(db, f"{brand} {b_place}", b.id)
            except Exception:
                try:
                    b.slug = _unique_slug(db, f"branch-{b.id}-{b_place}", b.id)
                except Exception:
                    pass

        # HQ public URL MUST include the place (same pattern as branches)
        slug_source = f"{brand} {place}"
    else:
        # --- Branch ---
        brand = (root.name or "").split(" — ")[0].strip() or root.name or "Salon"
        loc.name = f"{brand} — {place}"
        slug_source = f"{brand} {place}"

    # Always refresh this location's public booking slug
    try:
        loc.slug = _unique_slug(db, slug_source, loc.id)
    except Exception:
        try:
            loc.slug = _unique_slug(db, f"salon-{loc.id}-{place}", loc.id)
        except Exception:
            pass

    db.commit()
    return RedirectResponse(url="/dashboard?tab=settings&location_updated=1", status_code=303)



@app.post("/delete-location")
async def delete_location(
    location_id: int = Form(...),
    force: Optional[str] = Form(None),
    salon: Salon = Depends(get_active_salon),
    db: Session = Depends(get_db),
):
    """Delete a branch only (never HQ). Blocks if appointments exist unless force=1."""
    root = salon.root_salon() if hasattr(salon, "root_salon") else salon
    allowed = get_salon_tree_ids(db, root.id)
    if int(location_id) not in allowed:
        return RedirectResponse(url="/dashboard?tab=settings&error=invalid_location", status_code=303)

    loc = db.query(Salon).filter(Salon.id == location_id).first()
    if not loc:
        return RedirectResponse(url="/dashboard?tab=settings&error=invalid_location", status_code=303)
    if loc.parent_id is None or loc.id == root.id:
        return RedirectResponse(url="/dashboard?tab=settings&error=cannot_delete_hq", status_code=303)

    appt_count = (
        db.query(func.count(Appointment.id))
        .filter(Appointment.salon_id == loc.id)
        .scalar()
    ) or 0
    if appt_count > 0 and force not in ("1", "on", "true", "yes"):
        return RedirectResponse(
            url="/dashboard?tab=settings&error=location_has_bookings",
            status_code=303,
        )

    # Clean dependent rows for this branch only
    staff_ids = [r[0] for r in db.query(Staff.id).filter(Staff.salon_id == loc.id).all()]
    if staff_ids:
        db.query(StaffDayOff).filter(StaffDayOff.staff_id.in_(staff_ids)).delete(synchronize_session=False)
    db.query(Waitlist).filter(Waitlist.salon_id == loc.id).delete(synchronize_session=False)
    db.query(GalleryImage).filter(GalleryImage.salon_id == loc.id).delete(synchronize_session=False)
    db.query(Testimonial).filter(Testimonial.salon_id == loc.id).delete(synchronize_session=False)
    if appt_count > 0:
        # Soft-clear FKs then delete appointments
        db.query(Appointment).filter(Appointment.salon_id == loc.id).delete(synchronize_session=False)
    db.query(Service).filter(Service.salon_id == loc.id).delete(synchronize_session=False)
    db.query(Staff).filter(Staff.salon_id == loc.id).delete(synchronize_session=False)

    was_active = salon.id == loc.id
    db.delete(loc)
    db.commit()

    # If user was viewing the deleted branch, switch cookie to HQ
    if was_active:
        token = create_access_token({"sub": str(root.id), "active_salon_id": str(root.id)})
        resp = RedirectResponse(url="/dashboard?tab=settings&location_deleted=1", status_code=303)
        _set_auth_cookie(resp, "access_token", token)
        return resp

    return RedirectResponse(url="/dashboard?tab=settings&location_deleted=1", status_code=303)


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
    location_added: Optional[int] = None,
    location_updated: Optional[str] = None,
    location_deleted: Optional[str] = None,
):
    today = date.today()
    sel = _parse_date(selected_date) or today
    day_start = datetime.combine(sel, datetime.min.time())
    day_end = day_start + timedelta(days=1)

    # Active services for booking UI; full list (incl. archived) for services tab management
    services_all = db.query(Service).filter(Service.salon_id == salon.id).order_by(Service.name).all()
    services = [s for s in services_all if getattr(s, "is_active", 1) != 0]
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

    # Waitlist for this location; if HQ root, also show branch entries so nothing is "missing"
    _wl_salon_ids = [salon.id]
    try:
        if not getattr(salon, "parent_id", None):
            _branch_ids = [
                r[0]
                for r in db.query(Salon.id)
                .filter(Salon.parent_id == salon.id)
                .all()
            ]
            _wl_salon_ids.extend(_branch_ids)
    except Exception:
        pass
    waitlist = (
        db.query(Waitlist)
        .filter(Waitlist.salon_id.in_(_wl_salon_ids))
        .order_by(Waitlist.id.desc())
        .all()
    )
    # Hydrate payment proof columns (may exist in DB before models.py is updated)
    if waitlist:
        try:
            from sqlalchemy import text
            ids = [w.id for w in waitlist]
            if ids:
                # portable IN clause
                placeholders = ",".join(str(int(i)) for i in ids)
                rows = db.execute(
                    text(
                        f"SELECT id, payment_screenshot_url, payment_method FROM waitlist "
                        f"WHERE id IN ({placeholders})"
                    )
                ).fetchall()
                by_id = {int(r[0]): r for r in rows}
                for w in waitlist:
                    row = by_id.get(int(w.id))
                    if not row:
                        continue
                    if row[1]:
                        try:
                            setattr(w, "payment_screenshot_url", row[1])
                        except Exception:
                            pass
                    if len(row) > 2 and row[2]:
                        try:
                            setattr(w, "payment_method", row[2])
                        except Exception:
                            pass
        except Exception:
            pass

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
    # Keep public booking URL location-aware for HQ (brand-place), matching branches
    try:
        root_for_slug = salon.root_salon() if hasattr(salon, "root_salon") else salon
        place = (getattr(salon, "location_name", None) or "").strip()
        if salon.parent_id is None and place:
            brand = (salon.name or "").split(" — ")[0].strip() or salon.name or f"salon-{salon.id}"
            desired_src = f"{brand} {place}"
            desired = _slugify_name(desired_src)
            current = (salon.slug or "").strip()
            if not current or (_slugify_name(place) not in current and place.lower().replace(" ", "-") not in current):
                salon.slug = _unique_slug(db, desired_src, salon.id)
                db.commit()
        elif salon.parent_id is not None and place:
            brand = (root_for_slug.name or "").split(" — ")[0].strip() or root_for_slug.name or "Salon"
            desired_src = f"{brand} {place}"
            current = (salon.slug or "").strip()
            if not current or _slugify_name(place) not in current:
                salon.slug = _unique_slug(db, desired_src, salon.id)
                db.commit()
    except Exception:
        db.rollback()

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
            Appointment.status.in_([AppointmentStatus.completed, "Completed"]),
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
            Appointment.status.in_([AppointmentStatus.completed, "Completed"]),
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

    # Multi-location context
    locations = _locations_for(db, salon)
    root = salon.root_salon() if hasattr(salon, "root_salon") else salon

    context = {
        "salon": salon,
        "active_tab": tab,
        "services": services,
        "services_all": services_all,  # includes archived (is_active=0) for services tab
        "service_categories": _salon_service_categories(salon),
        "staff_members": staff_members,
        "appointments": appointments,
        "all_appointments": all_appointments,
        "waitlist": waitlist,
        "waitlist_entries": waitlist,  # dashboard template iterates waitlist_entries
        "current_date": today.isoformat(),
        "selected_date": sel.isoformat(),
        "selected_day_am": day_am,
        "selected_day_en": day_en,
        "daily_rev": daily_rev,
        "weekly_rev": weekly_rev,
        "monthly_rev": monthly_rev,
        "today_appt_count": today_appt_count,
        "total_customers": total_customers,
        "no_show_count_today": no_show_count_today,
        "pending_payment_appts": pending_payment_appts,
        "pending_payment_count": pending_payment_count,
        "testimonials": testimonials,
        "booking_url": booking_url,
        "revenue_trend": revenue_trend,
        "top_services": top_services,
        "top_staff": top_staff,
        "custom_rev": custom_rev,
        "custom_details": custom_details,
        "start_date": sd.isoformat(),
        "end_date": ed.isoformat(),
        "new_customers_month": new_customers_month,
        "error": error,
        "location_added": location_added,
        "location_updated": location_updated,
        "location_deleted": location_deleted,
        # Multi-branch
        "locations": locations,
        "active_location_id": salon.id,
        "root_salon": root,
        "brand_name": root.name,
        "is_multi_location": len(locations) > 1,
    }

    if conflict_time:
        context.update(
            _build_conflict_context(
                db, salon,
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
# Bookings (walk-in from dashboard) — abbreviated core; keep your existing
# handlers and they already filter by salon.id from get_active_salon
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

    svc = _owned_service(db, service_id, salon.id)
    staff_obj = _owned_staff(db, staff_id, salon.id)
    if not staff_obj or not svc:
        return RedirectResponse(url="/dashboard?tab=home&error=invalid", status_code=303)

    duration = _service_duration(db, service_id, salon_id=salon.id)
    end_dt = appt_dt + timedelta(minutes=duration)

    allow_outside = bool(svc and getattr(svc, "allow_outside_hours", 0))
    if not allow_outside and not _within_staff_hours(db, salon, staff_obj, appt_dt, end_dt):
        return RedirectResponse(
            url=f"/dashboard?tab=home&error=unavailable&unavailable_staff_name={staff_obj.name}",
            status_code=303,
        )
    if _staff_has_overlap(db, salon.id, staff_id, appt_dt, end_dt):
        params = urlencode({
            "error": "conflict",
            "conflict_name": customer_name,
            "conflict_phone": customer_phone,
            "conflict_service": service_id,
            "conflict_staff": staff_id,
            "conflict_time": appointment_time,
        })
        return RedirectResponse(url=f"/dashboard?tab=home&{params}", status_code=303)

    party = max(1, int(party_size or 1))
    snap_price = float(svc.price) if svc else None
    if svc and getattr(svc, "is_package", 0):
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
        service_name_snap=svc.name if svc else None,
        service_price_snap=snap_price,
    )
    db.add(appt)
    db.commit()
    db.refresh(appt)

    try:
        await manager.broadcast(salon.id, {
            "event": "new_booking",
            "appointment": {
                "id": appt.id,
                "customer_name": appt.customer_name,
                "service_name": appt.service_name,
                "staff_name": appt.staff_name,
                "status": "Confirmed",
            },
        })
    except Exception:
        pass

    return RedirectResponse(url="/dashboard?tab=home", status_code=303)


@app.post("/update-appointment-status")
async def update_appointment_status(
    appointment_id: int = Form(...),
    status_value: Optional[str] = Form(None),
    status: Optional[str] = Form(None),
    salon: Salon = Depends(get_active_salon),
    db: Session = Depends(get_db),
    request: Request = None,
):
    appt = _owned_appointment(db, appointment_id, salon.id)
    if not appt:
        return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)

    new_status = status_value or status or ""
    mapping = {
        "Completed": AppointmentStatus.completed,
        "Cancelled": AppointmentStatus.cancelled,
        "No-Show": AppointmentStatus.no_show,
        "Confirmed": AppointmentStatus.confirmed,
        "Pending Payment": AppointmentStatus.pending_payment,
    }
    if new_status in mapping:
        appt.status = mapping[new_status]
        db.commit()

    # AJAX clients get JSON
    accept = (request.headers.get("accept") or "") if request else ""
    if "application/json" in accept or (request and request.headers.get("x-requested-with")):
        return JSONResponse({"ok": True, "status": getattr(appt.status, "value", str(appt.status))})
    return RedirectResponse(url="/dashboard?tab=home", status_code=303)


@app.post("/reschedule-appointment")
async def reschedule_appointment(
    appointment_id: int = Form(...),
    appointment_time: str = Form(...),
    staff_id: Optional[int] = Form(None),
    service_id: Optional[int] = Form(None),
    salon: Salon = Depends(get_active_salon),
    db: Session = Depends(get_db),
):
    appt = _owned_appointment(db, appointment_id, salon.id)
    if not appt:
        return RedirectResponse(url="/dashboard?tab=home", status_code=303)
    try:
        appt_dt = _parse_appt_datetime(appointment_time)
    except ValueError:
        return RedirectResponse(url="/dashboard?tab=home&error=invalid_time", status_code=303)

    # Update service if provided and valid for this salon
    if service_id is not None:
        svc = _owned_service(db, service_id, salon.id)
        if svc and getattr(svc, "is_active", 1) != 0:
            appt.service_id = service_id
            appt.service_name_snap = svc.name
            party = max(1, int(appt.party_size or 1))
            if getattr(svc, "is_package", 0):
                appt.service_price_snap = svc.package_total(party)
            else:
                appt.service_price_snap = float(svc.price) if svc.price is not None else None

    # Update staff if provided and valid
    if staff_id is not None:
        st = _owned_staff(db, staff_id, salon.id)
        if st:
            # Ensure staff offers the (possibly new) service
            if appt.service_id and not st.offers_service(appt.service_id):
                return RedirectResponse(
                    url="/dashboard?tab=home&error=staff_service_mismatch", status_code=303
                )
            appt.staff_id = staff_id

    duration = _service_duration(db, appt.service_id) if appt.service_id else 30
    end_dt = appt_dt + timedelta(minutes=duration)

    # Re-check staff hours + overlap with the final staff/service
    staff_obj = _owned_staff(db, appt.staff_id, salon.id) if appt.staff_id else None
    if staff_obj:
        allow_outside = False
        if appt.service_id:
            svc = _owned_service(db, appt.service_id, salon.id)
            allow_outside = bool(svc and getattr(svc, "allow_outside_hours", 0))
        if not allow_outside and not _within_staff_hours(db, salon, staff_obj, appt_dt, end_dt):
            return RedirectResponse(
                url="/dashboard?tab=home&error=unavailable", status_code=303
            )
    if _staff_has_overlap(db, salon.id, appt.staff_id, appt_dt, end_dt):
        return RedirectResponse(url="/dashboard?tab=home&error=conflict", status_code=303)

    appt.appointment_datetime = appt_dt
    db.commit()
    return RedirectResponse(url="/dashboard?tab=home", status_code=303)


# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------

def _form_str(form, key: str, default: str = "") -> str:
    v = form.get(key)
    if v is None:
        return default
    return str(v).strip()


def _form_float(form, key: str, default: float = 0.0) -> float:
    v = form.get(key)
    if v is None or str(v).strip() == "":
        return default
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return default


def _form_int(form, key: str, default: int = 0) -> int:
    v = form.get(key)
    if v is None or str(v).strip() == "":
        return default
    try:
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
        return default


def _form_bool(form, key: str) -> bool:
    v = form.get(key)
    if v is None:
        return False
    return str(v).strip().lower() in ("1", "on", "true", "yes")


def _form_checkbox(form, key: str):
    """Checkbox with optional hidden value=0 companion.
    Returns True if any submitted value is truthy (1/on/true/yes),
    False if the key is present but only falsy values (e.g. hidden 0),
    None if the key is absent entirely (leave field unchanged).
    """
    if key not in form:
        return None
    try:
        vals = list(form.getlist(key))
    except Exception:
        vals = [form.get(key)]
    for v in vals:
        if v is None:
            continue
        if str(v).strip().lower() in ("1", "on", "true", "yes"):
            return True
    return False


@app.post("/add-service")
async def add_service(
    request: Request,
    photo: Optional[UploadFile] = File(None),
    salon: Salon = Depends(get_active_salon),
    db: Session = Depends(get_db),
):
    """Create a service. Parses form manually so empty fields never 422/500."""
    form = await request.form()
    name = _form_str(form, "name")
    if not name:
        return RedirectResponse(url="/dashboard?tab=services&error=service_name", status_code=303)

    category = (_form_str(form, "category") or "Other")[:80]
    price = max(0.0, _form_float(form, "price", 0.0))
    duration_minutes = max(5, _form_int(form, "duration_minutes", 30))
    deposit_amount = max(0.0, _form_float(form, "deposit_amount", 0.0))
    pkg_flag = _form_checkbox(form, "is_package")
    is_pkg = bool(pkg_flag) if pkg_flag is not None else _form_bool(form, "is_package")
    min_people = max(1, _form_int(form, "min_people", 1))
    max_raw = form.get("max_people")
    max_people = None
    if max_raw is not None and str(max_raw).strip() != "":
        try:
            max_people = max(min_people, int(float(str(max_raw).strip())))
        except (TypeError, ValueError):
            max_people = None
    includes_text = _form_str(form, "includes_text") or None
    allow_outside = _form_bool(form, "allow_outside_hours")
    extra_raw = form.get("extra_person_price")
    extra_person_price = None
    if extra_raw is not None and str(extra_raw).strip() != "":
        try:
            extra_person_price = max(0.0, float(str(extra_raw).strip()))
        except (TypeError, ValueError):
            extra_person_price = None

    photo_url = None
    # Prefer injected UploadFile; fall back to form file
    upload = photo
    if (not upload or not getattr(upload, "filename", None)) and "photo" in form:
        cand = form.get("photo")
        if cand is not None and hasattr(cand, "filename") and cand.filename:
            upload = cand
    if upload and getattr(upload, "filename", None):
        try:
            photo_url = _save_upload(upload, subfolder=f"services/{salon.id}", salon_id=salon.id)
        except ValueError:
            photo_url = None
        except Exception:
            photo_url = None

    try:
        svc = Service(
            salon_id=salon.id,
            name=name,
            category=category,
            price=price,
            duration_minutes=duration_minutes,
            deposit_amount=deposit_amount,
            is_package=1 if is_pkg else 0,
            min_people=min_people,
            max_people=max_people,
            includes_text=includes_text,
            allow_outside_hours=1 if allow_outside else 0,
            extra_person_price=extra_person_price,
            photo_url=photo_url,
            is_active=1,
        )
        db.add(svc)
        db.commit()
    except Exception:
        db.rollback()
        return RedirectResponse(url="/dashboard?tab=services&error=service_save_failed", status_code=303)
    return RedirectResponse(url="/dashboard?tab=services", status_code=303)




@app.post("/service-categories/save")
async def save_service_categories(
    request: Request,
    salon: Salon = Depends(get_active_salon),
    db: Session = Depends(get_db),
):
    """Owner manages their own service category list (Hair, Nails, Gel Nails…)."""
    form = await request.form()
    cats = []
    if hasattr(form, "getlist"):
        try:
            cats = list(form.getlist("categories"))
        except Exception:
            cats = []
    if not cats:
        text_val = str(form.get("categories_text") or "")
        cats = [c.strip() for c in text_val.replace("\n", ",").split(",")]
    cleaned = []
    for c in cats:
        s = str(c or "").strip()[:80]
        if s and s not in cleaned:
            cleaned.append(s)
    if not cleaned:
        cleaned = list(DEFAULT_SERVICE_CATEGORIES)
    salon.service_categories = cleaned
    try:
        flag_modified(salon, "service_categories")
    except Exception:
        pass
    try:
        db.add(salon)
        db.commit()
    except Exception:
        db.rollback()
        return RedirectResponse(url="/dashboard?tab=services&error=cats_save_failed", status_code=303)
    return RedirectResponse(url="/dashboard?tab=services&cats_saved=1", status_code=303)


@app.post("/delete-service")
async def delete_service(
    request: Request,
    salon: Salon = Depends(get_active_salon),
    db: Session = Depends(get_db),
):
    """
    Delete or archive a service.
    - No appointments / waitlist refs → hard delete.
    - Has history → soft-archive (is_active=0), keep service_id on past appointments
      (never NULL out FK — avoids NOT NULL / IntegrityError 500s on SQLite).
    - force=1 is accepted for UI confirm flow; always safe-archives when referenced.
    """
    form = await request.form()
    try:
        service_id = int(str(form.get("service_id") or "0"))
    except (TypeError, ValueError):
        service_id = 0
    force = _form_bool(form, "force")

    svc = _owned_service(db, service_id, salon.id)
    if not svc:
        return RedirectResponse(url="/dashboard?tab=services", status_code=303)

    appt_count = (
        db.query(func.count(Appointment.id))
        .filter(Appointment.service_id == service_id, Appointment.salon_id == salon.id)
        .scalar()
    ) or 0
    wait_count = (
        db.query(func.count(Waitlist.id))
        .filter(Waitlist.service_id == service_id, Waitlist.salon_id == salon.id)
        .scalar()
    ) or 0

    referenced = (appt_count + wait_count) > 0

    # Snapshot name/price on appointments so history stays readable after archive
    if appt_count > 0:
        try:
            for a in (
                db.query(Appointment)
                .filter(Appointment.service_id == service_id, Appointment.salon_id == salon.id)
                .all()
            ):
                if not getattr(a, "service_name_snap", None):
                    a.service_name_snap = svc.name
                if getattr(a, "service_price_snap", None) is None:
                    try:
                        a.service_price_snap = float(svc.price or 0)
                    except Exception:
                        pass
            # Keep service_id intact — do NOT set to None (breaks NOT NULL columns)
        except Exception:
            db.rollback()

    if referenced:
        # Soft archive only (UI may pass force=1 after confirm)
        try:
            svc.is_active = 0
            db.add(svc)
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

    # No references — hard delete
    try:
        db.delete(svc)
        db.commit()
    except IntegrityError:
        db.rollback()
        try:
            svc.is_active = 0
            db.add(svc)
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
    except Exception:
        db.rollback()
        return RedirectResponse(
            url="/dashboard?tab=services&error=service_delete_failed",
            status_code=303,
        )
    return RedirectResponse(url="/dashboard?tab=services", status_code=303)


@app.post("/update-service")
async def update_service(
    request: Request,
    photo: Optional[UploadFile] = File(None),
    salon: Salon = Depends(get_active_salon),
    db: Session = Depends(get_db),
):
    """
    Update service fields (including package options + optional photo).
    Manual form parsing avoids FastAPI 422/500 when the browser sends empty strings.
    """
    form = await request.form()
    try:
        service_id = int(str(form.get("service_id") or "0"))
    except (TypeError, ValueError):
        service_id = 0

    svc = _owned_service(db, service_id, salon.id)
    if not svc:
        return RedirectResponse(url="/dashboard?tab=services", status_code=303)

    name = _form_str(form, "name")
    if name:
        svc.name = name

    if "category" in form:
        svc.category = (_form_str(form, "category") or "Other")[:80]

    if "price" in form and str(form.get("price") or "").strip() != "":
        svc.price = max(0.0, _form_float(form, "price", float(svc.price or 0)))

    if "duration_minutes" in form and str(form.get("duration_minutes") or "").strip() != "":
        svc.duration_minutes = max(5, _form_int(form, "duration_minutes", int(svc.duration_minutes or 30)))

    if "deposit_amount" in form:
        svc.deposit_amount = max(0.0, _form_float(form, "deposit_amount", 0.0))

    # Package / checkbox fields — support hidden value=0 + checkbox value=1
    # so turning a package OFF actually persists (unchecked boxes are omitted otherwise).
    pkg = _form_checkbox(form, "is_package")
    if pkg is not None:
        svc.is_package = 1 if pkg else 0

    if "min_people" in form and str(form.get("min_people") or "").strip() != "":
        svc.min_people = max(1, _form_int(form, "min_people", 1))
    if "max_people" in form:
        raw = str(form.get("max_people") or "").strip()
        if raw == "":
            svc.max_people = None
        else:
            try:
                svc.max_people = max(int(getattr(svc, "min_people", 1) or 1), int(float(raw)))
            except (TypeError, ValueError):
                pass
    if "includes_text" in form:
        txt = _form_str(form, "includes_text")
        svc.includes_text = txt or None

    outside = _form_checkbox(form, "allow_outside_hours")
    if outside is not None:
        svc.allow_outside_hours = 1 if outside else 0

    if "extra_person_price" in form:
        raw = str(form.get("extra_person_price") or "").strip()
        if raw == "":
            svc.extra_person_price = None
        else:
            try:
                svc.extra_person_price = max(0.0, float(raw))
            except (TypeError, ValueError):
                pass

    active = _form_checkbox(form, "is_active")
    if active is not None:
        svc.is_active = 1 if active else 0

    upload = photo
    if (not upload or not getattr(upload, "filename", None)) and "photo" in form:
        cand = form.get("photo")
        if cand is not None and hasattr(cand, "filename") and cand.filename:
            upload = cand
    if upload and getattr(upload, "filename", None):
        try:
            svc.photo_url = _save_upload(upload, subfolder=f"services/{salon.id}", salon_id=salon.id)
        except ValueError:
            pass
        except Exception:
            pass

    try:
        db.add(svc)
        db.commit()
    except Exception:
        db.rollback()
        return RedirectResponse(url="/dashboard?tab=services&error=service_save_failed", status_code=303)
    return RedirectResponse(url="/dashboard?tab=services", status_code=303)


# ---------------------------------------------------------------------------
# Staff
# ---------------------------------------------------------------------------



@app.post("/update-service-package")
async def update_service_package_alias(
    request: Request,
    photo: Optional[UploadFile] = File(None),
    salon: Salon = Depends(get_active_salon),
    db: Session = Depends(get_db),
):
    """Alias for older dashboard forms that posted to /update-service-package."""
    return await update_service(request=request, photo=photo, salon=salon, db=db)


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
        _clear_auth_cookie(redirect, "access_token")
        return redirect
    return RedirectResponse(url="/dashboard?tab=staff", status_code=303)


@app.post("/update-staff-photo")
async def update_staff_photo(
    staff_id: int = Form(...),
    photo: UploadFile = File(...),
    salon: Salon = Depends(get_active_salon),
    db: Session = Depends(get_db),
):
    staff = _owned_staff(db, staff_id, salon.id)
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
    staff = _owned_staff(db, staff_id, salon.id)
    if not staff:
        return RedirectResponse(url="/dashboard?tab=staff", status_code=303)

    appt_count = (
        db.query(func.count(Appointment.id))
        .filter(Appointment.staff_id == staff_id, Appointment.salon_id == salon.id)
        .scalar()
    ) or 0
    if appt_count > 0:
        return RedirectResponse(url="/dashboard?tab=staff&error=staff_in_use", status_code=303)

    db.query(Waitlist).filter(
        Waitlist.staff_id == staff_id, Waitlist.salon_id == salon.id
    ).update({Waitlist.staff_id: None}, synchronize_session=False)
    db.query(StaffDayOff).filter(StaffDayOff.staff_id == staff_id).delete(synchronize_session=False)

    try:
        db.delete(staff)
        db.commit()
    except IntegrityError:
        db.rollback()
        return RedirectResponse(url="/dashboard?tab=staff&error=staff_in_use", status_code=303)
    return RedirectResponse(url="/dashboard?tab=staff", status_code=303)


@app.post("/update-staff-services")
async def update_staff_services(
    staff_id: int = Form(...),
    service_ids: List[int] = Form(default=[]),
    all_services: Optional[str] = Form(None),
    salon: Salon = Depends(get_active_salon),
    db: Session = Depends(get_db),
):
    staff = _owned_staff(db, staff_id, salon.id)
    if not staff:
        return RedirectResponse(url="/dashboard?tab=staff", status_code=303)

    if all_services in ("1", "on", "true", "yes") or not service_ids:
        staff.service_ids = None
    else:
        valid = {s.id for s in db.query(Service).filter(Service.salon_id == salon.id).all()}
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
    staff = _owned_staff(db, staff_id, salon.id)
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
    staff = _owned_staff(db, staff_id, salon.id)
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
    row = _owned_dayoff(db, dayoff_id, salon.id)
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
    entry = _owned_waitlist(db, waitlist_id, salon.id)
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
        free = _available_staff_for_slot(db, salon, appt_dt, end_dt, service_id=entry.service_id)
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
        payment_screenshot_url=getattr(entry, "payment_screenshot_url", None),
        payment_method=getattr(entry, "payment_method", None),
    ))
    db.delete(entry)
    db.commit()
    return RedirectResponse(url="/dashboard?tab=home", status_code=303)


@app.post("/add-waitlist")
async def add_waitlist(
    request: Request,
    salon: Salon = Depends(get_active_salon),
    db: Session = Depends(get_db),
):
    """Salon-owner waitlist add (dashboard conflict panel + modal). Never 500 on empty fields."""
    form = await request.form()
    customer_name = (str(form.get("customer_name") or "")).strip()
    customer_phone = (str(form.get("customer_phone") or "")).strip()
    if not customer_name or not customer_phone:
        return RedirectResponse(
            url="/dashboard?tab=reserve&error=waitlist_missing",
            status_code=303,
        )

    service_id = None
    raw_svc = str(form.get("service_id") or "").strip()
    if raw_svc.isdigit():
        sid = int(raw_svc)
        if _owned_service(db, sid, salon.id):
            service_id = sid
    if service_id is None:
        # fall back to first active service at this location
        for s in db.query(Service).filter(Service.salon_id == salon.id).order_by(Service.id).all():
            if getattr(s, "is_active", 1) != 0:
                service_id = s.id
                break
        if service_id is None:
            return RedirectResponse(
                url="/dashboard?tab=reserve&error=waitlist_no_service",
                status_code=303,
            )

    staff_id = None
    raw_staff = str(form.get("staff_id") or "").strip()
    if raw_staff.isdigit():
        stid = int(raw_staff)
        if _owned_staff(db, stid, salon.id):
            staff_id = stid

    preferred_date = _parse_date(str(form.get("preferred_date") or "").strip()) or date.today()

    try:
        db.add(Waitlist(
            salon_id=salon.id,
            customer_name=customer_name[:120],
            customer_phone=customer_phone[:40],
            service_id=service_id,
            staff_id=staff_id,
            preferred_date=preferred_date,
        ))
        db.commit()
    except Exception:
        db.rollback()
        return RedirectResponse(
            url="/dashboard?tab=home&error=waitlist_failed",
            status_code=303,
        )

    # Prefer returning to reserve tab; conflict flow started from home
    next_tab = str(form.get("next_tab") or "reserve").strip() or "reserve"
    if next_tab not in ("home", "reserve", "staff", "services", "settings"):
        next_tab = "reserve"
    return RedirectResponse(
        url=f"/dashboard?tab={next_tab}&waitlisted=1",
        status_code=303,
    )


@app.post("/delete-waitlist")
def delete_waitlist(
    waitlist_id: int = Form(...),
    salon: Salon = Depends(get_active_salon),
    db: Session = Depends(get_db),
):
    entry = _owned_waitlist(db, waitlist_id, salon.id)
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
            Appointment.status.in_([AppointmentStatus.completed, "Completed"]),
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
    img = _owned_gallery(db, image_id, salon.id)
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
    t = _owned_testimonial(db, testimonial_id, salon.id)
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
    t = _owned_testimonial(db, testimonial_id, salon.id)
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
    """Turn deposit requirement ON/OFF for this salon."""
    salon.deposit_enabled = 1 if (deposit_enabled or "").lower() in ("1", "on", "true", "yes") else 0
    db.add(salon)
    db.commit()
    return RedirectResponse(url="/dashboard?tab=settings&deposit_saved=1", status_code=303)


@app.post("/settings/payment-methods")
async def settings_payment_methods(
    method_names: List[str] = Form(default=[]),
    method_accounts: List[str] = Form(default=[]),
    method_notes: List[str] = Form(default=[]),
    salon: Salon = Depends(get_active_salon),
    db: Session = Depends(get_db),
):
    """
    Save payment methods.
    HTML form must use repeated fields:
      name="method_names"
      name="method_accounts"
      name="method_notes"
    """
    methods = []
    n = max(len(method_names or []), len(method_accounts or []), len(method_notes or []))
    for i in range(n):
        name = (method_names[i] if i < len(method_names) else "").strip()
        if not name:
            continue
        account = (method_accounts[i] if i < len(method_accounts) else "").strip()
        notes = (method_notes[i] if i < len(method_notes) else "").strip()
        methods.append({
            "name": name,
            "account": account,
            "instructions": notes,
        })

    salon.payment_methods = methods
    # Critical: make SQLAlchemy detect JSON/TEXT mutation
    try:
        flag_modified(salon, "payment_methods")
    except Exception:
        pass
    db.add(salon)
    db.commit()
    return RedirectResponse(url="/dashboard?tab=settings&payments_saved=1", status_code=303)


@app.post("/dismiss-payment-proof")
def dismiss_payment_proof(
    appointment_id: int = Form(...),
    salon: Salon = Depends(get_active_salon),
    db: Session = Depends(get_db),
):
    appt = _owned_appointment(db, appointment_id, salon.id)
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
    appt = _owned_appointment(db, appointment_id, salon.id)
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
    conflict_screenshot: Optional[str] = None,
    conflict_pay_method: Optional[str] = None,
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
    # Prefer effective payment methods (branch may inherit)
    payment_methods = salon.effective_payment_methods() if hasattr(salon, "effective_payment_methods") else (salon.payment_methods or [])

    context = {
        "salon": salon,
        "salon_ref": public_path,
        "services": services,
        "service_categories": _salon_service_categories(salon),
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
        "payment_methods": payment_methods,
        "brand_name": salon.brand_name if hasattr(salon, "brand_name") else salon.name,
    }
    if error == "conflict" and conflict_time:
        context.update(
            _build_conflict_context(
                db, salon,
                conflict_name=conflict_name,
                conflict_phone=conflict_phone,
                conflict_service=conflict_service,
                conflict_staff=conflict_staff,
                conflict_time=conflict_time,
            )
        )
        # Preserve payment proof across conflict → waitlist
        if conflict_screenshot and str(conflict_screenshot).startswith("/"):
            context["conflict_screenshot"] = conflict_screenshot
        if conflict_pay_method:
            context["conflict_pay_method"] = conflict_pay_method
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

    # Save payment proof EARLY so it is not lost on conflict → waitlist
    screenshot_url = None
    pay_method = (payment_method or "").strip() or None
    if payment_screenshot and getattr(payment_screenshot, "filename", None):
        try:
            screenshot_url = _save_upload(
                payment_screenshot, subfolder=f"payments/{salon.id}", salon_id=salon.id
            )
        except ValueError:
            return RedirectResponse(
                url=f"/book/{public_path}?error=invalid_image", status_code=303
            )
        except Exception:
            screenshot_url = None

    def _conflict_redirect():
        # Keep payment proof so waitlist / later booking can still show the screenshot
        if screenshot_url:
            _store_pending_proof(salon.id, customer_phone, screenshot_url, pay_method)
        params = {
            "error": "conflict",
            "conflict_name": customer_name,
            "conflict_phone": customer_phone,
            "conflict_service": service_id,
            "conflict_staff": staff_id if staff_id else 0,
            "conflict_time": appointment_time,
        }
        if screenshot_url:
            params["conflict_screenshot"] = screenshot_url
        if pay_method:
            params["conflict_pay_method"] = pay_method
        return RedirectResponse(
            url=f"/book/{public_path}?{urlencode(params)}", status_code=303
        )

    try:
        appt_dt = _parse_appt_datetime(appointment_time)
    except ValueError:
        return _conflict_redirect()

    duration = _service_duration(db, service_id)
    end_dt = appt_dt + timedelta(minutes=duration)
    svc = db.query(Service).filter(Service.id == service_id, Service.salon_id == salon.id).first()
    staff_obj = None

    if not staff_id or int(staff_id) == 0:
        free = _available_staff_for_slot(db, salon, appt_dt, end_dt, service_id=service_id)
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
    needs_deposit = bool(getattr(salon, "deposit_enabled", 0)) and deposit_amt > 0

    status_val = AppointmentStatus.confirmed
    if needs_deposit and not screenshot_url:
        status_val = AppointmentStatus.pending_payment

    snap_name = svc.name if svc else None
    snap_price = float(svc.price) if svc else None
    if svc and getattr(svc, "is_package", 0):
        try:
            snap_price = svc.package_total(party)
        except Exception:
            snap_price = float(svc.price or 0) * party

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
        return RedirectResponse(
            url=f"/book/{public_path}?error=booking_failed", status_code=303
        )

    try:
        await manager.broadcast(salon.id, {
            "event": "new_booking",
            "appointment": {
                "id": appt.id,
                "appointment_time": getattr(appt, "appointment_time", None),
                "customer_name": appt.customer_name,
                "customer_phone": appt.customer_phone,
                "service_name": appt.service_name,
                "service_price": appt.service_price,
                "staff_name": appt.staff_name,
                "status": getattr(appt.status, "value", str(appt.status)),
                "source": appt.source,
                "party_size": appt.party_size or 1,
                "payment_screenshot_url": screenshot_url,
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
    # IDOR guard: appointment must belong to THIS public salon only
    appt = _owned_appointment(db, appointment_id, salon.id)
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
async def public_join_waitlist(
    salon_ref: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """Join waitlist after a conflict. Keeps payment screenshot when provided."""
    salon = _resolve_salon(db, salon_ref)
    if not salon:
        return HTMLResponse("Salon not found", status_code=404)
    public_path = (salon.slug or "").strip() or str(salon.id)

    form = await request.form()
    customer_name = (str(form.get("customer_name") or "")).strip()
    customer_phone = (str(form.get("customer_phone") or "")).strip()
    if not customer_name or not customer_phone:
        return RedirectResponse(
            url=f"/book/{public_path}?error=waitlist_missing",
            status_code=303,
        )

    service_id = None
    raw_svc = str(form.get("service_id") or "").strip()
    if raw_svc.isdigit():
        service_id = int(raw_svc)
        svc = db.query(Service).filter(
            Service.id == service_id, Service.salon_id == salon.id
        ).first()
        if not svc:
            service_id = None
    if service_id is None:
        svc = None
        for s in db.query(Service).filter(Service.salon_id == salon.id).order_by(Service.id).all():
            if getattr(s, "is_active", 1) != 0:
                svc = s
                break
        if not svc:
            svc = db.query(Service).filter(Service.salon_id == salon.id).order_by(Service.id).first()
        if not svc:
            return RedirectResponse(
                url=f"/book/{public_path}?error=waitlist_no_service",
                status_code=303,
            )
        service_id = svc.id

    staff_id = None
    raw_staff = str(form.get("staff_id") or "").strip()
    if raw_staff.isdigit():
        sid = int(raw_staff)
        if db.query(Staff).filter(Staff.id == sid, Staff.salon_id == salon.id).first():
            staff_id = sid

    preferred_date = _parse_date(str(form.get("preferred_date") or "").strip()) or date.today()

    # Keep payment proof from the booking attempt (hidden field OR server-side pending store)
    shot = (str(form.get("payment_screenshot_url") or "")).strip() or None
    if shot and not shot.startswith("/"):
        shot = None
    pay_method = (str(form.get("payment_method") or "")).strip() or None
    if not shot:
        pending_url, pending_method = _take_pending_proof(salon.id, customer_phone)
        if pending_url:
            shot = pending_url
        if not pay_method and pending_method:
            pay_method = pending_method

    try:
        entry = Waitlist(
            salon_id=salon.id,
            customer_name=customer_name[:120],
            customer_phone=customer_phone[:40],
            service_id=service_id,
            staff_id=staff_id,
            preferred_date=preferred_date,
        )
        db.add(entry)
        db.commit()
        db.refresh(entry)
        # Persist payment proof (column may exist via migration even if model is older)
        if shot or pay_method:
            from sqlalchemy import text
            try:
                db.execute(
                    text(
                        "UPDATE waitlist SET payment_screenshot_url = :shot, payment_method = :pm "
                        "WHERE id = :id"
                    ),
                    {"shot": shot, "pm": pay_method, "id": entry.id},
                )
                db.commit()
            except Exception:
                db.rollback()
                try:
                    db.execute(
                        text("UPDATE waitlist SET payment_screenshot_url = :shot WHERE id = :id"),
                        {"shot": shot, "id": entry.id},
                    )
                    db.commit()
                except Exception:
                    db.rollback()
    except Exception:
        db.rollback()
        try:
            db.add(Waitlist(
                salon_id=salon.id,
                customer_name=customer_name[:120],
                customer_phone=customer_phone[:40],
                service_id=service_id,
                staff_id=staff_id,
                preferred_date=preferred_date,
            ))
            db.commit()
        except Exception:
            db.rollback()
            return RedirectResponse(
                url=f"/book/{public_path}?error=waitlist_failed",
                status_code=303,
            )
    return RedirectResponse(url=f"/book/{public_path}?waitlisted=1", status_code=303)



@app.get("/privacy", response_class=HTMLResponse)
def privacy(request: Request):
    return templates.TemplateResponse(request, "privacy.html", {})


# ---------------------------------------------------------------------------
# Super admin — Melkegna HQ command center
# ---------------------------------------------------------------------------

@app.get("/admin/login", response_class=HTMLResponse)
def admin_login_page(request: Request, error: Optional[str] = None):
    return templates.TemplateResponse(
        request,
        "admin_login.html",
        {"error": error, "totp_enabled": bool(ADMIN_TOTP_SECRET)},
    )


@app.post("/admin/login")
async def admin_login_submit(
    request: Request,
    key: str = Form(...),
    otp: Optional[str] = Form(None),
):
    ip = _client_ip(request) or "unknown"
    bucket = f"admin-login:{ip}"
    if _is_rate_limited(bucket):
        return RedirectResponse(url="/admin/login?error=rate", status_code=429)

    if not _valid_admin_credentials(key, otp):
        _record_attempt(bucket)
        try:
            db = SessionLocal()
            try:
                _admin_audit(db, action="login_failed", details="invalid credentials", ip=ip)
            finally:
                db.close()
        except Exception:
            pass
        return RedirectResponse(url="/admin/login?error=1", status_code=303)

    token = create_admin_token()
    resp = RedirectResponse(url="/admin", status_code=303)
    _set_auth_cookie(resp, "admin_token", token)
    try:
        db = SessionLocal()
        try:
            _admin_audit(db, action="login_ok", details="admin session started", ip=ip)
        finally:
            db.close()
    except Exception:
        pass
    return resp


@app.get("/admin/logout")
def admin_logout():
    resp = RedirectResponse(url="/admin/login", status_code=303)
    _clear_auth_cookie(resp, "admin_token")
    return resp


@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(
    request: Request,
    db: Session = Depends(get_db),
    admin=Depends(get_current_admin),
):
    now = datetime.utcnow()
    soon = now + timedelta(days=14)
    today_start = datetime.combine(date.today(), datetime.min.time())
    today_end = today_start + timedelta(days=1)

    roots = (
        db.query(Salon)
        .filter(Salon.parent_id.is_(None))
        .order_by(Salon.created_at.desc())
        .all()
    )
    salons = db.query(Salon).order_by(Salon.id).all()

    # Branch counts per root
    branch_counts: dict = {}
    for s in salons:
        if s.parent_id is not None:
            branch_counts[s.parent_id] = branch_counts.get(s.parent_id, 0) + 1

    def _status(s: Salon) -> str:
        return (s.status or "pending").lower()

    active = sum(1 for s in roots if _status(s) == "active")
    pending = sum(1 for s in roots if _status(s) == "pending")
    suspended = sum(1 for s in roots if _status(s) == "suspended")
    expiring_soon = sum(
        1
        for s in roots
        if _status(s) == "active"
        and s.subscription_expires_at
        and now <= s.subscription_expires_at <= soon
    )

    total_bookings = db.query(func.count(Appointment.id)).scalar() or 0
    today_bookings = (
        db.query(func.count(Appointment.id))
        .filter(
            Appointment.appointment_datetime >= today_start,
            Appointment.appointment_datetime < today_end,
        )
        .scalar()
    ) or 0

    # Completed booking value (all-time)
    completed = (
        db.query(Appointment)
        .filter(Appointment.status.in_([AppointmentStatus.completed, "Completed"]))
        .all()
    )
    booking_value = float(sum(float(a.service_price or 0) for a in completed))
    deposits = float(
        sum(float(a.deposit_amount or 0) for a in completed if a.deposit_amount)
    )

    pending_salons = [s for s in roots if _status(s) == "pending"][:15]
    expiring_salons = [
        s
        for s in roots
        if _status(s) == "active"
        and s.subscription_expires_at
        and now <= s.subscription_expires_at <= soon
    ][:15]

    stats = {
        "total_roots": len(roots),
        "active": active,
        "pending": pending,
        "suspended": suspended,
        "expiring_soon": expiring_soon,
        "total_bookings": total_bookings,
        "today_bookings": today_bookings,
        "booking_value": booking_value,
        "deposits": deposits,
    }

    # Recent admin audit trail
    audit_rows = []
    try:
        from sqlalchemy import text as sa_text
        rows = db.execute(
            sa_text(
                "SELECT id, action, target_type, target_id, target_name, details, ip, created_at "
                "FROM admin_audit_log ORDER BY id DESC LIMIT 50"
            )
        ).fetchall()
        for r in rows:
            audit_rows.append({
                "id": r[0],
                "action": r[1],
                "target_type": r[2],
                "target_id": r[3],
                "target_name": r[4],
                "details": r[5],
                "ip": r[6],
                "created_at": r[7],
            })
    except Exception:
        audit_rows = []

    # Billable branch signals for admin (new branches + branch usage)
    branch_events = [r for r in audit_rows if r.get("action") in ("branch_created", "branch_in_use")]
    recent_branches = (
        db.query(Salon)
        .filter(Salon.parent_id.isnot(None))
        .order_by(Salon.created_at.desc())
        .limit(20)
        .all()
    )
    multi_loc_roots = sum(1 for rid, c in branch_counts.items() if c > 0)
    stats["total_branches"] = sum(branch_counts.values())
    stats["multi_location_businesses"] = multi_loc_roots

    # ---- Platform ops data for sidebar panels ----
    salon_name_map = {s.id: s.name for s in salons}

    recent_appts = (
        db.query(Appointment)
        .order_by(Appointment.appointment_datetime.desc())
        .limit(80)
        .all()
    )
    platform_bookings = []
    for a in recent_appts:
        st = getattr(a.status, "value", str(a.status))
        platform_bookings.append({
            "id": a.id,
            "salon_id": a.salon_id,
            "salon_name": salon_name_map.get(a.salon_id, f"#{a.salon_id}"),
            "customer_name": a.customer_name,
            "customer_phone": a.customer_phone,
            "service_name": a.service_name,
            "staff_name": a.staff_name,
            "status": st,
            "source": a.source,
            "price": float(a.service_price or 0),
            "deposit": float(a.deposit_amount or 0),
            "when": a.appointment_datetime,
            "party_size": a.party_size or 1,
        })

    # Distinct customers (by phone) from recent appointments sample + totals
    cust_map = {}
    for a in recent_appts:
        phone = (a.customer_phone or "").strip()
        if not phone:
            continue
        if phone not in cust_map:
            cust_map[phone] = {
                "phone": phone,
                "name": a.customer_name,
                "salon_name": salon_name_map.get(a.salon_id, ""),
                "last_visit": a.appointment_datetime,
                "count": 1,
            }
        else:
            cust_map[phone]["count"] += 1
            if a.appointment_datetime and (
                not cust_map[phone]["last_visit"]
                or a.appointment_datetime > cust_map[phone]["last_visit"]
            ):
                cust_map[phone]["last_visit"] = a.appointment_datetime
                cust_map[phone]["name"] = a.customer_name
    platform_customers = sorted(
        cust_map.values(),
        key=lambda x: x["last_visit"] or datetime.min,
        reverse=True,
    )[:60]

    platform_payments = [
        b for b in platform_bookings
        if (b.get("deposit") or 0) > 0
        or b.get("status") in ("Pending Payment", "pending_payment", "Completed")
    ][:50]

    platform_noshows = [
        b for b in platform_bookings
        if b.get("status") in ("No-Show", "no_show")
    ]

    wait_rows = (
        db.query(Waitlist)
        .order_by(Waitlist.preferred_date.desc(), Waitlist.id.desc())
        .limit(50)
        .all()
    )
    platform_waitlist = []
    for w in wait_rows:
        platform_waitlist.append({
            "id": w.id,
            "salon_id": w.salon_id,
            "salon_name": salon_name_map.get(w.salon_id, f"#{w.salon_id}"),
            "customer_name": w.customer_name,
            "customer_phone": w.customer_phone,
            "service_name": w.service_name,
            "staff_name": w.staff_name,
            "preferred_date": w.preferred_date,
            "created_at": w.created_at,
        })

    # Subscriptions view = roots with status/expiry
    subscriptions = []
    for s in roots:
        st = (s.status or "pending").lower()
        exp = s.subscription_expires_at
        expired = bool(exp and exp < now)
        days_left = None
        if exp and not expired:
            days_left = (exp.date() - now.date()).days if hasattr(exp, "date") else None
        subscriptions.append({
            "id": s.id,
            "name": s.name,
            "owner": s.owner_name,
            "phone": s.phone,
            "status": st,
            "expired": expired,
            "expires_at": exp,
            "days_left": days_left,
            "branches": branch_counts.get(s.id, 0),
        })

    # Growth: registrations last 30 days
    month_ago = now - timedelta(days=30)
    new_roots_30 = sum(
        1 for s in roots
        if s.created_at and (
            (s.created_at.replace(tzinfo=None) if getattr(s.created_at, "tzinfo", None) else s.created_at) >= month_ago
        )
    )
    stats["new_roots_30d"] = new_roots_30
    stats["waitlist_open"] = len(platform_waitlist)
    stats["noshow_sample"] = len(platform_noshows)

    return templates.TemplateResponse(
        request,
        "admin.html",
        {
            "salons": salons,
            "roots": roots,
            "stats": stats,
            "pending_salons": pending_salons,
            "expiring_salons": expiring_salons,
            "branch_counts": branch_counts,
            "now": now,
            "audit_log": audit_rows,
            "branch_events": branch_events,
            "recent_branches": recent_branches,
            "platform_bookings": platform_bookings,
            "platform_customers": platform_customers,
            "platform_payments": platform_payments,
            "platform_noshows": platform_noshows,
            "platform_waitlist": platform_waitlist,
            "subscriptions": subscriptions,
            "totp_enabled": bool(ADMIN_TOTP_SECRET),
        },
    )


@app.post("/admin/approve/{salon_id}")
def admin_approve(
    request: Request,
    salon_id: int,
    days: int = Form(30),
    admin=Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    salon = db.query(Salon).filter(Salon.id == salon_id).first()
    if salon:
        days_n = max(1, int(days))
        salon.status = "active"
        base = salon.subscription_expires_at or datetime.utcnow()
        if base < datetime.utcnow():
            base = datetime.utcnow()
        salon.subscription_expires_at = base + timedelta(days=days_n)
        for b in db.query(Salon).filter(Salon.parent_id == salon.id).all():
            b.status = "active"
        db.commit()
        _admin_audit(
            db,
            action="approve_extend",
            target_type="salon",
            target_id=salon.id,
            target_name=salon.name,
            details=f"+{days_n} days → expires {salon.subscription_expires_at}",
            ip=_client_ip(request),
        )
    return RedirectResponse(url="/admin#salons", status_code=303)


@app.post("/admin/suspend/{salon_id}")
def admin_suspend(
    request: Request,
    salon_id: int,
    admin=Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    salon = db.query(Salon).filter(Salon.id == salon_id).first()
    if salon:
        salon.status = "suspended"
        for b in db.query(Salon).filter(Salon.parent_id == salon.id).all():
            b.status = "suspended"
        db.commit()
        _admin_audit(
            db,
            action="suspend",
            target_type="salon",
            target_id=salon.id,
            target_name=salon.name,
            details="status=suspended (branches included)",
            ip=_client_ip(request),
        )
    return RedirectResponse(url="/admin#salons", status_code=303)



# --- Net profit / expenses (see profit_module.py for full routes) ---
def _ensure_expense_columns():
    from sqlalchemy import inspect, text
    inspector = inspect(engine)
    dialect = engine.dialect.name
    try:
        tables = inspector.get_table_names()
    except Exception:
        return
    if "expenses" not in tables:
        if dialect == "postgresql":
            ddl = """CREATE TABLE IF NOT EXISTS expenses (
                id SERIAL PRIMARY KEY,
                salon_id INTEGER NOT NULL,
                category VARCHAR(40) NOT NULL DEFAULT 'other',
                amount NUMERIC(12,2) NOT NULL DEFAULT 0,
                expense_date DATE NOT NULL,
                note VARCHAR(400),
                staff_id INTEGER,
                is_recurring INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            )"""
        else:
            ddl = """CREATE TABLE IF NOT EXISTS expenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                salon_id INTEGER NOT NULL,
                category VARCHAR(40) NOT NULL DEFAULT 'other',
                amount REAL NOT NULL DEFAULT 0,
                expense_date DATE NOT NULL,
                note VARCHAR(400),
                staff_id INTEGER,
                is_recurring INTEGER NOT NULL DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )"""
        try:
            with engine.begin() as conn:
                conn.execute(text(ddl))
        except Exception:
            pass
    ntype = "NUMERIC(12,2)" if dialect == "postgresql" else "REAL"
    try:
        svc_cols = [c["name"] for c in inspector.get_columns("services")]
        if svc_cols and "cost_amount" not in svc_cols:
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE services ADD COLUMN cost_amount {ntype} DEFAULT 0"))
    except Exception:
        pass
    try:
        st_cols = [c["name"] for c in inspector.get_columns("staff")]
        if st_cols and "monthly_salary" not in st_cols:
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE staff ADD COLUMN monthly_salary {ntype} DEFAULT 0"))
    except Exception:
        pass

_ensure_expense_columns()


# ---------------------------------------------------------------------------
# Net profit routes — full helpers live in profit_module.py
# For a single-file deploy, keep these endpoints + _salon_profit helpers.
# ---------------------------------------------------------------------------
try:
    from profit_module import (  # if you keep profit_module.py beside main
        _ensure_expense_columns as _pmc_ensure,
        _salon_profit,
        _platform_profit_rows,
    )
except Exception:
    _salon_profit = None
    _platform_profit_rows = None

@app.post("/expenses/add")
async def expenses_add(
    category: str = Form("other"),
    amount: float = Form(...),
    expense_date: str = Form(...),
    note: Optional[str] = Form(None),
    staff_id: Optional[int] = Form(None),
    is_recurring: Optional[str] = Form(None),
    salon: Salon = Depends(get_active_salon),
    db: Session = Depends(get_db),
):
    from datetime import datetime as dt
    try:
        d = dt.strptime((expense_date or "")[:10], "%Y-%m-%d").date()
    except Exception:
        d = date.today()
    cat = (category or "other").strip().lower()[:40]
    allowed = {k for k, _ in EXPENSE_CATEGORIES}
    if cat not in allowed:
        cat = "other"
    row = Expense(
        salon_id=salon.id,
        category=cat,
        amount=max(0.0, float(amount or 0)),
        expense_date=d,
        note=(note or "")[:400] or None,
        staff_id=int(staff_id) if staff_id else None,
        is_recurring=1 if (is_recurring or "").lower() in ("1", "on", "true", "yes") else 0,
    )
    db.add(row)
    db.commit()
    return RedirectResponse(url="/dashboard?tab=settings&expense_saved=1", status_code=303)


@app.post("/expenses/delete")
async def expenses_delete(
    expense_id: int = Form(...),
    salon: Salon = Depends(get_active_salon),
    db: Session = Depends(get_db),
):
    row = db.query(Expense).filter(Expense.id == expense_id, Expense.salon_id == salon.id).first()
    if row:
        db.delete(row)
        db.commit()
    return RedirectResponse(url="/dashboard?tab=settings&expense_deleted=1", status_code=303)


@app.get("/admin/api/profit")
def admin_api_profit(
    period: str = "month",
    admin=Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Platform net profit per location. period=day|week|month"""
    if period not in ("day", "week", "month"):
        period = "month"
    # Inline minimal calculator so admin works even without importing profit_module helpers
    today = date.today()
    if period == "day":
        start = datetime.combine(today, datetime.min.time())
        end = start + timedelta(days=1)
    elif period == "week":
        start_d = today - timedelta(days=today.weekday())
        start = datetime.combine(start_d, datetime.min.time())
        end = start + timedelta(days=7)
    else:
        start = datetime.combine(today.replace(day=1), datetime.min.time())
        end = datetime(today.year + (1 if today.month == 12 else 0), 1 if today.month == 12 else today.month + 1, 1)

    start_d, end_d = start.date(), end.date()
    completed = [AppointmentStatus.completed, "Completed", "completed"]
    salons = db.query(Salon).order_by(Salon.id).all()
    rows = []
    for s in salons:
        appts = (
            db.query(Appointment)
            .filter(
                Appointment.salon_id == s.id,
                Appointment.appointment_datetime >= start,
                Appointment.appointment_datetime < end,
                Appointment.status.in_(completed),
            )
            .all()
        )
        revenue = sum(float(a.service_price or 0) for a in appts)
        cogs = 0.0
        for a in appts:
            party = max(1, int(a.party_size or 1))
            if a.service is not None:
                cogs += float(getattr(a.service, "cost_amount", 0) or 0) * party
        exp_q = db.query(Expense).filter(
            Expense.salon_id == s.id,
            Expense.expense_date >= start_d,
            Expense.expense_date < end_d,
        )
        expenses = float(sum(float(e.amount or 0) for e in exp_q.all()))
        staff = db.query(Staff).filter(Staff.salon_id == s.id).all()
        monthly = float(sum(float(getattr(st, "monthly_salary", 0) or 0) for st in staff))
        if period == "day":
            salary = monthly / 30.0
        elif period == "week":
            salary = monthly / 30.0 * 7
        else:
            salary = monthly
        net = revenue - cogs - expenses - salary
        rows.append({
            "salon_id": s.id,
            "name": s.name,
            "location_name": s.location_name,
            "parent_id": s.parent_id,
            "is_branch": bool(s.parent_id),
            "status": s.status,
            "period": period,
            "revenue": round(revenue, 2),
            "cogs": round(cogs, 2),
            "expenses": round(expenses, 2),
            "staff_salary_baseline": round(salary, 2),
            "net_profit": round(net, 2),
            "completed_bookings": len(appts),
        })
    totals = {
        "revenue": round(sum(r["revenue"] for r in rows), 2),
        "cogs": round(sum(r["cogs"] for r in rows), 2),
        "expenses": round(sum(r["expenses"] for r in rows), 2),
        "staff_salary_baseline": round(sum(r["staff_salary_baseline"] for r in rows), 2),
        "net_profit": round(sum(r["net_profit"] for r in rows), 2),
    }
    return JSONResponse({"period": period, "totals": totals, "salons": rows})




# ---------------------------------------------------------------------------
# HQ-only cost entry (psychology: you own the P&L, not the salon)
# ---------------------------------------------------------------------------

@app.post("/admin/expenses/add")
async def admin_expenses_add(
    request: Request,
    salon_id: int = Form(...),
    category: str = Form("other"),
    amount: float = Form(...),
    expense_date: str = Form(...),
    note: Optional[str] = Form(None),
    admin=Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    salon = db.query(Salon).filter(Salon.id == salon_id).first()
    if not salon:
        return RedirectResponse(url="/admin#netprofit", status_code=303)
    from datetime import datetime as dt
    try:
        d = dt.strptime((expense_date or "")[:10], "%Y-%m-%d").date()
    except Exception:
        d = date.today()
    cat = (category or "other").strip().lower()[:40]
    try:
        allowed = {k for k, _ in EXPENSE_CATEGORIES}
    except Exception:
        allowed = {"rent", "staff_salary", "water", "electricity", "generator", "supplies", "marketing", "other"}
    if cat not in allowed:
        cat = "other"
    row = Expense(
        salon_id=salon.id,
        category=cat,
        amount=max(0.0, float(amount or 0)),
        expense_date=d,
        note=(note or "")[:400] or None,
        is_recurring=0,
    )
    db.add(row)
    db.commit()
    try:
        _admin_audit(
            db,
            action="expense_added",
            target_type="salon",
            target_id=salon.id,
            target_name=salon.name,
            details=f"{cat} {float(amount or 0):.0f} ETB on {d}",
            ip=_client_ip(request),
        )
    except Exception:
        pass
    return RedirectResponse(url=f"/admin#netprofit", status_code=303)


@app.post("/admin/expenses/delete")
async def admin_expenses_delete(
    request: Request,
    expense_id: int = Form(...),
    admin=Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    row = db.query(Expense).filter(Expense.id == expense_id).first()
    if row:
        sid, cat, amt = row.salon_id, row.category, float(row.amount or 0)
        db.delete(row)
        db.commit()
        try:
            _admin_audit(
                db,
                action="expense_deleted",
                target_type="salon",
                target_id=sid,
                details=f"removed {cat} {amt:.0f} ETB",
                ip=_client_ip(request),
            )
        except Exception:
            pass
    return RedirectResponse(url="/admin#netprofit", status_code=303)


@app.get("/admin/api/expenses/{salon_id}")
def admin_api_expenses(
    salon_id: int,
    admin=Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    rows = (
        db.query(Expense)
        .filter(Expense.salon_id == salon_id)
        .order_by(Expense.expense_date.desc(), Expense.id.desc())
        .limit(100)
        .all()
    )
    return JSONResponse({
        "salon_id": salon_id,
        "expenses": [
            {
                "id": e.id,
                "category": e.category,
                "amount": float(e.amount or 0),
                "date": e.expense_date.isoformat() if e.expense_date else None,
                "note": e.note or "",
            }
            for e in rows
        ],
    })


@app.post("/admin/service-cost")
async def admin_set_service_cost(
    request: Request,
    service_id: int = Form(...),
    cost_amount: float = Form(0),
    admin=Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """HQ sets COGS on a service without salon touching it."""
    svc = db.query(Service).filter(Service.id == service_id).first()
    if svc:
        svc.cost_amount = max(0.0, float(cost_amount or 0))
        db.add(svc)
        db.commit()
        try:
            _admin_audit(
                db,
                action="service_cost_set",
                target_type="service",
                target_id=svc.id,
                target_name=svc.name,
                details=f"cost_amount={svc.cost_amount}",
                ip=_client_ip(request),
            )
        except Exception:
            pass
    return RedirectResponse(url="/admin#netprofit", status_code=303)


@app.post("/admin/staff-salary")
async def admin_set_staff_salary(
    request: Request,
    staff_id: int = Form(...),
    monthly_salary: float = Form(0),
    admin=Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    st = db.query(Staff).filter(Staff.id == staff_id).first()
    if st:
        st.monthly_salary = max(0.0, float(monthly_salary or 0))
        db.add(st)
        db.commit()
        try:
            _admin_audit(
                db,
                action="staff_salary_set",
                target_type="staff",
                target_id=st.id,
                target_name=st.name,
                details=f"monthly_salary={st.monthly_salary}",
                ip=_client_ip(request),
            )
        except Exception:
            pass
    return RedirectResponse(url="/admin#netprofit", status_code=303)


@app.get("/admin/api/salon-costs/{salon_id}")
def admin_api_salon_costs(
    salon_id: int,
    admin=Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Services + staff for HQ to set cost/salary without salon UI."""
    salon = db.query(Salon).filter(Salon.id == salon_id).first()
    if not salon:
        return JSONResponse({"ok": False}, status_code=404)
    services = db.query(Service).filter(Service.salon_id == salon_id).order_by(Service.name).all()
    staff = db.query(Staff).filter(Staff.salon_id == salon_id).order_by(Staff.name).all()
    return JSONResponse({
        "ok": True,
        "salon": {"id": salon.id, "name": salon.name, "location_name": salon.location_name},
        "services": [
            {"id": s.id, "name": s.name, "price": float(s.price or 0),
             "cost_amount": float(getattr(s, "cost_amount", 0) or 0)}
            for s in services
        ],
        "staff": [
            {"id": st.id, "name": st.name,
             "monthly_salary": float(getattr(st, "monthly_salary", 0) or 0)}
            for st in staff
        ],
    })


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)