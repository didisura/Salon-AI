from typing import Optional, List
from pydantic import BaseModel, EmailStr

# ==========================================
# USER SCHEMAS
# ==========================================

class UserCreate(BaseModel):
    name: str
    phone: str
    email: EmailStr
    password: str


class UserLogin(BaseModel):
    email: EmailStr
    password: str


class UserResponse(BaseModel):
    id: int
    name: str
    phone: str
    email: EmailStr
    role: str

    class Config:
        from_attributes = True


class Token(BaseModel):
    access_token: str
    token_type: str


# ==========================================
# SALON SCHEMAS
# ==========================================

class SalonCreate(BaseModel):
    salon_name: str
    owner_name: str
    phone: str
    address: Optional[str] = None
    city: Optional[str] = None


class SalonResponse(BaseModel):
    id: int
    owner_id: int
    salon_name: str
    owner_name: str
    phone: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    subscription_status: str

    class Config:
        from_attributes = True


# ==========================================
# SERVICE SCHEMAS
# ==========================================

class ServiceCreate(BaseModel):
    salon_id: int
    service_name: str
    price: int
    duration: int


class ServiceResponse(BaseModel):
    id: int
    salon_id: int
    service_name: str
    price: int
    duration: int

    class Config:
        from_attributes = True


# ==========================================
# STAFF SCHEMAS
# ==========================================

class StaffCreate(BaseModel):
    salon_id: int
    name: str
    specialty: Optional[str] = None
    phone: Optional[str] = None
    photo: Optional[str] = None
    is_available: Optional[bool] = True


class StaffResponse(BaseModel):
    id: int
    salon_id: int
    name: str
    specialty: Optional[str] = None
    phone: Optional[str] = None
    photo: Optional[str] = None
    is_available: bool

    class Config:
        from_attributes = True


# ==========================================
# APPOINTMENT SCHEMAS
# ==========================================

class AppointmentCreate(BaseModel):
    salon_id: int
    staff_id: int
    service_id: int
    customer_name: str
    customer_phone: str
    appointment_date: str
    appointment_time: str
    deposit_amount: Optional[int] = 0
    language: Optional[str] = "am"  # Default to Amharic


class AppointmentStatusUpdate(BaseModel):
    status: str


class AppointmentResponse(BaseModel):
    id: int
    salon_id: int
    staff_id: int
    service_id: int
    customer_name: str
    customer_phone: str
    appointment_date: str
    appointment_time: str
    status: str
    deposit_amount: int
    payment_screenshot: Optional[str] = None
    language: str

    class Config:
        from_attributes = True