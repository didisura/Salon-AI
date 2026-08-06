import os
from datetime import date, datetime, timedelta
from typing import Optional, List
from fastapi import FastAPI, Depends, HTTPException, status, Form, Request, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import SQLModel, Field, Session, create_engine, select, func, or_, and_
from jose import JWTError, jwt
from passlib.context import CryptContext

# ==========================================
# CONFIGURATION & SETUP
# ==========================================

SECRET_KEY = os.getenv("SECRET_KEY", "super-secret-key-change-in-production")
ALGORITHM = "HS256"
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./melkegna.db")

# Fix for PostgreSQL connection strings on Railway/Render (postgres:// to postgresql://)
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

connect_args = {"check_same_thread": False} if "sqlite" in DATABASE_URL else {}
engine = create_engine(DATABASE_URL, echo=False, connect_args=connect_args)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

app = FastAPI(title="Melkegna Salon Platform")

# Mount templates (assuming dashboard.html is inside 'templates' directory)
templates = Jinja2Templates(directory="templates")


# ==========================================
# DATABASE MODELS
# ==========================================

class Salon(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    owner_name: str
    username: str = Field(unique=True, index=True)
    password_hash: str
    slug: str = Field(unique=True, index=True)

class Service(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    salon_id: int = Field(foreign_key="salon.id", index=True)
    name: str
    price: float
    duration_minutes: int = 45

class Staff(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    salon_id: int = Field(foreign_key="salon.id", index=True)
    name: str

class Appointment(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    salon_id: int = Field(foreign_key="salon.id", index=True)
    service_id: int = Field(foreign_key="service.id")
    staff_id: int = Field(foreign_key="staff.id", index=True)
    customer_name: str
    customer_phone: str
    appointment_time: datetime = Field(index=True)
    status: str = Field(default="Confirmed")  # Confirmed, Completed, Cancelled, No-Show

class Waitlist(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    salon_id: int = Field(foreign_key="salon.id", index=True)
    service_id: int = Field(foreign_key="service.id")
    staff_id: Optional[int] = Field(default=None, foreign_key="staff.id")
    customer_name: str
    customer_phone: str
    preferred_date: date
    created_at: datetime = Field(default_factory=datetime.utcnow)


# Create tables on startup
@app.on_event("startup")
def on_startup():
    SQLModel.metadata.create_all(engine)


# ==========================================
# HELPER FUNCTIONS & AUTHENTICATION
# ==========================================

def get_db():
    with Session(engine) as session:
        yield session

def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password):
    return pwd_context.hash(password)

def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(days=7)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def get_current_salon_from_cookie(request: Request, db: Session = Depends(get_db)) -> Optional[Salon]:
    token = request.cookies.get("access_token")
    if not token:
        return None
    try:
        if token.startswith("Bearer "):
            token = token[7:]
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        salon_id: str = payload.get("sub")
        if salon_id is None:
            return None
    except JWTError:
        return None
    return db.get(Salon, int(salon_id))

def check_double_booking(db: Session, salon_id: int, staff_id: int, appt_time: datetime, service_duration: int, exclude_appt_id: Optional[int] = None) -> bool:
    """Returns True if there is a scheduling conflict for the staff member."""
    new_start = appt_time
    new_end = appt_time + timedelta(minutes=service_duration)

    query = select(Appointment, Service).join(Service, Appointment.service_id == Service.id).where(
        Appointment.salon_id == salon_id,
        Appointment.staff_id == staff_id,
        Appointment.status == "Confirmed"
    )
    if exclude_appt_id:
        query = query.where(Appointment.id != exclude_appt_id)

    existing_appointments = db.exec(query).all()

    for appt, srv in existing_appointments:
        existing_start = appt.appointment_time
        existing_end = appt.appointment_time + timedelta(minutes=srv.duration_minutes)

        if new_start < existing_end and new_end > existing_start:
            return True  # Conflict detected
    return False


# ==========================================
# AUTHENTICATION ROUTES
# ==========================================

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": None})

@app.post("/login")
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db)
):
    salon = db.exec(select(Salon).where(Salon.username == username)).first()
    if not salon or not verify_password(password, salon.password_hash):
        return templates.TemplateResponse("login.html", {"request": request, "error": "የተሳሳተ የተጠቃሚ ስም ወይም የደህንነት ቃል (Invalid credentials)"})

    token = create_access_token({"sub": str(salon.id)})
    response = RedirectResponse(url="/dashboard?tab=home", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(key="access_token", value=f"Bearer {token}", httponly=True)
    return response

@app.get("/logout")
def logout():
    response = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie("access_token")
    return response


# ==========================================
# ADMIN DASHBOARD ROUTES
# ==========================================

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(
    request: Request,
    tab: str = Query("home"),
    selected_date: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
    db: Session = Depends(get_db)
):
    salon = get_current_salon_from_cookie(request, db)
    if not salon:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    today = date.today()
    filter_date = datetime.strptime(selected_date, "%Y-%m-%d").date() if selected_date else today

    # Fetch Dashboard Metrics
    services = db.exec(select(Service).where(Service.salon_id == salon.id)).all()
    staff_members = db.exec(select(Staff).where(Staff.salon_id == salon.id)).all()

    # Base Query for Day Appointments
    day_start = datetime.combine(filter_date, datetime.min.time())
    day_end = datetime.combine(filter_date, datetime.max.time())

    appts_query = select(Appointment).where(
        Appointment.salon_id == salon.id,
        Appointment.appointment_time >= day_start,
        Appointment.appointment_time <= day_end
    ).order_by(Appointment.appointment_time.asc())
    raw_appts = db.exec(appts_query).all()

    # Hydrate Appointments with names for display
    appointments_list = []
    for appt in raw_appts:
        srv = db.get(Service, appt.service_id)
        stf = db.get(Staff, appt.staff_id)
        appointments_list.append({
            "id": appt.id,
            "appointment_time": appt.appointment_time.strftime("%I:%M %p"),
            "customer_name": appt.customer_name,
            "customer_phone": appt.customer_phone,
            "service_name": srv.name if srv else "N/A",
            "service_price": srv.price if srv else 0.0,
            "staff_name": stf.name if stf else "N/A",
            "status": appt.status
        })

    # Calculations for Metrics Cards
    today_start = datetime.combine(today, datetime.min.time())
    today_end = datetime.combine(today, datetime.max.time())
    
    today_completed_appts = db.exec(
        select(Appointment).where(
            Appointment.salon_id == salon.id,
            Appointment.status == "Completed",
            Appointment.appointment_time >= today_start,
            Appointment.appointment_time <= today_end
        )
    ).all()
    
    daily_rev = sum([db.get(Service, a.service_id).price for a in today_completed_appts if db.get(Service, a.service_id)])
    today_appt_count = len(raw_appts) if filter_date == today else len(db.exec(select(Appointment).where(Appointment.salon_id == salon.id, Appointment.appointment_time >= today_start, Appointment.appointment_time <= today_end)).all())
    
    total_customers_count = db.exec(select(func.count(func.distinct(Appointment.customer_phone))).where(Appointment.salon_id == salon.id)).one() or 0
    no_show_count_today = len([a for a in raw_appts if a["status"] == "No-Show"])

    # Waitlist Entries
    waitlist_raw = db.exec(select(Waitlist).where(Waitlist.salon_id == salon.id).order_by(Waitlist.created_at.desc())).all()
    waitlist_entries = []
    for w in waitlist_raw:
        srv = db.get(Service, w.service_id)
        stf = db.get(Staff, w.staff_id) if w.staff_id else None
        waitlist_entries.append({
            "id": w.id,
            "customer_name": w.customer_name,
            "customer_phone": w.customer_phone,
            "service_name": srv.name if srv else "N/A",
            "staff_name": stf.name if stf else "ማንኛውም (Any)",
            "preferred_date": w.preferred_date.strftime("%Y-%m-%d")
        })

    # Revenue Date Filter Calculations
    weekly_rev = 0.0
    monthly_rev = 0.0
    custom_rev = None

    if tab == "revenue":
        week_ago = datetime.combine(today - timedelta(days=7), datetime.min.time())
        month_ago = datetime.combine(today - timedelta(days=30), datetime.min.time())

        completed_week = db.exec(select(Appointment).where(Appointment.salon_id == salon.id, Appointment.status == "Completed", Appointment.appointment_time >= week_ago)).all()
        weekly_rev = sum([db.get(Service, a.service_id).price for a in completed_week if db.get(Service, a.service_id)])

        completed_month = db.exec(select(Appointment).where(Appointment.salon_id == salon.id, Appointment.status == "Completed", Appointment.appointment_time >= month_ago)).all()
        monthly_rev = sum([db.get(Service, a.service_id).price for a in completed_month if db.get(Service, a.service_id)])

        if start_date and end_date:
            c_start = datetime.combine(datetime.strptime(start_date, "%Y-%m-%d").date(), datetime.min.time())
            c_end = datetime.combine(datetime.strptime(end_date, "%Y-%m-%d").date(), datetime.max.time())
            custom_appts = db.exec(select(Appointment).where(Appointment.salon_id == salon.id, Appointment.status == "Completed", Appointment.appointment_time >= c_start, Appointment.appointment_time <= c_end)).all()
            custom_rev = sum([db.get(Service, a.service_id).price for a in custom_appts if db.get(Service, a.service_id)])

    booking_url = f"{request.url.scheme}://{request.url.netloc}/book/{salon.slug}"

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "salon": salon,
        "active_tab": tab,
        "error": error,
        "selected_date": filter_date.strftime("%Y-%m-%d"),
        "current_date": today.strftime("%Y-%m-%d"),
        "daily_rev": daily_rev,
        "today_appt_count": today_appt_count,
        "total_customers": total_customers_count,
        "no_show_count_today": no_show_count_today,
        "appointments": appointments_list,
        "waitlist_entries": waitlist_entries,
        "services": services,
        "staff_members": staff_members,
        "weekly_rev": weekly_rev,
        "monthly_rev": monthly_rev,
        "start_date": start_date or "",
        "end_date": end_date or "",
        "custom_rev": custom_rev,
        "booking_url": booking_url
    })


# ==========================================
# APPOINTMENT & WAITLIST ACTIONS
# ==========================================

@app.post("/book-walkin")
def book_walkin(
    request: Request,
    customer_name: str = Form(...),
    customer_phone: str = Form(...),
    service_id: int = Form(...),
    staff_id: int = Form(...),
    appointment_time: str = Form(...),
    db: Session = Depends(get_db)
):
    salon = get_current_salon_from_cookie(request, db)
    if not salon:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    appt_dt = datetime.strptime(appointment_time, "%Y-%m-%dT%H:%M")
    srv = db.get(Service, service_id)

    if srv and check_double_booking(db, salon.id, staff_id, appt_dt, srv.duration_minutes):
        return RedirectResponse(url="/dashboard?tab=home&error=conflict", status_code=status.HTTP_303_SEE_OTHER)

    new_appt = Appointment(
        salon_id=salon.id,
        service_id=service_id,
        staff_id=staff_id,
        customer_name=customer_name,
        customer_phone=customer_phone,
        appointment_time=appt_dt,
        status="Confirmed"
    )
    db.add(new_appt)
    db.commit()
    return RedirectResponse(url=f"/dashboard?tab=home&selected_date={appt_dt.strftime('%Y-%m-%d')}", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/update-appointment-status")
def update_appointment_status(
    request: Request,
    appointment_id: int = Form(...),
    status: str = Form(...),
    db: Session = Depends(get_db)
):
    salon = get_current_salon_from_cookie(request, db)
    if not salon:
        raise HTTPException(status_code=401, detail="Unauthorized")

    appt = db.get(Appointment, appointment_id)
    if appt and appt.salon_id == salon.id:
        previous_status = appt.status
        appt.status = status
        db.add(appt)
        db.commit()

        # If cancelled, check if waitlisted client can be offered this slot
        if status in ["Cancelled", "No-Show"] and previous_status == "Confirmed":
            db.refresh(appt)
            cancelled_date = appt.appointment_time.date()
            srv = db.get(Service, appt.service_id)
            
            # Look for suitable waitlisted customer
            waitlist_match = db.exec(
                select(Waitlist).where(
                    Waitlist.salon_id == salon.id,
                    Waitlist.service_id == appt.service_id,
                    Waitlist.preferred_date == cancelled_date,
                    or_(Waitlist.staff_id == appt.staff_id, Waitlist.staff_id == None)
                ).order_by(Waitlist.created_at.asc())
            ).first()

            if waitlist_match:
                # Auto convert matching waitlist customer to new booking slot
                auto_appt = Appointment(
                    salon_id=salon.id,
                    service_id=waitlist_match.service_id,
                    staff_id=appt.staff_id,
                    customer_name=waitlist_match.customer_name,
                    customer_phone=waitlist_match.customer_phone,
                    appointment_time=appt.appointment_time,
                    status="Confirmed"
                )
                db.add(auto_appt)
                db.delete(waitlist_match)
                db.commit()

    return {"status": "success", "new_status": status}


@app.post("/add-waitlist")
def add_waitlist(
    request: Request,
    customer_name: str = Form(...),
    customer_phone: str = Form(...),
    service_id: int = Form(...),
    staff_id: Optional[str] = Form(None),
    preferred_date: str = Form(...),
    db: Session = Depends(get_db)
):
    salon = get_current_salon_from_cookie(request, db)
    if not salon:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    parsed_date = datetime.strptime(preferred_date, "%Y-%m-%d").date()
    parsed_staff_id = int(staff_id) if staff_id and staff_id.strip() != "" else None

    w_entry = Waitlist(
        salon_id=salon.id,
        service_id=service_id,
        staff_id=parsed_staff_id,
        customer_name=customer_name,
        customer_phone=customer_phone,
        preferred_date=parsed_date
    )
    db.add(w_entry)
    db.commit()
    return RedirectResponse(url="/dashboard?tab=reserve", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/convert-waitlist/{waitlist_id}")
def convert_waitlist(
    waitlist_id: int,
    request: Request,
    appointment_time: str = Form(...),
    db: Session = Depends(get_db)
):
    salon = get_current_salon_from_cookie(request, db)
    if not salon:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    w_entry = db.get(Waitlist, waitlist_id)
    if w_entry and w_entry.salon_id == salon.id:
        appt_dt = datetime.strptime(appointment_time, "%Y-%m-%dT%H:%M")
        
        # Fallback to first available staff if waitlist entry didn't specify one
        staff_id = w_entry.staff_id
        if not staff_id:
            first_staff = db.exec(select(Staff).where(Staff.salon_id == salon.id)).first()
            if not first_staff:
                return RedirectResponse(url="/dashboard?tab=reserve&error=nostaff", status_code=status.HTTP_303_SEE_OTHER)
            staff_id = first_staff.id

        srv = db.get(Service, w_entry.service_id)
        if srv and check_double_booking(db, salon.id, staff_id, appt_dt, srv.duration_minutes):
            return RedirectResponse(url="/dashboard?tab=reserve&error=conflict", status_code=status.HTTP_303_SEE_OTHER)

        new_appt = Appointment(
            salon_id=salon.id,
            service_id=w_entry.service_id,
            staff_id=staff_id,
            customer_name=w_entry.customer_name,
            customer_phone=w_entry.customer_phone,
            appointment_time=appt_dt,
            status="Confirmed"
        )
        db.add(new_appt)
        db.delete(w_entry)
        db.commit()

    return RedirectResponse(url="/dashboard?tab=home", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/delete-waitlist")
def delete_waitlist(
    request: Request,
    waitlist_id: int = Form(...),
    db: Session = Depends(get_db)
):
    salon = get_current_salon_from_cookie(request, db)
    if not salon:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    w_entry = db.get(Waitlist, waitlist_id)
    if w_entry and w_entry.salon_id == salon.id:
        db.delete(w_entry)
        db.commit()
    return RedirectResponse(url="/dashboard?tab=reserve", status_code=status.HTTP_303_SEE_OTHER)


# ==========================================
# SERVICE & STAFF MANAGEMENT
# ==========================================

@app.post("/add-service")
def add_service(
    request: Request,
    name: str = Form(...),
    price: float = Form(...),
    duration_minutes: int = Form(45),
    db: Session = Depends(get_db)
):
    salon = get_current_salon_from_cookie(request, db)
    if not salon:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    service = Service(salon_id=salon.id, name=name, price=price, duration_minutes=duration_minutes)
    db.add(service)
    db.commit()
    return RedirectResponse(url="/dashboard?tab=services", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/delete-service")
def delete_service(
    request: Request,
    service_id: int = Form(...),
    db: Session = Depends(get_db)
):
    salon = get_current_salon_from_cookie(request, db)
    if not salon:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    srv = db.get(Service, service_id)
    if srv and srv.salon_id == salon.id:
        db.delete(srv)
        db.commit()
    return RedirectResponse(url="/dashboard?tab=services", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/add-staff")
def add_staff(
    request: Request,
    name: str = Form(...),
    db: Session = Depends(get_db)
):
    salon = get_current_salon_from_cookie(request, db)
    if not salon:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    stf = Staff(salon_id=salon.id, name=name)
    db.add(stf)
    db.commit()
    return RedirectResponse(url="/dashboard?tab=staff", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/delete-staff")
def delete_staff(
    request: Request,
    staff_id: int = Form(...),
    db: Session = Depends(get_db)
):
    salon = get_current_salon_from_cookie(request, db)
    if not salon:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    stf = db.get(Staff, staff_id)
    if stf and stf.salon_id == salon.id:
        db.delete(stf)
        db.commit()
    return RedirectResponse(url="/dashboard?tab=staff", status_code=status.HTTP_303_SEE_OTHER)


# ==========================================
# PUBLIC ONLINE CLIENT BOOKING ROUTE
# ==========================================

@app.get("/book/{slug}", response_class=HTMLResponse)
def client_booking_page(slug: str, request: Request, db: Session = Depends(get_db)):
    salon = db.exec(select(Salon).where(Salon.slug == slug)).first()
    if not salon:
        raise HTTPException(status_code=404, detail="Salon not found")

    services = db.exec(select(Service).where(Service.salon_id == salon.id)).all()
    staff_members = db.exec(select(Staff).where(Staff.salon_id == salon.id)).all()

    return templates.TemplateResponse("client_booking.html", {
        "request": request,
        "salon": salon,
        "services": services,
        "staff_members": staff_members
    })

@app.post("/book/{slug}")
def submit_client_booking(
    slug: str,
    customer_name: str = Form(...),
    customer_phone: str = Form(...),
    service_id: int = Form(...),
    staff_id: int = Form(...),
    appointment_time: str = Form(...),
    db: Session = Depends(get_db)
):
    salon = db.exec(select(Salon).where(Salon.slug == slug)).first()
    if not salon:
        raise HTTPException(status_code=404, detail="Salon not found")

    appt_dt = datetime.strptime(appointment_time, "%Y-%m-%dT%H:%M")
    srv = db.get(Service, service_id)

    if srv and check_double_booking(db, salon.id, staff_id, appt_dt, srv.duration_minutes):
        return HTMLResponse(content="<h3>እባክዎ ሌላ ሰዓት ይምረጡ - ይህ ሰዓት ተይዟል (Time slot unavailable)</h3><a href='javascript:history.back()'>ተመለስ</a>")

    new_appt = Appointment(
        salon_id=salon.id,
        service_id=service_id,
        staff_id=staff_id,
        customer_name=customer_name,
        customer_phone=customer_phone,
        appointment_time=appt_dt,
        status="Confirmed"
    )
    db.add(new_appt)
    db.commit()

    return HTMLResponse(content=f"<h2>ቀጠሮዎ በስኬት ተይዟል!</h2><p>እናመሰግናለን {customer_name}፣ በ {appt_dt.strftime('%Y-%m-%d %I:%M %p')} እንጠብቆታለን።</p>")