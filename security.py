import os
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, Request
from pwdlib import PasswordHash
from pwdlib.hashers.bcrypt import BcryptHasher
from sqlalchemy.orm import Session

from database import get_db

# Initialize pwdlib with BcryptHasher
password_hash = PasswordHash((BcryptHasher(),))

# NOTE: pull this from the environment in real deployments.
SECRET_KEY = os.getenv("SECRET_KEY", "your-secret-key-change-this")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 14  # 14 days (owner stays logged in)
ADMIN_TOKEN_EXPIRE_MINUTES = 8 * 60  # 8 hours


def hash_password(password: str) -> str:
    """Hashes a password after truncating to 72 bytes (Bcrypt limit),
    backing off byte-by-byte so we never cut a multi-byte UTF-8
    character (e.g. Amharic script) in half."""
    truncated = password.encode("utf-8")[:72]
    while truncated:
        try:
            return password_hash.hash(truncated.decode("utf-8"))
        except UnicodeDecodeError:
            truncated = truncated[:-1]
    raise ValueError("Password cannot be empty")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verifies a plain password against the stored bcrypt hash."""
    truncated = plain_password.encode("utf-8")[:72]
    while truncated:
        try:
            decoded = truncated.decode("utf-8")
            break
        except UnicodeDecodeError:
            truncated = truncated[:-1]
    else:
        decoded = ""
    return password_hash.verify(decoded, hashed_password)


def create_access_token(data: dict) -> str:
    """
    JWT for salon owner session.

    Required claim:
      sub              → root / HQ salon id (owner identity)

    Optional claim:
      active_salon_id  → current location (branch or HQ). Defaults to sub.
    """
    to_encode = data.copy()
    if "active_salon_id" not in to_encode and "sub" in to_encode:
        to_encode["active_salon_id"] = to_encode["sub"]
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def create_admin_token() -> str:
    """Generates a JWT for the super-admin session. Kept separate from
    salon tokens via the 'admin' claim so one can never be mistaken
    for the other."""
    to_encode = {
        "admin": True,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=ADMIN_TOKEN_EXPIRE_MINUTES),
    }
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict | None:
    """Decodes a JWT, returning None (never raising) if invalid/expired."""
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        return None


class NotAuthenticatedException(Exception):
    """Raised by get_current_salon/get_current_admin; caught by an
    app-level handler that redirects the browser to the right login
    page instead of returning a raw 401."""
    pass


def get_salon_tree_ids(db: Session, root_id: int) -> set:
    """Root + direct children (one-level branch tree)."""
    from models import Salon

    ids = {int(root_id)}
    for row in db.query(Salon.id).filter(Salon.parent_id == root_id).all():
        ids.add(int(row[0]))
    return ids


def get_current_salon(request: Request, db: Session = Depends(get_db)):
    """
    Reads the JWT from the HttpOnly 'access_token' cookie and returns
    the *active location* (branch or HQ), or raises NotAuthenticatedException.

    Token layout:
      sub              = root / HQ id (stable owner identity)
      active_salon_id  = location currently selected in the dashboard
    """
    from models import Salon

    token = request.cookies.get("access_token")
    if not token:
        raise NotAuthenticatedException()

    payload = decode_access_token(token)
    if not payload or "sub" not in payload:
        raise NotAuthenticatedException()

    try:
        root_id = int(payload["sub"])
    except (TypeError, ValueError):
        raise NotAuthenticatedException()

    active_raw = payload.get("active_salon_id", root_id)
    try:
        active_id = int(active_raw)
    except (TypeError, ValueError):
        active_id = root_id

    allowed = get_salon_tree_ids(db, root_id)
    if active_id not in allowed:
        active_id = root_id

    salon = db.query(Salon).filter(Salon.id == active_id).first()
    if not salon:
        raise NotAuthenticatedException()

    return salon


def get_root_salon(request: Request, db: Session = Depends(get_db)):
    """Always returns the HQ / root node for the logged-in owner."""
    from models import Salon

    token = request.cookies.get("access_token")
    if not token:
        raise NotAuthenticatedException()

    payload = decode_access_token(token)
    if not payload or "sub" not in payload:
        raise NotAuthenticatedException()

    try:
        root_id = int(payload["sub"])
    except (TypeError, ValueError):
        raise NotAuthenticatedException()

    salon = db.query(Salon).filter(Salon.id == root_id).first()
    if not salon:
        raise NotAuthenticatedException()
    return salon


def get_current_admin(request: Request) -> bool:
    """Reads the JWT from the HttpOnly 'admin_token' cookie. Raises
    NotAuthenticatedException if missing/invalid/expired, or if it's
    a salon token being reused here (no 'admin' claim)."""
    token = request.cookies.get("admin_token")
    if not token:
        raise NotAuthenticatedException()

    payload = decode_access_token(token)
    if not payload or not payload.get("admin"):
        raise NotAuthenticatedException()

    return True