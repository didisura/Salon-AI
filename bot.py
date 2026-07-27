import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    ConversationHandler,
    filters,
)
from sqlmodel import Session, select
from database import engine
from models import User, Appointment, Service, Salon, Staff

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

# Conversation states
SELECT_SERVICE, SELECT_TIME, INPUT_PHONE = range(3)

# -------------------------------------------------------------------
# Database Helpers
# -------------------------------------------------------------------
def get_or_create_user(telegram_id: int, full_name: str, phone: str = "N/A") -> User:
    with Session(engine) as session:
        user = session.exec(select(User).where(User.telegram_id == telegram_id)).first()
        if not user:
            user = User(
                telegram_id=telegram_id,
                full_name=full_name,
                phone_number=phone,
                role="customer"
            )
            session.add(user)
            session.commit()
            session.refresh(user)
        return user

def create_telegram_appointment(user_id: int, salon_id: int, service_id: int, time_str: str) -> Appointment:
    with Session(engine) as session:
        appointment = Appointment(
            user_id=user_id,
            salon_id=salon_id,
            service_id=service_id,
            appointment_time=time_str,
            status="confirmed"
        )
        session.add(appointment)
        session.commit()
        session.refresh(appointment)
        return appointment

# -------------------------------------------------------------------
# Bot Flow Handlers
# -------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    db_user = get_or_create_user(tg_user.id, tg_user.full_name)
    context.user_data["db_user_id"] = db_user.id

    # Fetch available services for Salon #1
    with Session(engine) as session:
        services = session.exec(select(Service).where(Service.salon_id == 1)).all()
        salon = session.exec(select(Salon).where(Salon.id == 1)).first()
        salon_name = salon.name if salon else "Melkegna Beauty"

    if not services:
        await update.message.reply_text("Welcome to Melkegna! Currently, no services are configured.\n\nእንኳን ወደ መልከኛ በደህና መጡ! በአሁኑ ጊዜ ምንም አገልግሎቶች አልተዘጋጁም።")
        return ConversationHandler.END

    keyboard = [
        [InlineKeyboardButton(f"{s.name} - {s.price_etb} ETB ({s.duration_min} min)", callback_data=f"service_{s.id}")]
        for s in services
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        f"👋 Welcome to **{salon_name}**!\n"
        f"እንኳን ወደ **{salon_name}** በደህና መጡ!\n\n"
        f"👇 Please select a service / እባክዎ የሚፈልጉትን አገልግሎት ይምረጡ:",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )
    return SELECT_SERVICE

async def service_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    service_id = int(query.data.split("_")[1])
    context.user_data["service_id"] = service_id

    # Time slot selections
    time_slots = ["10:00 AM", "11:30 AM", "02:00 PM", "03:30 PM", "05:00 PM"]
    keyboard = [
        [InlineKeyboardButton(t, callback_data=f"time_{t}")] for t in time_slots
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        "🕒 Great choice! Please select a preferred time slot:\n"
        "ታላቅ ምርጫ! እባክዎ የሚመችዎትን ሰዓት ይምረጡ:",
        reply_markup=reply_markup
    )
    return SELECT_TIME

async def time_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    time_str = query.data.split("_")[1]
    context.user_data["appointment_time"] = time_str

    await query.edit_message_text(
        "📱 Almost done! Please type your **phone number** so reception can confirm:\n"
        "ሊጠናቀቅ ነው። መቀበያው (ሪሴፕሽን) እንዲያረጋግጥልዎ እባክዎ **ስልክ ቁጥርዎን** ይጻፉ:"
    )
    return INPUT_PHONE

async def phone_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone_number = update.message.text.strip()
    tg_user = update.effective_user

    # Update user phone number in DB
    with Session(engine) as session:
        db_user = session.exec(select(User).where(User.telegram_id == tg_user.id)).first()
        if db_user:
            db_user.phone_number = phone_number
            session.add(db_user)
            session.commit()
            session.refresh(db_user)

    # Save Appointment
    service_id = context.user_data["service_id"]
    time_str = context.user_data["appointment_time"]
    appointment = create_telegram_appointment(db_user.id, 1, service_id, time_str)

    # Details for confirmation message
    with Session(engine) as session:
        service = session.exec(select(Service).where(Service.id == service_id)).first()
        service_name = service.name if service else "Service"

    await update.message.reply_text(
        f"✅ **Booking Confirmed! / ቀጠሮዎ ተረጋግጧል!**\n\n"
        f"📍 **Salon / ሳሎን:** Melkegna Beauty\n"
        f"💇 **Service / አገልግሎት:** {service_name}\n"
        f"⏰ **Time / ሰዓት:** {time_str}\n"
        f"📱 **Phone / ስልክ:** {phone_number}\n\n"
        f"Your booking has been sent to the reception dashboard. See you soon!\n"
        f"ቀጠሮዎ ወደ ሪሴፕሽን ሰሌዳ ተልኳል። በቅርቡ እንገናኛለን!",
        parse_mode="Markdown"
    )
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Booking cancelled. Type /start anytime to begin again.\nቀጠሮው ተሰርዟል። ለመጀመር /start ይጻፉ።")
    return ConversationHandler.END

# -------------------------------------------------------------------
# Main Bot Runner
# -------------------------------------------------------------------
if __name__ == "__main__":
    BOT_TOKEN = "8677551972:AAHc86kxF0_fKMiHzLNVWPV2LOVgEL955mY"

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            SELECT_SERVICE: [CallbackQueryHandler(service_selected, pattern="^service_")],
            SELECT_TIME: [CallbackQueryHandler(time_selected, pattern="^time_")],
            INPUT_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, phone_received)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(conv_handler)
    print("🤖 Melkegna Telegram Bot is running with Amharic & English support...")
    app.run_polling()