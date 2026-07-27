import logging
import requests
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)
from config import TELEGRAM_BOT_TOKEN, API_BASE_URL

# Configure logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", 
    level=logging.INFO
)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start command: Welcomes user and fetches live salons from API."""
    user = update.effective_user
    welcome_text = (
        f"👋 Welcome to **Melkegna**, {user.first_name}!\n\n"
        "Book your beauty & wellness appointments directly in Telegram.\n"
        "Please select a salon to get started:"
    )

    try:
        response = requests.get(f"{API_BASE_URL}/salons/")
        if response.status_code == 200 and response.json():
            salons = response.json()
            keyboard = [
                [InlineKeyboardButton(salon["name"], callback_data=f"salon_{salon['id']}")]
                for salon in salons
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(
                welcome_text, 
                parse_mode="Markdown", 
                reply_markup=reply_markup
            )
        else:
            await update.message.reply_text("No salons available at the moment. Please try again shortly!")
    except Exception as e:
        logging.error(f"Error fetching salons: {e}")
        await update.message.reply_text("Unable to connect to Melkegna backend API.")

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles selection of Salons, Services, Staff, and Appointment creation."""
    query = update.callback_query
    await query.answer()

    data = query.data

    # 1. Salon Selected -> Fetch Services
    if data.startswith("salon_"):
        salon_id = data.split("_")[1]
        context.user_data["salon_id"] = int(salon_id)
        try:
            response = requests.get(f"{API_BASE_URL}/services/salon/{salon_id}")
            if response.status_code == 200 and response.json():
                services = response.json()
                keyboard = [
                    [
                        InlineKeyboardButton(
                            f"{srv['name']} ({srv['price_etb']} ETB)",
                            callback_data=f"service_{srv['id']}"
                        )
                    ]
                    for srv in services
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await query.edit_message_text(
                    text="✨ Choose a service:",
                    reply_markup=reply_markup
                )
            else:
                await query.edit_message_text("No services found for this salon.")
        except Exception as e:
            logging.error(f"Error fetching services: {e}")
            await query.edit_message_text("Error fetching service list.")

    # 2. Service Selected -> Fetch Staff
    elif data.startswith("service_"):
        service_id = data.split("_")[1]
        context.user_data["service_id"] = int(service_id)
        salon_id = context.user_data.get("salon_id")

        try:
            response = requests.get(f"{API_BASE_URL}/staff/salon/{salon_id}")
            if response.status_code == 200 and response.json():
                staff_members = response.json()
                keyboard = [
                    [
                        InlineKeyboardButton(
                            f"✂️ {staff['name']} ({staff['role']})",
                            callback_data=f"staff_{staff['id']}"
                        )
                    ]
                    for staff in staff_members
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await query.edit_message_text(
                    text="👤 Select your stylist / specialist:",
                    reply_markup=reply_markup
                )
            else:
                # If no staff found, skip directly to time slots
                await show_time_slots(query, context, staff_id=None)
        except Exception as e:
            logging.error(f"Error fetching staff: {e}")
            await query.edit_message_text("Error loading staff options.")

    # 3. Staff Selected -> Show Time Slots
    elif data.startswith("staff_"):
        staff_id = int(data.split("_")[1])
        context.user_data["staff_id"] = staff_id
        await show_time_slots(query, context, staff_id)

    # 4. Slot Selected -> Create Appointment in Database
    elif data.startswith("book_"):
        slot = data.replace("book_", "")
        salon_id = context.user_data.get("salon_id", 1)
        service_id = context.user_data.get("service_id", 1)
        staff_id = context.user_data.get("staff_id")

        # Set default test appointment time
        appointment_time = (datetime.utcnow() + timedelta(days=1)).isoformat()

        payload = {
            "user_id": 1,
            "salon_id": salon_id,
            "service_id": service_id,
            "staff_id": staff_id,
            "appointment_time": appointment_time,
            "status": "confirmed"
        }

        try:
            res = requests.post(f"{API_BASE_URL}/appointments/", json=payload)
            if res.status_code in [200, 201]:
                await query.edit_message_text(
                    text=f"🎉 **Appointment Confirmed!**\n\n"
                         f"🗓 **Time:** Tomorrow at {slot}\n"
                         f"📌 **Status:** Confirmed in Melkegna Database.\n\n"
                         f"Thank you for choosing Melkegna!",
                    parse_mode="Markdown"
                )
            else:
                await query.edit_message_text(f"Failed to record booking. Status: {res.status_code}")
        except Exception as e:
            logging.error(f"Error creating appointment: {e}")
            await query.edit_message_text("Error connecting to backend server.")

async def show_time_slots(query, context, staff_id):
    """Helper to render time slot choices."""
    slots = ["10:00 AM", "02:00 PM", "04:30 PM", "06:00 PM"]
    keyboard = [
        [InlineKeyboardButton(f"⏰ {slot}", callback_data=f"book_{slot}")]
        for slot in slots
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        text="📅 Choose your preferred time slot:",
        reply_markup=reply_markup
    )

def main():
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN_HERE":
        print("⚠️ Set your TELEGRAM_BOT_TOKEN in config.py before launching!")
        return

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_callback))

    print("🤖 Melkegna Telegram Bot starting...")
    app.run_polling()

if __name__ == "__main__":
    main()