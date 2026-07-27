from typing import List
from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session, select

from database import get_session
from models import Service

router = APIRouter()

@router.get("/", response_model=List[Service])
def get_services(session: Session = Depends(get_session)):
    return session.exec(select(Service)).all()

@router.get("/salon/{salon_id}", response_model=List[Service])
def get_services_by_salon(salon_id: int, session: Session = Depends(get_session)):
    return session.exec(select(Service).where(Service.salon_id == salon_id)).all()

@router.post("/", response_model=Service, status_code=status.HTTP_201_CREATED)
def create_service(service: Service, session: Session = Depends(get_session)):
    session.add(service)
    session.commit()
    session.refresh(service)
    return service