from typing import List
from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session, select

from database import get_session
from models import Staff

router = APIRouter()

@router.get("/", response_model=List[Staff])
def get_all_staff(session: Session = Depends(get_session)):
    return session.exec(select(Staff)).all()

@router.get("/salon/{salon_id}", response_model=List[Staff])
def get_staff_by_salon(salon_id: int, session: Session = Depends(get_session)):
    return session.exec(select(Staff).where(Staff.salon_id == salon_id)).all()

@router.post("/", response_model=Staff, status_code=status.HTTP_201_CREATED)
def create_staff(staff: Staff, session: Session = Depends(get_session)):
    session.add(staff)
    session.commit()
    session.refresh(staff)
    return staff