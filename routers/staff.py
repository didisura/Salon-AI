from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from database import SessionLocal
from models import Staff
from schemas import StaffCreate

router = APIRouter(
    prefix="/staff",
    tags=["Staff"]
)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.post("", status_code=status.HTTP_201_CREATED)
def create_staff(
    staff: StaffCreate,
    db: Session = Depends(get_db)
):
    new_staff = Staff(
        salon_id=staff.salon_id,
        name=staff.name,
        specialty=staff.specialty,
        phone=staff.phone
    )

    db.add(new_staff)
    db.commit()
    db.refresh(new_staff)

    return {
        "message": "Staff created successfully",
        "staff_id": new_staff.id
    }


@router.get("/{salon_id}")
def get_staff(
    salon_id: int,
    db: Session = Depends(get_db)
):
    return db.query(Staff).filter(Staff.salon_id == salon_id).all()