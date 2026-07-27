from typing import List
from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session, select

from database import get_session
from models import Salon, Service, Staff, User
from routers.auth import get_current_user

router = APIRouter()

@router.get("/", response_model=List[Salon])
def get_salons(session: Session = Depends(get_session)):
    return session.exec(select(Salon)).all()

@router.get("/{salon_id}", response_model=Salon)
def get_salon(salon_id: int, session: Session = Depends(get_session)):
    salon = session.get(Salon, salon_id)
    if not salon:
        raise HTTPException(status_code=404, detail="Salon not found")
    return salon

@router.post("/", response_model=Salon, status_code=status.HTTP_201_CREATED)
def create_salon(
    salon: Salon, 
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user)
):
    salon.owner_id = current_user.id
    session.add(salon)
    session.commit()
    session.refresh(salon)
    return salon