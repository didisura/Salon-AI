from typing import List
from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from database import get_session
from models import User
from routers.auth import get_current_user

router = APIRouter()

@router.get("/me", response_model=User)
def get_me(current_user: User = Depends(get_current_user)):
    return current_user

@router.get("/", response_model=List[User])
def get_users(session: Session = Depends(get_session)):
    return session.exec(select(User)).all()