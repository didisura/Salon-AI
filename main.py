import datetime
import os
from typing import Optional, List

import jwt
from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from passlib.context import CryptContext
from sqlmodel import Field, Session, SQLModel, create_engine, select

# ==============================================================================
# 1. SECURITY & CONFIGURATION
# ==============================================================================

SECRET_KEY = os.getenv("SECRET_KEY", "SUPER_SECRET_MELKEGNA_KEY_CHANGE_THIS_IN_PRODUCTION")
ADMIN_SECRET_KEY = os.getenv("ADMIN_SECRET_KEY", "MELKEGNA_ADMIN_2026")
ALGORITHM = "HS256"
TOKEN_EXPIRE_HOURS = 24

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def hash_password(password: str) -> str:
    return pwd_context.hash(password)

def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)

def create_access_token(salon_id: int) -> str:
    payload = {
        "sub": str(salon_id),
        "exp": datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=TOKEN_EXPIRE_HOURS)
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)

def get_current_salon_id(request: Request) -> Optional[int]:
    token = request.cookies.get("access_token")
    if not token:
        return None
    try:
        if token.startswith("Bearer "):
            token = token.split(" ")[1]
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return int(payload.get("sub"))
    except (jwt.InvalidTokenError, ValueError):
        return None


# ==============================================================================
# 2. DATABASE MODELS & DYNAMIC ENGINE
# ==============================================================================

class Salon(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    owner_name: str
    phone: str = Field(unique=True, index=True)
    password_hash: str
    status: str = Field(default="pending")  # Options: pending, active, suspended, rejected
    subscription_expires_at: Optional[datetime.date] = None
    created_at: datetime.date = Field(default_factory=datetime.date.today)

class Service(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    salon_id: int = Field(foreign_key="salon.id", index=True)
    name: str
    price: float

class Staff(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    salon_id: int = Field(foreign_key="salon.id", index=True)
    name: str

class Appointment(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    salon_id: int = Field(foreign_key="salon.id", index=True)
    customer_name: str
    customer_phone: str
    service_id: int = Field(foreign_key="service.id")
    staff_id: int = Field(foreign_key="staff.id")
    appointment_time: str
    appointment_date: datetime.date = Field(default_factory=datetime.date.today)
    status: str = Field(default="Confirmed")  # Options: Confirmed, Completed, Cancelled

# Dynamic Database URL Configuration (PostgreSQL on Railway / SQLite locally)
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///melkegna.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

connect_args = {"check_same_thread": False} if "sqlite" in DATABASE_URL else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)

def get_session():
    with Session(engine) as session:
        yield session


# ==============================================================================
# 3. APPLICATION & DEPENDENCIES
# ==============================================================================

app = FastAPI(title="Melkegna Platform")
templates = Jinja2Templates(directory="templates")

@app.on_event("startup")
def on_startup():
    SQLModel.metadata.create_all(engine)


def get_active_salon(request: Request, db: Session = Depends(get_session)) -> Salon:
    """Check if the salon is logged in, approved, and has an active subscription."""
    salon_id = get_current_salon_id(request)
    if not salon_id:
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})

    salon = db.get(Salon, salon_id)
    if not salon:
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})

    today = datetime.date.today()

    # Check auto-expiration of subscription
    if salon.subscription_expires_at and salon.subscription_expires_at < today and salon.status == "active":
        salon.status = "suspended"
        db.add(salon)
        db.commit()

    # Redirect to status notification page if not active
    if salon.status != "active":
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/pending"})

    return salon


# ==============================================================================
# 4. AUTHENTICATION & STATUS ROUTES
# ==============================================================================

@app.get("/", response_class=HTMLResponse)
def root(request: Request):
    if get_current_salon_id(request):
        return RedirectResponse(url="/dashboard", status_code=status.HTTP_303_SEE_OTHER)
    return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

@app.get("/login", response_class=HTMLResponse)
def get_login(request: Request):
    if get_current_salon_id(request):
        return RedirectResponse(url="/dashboard", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse("auth.html", {"request": request, "mode": "login"})

@app.post("/login")
def post_login(
    request: Request,
    phone: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_session)
):
    salon = db.exec(select(Salon).where(Salon.phone == phone)).first()

    if not salon or not verify_password(password, salon.password_hash):
        return templates.TemplateResponse(
            "auth.html",
            {
                "request": request,
                "mode": "login",
                "error": "የስልክ ቁጥር ወይም የይለፍ ቃል ተሳስቷል (Invalid phone or password)"
            }
        )

    token = create_access_token(salon.id)
    response = RedirectResponse(url="/dashboard", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        key="access_token",
        value=f"Bearer {token}",
        httponly=True,
        samesite="lax"
    )
    return response

@app.get("/signup", response_class=HTMLResponse)
def get_signup(request: Request):
    if get_current_salon_id(request):
        return RedirectResponse(url="/dashboard", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse("auth.html", {"request": request, "mode": "signup"})

@app.post("/signup")
def post_signup(
    request: Request,
    salon_name: str = Form(...),
    owner_name: str = Form(...),
    phone: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_session)
):
    existing = db.exec(select(Salon).where(Salon.phone == phone)).first()
    if existing:
        return templates.TemplateResponse(
            "auth.html",
            {
                "request": request,
                "mode": "signup",
                "error": "ይህ ስልክ ቁጥር ቀደም ሲል ተመዝግቧል (Phone number already registered)"
            }
        )

    new_salon = Salon(
        name=salon_name,
        owner_name=owner_name,
        phone=phone,
        password_hash=hash_password(password),
        status="pending"
    )
    db.add(new_salon)
    db.commit()
    db.refresh(new_salon)

    # Add default service and staff member
    default_service = Service(salon_id=new_salon.id, name="Hair Styling / የፀጉር ስራ", price=500.0)
    default_staff = Staff(salon_id=new_salon.id, name="General Staff / ሰራተኛ")
    db.add(default_service)
    db.add(default_staff)
    db.commit()

    token = create_access_token(new_salon.id)
    response = RedirectResponse(url="/pending", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        key="access_token",
        value=f"Bearer {token}",
        httponly=True,
        samesite="lax"
    )
    return response

@app.get("/pending", response_class=HTMLResponse)
def pending_page(request: Request, db: Session = Depends(get_session)):
    salon_id = get_current_salon_id(request)
    if not salon_id:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    
    salon = db.get(Salon, salon_id)
    if not salon:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    if salon.status == "active":
        return RedirectResponse(url="/dashboard", status_code=status.HTTP_303_SEE_OTHER)

    return templates.TemplateResponse("pending.html", {"request": request, "salon": salon})

@app.get("/logout")
def logout():
    response = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(key="access_token")
    return response


# ==============================================================================
# 5. DASHBOARD ROUTE (PROTECTED BY STATUS CHECK)
# ==============================================================================

@app.get("/dashboard", response_class=HTMLResponse)
def get_dashboard(
    request: Request,
    tab: str = "home",
    selected_date: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    db: Session = Depends(get_session),
    salon: Salon = Depends(get_active_salon)
):
    salon_id = salon.id
    today = datetime.date.today()
    
    target_date = today
    if selected_date:
        try:
            target_date = datetime.datetime.strptime(selected_date, "%Y-%m-%d").date()
        except ValueError:
            target_date = today

    start_of_week = today - datetime.timedelta(days=today.weekday())
    start_of_month = today.replace(day=1)

    services = db.exec(select(Service).where(Service.salon_id == salon_id)).all()
    staff_members = db.exec(select(Staff).where(Staff.salon_id == salon_id)).all()

    service_map = {s.id: s for s in services}
    staff_map = {st.id: st for st in staff_members}

    schedule_appts = db.exec(
        select(Appointment)
        .where(Appointment.salon_id == salon_id)
        .where(Appointment.appointment_date == target_date)
    ).all()

    formatted_appts = []
    for appt in schedule_appts:
        srv = service_map.get(appt.service_id)
        stf = staff_map.get(appt.staff_id)
        formatted_appts.append({
            "id": appt.id,
            "appointment_time": appt.appointment_time,
            "customer_name": appt.customer_name,
            "customer_phone": appt.customer_phone,
            "service_name": srv.name if srv else "N/A",
            "service_price": srv.price if srv else 0.0,
            "staff_name": stf.name if stf else "N/A",
            "status": appt.status
        })

    completed_appts = db.exec(
        select(Appointment)
        .where(Appointment.salon_id == salon_id)
        .where(Appointment.status == "Completed")
    ).all()

    daily_rev = sum(
        service_map[a.service_id].price 
        for a in completed_appts 
        if a.service_id in service_map and a.appointment_date == today
    )
    weekly_rev = sum(
        service_map[a.service_id].price 
        for a in completed_appts 
        if a.service_id in service_map and a.appointment_date >= start_of_week
    )
    monthly_rev = sum(
        service_map[a.service_id].price 
        for a in completed_appts 
        if a.service_id in service_map and a.appointment_date >= start_of_month
    )

    custom_rev = None
    if start_date and end_date:
        try:
            s_date = datetime.datetime.strptime(start_date, "%Y-%m-%d").date()
            e_date = datetime.datetime.strptime(end_date, "%Y-%m-%d").date()
            custom_rev = sum(
                service_map[a.service_id].price 
                for a in completed_appts 
                if a.service_id in service_map and s_date <= a.appointment_date <= e_date
            )
        except ValueError:
            custom_rev = 0.0

    all_appts = db.exec(select(Appointment).where(Appointment.salon_id == salon_id)).all()
    unique_customers = len(set(a.customer_phone for a in all_appts))

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "salon": salon,
            "active_tab": tab,
            "daily_rev": daily_rev,
            "weekly_rev": weekly_rev,
            "monthly_rev": monthly_rev,
            "today_appt_count": len(schedule_appts),
            "total_customers": unique_customers,
            "appointments": formatted_appts,
            "services": services,
            "staff_members": staff_members,
            "selected_date": target_date.strftime("%Y-%m-%d"),
            "current_date": today.strftime("%Y-%m-%d"),
            "start_date": start_date or "",
            "end_date": end_date or "",
            "custom_rev": custom_rev
        }
    )


# ==============================================================================
# 6. ACTION ROUTES
# ==============================================================================

@app.post("/book-appointment")
def book_appointment(
    request: Request,
    customer_name: str = Form(...),
    customer_phone: str = Form(...),
    service_id: int = Form(...),
    staff_id: int = Form(...),
    appointment_time: str = Form(...),
    db: Session = Depends(get_session),
    salon: Salon = Depends(get_active_salon)
):
    appt_date = datetime.date.today()
    if "T" in appointment_time:
        try:
            date_part = appointment_time.split("T")[0]
            appt_date = datetime.datetime.strptime(date_part, "%Y-%m-%d").date()
        except ValueError:
            pass

    new_appt = Appointment(
        salon_id=salon.id,
        customer_name=customer_name,
        customer_phone=customer_phone,
        service_id=service_id,
        staff_id=staff_id,
        appointment_time=appointment_time,
        appointment_date=appt_date,
        status="Confirmed"
    )
    db.add(new_appt)
    db.commit()

    return RedirectResponse(url="/dashboard?tab=home", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/update-appointment-status")
def update_status(
    request: Request,
    appointment_id: int = Form(...),
    new_status: str = Form(..., alias="status"),
    db: Session = Depends(get_session),
    salon: Salon = Depends(get_active_salon)
):
    appt = db.get(Appointment, appointment_id)
    if appt and appt.salon_id == salon.id:
        appt.status = new_status
        db.add(appt)
        db.commit()

    return RedirectResponse(url="/dashboard?tab=home", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/add-service")
def add_service(
    request: Request,
    name: str = Form(...),
    price: float = Form(...),
    db: Session = Depends(get_session),
    salon: Salon = Depends(get_active_salon)
):
    new_service = Service(salon_id=salon.id, name=name, price=price)
    db.add(new_service)
    db.commit()

    return RedirectResponse(url="/dashboard?tab=services", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/delete-service")
def delete_service(
    request: Request,
    service_id: int = Form(...),
    db: Session = Depends(get_session),
    salon: Salon = Depends(get_active_salon)
):
    srv = db.get(Service, service_id)
    if srv and srv.salon_id == salon.id:
        db.delete(srv)
        db.commit()

    return RedirectResponse(url="/dashboard?tab=services", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/add-staff")
def add_staff(
    request: Request,
    name: str = Form(...),
    db: Session = Depends(get_session),
    salon: Salon = Depends(get_active_salon)
):
    new_staff = Staff(salon_id=salon.id, name=name)
    db.add(new_staff)
    db.commit()

    return RedirectResponse(url="/dashboard?tab=staff", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/delete-staff")
def delete_staff(
    request: Request,
    staff_id: int = Form(...),
    db: Session = Depends(get_session),
    salon: Salon = Depends(get_active_salon)
):
    stf = db.get(Staff, staff_id)
    if stf and stf.salon_id == salon.id:
        db.delete(stf)
        db.commit()

    return RedirectResponse(url="/dashboard?tab=staff", status_code=status.HTTP_303_SEE_OTHER)


# ==============================================================================
# 7. SUPER ADMIN PORTAL
# ==============================================================================

@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(
    request: Request, 
    key: str = "", 
    db: Session = Depends(get_session)
):
    if key != ADMIN_SECRET_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized Admin Access")

    salons = db.exec(select(Salon)).all()
    
    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Melkegna Super Admin</title>
        <script src="https://cdn.tailwindcss.com"></script>
    </head>
    <body class="bg-slate-900 text-white p-8">
        <h1 class="text-3xl font-bold mb-6">Melkegna Platform Admin</h1>
        <div class="bg-slate-800 rounded-lg p-6 shadow-xl overflow-x-auto">
            <table class="w-full text-left border-collapse">
                <thead>
                    <tr class="border-b border-slate-700 text-slate-400">
                        <th class="p-3">Salon Name</th>
                        <th class="p-3">Owner</th>
                        <th class="p-3">Phone</th>
                        <th class="p-3">Status</th>
                        <th class="p-3">Expires On</th>
                        <th class="p-3">Actions</th>
                    </tr>
                </thead>
                <tbody>
    """
    for s in salons:
        status_color = "text-yellow-400" if s.status == "pending" else "text-green-400" if s.status == "active" else "text-red-400"
        html_content += f"""
                    <tr class="border-b border-slate-700/50">
                        <td class="p-3 font-semibold">{s.name}</td>
                        <td class="p-3">{s.owner_name}</td>
                        <td class="p-3">{s.phone}</td>
                        <td class="p-3 {status_color} uppercase font-bold">{s.status}</td>
                        <td class="p-3">{s.subscription_expires_at or 'N/A'}</td>
                        <td class="p-3 flex gap-2">
                            <form action="/admin/approve/{s.id}" method="post">
                                <input type="hidden" name="key" value="{ADMIN_SECRET_KEY}">
                                <input type="number" name="days" value="30" class="w-16 text-black px-2 py-1 rounded">
                                <button type="submit" class="bg-green-600 px-3 py-1 rounded text-sm hover:bg-green-500">Approve / Extend</button>
                            </form>
                            <form action="/admin/suspend/{s.id}" method="post">
                                <input type="hidden" name="key" value="{ADMIN_SECRET_KEY}">
                                <button type="submit" class="bg-red-600 px-3 py-1 rounded text-sm hover:bg-red-500">Suspend</button>
                            </form>
                        </td>
                    </tr>
        """
    html_content += """
                </tbody>
            </table>
        </div>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)

@app.post("/admin/approve/{salon_id}")
def approve_salon_admin(
    salon_id: int,
    key: str = Form(...),
    days: int = Form(30),
    db: Session = Depends(get_session)
):
    if key != ADMIN_SECRET_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")

    salon = db.get(Salon, salon_id)
    if salon:
        today = datetime.date.today()
        base_date = salon.subscription_expires_at if (salon.subscription_expires_at and salon.subscription_expires_at > today) else today
        
        salon.status = "active"
        salon.subscription_expires_at = base_date + datetime.timedelta(days=days)
        db.add(salon)
        db.commit()

    return RedirectResponse(url=f"/admin?key={ADMIN_SECRET_KEY}", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/admin/suspend/{salon_id}")
def suspend_salon_admin(
    salon_id: int,
    key: str = Form(...),
    db: Session = Depends(get_session)
):
    if key != ADMIN_SECRET_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")

    salon = db.get(Salon, salon_id)
    if salon:
        salon.status = "suspended"
        db.add(salon)
        db.commit()

    return RedirectResponse(url=f"/admin?key={ADMIN_SECRET_KEY}", status_code=status.HTTP_303_SEE_OTHER)

# ==============================================================================
# 8. PUBLIC CLIENT BOOKING ROUTES (NO AUTH REQUIRED)
# ==============================================================================

@app.get("/book/{salon_id}", response_class=HTMLResponse)
def get_public_booking_page(
    salon_id: int, 
    request: Request, 
    db: Session = Depends(get_session)
):
    salon = db.get(Salon, salon_id)
    if not salon or salon.status != "active":
        raise HTTPException(status_code=404, detail="Salon not found or inactive")

    services = db.exec(select(Service).where(Service.salon_id == salon_id)).all()
    staff_members = db.exec(select(Staff).where(Staff.salon_id == salon_id)).all()

    return templates.TemplateResponse(
        "public_book.html",
        {
            "request": request,
            "salon": salon,
            "services": services,
            "staff_members": staff_members
        }
    )


@app.post("/public-book-appointment")
def post_public_appointment(
    salon_id: int = Form(...),
    customer_name: str = Form(...),
    customer_phone: str = Form(...),
    service_id: int = Form(...),
    staff_id: int = Form(...),
    appointment_time: str = Form(...),
    db: Session = Depends(get_session)
):
    salon = db.get(Salon, salon_id)
    if not salon or salon.status != "active":
        raise HTTPException(status_code=400, detail="Invalid salon")

    appt_date = datetime.date.today()
    if "T" in appointment_time:
        try:
            date_part = appointment_time.split("T")[0]
            appt_date = datetime.datetime.strptime(date_part, "%Y-%m-%d").date()
        except ValueError:
            pass

    new_appt = Appointment(
        salon_id=salon_id,
        customer_name=customer_name,
        customer_phone=customer_phone,
        service_id=service_id,
        staff_id=staff_id,
        appointment_time=appointment_time,
        appointment_date=appt_date,
        status="Confirmed"
    )
    db.add(new_appt)
    db.commit()

    return HTMLResponse(
        content="""
        <div style='text-align: center; font-family: sans-serif; padding: 50px;'>
            <h2 style='color: #16a34a;'>ቀጠሮዎ ተይዟል! (Booking Confirmed!)</h2>
            <p>እናመሰግናለን፤ በቅርቡ እንገናኛለን።</p>
        </div>
        """
    )