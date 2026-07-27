from typing import Optional, List
from datetime import datetime
from sqlmodel import SQLModel, Field, Relationship

class User(SQLModel, table=True):
    __tablename__ = "users"

    id: Optional[int] = Field(default=None, primary_key=True)
    telegram_id: int = Field(unique=True, index=True)
    full_name: str
    phone_number: Optional[str] = None
    role: str = Field(default="customer")  # "customer", "salon_owner", "admin"
    created_at: datetime = Field(default_factory=datetime.utcnow)

    # Relationships
    salons: List["Salon"] = Relationship(back_populates="owner")
    appointments: List["Appointment"] = Relationship(back_populates="user")


class Salon(SQLModel, table=True):
    __tablename__ = "salons"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    location: str
    phone_number: str
    owner_id: Optional[int] = Field(default=None, foreign_key="users.id")

    # Relationships
    owner: Optional[User] = Relationship(back_populates="salons")
    services: List["Service"] = Relationship(back_populates="salon")
    staff_members: List["Staff"] = Relationship(back_populates="salon")
    appointments: List["Appointment"] = Relationship(back_populates="salon")


class Service(SQLModel, table=True):
    __tablename__ = "services"

    id: Optional[int] = Field(default=None, primary_key=True)
    salon_id: int = Field(foreign_key="salons.id")
    name: str
    price_etb: float
    duration_min: int

    # Relationships
    salon: Salon = Relationship(back_populates="services")
    appointments: List["Appointment"] = Relationship(back_populates="service")


class Staff(SQLModel, table=True):
    __tablename__ = "staff"

    id: Optional[int] = Field(default=None, primary_key=True)
    salon_id: int = Field(foreign_key="salons.id")
    name: str
    role: str

    # Relationships
    salon: Salon = Relationship(back_populates="staff_members")
    appointments: List["Appointment"] = Relationship(back_populates="staff")


class Appointment(SQLModel, table=True):
    __tablename__ = "appointments"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id")
    salon_id: int = Field(foreign_key="salons.id")
    service_id: int = Field(foreign_key="services.id")
    staff_id: Optional[int] = Field(default=None, foreign_key="staff.id")
    appointment_time: str
    status: str = Field(default="pending")  # "pending", "confirmed", "cancelled"
    created_at: datetime = Field(default_factory=datetime.utcnow)

    # Relationships
    user: User = Relationship(back_populates="appointments")
    salon: Salon = Relationship(back_populates="appointments")
    service: Service = Relationship(back_populates="appointments")
    staff: Optional[Staff] = Relationship(back_populates="appointments")