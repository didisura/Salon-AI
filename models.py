import enum

from sqlalchemy import (
    Column, Integer, String, Numeric, DateTime, Date, ForeignKey, Enum, func
)
from sqlalchemy.orm import relationship

from database import Base


class AppointmentStatus(str, enum.Enum):
    confirmed = "Confirmed"
    completed = "Completed"
    no_show = "No-Show"
    cancelled = "Cancelled"


class Salon(Base):
    __tablename__ = "salons"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(120), nullable=False)
    owner_name = Column(String(120), nullable=False)
    email = Column(String(255), unique=True, nullable=False, index=True)
    hashed_password = Column(String(255), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    services = relationship("Service", back_populates="salon", cascade="all, delete-orphan")
    staff_members = relationship("Staff", back_populates="salon", cascade="all, delete-orphan")
    appointments = relationship("Appointment", back_populates="salon", cascade="all, delete-orphan")
    waitlist_entries = relationship("Waitlist", back_populates="salon", cascade="all, delete-orphan")


class Service(Base):
    __tablename__ = "services"

    id = Column(Integer, primary_key=True, index=True)
    salon_id = Column(Integer, ForeignKey("salons.id"), nullable=False, index=True)
    name = Column(String(120), nullable=False)
    price = Column(Numeric(10, 2), nullable=False)
    duration_minutes = Column(Integer, nullable=False)

    salon = relationship("Salon", back_populates="services")


class Staff(Base):
    __tablename__ = "staff"

    id = Column(Integer, primary_key=True, index=True)
    salon_id = Column(Integer, ForeignKey("salons.id"), nullable=False, index=True)
    name = Column(String(120), nullable=False)

    salon = relationship("Salon", back_populates="staff_members")


class Appointment(Base):
    __tablename__ = "appointments"

    id = Column(Integer, primary_key=True, index=True)
    salon_id = Column(Integer, ForeignKey("salons.id"), nullable=False, index=True)
    customer_name = Column(String(120), nullable=False)
    customer_phone = Column(String(30), nullable=False, index=True)
    service_id = Column(Integer, ForeignKey("services.id"), nullable=False)
    staff_id = Column(Integer, ForeignKey("staff.id"), nullable=False)
    appointment_datetime = Column(DateTime, nullable=False, index=True)
    status = Column(Enum(AppointmentStatus), default=AppointmentStatus.confirmed, nullable=False)
    source = Column(String(20), default="walk-in")  # "walk-in" (admin) or "online" (public link)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    salon = relationship("Salon", back_populates="appointments")
    service = relationship("Service")
    staff = relationship("Staff")

    # ---- flattened accessors so the Jinja template can use appt.xxx directly ----
    @property
    def appointment_time(self):
        return self.appointment_datetime.strftime("%I:%M %p")

    @property
    def service_name(self):
        return self.service.name if self.service else ""

    @property
    def service_price(self):
        return float(self.service.price) if self.service else 0.0

    @property
    def staff_name(self):
        return self.staff.name if self.staff else ""


class Waitlist(Base):
    __tablename__ = "waitlist"

    id = Column(Integer, primary_key=True, index=True)
    salon_id = Column(Integer, ForeignKey("salons.id"), nullable=False, index=True)
    customer_name = Column(String(120), nullable=False)
    customer_phone = Column(String(30), nullable=False)
    service_id = Column(Integer, ForeignKey("services.id"), nullable=False)
    staff_id = Column(Integer, ForeignKey("staff.id"), nullable=True)
    preferred_date = Column(Date, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    salon = relationship("Salon", back_populates="waitlist_entries")
    service = relationship("Service")
    staff = relationship("Staff")

    @property
    def service_name(self):
        return self.service.name if self.service else ""

    @property
    def staff_name(self):
        return self.staff.name if self.staff else "Any"