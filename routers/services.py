from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from routers.auth import get_current_user
from database import get_db
from models import Service, Salon, User
from schemas import ServiceCreate

router = APIRouter(
    prefix="/services",
    tags=["Services"]
)


@router.post("/")
def create_service(
    service: ServiceCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    salon = db.query(Salon).filter(
        Salon.id == service.salon_id
    ).first()

    if not salon:
        return {"error": "Salon not found"}

    new_service = Service(
        salon_id=service.salon_id,
        service_name=service.service_name,
        price=service.price,
        duration=service.duration
    )

    db.add(new_service)
    db.commit()
    db.refresh(new_service)

    return {
        "message": "Service created successfully",
        "service_id": new_service.id
    }


@router.get("/{salon_id}")
def get_services(
    salon_id: int,
    db: Session = Depends(get_db)
):
    services = db.query(Service).filter(
        Service.salon_id == salon_id
    ).all()

    return services


@router.get("/service/{service_id}")
def get_service(
    service_id: int,
    db: Session = Depends(get_db)
):
    service = db.query(Service).filter(
        Service.id == service_id
    ).first()

    if service is None:
        return {"error": "Service not found"}

    return service

@router.put("/{service_id}")
def update_service(
    service_id: int,
    service: ServiceCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    existing_service = db.query(Service).filter(
        Service.id == service_id
    ).first()

    if not existing_service:
        raise HTTPException(
            status_code=404,
            detail="Service not found"
        )

    salon = db.query(Salon).filter(
        Salon.id == existing_service.salon_id
    ).first()

    if salon.owner_id != current_user.id:
        raise HTTPException(
            status_code=403,
            detail="You are not allowed to update this service"
        )

    existing_service.service_name = service.service_name
    existing_service.price = service.price
    existing_service.duration = service.duration

    db.commit()
    db.refresh(existing_service)

    return {
        "message": "Service updated successfully",
        "service": existing_service
    }

@router.delete("/{service_id}")
def delete_service(
    service_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    service = db.query(Service).filter(
        Service.id == service_id
    ).first()

    if service is None:
        return {"error": "Service not found"}

    db.delete(service)
    db.commit()

    return {"message": "Service deleted successfully"}