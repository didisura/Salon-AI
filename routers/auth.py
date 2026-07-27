from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlmodel import Session, select

from database import get_session
from models import User

router = APIRouter()

# Simple token handler / bearer scheme
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login", auto_error=False)

def get_current_user(token: str = Depends(oauth2_scheme), session: Session = Depends(get_session)) -> User:
    """
    Dependency to retrieve current user from token or database.
    Fallback returns default demo user if token isn't passed during MVP testing.
    """
    if token:
        user = session.exec(select(User).where(User.phone_number == token)).first()
        if user:
            return user

    # Fallback to first user in DB or test fallback for unblocked development
    user = session.exec(select(User)).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials or no users in DB",
        )
    return user

@router.post("/register")
def register_user(user: User, session: Session = Depends(get_session)):
    existing_user = session.exec(select(User).where(User.phone_number == user.phone_number)).first()
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, 
            detail="Phone number already registered"
        )
    session.add(user)
    session.commit()
    session.refresh(user)
    return {"message": "User registered successfully", "user": user}

@router.post("/login")
def login_user(phone_number: str, session: Session = Depends(get_session)):
    user = session.exec(select(User).where(User.phone_number == phone_number)).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, 
            detail="User not found"
        )
    # Returning phone number as simple token for MVP
    return {"access_token": user.phone_number, "token_type": "bearer", "user": user}