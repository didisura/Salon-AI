import enum
import datetime

from sqlalchemy import (
    Column, Integer, String, Numeric, DateTime, Date, Time, ForeignKey, Enum, func
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
    phone = Column(String(30), unique=True, nullable=False, index=True)
    address = Column(String(255), nullable=True)  # physical location shown on public booking
    hashed_password = Column(String(255), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    status = Column(String(20), default="pending", nullable=False)
    subscription_expires_at = Column(DateTime, nullable=True)

    opening_time = Column(Time, nullable=False, default=datetime.time(8, 0))
    closing_time = Column(Time, nullable=False, default=datetime.time(20, 0))
    working_days = Column(String(20), nullable=False, default="0,1,2,3,4,5")

    services = relationship("Service", back_populates="salon", cascade="all, delete-orphan")
    staff_members = relationship("Staff", back_populates="salon", cascade="all, delete-orphan")
    appointments = relationship("Appointment", back_populates="salon", cascade="all, delete-orphan")
    waitlist_entries = relationship("Waitlist", back_populates="salon", cascade="all, delete-orphan")

    @property
    def working_days_set(self) -> set[int]:
        raw = (self.working_days or "").strip()
        if not raw:
            return {0, 1, 2, 3, 4, 5, 6}
        return {int(d) for d in raw.split(",") if d.strip().isdigit()}

    @property
    def hours_label(self) -> str:
        """Ethiopian-style working hours, e.g. 'ጧት 2:00 – ማታ 2:00'"""
        def _fmt(t):
            total = (t.hour * 60 + t.minute - 360) % 1440
            eh, em = total // 60, total % 60
            if eh < 6:
                p, h = "ጧት", 12 if eh == 0 else eh
            elif eh < 12:
                p, h = "ቀን", eh
            elif eh < 18:
                p, h = "ማታ", 12 if eh == 12 else eh - 12
            else:
                p, h = "ለሊት", eh - 12
            return f"{p} {h}:{em:02d}"
        return f"{_fmt(self.opening_time)} – {_fmt(self.closing_time)}"

    @property
    def working_days_label(self) -> str:
        names = ["ሰኞ", "ማክሰኞ", "ረቡዕ", "ሐሙስ", "ዓርብ", "ቅዳሜ", "እሁድ"]
        days = sorted(self.working_days_set)
        return "፣ ".join(names[d] for d in days if 0 <= d <= 6)


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

    # NULL = inherit the salon's hours/days. Set = staff-specific override.
    opening_time = Column(Time, nullable=True)
    closing_time = Column(Time, nullable=True)
    working_days = Column(String(20), nullable=True)

    salon = relationship("Salon", back_populates="staff_members")
    day_offs = relationship(
        "StaffDayOff", back_populates="staff", cascade="all, delete-orphan",
        order_by="StaffDayOff.off_date"
    )

    def effective_hours(self, salon: "Salon"):
        open_t = self.opening_time or salon.opening_time
        close_t = self.closing_time or salon.closing_time
        return open_t, close_t

    def effective_working_days(self, salon: "Salon") -> set[int]:
        if not self.working_days:
            return salon.working_days_set
        return {int(d) for d in self.working_days.split(",") if d.strip().isdigit()}

    @property
    def hours_label(self) -> str:
        if self.opening_time and self.closing_time:
            def _fmt(t):
                total = (t.hour * 60 + t.minute - 360) % 1440
                eh, em = total // 60, total % 60
                if eh < 6:
                    p, h = "ጧት", 12 if eh == 0 else eh
                elif eh < 12:
                    p, h = "ቀን", eh
                elif eh < 18:
                    p, h = "ማታ", 12 if eh == 12 else eh - 12
                else:
                    p, h = "ለሊት", eh - 12
                return f"{p} {h}:{em:02d}"
            return f"{_fmt(self.opening_time)} – {_fmt(self.closing_time)}"
        return "እንደ ሳሎኑ (Same as salon)"

    @property
    def working_days_label(self) -> str:
        if not self.working_days:
            return "እንደ ሳሎኑ (Same as salon)"
        names = ["ሰኞ", "ማክሰኞ", "ረቡዕ", "ሐሙስ", "ዓርብ", "ቅዳሜ", "እሁድ"]
        days = sorted({int(d) for d in self.working_days.split(",") if d.strip().isdigit()})
        return "፣ ".join(names[d] for d in days if 0 <= d <= 6)


class StaffDayOff(Base):
    __tablename__ = "staff_day_off"

    id = Column(Integer, primary_key=True, index=True)
    staff_id = Column(Integer, ForeignKey("staff.id"), nullable=False, index=True)
    off_date = Column(Date, nullable=False)

    staff = relationship("Staff", back_populates="day_offs")


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
    source = Column(String(20), default="walk-in")
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    salon = relationship("Salon", back_populates="appointments")
    service = relationship("Service")
    staff = relationship("Staff")

    @property
    def appointment_time(self) -> str:
        """Ethiopian display, e.g. 'ጧት 3:15 ሰዓት'"""
        dt = self.appointment_datetime
        total = (dt.hour * 60 + dt.minute - 360) % 1440
        eh, em = total // 60, total % 60
        if eh < 6:
            p, h = "ጧት", 12 if eh == 0 else eh
        elif eh < 12:
            p, h = "ቀን", eh
        elif eh < 18:
            p, h = "ማታ", 12 if eh == 12 else eh - 12
        else:
            p, h = "ለሊት", eh - 12
        return f"{p} {h}:{em:02d} ሰዓት"

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
