from sqlalchemy import Column, Integer, String, Boolean, ForeignKey
from sqlalchemy.orm import relationship
from database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    phone = Column(String, nullable=False)
    email = Column(String, unique=True, nullable=False, index=True)
    password = Column(String, nullable=False)
    role = Column(String, nullable=False)

    salons = relationship("Salon", back_populates="owner")


class Salon(Base):
    __tablename__ = "salons"

    id = Column(Integer, primary_key=True, index=True)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    salon_name = Column(String, nullable=False)
    owner_name = Column(String, nullable=False)
    phone = Column(String, unique=True, index=True)
    address = Column(String)
    city = Column(String)
    subscription_status = Column(String, default="inactive")
    
    # NEW: Payment Info for Customers
    payment_info = Column(String, nullable=True)  # e.g. "Telebirr: 0911223344 or CBE: 1000123456789"

    owner = relationship("User", back_populates="salons")
    services = relationship("Service", back_populates="salon", cascade="all, delete-orphan")
    staff = relationship("Staff", back_populates="salon", cascade="all, delete-orphan")
    appointments = relationship("Appointment", back_populates="salon", cascade="all, delete-orphan")


class Service(Base):
    __tablename__ = "services"

    id = Column(Integer, primary_key=True, index=True)
    salon_id = Column(Integer, ForeignKey("salons.id"), nullable=False)

    service_name = Column(String, nullable=False)
    price = Column(Integer, nullable=False)
    duration = Column(String)

    salon = relationship("Salon", back_populates="services")
    appointments = relationship("Appointment", back_populates="service")


class Staff(Base):
    __tablename__ = "staff"

    id = Column(Integer, primary_key=True, index=True)
    salon_id = Column(Integer, ForeignKey("salons.id"), nullable=False)

    name = Column(String, nullable=False)
    specialty = Column(String)
    phone = Column(String)
    photo = Column(String)
    is_available = Column(Boolean, default=True)

    salon = relationship("Salon", back_populates="staff")
    appointments = relationship("Appointment", back_populates="staff")


class Appointment(Base):
    __tablename__ = "appointments"

    id = Column(Integer, primary_key=True, index=True)
    salon_id = Column(Integer, ForeignKey("salons.id"), nullable=False)
    staff_id = Column(Integer, ForeignKey("staff.id"), nullable=False)
    service_id = Column(Integer, ForeignKey("services.id"), nullable=False)

    customer_name = Column(String, nullable=False)
    customer_phone = Column(String, nullable=False)
    appointment_date = Column(String, nullable=False)
    appointment_time = Column(String, nullable=False)
    status = Column(String, default="pending_deposit")  # Options: pending_deposit, pending_approval, confirmed, cancelled
    
    # NEW: Deposit & Screenshot Fields
    deposit_amount = Column(Integer, default=0)
    payment_screenshot = Column(String, nullable=True)  # File path to receipt image
    language = Column(String, default="am")  # "am" for Amharic, "en" for English

    salon = relationship("Salon", back_populates="appointments")
    staff = relationship("Staff", back_populates="appointments")
    service = relationship("Service", back_populates="appointments")