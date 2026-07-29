import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    filters,
)
from datetime import datetime, timedelta
from sqlmodel import Session, select

# Import database models and settings from main.py
from main import (
    engine,
    Service,
    Staff,
    Appointment,
    TELEGRAM_BOT_TOKEN,
)

# Set up logging
logging.basicConfig(level=logging.INFO)

# Define Conversation States
SERVICE, STAFF, DATE, TIME, PHONE, CONFIRM = range(6)


def get_available_slots(staff_id: int, selected_date_str: str):
    base_slots = ["09:00 AM", "10:30 AM", "01:00 PM", "02:30 PM", "04:00 PM", "05:30 PM"]
    with Session(engine) as session:
        query = select(Appointment).where(
            Appointment.staff_id == staff_id,
            Appointment.appointment_time.like(f"{selected_date_str}%"),
            Appointment.status != "CANCELLED",
        )
        booked_appts = session.exec(query).all()
        booked_times = []
        for a in booked_appts:
            parts = a.appointment_time.rsplit(" ", 2)
            if len(parts) >= 2:
                booked_times.append(f"{parts[-2]} {parts[-1]}")

    return [slot for slot in base_slots if slot not in booked_times]


async def bot_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    with Session(engine) as session:
        services = session.exec(select(Service)).all()

    if not services:
        await update.message.reply_text("Welcome to Melkegna! No services are currently available.")
        return ConversationHandler.END

    keyboard = [
        [InlineKeyboardButton(f"{s.name} - {s.price} ETB ({s.duration_min} min)", callback_data=f"srv_{s.id}")]
        for s in services
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Welcome to Melkegna! 🌸\nPlease select a service:", reply_markup=reply_markup)
    return SERVICE


async def bot_select_service(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    service_id = int(query.data.split("_")[1])

    with Session(engine) as session:
        service = session.get(Service, service_id)
        staff_members = session.exec(select(Staff)).all()

    if not service or not staff_members:
        await query.edit_message_text("Selected service or staff unavailable.")
        return ConversationHandler.END

    context.user_data["service_id"] = service.id
    context.user_data["service_name"] = service.name
    context.user_data["service_price"] = service.price

    keyboard = [
        [InlineKeyboardButton(f"💇 {st.name} ({st.role or 'Stylist'})", callback_data=f"stf_{st.id}")]
        for st in staff_members
    ]
    await query.edit_message_text("Choose your preferred specialist:", reply_markup=InlineKeyboardMarkup(keyboard))
    return STAFF


async def bot_select_staff(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    staff_id = int(query.data.split("_")[1])

    with Session(engine) as session:
        staff = session.get(Staff, staff_id)

    if not staff:
        await query.edit_message_text("Staff member not found.")
        return ConversationHandler.END

    context.user_data["staff_id"] = staff.id
    context.user_data["staff_name"] = staff.name

    today = datetime.now()
    dates = [(today + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(3)]
    date_labels = ["Today", "Tomorrow", (today + timedelta(days=2)).strftime("%a, %b %d")]

    keyboard = [
        [InlineKeyboardButton(label, callback_data=f"dt_{d}")]
        for label, d in zip(date_labels, dates)
    ]
    await query.edit_message_text("Select an appointment date:", reply_markup=InlineKeyboardMarkup(keyboard))
    return DATE


async def bot_select_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    selected_date = query.data.split("_")[1]
    context.user_data["selected_date"] = selected_date

    slots = get_available_slots(context.user_data["staff_id"], selected_date)

    if not slots:
        await query.edit_message_text("No slots available for this staff member on that date.")
        return ConversationHandler.END

    keyboard = [[InlineKeyboardButton(t, callback_data=f"tm_{t}")] for t in slots]
    await query.edit_message_text(f"Available times for {selected_date}:", reply_markup=InlineKeyboardMarkup(keyboard))
    return TIME


async def bot_select_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    selected_time = query.data.split("_")[1]

    context.user_data["appointment_time"] = f"{context.user_data['selected_date']} {selected_time}"
    await query.edit_message_text("Please reply with your **Full Name**:")
    return PHONE


async def bot_get_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["customer_name"] = update.message.text
    await update.message.reply_text("Got it! Now, please share your **Phone Number**:")
    return CONFIRM


async def bot_get_phone_and_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["customer_phone"] = update.message.text
    ud = context.user_data

    with Session(engine) as session:
        existing = session.exec(
            select(Appointment).where(
                Appointment.staff_id == ud["staff_id"],
                Appointment.appointment_time == ud["appointment_time"],
                Appointment.status != "CANCELLED",
            )
        ).first()

        if existing:
            await update.message.reply_text("⚠️ Sorry, that exact slot was taken just now! Type /start to try another time.")
            return ConversationHandler.END

        appt = Appointment(
            telegram_id=update.effective_user.id,
            customer_name=ud["customer_name"],
            customer_phone=ud["customer_phone"],
            service_id=ud["service_id"],
            service_name=ud["service_name"],
            staff_id=ud["staff_id"],
            staff_name=ud["staff_name"],
            appointment_time=ud["appointment_time"],
            status="PENDING",
            booking_source="TELEGRAM"
        )
        session.add(appt)
        session.commit()

    summary = (
        f"✅ **Booking Request Received!**\n\n"
        f"**Service:** {ud['service_name']} ({ud['service_price']} ETB)\n"
        f"**Specialist:** {ud['staff_name']}\n"
        f"**Time:** {ud['appointment_time']}\n"
        f"**Name:** {ud['customer_name']}\n"
        f"**Phone:** {ud['customer_phone']}\n\n"
        f"Our reception will confirm your appointment shortly."
    )
    await update.message.reply_text(summary, parse_mode="Markdown")
    return ConversationHandler.END


async def bot_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Booking cancelled. Type /start whenever you're ready!")
    return ConversationHandler.END


def main():
    bot_app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", bot_start)],
        states={
            SERVICE: [CallbackQueryHandler(bot_select_service, pattern="^srv_")],
            STAFF: [CallbackQueryHandler(bot_select_staff, pattern="^stf_")],
            DATE: [CallbackQueryHandler(bot_select_date, pattern="^dt_")],
            TIME: [CallbackQueryHandler(bot_select_time, pattern="^tm_")],
            PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, bot_get_name)],
            CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, bot_get_phone_and_confirm)],
        },
        fallbacks=[CommandHandler("cancel", bot_cancel)],
    )

    bot_app.add_handler(conv_handler)
    print("🤖 Melkegna Telegram Bot is running...")
    bot_app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()