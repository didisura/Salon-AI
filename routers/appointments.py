import os
import shutil
from datetime import datetime, timedelta
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status, File, UploadFile
from sqlalchemy.orm import Session

from database import get_db
from models import Appointment, Service, Staff, Salon
from schemas import AppointmentCreate, AppointmentResponse, AppointmentStatusUpdate

router = APIRouter(
    prefix="/appointments",
    tags=["Appointments"]
)

# Helper function to parse datetimes and durations
def parse_time_slot(date_str: str, time_str: str, duration_minutes: int):
    start_dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    end_dt = start_dt + timedelta(minutes=duration_minutes)
    return start_dt, end_dt


# 1. CREATE APPOINTMENT
@router.post("/", response_model=AppointmentResponse, status_code=status.HTTP_201_CREATED)
def create_appointment(
    appointment: AppointmentCreate,
    db: Session = Depends(get_db)
):
    lang = getattr(appointment, "language", "am") or "am"

    # Verify Service exists
    service = db.query(Service).filter(Service.id == appointment.service_id).first()
    if not service:
        msg = "አገልግሎቱ አልተገኘም።" if lang == "am" else "Service not found."
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=msg)

    # Verify Staff exists
    staff = db.query(Staff).filter(Staff.id == appointment.staff_id).first()
    if not staff:
        msg = "ባለሙያው አልተገኘም።" if lang == "am" else "Staff member not found."
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=msg)

    # Calculate requested start and end times
    try:
        duration = int(service.duration) if service.duration else 30
        new_start, new_end = parse_time_slot(
            appointment.appointment_date,
            appointment.appointment_time,
            duration
        )
    except ValueError:
        msg = "የተሳሳተ ቀን ወይም ሰዓት አቀራረብ። እባክዎን YYYY-MM-DD እና HH:MM ይጠቀሙ።" if lang == "am" else "Invalid date or time format. Use YYYY-MM-DD and HH:MM."
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=msg)

    # Check for duration overlap with existing active bookings
    existing_appointments = db.query(Appointment).filter(
        Appointment.staff_id == appointment.staff_id,
        Appointment.appointment_date == appointment.appointment_date,
        Appointment.status != "cancelled"
    ).all()

    for existing in existing_appointments:
        existing_service = db.query(Service).filter(Service.id == existing.service_id).first()
        existing_duration = int(existing_service.duration) if existing_service and existing_service.duration else 30

        existing_start, existing_end = parse_time_slot(
            existing.appointment_date,
            existing.appointment_time,
            existing_duration
        )

        if new_start < existing_end and new_end > existing_start:
            msg = f"ባለሙያው ከሰዓት {existing_start.strftime('%H:%M')} እስከ {existing_end.strftime('%H:%M')} ሌላ ቀጠሮ አላቸው።" if lang == "am" else f"Staff member is busy from {existing_start.strftime('%H:%M')} to {existing_end.strftime('%H:%M')}."
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=msg)

    # Create new appointment (Default status: pending_deposit)
    new_appointment = Appointment(
        salon_id=appointment.salon_id,
        staff_id=appointment.staff_id,
        service_id=appointment.service_id,
        customer_name=appointment.customer_name,
        customer_phone=appointment.customer_phone,
        appointment_date=appointment.appointment_date,
        appointment_time=appointment.appointment_time,
        deposit_amount=getattr(appointment, "deposit_amount", 0) or 0,
        language=lang,
        status="pending_deposit"
    )

    db.add(new_appointment)
    db.commit()
    db.refresh(new_appointment)

    return new_appointment


# 2. UPLOAD DEPOSIT PAYMENT SCREENSHOT
@router.post("/{appointment_id}/upload-receipt")
def upload_payment_receipt(
    appointment_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db)
):
    appointment = db.query(Appointment).filter(Appointment.id == appointment_id).first()
    if not appointment:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="ቀጠሮው አልተገኘም / Appointment not found"
        )

    # Save file to uploads/receipts/
    os.makedirs("uploads/receipts", exist_ok=True)
    file_extension = os.path.splitext(file.filename)[1] or ".jpg"
    filename = f"receipt_app_{appointment_id}{file_extension}"
    file_path = os.path.join("uploads/receipts", filename)

    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    # Update appointment record
    appointment.payment_screenshot = f"/uploads/receipts/{filename}"
    appointment.status = "pending_approval"  # Awaiting salon owner review
    db.commit()

    is_amharic = appointment.language == "am"
    return {
        "message": "የክፍያ ስክሪንሾት በትክክል ተልኳል! ክፍያው ሲረጋገጥ ቀጠሮዎ ይፀድቃል።" if is_amharic else "Receipt uploaded successfully! Appointment pending approval.",
        "status": appointment.status,
        "receipt_url": appointment.payment_screenshot
    }


# 3. GET ALL APPOINTMENTS
@router.get("/", response_model=List[AppointmentResponse])
def get_appointments(
    salon_id: Optional[int] = None,
    staff_id: Optional[int] = None,
    status_filter: Optional[str] = None,
    db: Session = Depends(get_db)
):
    query = db.query(Appointment)
    if salon_id:
        query = query.filter(Appointment.salon_id == salon_id)
    if staff_id:
        query = query.filter(Appointment.staff_id == staff_id)
    if status_filter:
        query = query.filter(Appointment.status == status_filter)
        
    return query.all()


# 4. GET SPECIFIC APPOINTMENT BY ID
@router.get("/{appointment_id}", response_model=AppointmentResponse)
def get_appointment(
    appointment_id: int,
    db: Session = Depends(get_db)
):
    appointment = db.query(Appointment).filter(Appointment.id == appointment_id).first()
    if not appointment:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="ቀጠሮው አልተገኘም / Appointment not found"
        )
    return appointment


# 5. UPDATE APPOINTMENT STATUS (e.g. confirmed, completed, cancelled)
@router.patch("/{appointment_id}/status", response_model=AppointmentResponse)
def update_appointment_status(
    appointment_id: int,
    status_update: AppointmentStatusUpdate,
    db: Session = Depends(get_db)
):
    appointment = db.query(Appointment).filter(Appointment.id == appointment_id).first()
    if not appointment:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="ቀጠሮው አልተገኘም / Appointment not found"
        )

    appointment.status = status_update.status
    db.commit()
    db.refresh(appointment)

    return appointment


# 6. CANCEL APPOINTMENT
@router.delete("/{appointment_id}")
def cancel_appointment(
    appointment_id: int,
    db: Session = Depends(get_db)
):
    appointment = db.query(Appointment).filter(Appointment.id == appointment_id).first()
    if not appointment:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="ቀጠሮው አልተገኘም / Appointment not found"
        )

    appointment.status = "cancelled"
    db.commit()

    return {
        "message": "ቀጠሮው ተሰርዟል / Appointment cancelled successfully",
        "appointment_id": appointment_id
    }