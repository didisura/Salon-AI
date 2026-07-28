import os
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

from dotenv import load_dotenv
from sqlmodel import SQLModel, Session, select
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# Load environment variables
load_dotenv()

# Import models & engine from your project configuration/main setup
# (Ensure database models are imported or shared cleanly across modules)
from main import engine, Service, Staff, Customer, Appointment

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_RECEPTION_CHAT_ID = os.getenv("TELEGRAM_RECEPTION_CHAT_ID", "")

TIME_SLOTS = [
    "09:00 AM", "10:00 AM", "11:00 AM",
    "01:00 PM", "02:00 PM", "03:00 PM",
    "04:00 PM", "05:00 PM"
]

# Global reference to Telegram Application for sending external notifications
tg_app_global: Optional[Application] = None


async def send_reception_notification(message_text: str):
    """Helper function to send instant alerts to the reception Telegram chat."""
    if tg_app_global and TELEGRAM_RECEPTION_CHAT_ID:
        try:
            await tg_app_global.bot.send_message(
                chat_id=TELEGRAM_RECEPTION_CHAT_ID,
                text=message_text,
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.error(f"Failed to send Telegram notification: {e}")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /start command."""
    keyboard = [
        [InlineKeyboardButton("📅 Book Appointment", callback_data="book_start")],
        [InlineKeyboardButton("💈 View Services", callback_data="view_services")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    user_first_name = (
        update.effective_user.first_name if update.effective_user else "Valued Client"
    )
    await update.message.reply_text(
        f"Selam {user_first_name}! 👋\nWelcome to Melkegna Beauty Center.\n\nHow can we assist you today?",
        reply_markup=reply_markup,
    )


async def button_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles all inline keyboard button clicks for booking workflow."""
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
            await query.edit_message_text(
                text=text,
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )

    # 2. Step 1: Select Service
    elif query.data == "book_start":
        with Session(engine) as session:
            services = session.exec(select(Service)).all()
            if not services:
                await query.edit_message_text("No services currently available to book.")
                return

            keyboard = []
            for s in services:
                keyboard.append(
                    [
                        InlineKeyboardButton(
                            f"{s.name} ({s.price_etb:.0f} ETB)",
                            callback_data=f"select_service_{s.id}",
                        )
                    ]
                )

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
                keyboard.append(
                    [
                        InlineKeyboardButton(
                            f"💇 {st.name} ({st.role})",
                            callback_data=f"select_staff_{service.id}_{st.id}",
                        )
                    ]
                )

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
            [
                InlineKeyboardButton(
                    f"📅 Today ({day1})",
                    callback_data=f"select_date_{service_id}_{staff_id}_Today",
                )
            ],
            [
                InlineKeyboardButton(
                    f"📅 Tomorrow ({day2})",
                    callback_data=f"select_date_{service_id}_{staff_id}_Tomorrow",
                )
            ],
            [
                InlineKeyboardButton(
                    f"📅 {day3}",
                    callback_data=f"select_date_{service_id}_{staff_id}_{day3}",
                )
            ],
        ]

        await query.edit_message_text(
            "Step 3/4: Select your preferred day:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    # 5. Step 4: Select Time Slot
    elif query.data.startswith("select_date_"):
        parts = query.data.split("_")
        service_id, staff_id, chosen_date = int(parts[2]), int(parts[3]), parts[4]

        keyboard = []
        for i in range(0, len(TIME_SLOTS), 2):
            row = [
                InlineKeyboardButton(
                    TIME_SLOTS[i],
                    callback_data=f"confirm_{service_id}_{staff_id}_{chosen_date}_{TIME_SLOTS[i]}",
                ),
            ]
            if i + 1 < len(TIME_SLOTS):
                row.append(
                    InlineKeyboardButton(
                        TIME_SLOTS[i + 1],
                        callback_data=f"confirm_{service_id}_{staff_id}_{chosen_date}_{TIME_SLOTS[i + 1]}",
                    )
                )
            keyboard.append(row)

        await query.edit_message_text(
            f"Date selected: **{chosen_date}**\n\nStep 4/4: Choose an available time slot:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
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
                await query.edit_message_text(
                    "Error processing request. Please try again."
                )
                return

            existing_cust = session.exec(
                select(Customer).where(Customer.telegram_id == telegram_id)
            ).first()
            if not existing_cust:
                new_cust = Customer(
                    salon_id=1,
                    full_name=customer_name,
                    phone="Telegram Booking",
                    telegram_id=telegram_id,
                )
                session.add(new_cust)

            customer_phone_str = (
                f"Telegram (@{user.username})" if user.username else "Telegram User"
            )

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

            # Instant alert sent to your reception chat ID
            alert_msg = (
                f"🚨 **NEW BOOKING ALERT (Telegram Bot)**\n\n"
                f"👤 **Customer:** {customer_name} ({customer_phone_str})\n"
                f"💇 **Service:** {service.name}\n"
                f"👤 **Assigned Staff:** {staff.name}\n"
                f"⏰ **Scheduled Time:** {formatted_appointment_time}\n"
                f"💵 **Price:** {service.price_etb:.0f} ETB"
            )
            asyncio.create_task(send_reception_notification(alert_msg))


def create_bot_app() -> Application:
    """Initializes and configures the Telegram Application."""
    global tg_app_global

    if not TELEGRAM_BOT_TOKEN:
        logger.warning("TELEGRAM_BOT_TOKEN is missing or empty in environment!")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CallbackQueryHandler(button_callback_handler))

    tg_app_global = app
    return app