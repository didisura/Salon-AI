import enum
import datetime

from sqlalchemy import (
    Column, Integer, String, Text, Numeric, DateTime, Date, Time,
    ForeignKey, func, JSON, TypeDecorator,
)
from sqlalchemy.orm import relationship

from database import Base


class AppointmentStatus(str, enum.Enum):
    confirmed = "Confirmed"
    completed = "Completed"
    no_show = "No-Show"
    cancelled = "Cancelled"
    pending_payment = "Pending Payment"


class AppointmentStatusType(TypeDecorator):
    """Store AppointmentStatus as VARCHAR; accept enum or string on bind/result."""
    impl = String(30)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, AppointmentStatus):
            return value.value
        if isinstance(value, str):
            for e in AppointmentStatus:
                if value == e.value or value == e.name:
                    return e.value
            return value
        return str(value)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        try:
            return AppointmentStatus(value)
        except Exception:
            for e in AppointmentStatus:
                if e.name == value:
                    return e
            return AppointmentStatus.confirmed


class Salon(Base):
    __tablename__ = "salons"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(120), nullable=False)
    slug = Column(String(140), unique=True, nullable=True, index=True)
    owner_name = Column(String(120), nullable=False)
    phone = Column(String(30), unique=True, nullable=False, index=True)
    address = Column(String(255), nullable=True)
    hashed_password = Column(String(255), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    status = Column(String(20), default="pending", nullable=False)
    subscription_expires_at = Column(DateTime, nullable=True)

    opening_time = Column(Time, nullable=False, default=datetime.time(8, 0))
    closing_time = Column(Time, nullable=False, default=datetime.time(20, 0))
    working_days = Column(String(20), nullable=False, default="0,1,2,3,4,5")

    cover_photo_url = Column(String(500), nullable=True)

    deposit_enabled = Column(Integer, nullable=False, default=0)
    payment_methods = Column(JSON, nullable=True)

    # --- Multi-branch ---
    # parent_id IS NULL  → root / HQ / independent salon
    # parent_id set      → branch of that HQ
    parent_id = Column(Integer, ForeignKey("salons.id"), nullable=True, index=True)
    location_name = Column(String(80), nullable=True)  # e.g. "Bole", "CMC"

    parent = relationship(
        "Salon",
        remote_side="Salon.id",
        back_populates="branches",
        foreign_keys=[parent_id],
    )
    branches = relationship(
        "Salon",
        back_populates="parent",
        foreign_keys=[parent_id],
        cascade="all, delete-orphan",
    )

    services = relationship("Service", back_populates="salon", cascade="all, delete-orphan")
    staff_members = relationship("Staff", back_populates="salon", cascade="all, delete-orphan")
    appointments = relationship("Appointment", back_populates="salon", cascade="all, delete-orphan")
    waitlist_entries = relationship("Waitlist", back_populates="salon", cascade="all, delete-orphan")
    gallery_images = relationship(
        "GalleryImage",
        back_populates="salon",
        cascade="all, delete-orphan",
        order_by="GalleryImage.id",
    )

    # ------------------------------------------------------------------
    # Branch helpers
    # ------------------------------------------------------------------

    @property
    def is_root(self) -> bool:
        return self.parent_id is None

    @property
    def is_branch(self) -> bool:
        return self.parent_id is not None

    @property
    def display_location(self) -> str:
        """Short label for the location switcher."""
        return (self.location_name or self.name or "").strip() or f"#{self.id}"

    @property
    def brand_name(self) -> str:
        """Parent business / HQ name for branding."""
        if self.parent_id and self.parent is not None:
            return self.parent.name
        return self.name

    def root_salon(self):
        """Walk up to the HQ / root node (guards against cycles)."""
        node = self
        seen = set()
        while getattr(node, "parent_id", None) and getattr(node, "parent", None) is not None:
            if node.id in seen:
                break
            seen.add(node.id)
            node = node.parent
        return node

    def effective_payment_methods(self):
        """Branch uses own list if set; otherwise inherit from root."""
        own = self.payment_methods
        if own:
            return own
        if self.parent_id and self.parent is not None:
            return self.parent.payment_methods or []
        return []

    def effective_deposit_enabled(self) -> bool:
        """Use this location's flag (copied from HQ on create if requested)."""
        return bool(self.deposit_enabled)

    # ------------------------------------------------------------------
    # Existing helpers
    # ------------------------------------------------------------------

    @property
    def working_days_set(self) -> set:
        raw = (self.working_days or "").strip()
        if not raw:
            return {0, 1, 2, 3, 4, 5, 6}
        return {int(d) for d in raw.split(",") if d.strip().isdigit()}

    @property
    def hours_label(self) -> str:
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

    @property
    def deposit_on(self) -> bool:
        return bool(self.deposit_enabled)


class GalleryImage(Base):
    __tablename__ = "gallery_images"

    id = Column(Integer, primary_key=True, index=True)
    salon_id = Column(Integer, ForeignKey("salons.id"), nullable=False, index=True)
    image_url = Column(String(500), nullable=False)
    caption = Column(String(200), nullable=True)
    category = Column(String(40), nullable=True, default="other")
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    salon = relationship("Salon", back_populates="gallery_images")


class Testimonial(Base):
    __tablename__ = "testimonials"

    id = Column(Integer, primary_key=True, index=True)
    salon_id = Column(Integer, ForeignKey("salons.id"), nullable=False, index=True)
    client_name = Column(String(120), nullable=False)
    comment = Column(String(500), nullable=False)
    rating = Column(Integer, nullable=False, default=5)
    service_label = Column(String(120), nullable=True)
    client_photo_url = Column(String(500), nullable=True)
    is_pinned = Column(Integer, nullable=False, default=0)
    is_celebrity = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class MediaAsset(Base):
    __tablename__ = "media_assets"

    id = Column(String(36), primary_key=True)
    salon_id = Column(Integer, ForeignKey("salons.id"), nullable=True, index=True)
    content_type = Column(String(80), nullable=False, default="image/jpeg")
    data = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Service(Base):
    __tablename__ = "services"

    id = Column(Integer, primary_key=True, index=True)
    salon_id = Column(Integer, ForeignKey("salons.id"), nullable=False, index=True)
    name = Column(String(120), nullable=False)
    price = Column(Numeric(10, 2), nullable=False)
    duration_minutes = Column(Integer, nullable=False)
    deposit_amount = Column(Numeric(10, 2), nullable=True, default=0)

    is_package = Column(Integer, nullable=False, default=0)
    min_people = Column(Integer, nullable=True, default=1)
    max_people = Column(Integer, nullable=True)
    includes_text = Column(String(600), nullable=True)
    allow_outside_hours = Column(Integer, nullable=False, default=0)
    photo_url = Column(String(500), nullable=True)
    extra_person_price = Column(Numeric(10, 2), nullable=True)
    is_active = Column(Integer, nullable=False, default=1)

    salon = relationship("Salon", back_populates="services")

    @property
    def is_pkg(self) -> bool:
        return bool(self.is_package)

    @property
    def allows_outside(self) -> bool:
        return bool(self.allow_outside_hours)

    def package_total(self, party_size: int = 1) -> float:
        base = float(self.price or 0)
        n = max(1, int(party_size or 1))
        mn = int(self.min_people or 1)
        mx = int(self.max_people) if self.max_people else None
        if n < mn:
            n = mn
        if mx and n > mx:
            n = mx
        extra = float(self.extra_person_price or 0)
        if extra > 0:
            return base + (n - 1) * extra
        return base


class Staff(Base):
    __tablename__ = "staff"

    id = Column(Integer, primary_key=True, index=True)
    salon_id = Column(Integer, ForeignKey("salons.id"), nullable=False, index=True)
    name = Column(String(120), nullable=False)
    photo_url = Column(String(500), nullable=True)
    opening_time = Column(Time, nullable=True)
    closing_time = Column(Time, nullable=True)
    working_days = Column(String(20), nullable=True)
    day_hours = Column(JSON, nullable=True)
    service_ids = Column(JSON, nullable=True)

    salon = relationship("Salon", back_populates="staff_members")
    day_offs = relationship(
        "StaffDayOff", back_populates="staff", cascade="all, delete-orphan",
        order_by="StaffDayOff.off_date",
    )

    def effective_hours(self, salon: "Salon"):
        open_t = self.opening_time or salon.opening_time
        close_t = self.closing_time or salon.closing_time
        return open_t, close_t

    def effective_working_days(self, salon: "Salon") -> set:
        if not self.working_days:
            return salon.working_days_set
        return {int(d) for d in self.working_days.split(",") if d.strip().isdigit()}

    def offers_service(self, service_id: int) -> bool:
        ids = self.service_ids
        if not ids:
            return True
        try:
            return int(service_id) in {int(x) for x in ids}
        except (TypeError, ValueError):
            return True

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
    service_id = Column(Integer, ForeignKey("services.id"), nullable=True)
    staff_id = Column(Integer, ForeignKey("staff.id"), nullable=False)
    service_name_snap = Column(String(120), nullable=True)
    service_price_snap = Column(Numeric(10, 2), nullable=True)
    appointment_datetime = Column(DateTime, nullable=False, index=True)
    status = Column(AppointmentStatusType(), default=AppointmentStatus.confirmed, nullable=False)
    source = Column(String(20), default="walk-in")
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    deposit_amount = Column(Numeric(10, 2), nullable=True, default=0)
    payment_method = Column(String(80), nullable=True)
    payment_screenshot_url = Column(String(500), nullable=True)
    payment_reviewed = Column(Integer, nullable=False, default=0)

    party_size = Column(Integer, nullable=True, default=1)

    salon = relationship("Salon", back_populates="appointments")
    service = relationship("Service")
    staff = relationship("Staff")

    @property
    def appointment_time(self) -> str:
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
    def appointment_time_western(self) -> str:
        dt = self.appointment_datetime
        h, m = dt.hour, dt.minute
        suffix = "AM" if h < 12 else "PM"
        h12 = h % 12
        if h12 == 0:
            h12 = 12
        return f"{h12}:{m:02d} {suffix}"

    @property
    def appointment_time_dual(self) -> str:
        return f"{self.appointment_time} · {self.appointment_time_western}"

    @property
    def service_name(self):
        if self.service:
            return self.service.name
        return self.service_name_snap or ""

    @property
    def service_price(self):
        if self.service_price_snap is not None:
            return float(self.service_price_snap or 0)
        if self.service:
            return float(self.service.price or 0)
        return 0.0

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