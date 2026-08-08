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
ACCESS_TOKEN_EXPIRE_MINUTES = 60


def hash_password(password: str) -> str:
    """Hashes a password after truncating to 72 bytes (Bcrypt limit)."""
    password_bytes = password.encode("utf-8")[:72].decode("utf-8", errors="ignore")
    return password_hash.hash(password_bytes)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verifies a plain password against the stored bcrypt hash."""
    password_bytes = plain_password.encode("utf-8")[:72].decode("utf-8", errors="ignore")
    return password_hash.verify(password_bytes, hashed_password)


def create_access_token(data: dict) -> str:
    """Generates a JWT access token with an expiration timestamp."""
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict | None:
    """Decodes a JWT, returning None (never raising) if invalid/expired."""
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        return None


class NotAuthenticatedException(Exception):
    """Raised by get_current_salon; caught by an app-level handler that
    redirects the browser to /login instead of returning a raw 401."""
    pass


def get_current_salon(request: Request, db: Session = Depends(get_db)):
    """Reads the JWT from the HttpOnly 'access_token' cookie and returns
    the logged-in Salon, or raises NotAuthenticatedException."""
    # Local import avoids a circular import between security.py and models.py
    from models import Salon

    token = request.cookies.get("access_token")
    if not token:
        raise NotAuthenticatedException()

    payload = decode_access_token(token)
    if not payload or "sub" not in payload:
        raise NotAuthenticatedException()

    salon = db.query(Salon).filter(Salon.id == int(payload["sub"])).first()
    if not salon:
        raise NotAuthenticatedException()

    return salon