from datetime import datetime, timedelta
from typing import Optional
from jose import jwt
from pwdlib import PasswordHash
from pwdlib.hashers.bcrypt import BcryptHasher

# Modern, Python 3.13-compatible password hashing
password_hash = PasswordHash((BcryptHasher(),))

# SECRET_KEY for JWT
SECRET_KEY = "YOUR_SUPER_SECRET_KEY_CHANGE_THIS_IN_PRODUCTION"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24


def hash_password(password: str) -> str:
    """Hashes a plain text password safely."""
    return password_hash.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verifies a plain text password against the hashed password."""
    return password_hash.verify(plain_password, hashed_password)


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """Generates a JWT access token."""
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)

    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt