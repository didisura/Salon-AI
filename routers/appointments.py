from typing import List
from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session, select

from database import get_session
from models import Appointment, Service, Staff, Salon, User

router = APIRouter()

@router.get("/", response_model=List[Appointment])
def get_appointments(session: Session = Depends(get_session)):
    return session.exec(select(Appointment)).all()

@router.post("/", response_model=Appointment, status_code=status.HTTP_201_CREATED)
def create_appointment(appointment: Appointment, session: Session = Depends(get_session)):
    session.add(appointment)
    session.commit()
    session.refresh(appointment)
    return appointment

@router.get("/{appointment_id}", response_model=Appointment)
def get_appointment(appointment_id: int, session: Session = Depends(get_session)):
    appointment = session.get(Appointment, appointment_id)
    if not appointment:
        raise HTTPException(status_code=404, detail="Appointment not found")
    return appointment