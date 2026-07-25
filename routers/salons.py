from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database import get_db
from models import Salon, User
from schemas import SalonCreate
from routers.auth import get_current_user

router = APIRouter(
    prefix="/salons",
    tags=["Salons"]
)


# Create Salon
@router.post("/")
def create_salon(
    salon: SalonCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    print("---> POST /salons request received <---")  # Debug line to confirm request reaches here

    new_salon = Salon(
        owner_id=current_user.id,
        salon_name=salon.salon_name,
        owner_name=salon.owner_name,
        phone=salon.phone,
        address=salon.address,
        city=salon.city
    )

    db.add(new_salon)
    db.commit()
    db.refresh(new_salon)

    return {
        "message": "Salon created successfully",
        "salon_id": new_salon.id
    }


# Get All Salons
@router.get("/")
def get_salons(
    db: Session = Depends(get_db)
):
    return db.query(Salon).all()


# Get One Salon
@router.get("/{salon_id}")
def get_salon(
    salon_id: int,
    db: Session = Depends(get_db)
):
    salon = db.query(Salon).filter(
        Salon.id == salon_id
    ).first()

    if not salon:
        raise HTTPException(
            status_code=404,
            detail="Salon not found"
        )

    return salon


# Update Salon
@router.put("/{salon_id}")
def update_salon(
    salon_id: int,
    salon: SalonCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    existing_salon = db.query(Salon).filter(
        Salon.id == salon_id
    ).first()

    if existing_salon.owner_id != current_user.id:
        raise HTTPException(
            status_code=403,
            detail="You are not allowed to update this salon"
        )

    existing_salon.salon_name = salon.salon_name
    existing_salon.owner_name = salon.owner_name
    existing_salon.phone = salon.phone
    existing_salon.address = salon.address
    existing_salon.city = salon.city

    db.commit()
    db.refresh(existing_salon)

    return {
        "message": "Salon updated successfully",
        "salon": existing_salon
    }

@router.delete("/{salon_id}")
def delete_salon(
    salon_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    salon = db.query(Salon).filter(
        Salon.id == salon_id
    ).first()

    if not salon:
        raise HTTPException(
            status_code=404,
            detail="Salon not found"
        )

    # Make sure the logged-in user owns this salon
    if salon.owner_id != current_user.id:
        raise HTTPException(
            status_code=403,
            detail="You are not allowed to delete this salon"
        )

    db.delete(salon)
    db.commit()

    return {
        "message": "Salon deleted successfully"
    }