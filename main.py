import os
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Optional, List

from dotenv import load_dotenv
from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlmodel import SQLModel, Field, create_engine, Session, select
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# Load local environment variables from .env file
load_dotenv()

# ----------------------------------------------------
# Configuration & Environment Setup
# ----------------------------------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_RECEPTION_CHAT_ID = os.getenv("TELEGRAM_RECEPTION_CHAT_ID", "")

# Supports Render/Railway PostgreSQL automatically, falls back to local SQLite
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./melkegna.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

connect_args = {"check_same_thread": False} if "sqlite" in DATABASE_URL else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
templates = Jinja2Templates(directory="templates")

# Global reference to Telegram Application for external notifications
tg_app_global: Optional[Application] = None

TIME_SLOTS = [
    "09:00 AM", "10:00 AM", "11:00 AM",
    "01:00 PM", "02:00 PM", "03:00 PM",
    "04:00 PM", "05:00 PM"
]


# ----------------------------------------------------
# Database Models (SQLModel)
# ----------------------------------------------------
class Salon(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    phone: str
    address: str


class Service(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    salon_id: int
    name: str
    price_etb: float
    duration_min: int


class Staff(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    salon_id: int
    name: str
    role: str


class Customer(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    salon_id: int = 1
    full_name: str
    phone: str
    telegram_id: Optional[int] = None


class Appointment(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    salon_id: int = 1
    customer_name: str
    customer_phone: str
    service_name: str
    staff_name: str
    appointment_time: str
    status: str = "confirmed"


def get_db():
    with Session(engine) as session:
        yield session


# Helper function to send instant alerts to reception
async def send_reception_notification(message_text: str):
    if tg_app_global and TELEGRAM_RECEPTION_CHAT_ID:
        try:
            await tg_app_global.bot.send_message(
                chat_id=TELEGRAM_RECEPTION_CHAT_ID,
                text=message_text,
                parse_mode="Markdown"
            )
        except Exception as e:
            print(f"Failed to send Telegram notification: {e}")


# ----------------------------------------------------
# Telegram Bot Handlers
# ----------------------------------------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📅 Book Appointment", callback_data="book_start")],
        [InlineKeyboardButton("💈 View Services", callback_data="view_services")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    user_first_name = update.effective_user.first_name if update.effective_user else "Valued Client"
    await update.message.reply_text(
        f"Selam {user_first_name}! 👋\nWelcome to Melkegna Beauty Center.\n\nHow can we assist you today?",
        reply_markup=reply_markup,
    )


async def button_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    # 1. View Services
    if query.data == "view_services":
        with Session(engine) as session:
            services = session.exec(select(Service)).all()
            if not services:
                await query.edit_message_text("No services available at the moment.")
                return

            text = "💈 **Melkegna Services Menu**:\n\n"
            for s in services:
                text += f"• **{s.name}** — {s.price_etb:.0f} ETB ({s.duration_min} mins)\n"

            keyboard = [[InlineKeyboardButton("📅 Book Now", callback_data="book_start")]]
            await query.edit_message_text(text=text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

    # 2. Step 1: Select Service
    elif query.data == "book_start":
        with Session(engine) as session:
            services = session.exec(select(Service)).all()
            if not services:
                await query.edit_message_text("No services currently available to book.")
                return

            keyboard = []
            for s in services:
                keyboard.append([InlineKeyboardButton(f"{s.name} ({s.price_etb:.0f} ETB)", callback_data=f"select_service_{s.id}")])

            await query.edit_message_text(
                "Step 1/4: Choose a service for your appointment:",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )

    # 3. Step 2: Select Staff
    elif query.data.startswith("select_service_"):
        service_id = int(query.data.split("_")[2])
        with Session(engine) as session:
            service = session.get(Service, service_id)
            staff_members = session.exec(select(Staff)).all()

            if not service:
                await query.edit_message_text("Selected service not found.")
                return

            keyboard = []
            for st in staff_members:
                keyboard.append([InlineKeyboardButton(f"💇 {st.name} ({st.role})", callback_data=f"select_staff_{service.id}_{st.id}")])

            await query.edit_message_text(
                f"Selected: **{service.name}**\n\nStep 2/4: Choose your preferred stylist/specialist:",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )

    # 4. Step 3: Select Date
    elif query.data.startswith("select_staff_"):
        parts = query.data.split("_")
        service_id, staff_id = int(parts[2]), int(parts[3])

        today = datetime.now()
        day1 = today.strftime("%a, %b %d")
        day2 = (today + timedelta(days=1)).strftime("%a, %b %d")
        day3 = (today + timedelta(days=2)).strftime("%a, %b %d")

        keyboard = [
            [InlineKeyboardButton(f"📅 Today ({day1})", callback_data=f"select_date_{service_id}_{staff_id}_Today")],
            [InlineKeyboardButton(f"📅 Tomorrow ({day2})", callback_data=f"select_date_{service_id}_{staff_id}_Tomorrow")],
            [InlineKeyboardButton(f"📅 {day3}", callback_data=f"select_date_{service_id}_{staff_id}_{day3}")],
        ]

        await query.edit_message_text(
            "Step 3/4: Select your preferred day:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    # 5. Step 4: Select Time Slot
    elif query.data.startswith("select_date_"):
        parts = query.data.split("_")
        service_id, staff_id, chosen_date = int(parts[2]), int(parts[3]), parts[4]

        keyboard = []
        for i in range(0, len(TIME_SLOTS), 2):
            row = [
                InlineKeyboardButton(TIME_SLOTS[i], callback_data=f"confirm_{service_id}_{staff_id}_{chosen_date}_{TIME_SLOTS[i]}"),
            ]
            if i + 1 < len(TIME_SLOTS):
                row.append(InlineKeyboardButton(TIME_SLOTS[i+1], callback_data=f"confirm_{service_id}_{staff_id}_{chosen_date}_{TIME_SLOTS[i+1]}"))
            keyboard.append(row)

        await query.edit_message_text(
            f"Date selected: **{chosen_date}**\n\nStep 4/4: Choose an available time slot:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    # 6. Final Step: Confirm Booking & Notify Reception
    elif query.data.startswith("confirm_"):
        parts = query.data.split("_")
        service_id = int(parts[1])
        staff_id = int(parts[2])
        chosen_date = parts[3]
        chosen_time = parts[4]

        formatted_appointment_time = f"{chosen_date} at {chosen_time}"

        user = query.from_user
        customer_name = f"{user.first_name} {user.last_name or ''}".strip()
        telegram_id = user.id

        with Session(engine) as session:
            service = session.get(Service, service_id)
            staff = session.get(Staff, staff_id)

            if not service or not staff:
                await query.edit_message_text("Error processing request. Please try again.")
                return

            existing_cust = session.exec(select(Customer).where(Customer.telegram_id == telegram_id)).first()
            if not existing_cust:
                new_cust = Customer(
                    salon_id=1,
                    full_name=customer_name,
                    phone="Telegram Booking",
                    telegram_id=telegram_id,
                )
                session.add(new_cust)

            customer_phone_str = f"Telegram (@{user.username})" if user.username else "Telegram User"

            appt = Appointment(
                salon_id=1,
                customer_name=customer_name,
                customer_phone=customer_phone_str,
                service_name=service.name,
                staff_name=staff.name,
                appointment_time=formatted_appointment_time,
                status="confirmed",
            )
            session.add(appt)
            session.commit()

            # Client confirmation message
            await query.edit_message_text(
                f"🎉 **Booking Confirmed!**\n\n"
                f"👤 **Client:** {customer_name}\n"
                f"💈 **Service:** {service.name}\n"
                f"💇 **Stylist:** {staff.name}\n"
                f"💵 **Price:** {service.price_etb:.0f} ETB\n"
                f"⏰ **Appointment Time:** {formatted_appointment_time}\n\n"
                f"Thank you for choosing Melkegna! We look forward to seeing you.",
                parse_mode="Markdown",
            )

            # Instant alert sent to reception chat ID
            alert_msg = (
                f"🚨 **NEW BOOKING ALERT (Telegram Bot)**\n\n"
                f"👤 **Customer:** {customer_name} ({customer_phone_str})\n"
                f"💇 **Service:** {service.name}\n"
                f"👤 **Assigned Staff:** {staff.name}\n"
                f"⏰ **Scheduled Time:** {formatted_appointment_time}\n"
                f"💵 **Price:** {service.price_etb:.0f} ETB"
            )
            asyncio.create_task(send_reception_notification(alert_msg))


# ----------------------------------------------------
# FastAPI Application Lifespan
# ----------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    global tg_app_global
    SQLModel.metadata.create_all(engine)

    with Session(engine) as session:
        salon = session.get(Salon, 1)
        if not salon:
            demo_salon = Salon(id=1, name="Melkegna Beauty Center", phone="0911223344", address="Bole, Addis Ababa")
            session.add(demo_salon)
            session.commit()

            session.add(Service(salon_id=1, name="Classic Haircut & Styling", price_etb=350.0, duration_min=45))
            session.add(Service(salon_id=1, name="Full Beard Trim & Lineup", price_etb=200.0, duration_min=25))
            session.add(Service(salon_id=1, name="Gel Manicure", price_etb=450.0, duration_min=50))

            session.add(Staff(salon_id=1, name="Abebe Kebede", role="Senior Stylist"))
            session.add(Staff(salon_id=1, name="Tigist Haile", role="Nail Artist"))
            session.commit()

    if TELEGRAM_BOT_TOKEN:
        tg_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        tg_app.add_handler(CommandHandler("start", start_command))
        tg_app.add_handler(CallbackQueryHandler(button_callback_handler))

        await tg_app.initialize()
        await tg_app.start()
        await tg_app.updater.start_polling(drop_pending_updates=True)

        tg_app_global = tg_app
        print("🤖 Melkegna Telegram Bot is live with Reception Notifications!")
    else:
        print("⚠️ WARNING: TELEGRAM_BOT_TOKEN not found in environment!")

    yield

    if tg_app_global:
        await tg_app_global.updater.stop()
        await tg_app_global.stop()
        await tg_app_global.shutdown()


app = FastAPI(title="Melkegna Backend API", version="1.0", lifespan=lifespan)

# Allow cross-origin requests from frontend dashboards
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ----------------------------------------------------
# Root & Health Check Endpoints
# ----------------------------------------------------
@app.get("/", tags=["Health Check"])
def read_root():
    return {
        "status": "ok",
        "message": "Melkegna server is running",
        "documentation": "/docs"
    }


# ----------------------------------------------------
# API & Web Routes
# ----------------------------------------------------
@app.get("/dashboard", response_class=HTMLResponse)
def read_dashboard(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request})


@app.get("/api/reception/salons/{salon_id}/details")
def get_salon_details(salon_id: int, db: Session = Depends(get_db)):
    salon = db.get(Salon, salon_id)
    if not salon:
        raise HTTPException(status_code=404, detail="Salon not found")

    services = db.exec(select(Service).where(Service.salon_id == salon_id)).all()
    staff = db.exec(select(Staff).where(Staff.salon_id == salon_id)).all()

    return {
        "salon_name": salon.name,
        "services": services,
        "staff": staff
    }


@app.get("/api/reception/salons/{salon_id}/today-schedule")
def get_today_schedule(salon_id: int, db: Session = Depends(get_db)):
    appointments = db.exec(select(Appointment).where(Appointment.salon_id == salon_id)).all()

    schedule_list = []
    for appt in appointments:
        schedule_list.append({
            "appointment_id": appt.id,
            "customer_name": appt.customer_name,
            "customer_phone": appt.customer_phone,
            "service_name": appt.service_name,
            "assigned_staff": appt.staff_name,
            "appointment_time": appt.appointment_time,
            "status": appt.status.lower(),
            "booking_channel": "Telegram" if "Telegram" in appt.customer_phone else "Manual",
            "price_etb": 0.0,
            "duration_min": 0
        })

    return {"schedule": schedule_list}


class StatusUpdate(BaseModel):
    status: str


@app.patch("/api/reception/appointments/{app_id}/status")
def update_appointment_status(app_id: int, update: StatusUpdate, db: Session = Depends(get_db)):
    appt = db.get(Appointment, app_id)
    if not appt:
        raise HTTPException(status_code=404, detail="Appointment not found")

    appt.status = update.status.lower()
    db.add(appt)
    db.commit()

    alert_msg = f"ℹ️ **APPOINTMENT STATUS UPDATED**\n\nClient: {appt.customer_name}\nNew Status: **{appt.status.upper()}**"
    asyncio.create_task(send_reception_notification(alert_msg))

    return {"message": "Status updated successfully"}


class ServiceCreate(BaseModel):
    name: str
    price_etb: float
    duration_min: int


@app.post("/api/reception/salons/{salon_id}/services")
def create_service(salon_id: int, service_in: ServiceCreate, db: Session = Depends(get_db)):
    db_service = Service(
        salon_id=salon_id,
        name=service_in.name,
        price_etb=service_in.price_etb,
        duration_min=service_in.duration_min
    )
    db.add(db_service)
    db.commit()
    db.refresh(db_service)
    return {"message": "Service created successfully"}


@app.delete("/api/reception/services/{service_id}")
def delete_service(service_id: int, db: Session = Depends(get_db)):
    db_service = db.get(Service, service_id)
    if not db_service:
        raise HTTPException(status_code=404, detail="Service not found")
    db.delete(db_service)
    db.commit()
    return {"message": "Service deleted successfully"}


class StaffCreate(BaseModel):
    name: str
    role: str


@app.post("/api/reception/salons/{salon_id}/staff")
def create_staff(salon_id: int, staff_in: StaffCreate, db: Session = Depends(get_db)):
    db_staff = Staff(
        salon_id=salon_id,
        name=staff_in.name,
        role=staff_in.role
    )
    db.add(db_staff)
    db.commit()
    db.refresh(db_staff)
    return {"message": "Staff added successfully"}


@app.delete("/api/reception/staff/{staff_id}")
def delete_staff(staff_id: int, db: Session = Depends(get_db)):
    db_staff = db.get(Staff, staff_id)
    if not db_staff:
        raise HTTPException(status_code=404, detail="Staff not found")
    db.delete(db_staff)
    db.commit()
    return {"message": "Staff deleted successfully"}


@app.get("/api/reception/salons/{salon_id}/customers")
def get_customers(salon_id: int, db: Session = Depends(get_db)):
    appointments = db.exec(select(Appointment).where(Appointment.salon_id == salon_id)).all()

    customers_dict = {}
    for appt in appointments:
        phone = appt.customer_phone
        if phone not in customers_dict:
            customers_dict[phone] = {
                "name": appt.customer_name,
                "phone": phone,
                "total_visits": 0,
                "completed_visits": 0,
            }
        customers_dict[phone]["total_visits"] += 1
        if appt.status.lower() == "completed":
            customers_dict[phone]["completed_visits"] += 1

    return {"customers": list(customers_dict.values())}


class ManualBookingCreate(BaseModel):
    customer_name: str
    customer_phone: str
    salon_id: int
    service_id: int
    staff_id: Optional[int] = None
    appointment_time: str


@app.post("/api/reception/bookings/manual")
def create_manual_booking(payload: ManualBookingCreate, db: Session = Depends(get_db)):
    service = db.get(Service, payload.service_id)
    staff = db.get(Staff, payload.staff_id) if payload.staff_id else None

    appointment = Appointment(
        salon_id=payload.salon_id,
        customer_name=payload.customer_name,
        customer_phone=payload.customer_phone,
        service_name=service.name if service else "Manual Service",
        staff_name=staff.name if staff else "Any Available",
        appointment_time=payload.appointment_time,
        status="confirmed"
    )
    db.add(appointment)
    db.commit()

    alert_msg = (
        f"📝 **NEW MANUAL BOOKING CREATED**\n\n"
        f"👤 **Customer:** {payload.customer_name} ({payload.customer_phone})\n"
        f"💇 **Service:** {service.name if service else 'N/A'}\n"
        f"⏰ **Time:** {payload.appointment_time}"
    )
    asyncio.create_task(send_reception_notification(alert_msg))

    return {"message": "Booking created successfully"}