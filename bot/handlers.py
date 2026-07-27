import uuid
from datetime import datetime, timedelta
from sqlmodel import Session, select
from telegram import (
    Update, 
    ReplyKeyboardMarkup, 
    KeyboardButton, 
    InlineKeyboardMarkup, 
    InlineKeyboardButton,
    InlineQueryResultArticle,
    InputTextMessageContent
)
from telegram.ext import ContextTypes

# Imports from parent directory (backend/database.py, backend/models.py)
from database import engine
from models import User, Salon, Service, Booking

# Bilingual Translation Dictionary
TEXTS = {
    "en": {
        "welcome": "Welcome to **Melkegna**, {name}! 💇✨\n\nSearch any salon by name, or choose from the menu below:",
        "menu_browse": "💇 Browse by Area",
        "menu_search": "🔍 Search Salon Name",
        "menu_bookings": "📅 My Bookings",
        "menu_lang": "🌐 Language / ቋንቋ",
        "select_area": "📍 Select your preferred area in Addis Ababa:",
        "select_category": "Select service type in **{area}**:",
        "prompt_search": "🔍 **Type the name of the salon you are looking for:**\n\n_Example: Bole Beauty Hub, Queen's Touch_",
        "search_results": "🔎 **Search results for '{query}':**",
        "no_results": "❌ No salons found matching '{query}'. Try searching another name or browse by area.",
        "hair": "Hair Salons / ፀጉር",
        "spa": "Nail & Spa / ስፓ",
        "barber": "Barbershops / ወንዶች",
        "no_bookings": "You currently have no active bookings.",
        "lang_select": "እባክዎ ቋንቋ ይምረጡ / Please select your language:",
        "lang_changed": "ቋንቋው ወደ አማርኛ ተቀይሯል። 🇪🇹",
        "select_service": "💈 **{salon_name}**\n📍 Location: {area}\n\nSelect a service to book:",
        "select_date": "📅 **{salon_name}** — {service_name}\nPrice: {price} ETB\n\nSelect booking date:",
        "select_time": "⏰ Select available time slot for **{date}**:",
        "confirm_prompt": "📋 **Confirm Your Appointment Details:**\n\n💈 **Salon:** {salon}\n📍 **Area:** {area}\n💇 **Service:** {service} ({price} ETB)\n📅 **Date:** {date}\n⏰ **Time:** {time}\n\nProceed to confirm?",
        "btn_confirm": "✅ Confirm Appointment",
        "btn_cancel": "❌ Cancel",
        "booking_success": "🎉 **Appointment Confirmed!**\n\n🆔 **Booking Ref:** `{ref}`\n💈 **Salon:** {salon}\n💇 **Service:** {service}\n📅 **Date:** {date} at {time}\n\nThank you for using Melkegna! Present this reference when you arrive.",
        "booking_cancelled": "❌ Booking process cancelled."
    },
    "am": {
        "welcome": "እንኳን ወደ **መልከኛ** በደህና መጡ, {name}! 💇✨\n\nየሳሎን ስም በቀጥታ በመጻፍ ይፈልጉ ወይም ከታች ያለውን ማውጫ ይጠቀሙ:",
        "menu_browse": "💇 በአካባቢ ይፈልጉ",
        "menu_search": "🔍 በሳሎን ስም ይፈልጉ",
        "menu_bookings": "📅 የእኔ ቀጠሮዎች",
        "menu_lang": "🌐 Language / ቋንቋ",
        "select_area": "📍 እባክዎ የሚፈልጉበትን አካባቢ ይምረጡ:",
        "select_category": "በ **{area}** አካባቢ የሚፈልጉትን የአገልግሎት ዓይነት ይምረጡ:",
        "prompt_search": "🔍 **የሚፈልጉትን የሳሎን ስም ይጻፉ:**\n\n_ምሳሌ፦ Bole Beauty Hub, Queen's Touch_",
        "search_results": "🔎 **ለ '{query}' የተገኙ ሳሎኖች፦**",
        "no_results": "❌ '{query}' የሚል ሳሎን አልተገኘም። እባክዎ ሌላ ስም ይሞክሩ ወይም በአካባቢ ይፈልጉ።",
        "hair": "የሴቶች ፀጉር ሳሎን",
        "spa": "የጥፍር እና ስፓ",
        "barber": "የወንዶች ፀጉር ቤት",
        "no_bookings": "ምንም ንቁ ቀጠሮ የሎትም።",
        "lang_select": "እባክዎ ቋንቋ ይምረጡ / Please select your language:",
        "lang_changed": "Language set to English. 🇬🇧",
        "select_service": "💈 **{salon_name}**\n📍 ቦታ፦ {area}\n\nእባክዎ የሚፈልጉትን አገልግሎት ይምረጡ፦",
        "select_date": "📅 **{salon_name}** — {service_name}\nዋጋ፦ {price} ETB\n\nቀን ይምረጡ፦",
        "select_time": "⏰ ለ **{date}** የሚመችዎትን ሰዓት ይምረጡ፦",
        "confirm_prompt": "📋 **የቀጠሮ መረጃ ማረጋገጫ፦**\n\n💈 **ሳሎን፦** {salon}\n📍 **ቦታ፦** {area}\n💇 **አገልግሎት፦** {service} ({price} ETB)\n📅 **ቀን፦** {date}\n⏰ **ሰዓት፦** {time}\n\nቀጠሮውን ማረጋገጥ ይፈልጋሉ?",
        "btn_confirm": "✅ ቀጠሮውን አረጋግጥ",
        "btn_cancel": "❌ ሰርዝ",
        "booking_success": "🎉 **ቀጠሮዎ በተሳካ ሁኔታ ተይዟል!**\n\n🆔 **የቀጠሮ መለያ፦** `{ref}`\n💈 **ሳሎን፦** {salon}\n💇 **አገልግሎት፦** {service}\n📅 **ቀን፦** {date} በ {time}\n\nመልከኛን ስለተጠቀሙ እናመሰግናለን! ሲደርሱ ይህንን የቀጠሮ መለያ ያሳዩ።",
        "booking_cancelled": "❌ የቀጠሮ መያዝ ሂደት ተሰርዟል።"
    }
}

# Helpers
def get_main_menu_keyboard(lang="am"):
    t = TEXTS.get(lang, TEXTS["am"])
    keyboard = [
        [KeyboardButton(t["menu_browse"]), KeyboardButton(t["menu_search"])],
        [KeyboardButton(t["menu_bookings"]), KeyboardButton(t["menu_lang"])]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_or_create_user(telegram_id: int, first_name: str) -> User:
    with Session(engine) as session:
        user = session.exec(select(User).where(User.telegram_id == telegram_id)).first()
        if not user:
            user = User(telegram_id=telegram_id, first_name=first_name, language="am")
            session.add(user)
            session.commit()
            session.refresh(user)
        return user

# Command: /start
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    user = get_or_create_user(tg_user.id, tg_user.first_name)
    lang = user.language
    t = TEXTS[lang]
    
    await update.message.reply_text(
        text=t["welcome"].format(name=user.first_name),
        parse_mode="Markdown",
        reply_markup=get_main_menu_keyboard(lang)
    )

# Text Messages Handler
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    tg_user = update.effective_user
    user = get_or_create_user(tg_user.id, tg_user.first_name)
    lang = user.language
    t = TEXTS[lang]

    if text == "🌐 Language / ቋንቋ":
        inline_keyboard = [
            [InlineKeyboardButton("አማርኛ 🇪🇹", callback_data="set_lang_am")],
            [InlineKeyboardButton("English 🇬🇧", callback_data="set_lang_en")]
        ]
        await update.message.reply_text(t["lang_select"], reply_markup=InlineKeyboardMarkup(inline_keyboard))

    elif text in [TEXTS["en"]["menu_browse"], TEXTS["am"]["menu_browse"]]:
        context.user_data["awaiting_search"] = False
        area_keyboard = [
            [InlineKeyboardButton("📍 Bole / ቦሌ", callback_data="area_Bole"), InlineKeyboardButton("📍 Lebu / ለቡ", callback_data="area_Lebu")],
            [InlineKeyboardButton("📍 Summit / ሰሚት", callback_data="area_Summit"), InlineKeyboardButton("📍 Piassa / ፒያሳ", callback_data="area_Piassa")],
            [InlineKeyboardButton("📍 CMC / ሲኤምሲ", callback_data="area_CMC"), InlineKeyboardButton("📍 Sarbet / ሳርቤት", callback_data="area_Sarbet")]
        ]
        await update.message.reply_text(t["select_area"], reply_markup=InlineKeyboardMarkup(area_keyboard))

    elif text in [TEXTS["en"]["menu_search"], TEXTS["am"]["menu_search"]]:
        context.user_data["awaiting_search"] = True
        await update.message.reply_text(t["prompt_search"], parse_mode="Markdown")

    elif text in [TEXTS["en"]["menu_bookings"], TEXTS["am"]["menu_bookings"]]:
        context.user_data["awaiting_search"] = False
        
        with Session(engine) as session:
            db_user = session.exec(select(User).where(User.telegram_id == tg_user.id)).first()
            if db_user:
                bookings = session.exec(
                    select(Booking).where(
                        Booking.user_id == db_user.id,
                        Booking.status == "confirmed"
                    )
                ).all()

                if bookings:
                    msg = "📅 **Your Active Bookings / የእርስዎ ንቁ ቀጠሮዎች:**\n\n"
                    for b in bookings:
                        salon = session.get(Salon, b.salon_id)
                        service = session.get(Service, b.service_id)
                        msg += f"🆔 Ref: `{b.booking_ref}`\n💈 **{salon.name if salon else 'Salon'}**\n💇 {service.name if service else 'Service'}\n📅 {b.booking_date} at {b.booking_time}\n───────────────\n"
                    await update.message.reply_text(msg, parse_mode="Markdown")
                else:
                    await update.message.reply_text(t["no_bookings"])
            else:
                await update.message.reply_text(t["no_bookings"])

    elif context.user_data.get("awaiting_search"):
        query = text.strip().lower()
        context.user_data["awaiting_search"] = False
        
        with Session(engine) as session:
            salons = session.exec(
                select(Salon).where(
                    (Salon.name.ilike(f"%{query}%")) | (Salon.area.ilike(f"%{query}%"))
                )
            ).all()

            if salons:
                buttons = [
                    [InlineKeyboardButton(f"💇 {s.name} ({s.area}) - {s.rating} ⭐", callback_data=f"salon_{s.id}")]
                    for s in salons
                ]
                await update.message.reply_text(
                    t["search_results"].format(query=text),
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(buttons)
                )
            else:
                await update.message.reply_text(t["no_results"].format(query=text))

    else:
        await update.message.reply_text("እባክዎ ከታች ያለውን ማውጫ ይጠቀሙ / Please use the menu buttons below.")

# Callback Query Handler
async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    tg_user = update.effective_user
    user = get_or_create_user(tg_user.id, tg_user.first_name)
    lang = user.language
    t = TEXTS[lang]
    data = query.data

    # Change Language
    if data.startswith("set_lang_"):
        new_lang = data.split("_")[2]
        with Session(engine) as session:
            db_user = session.exec(select(User).where(User.telegram_id == tg_user.id)).first()
            if db_user:
                db_user.language = new_lang
                session.add(db_user)
                session.commit()

        t_new = TEXTS[new_lang]
        await query.message.reply_text(t_new["lang_changed"], reply_markup=get_main_menu_keyboard(new_lang))

    # Select Area
    elif data.startswith("area_"):
        selected_area = data.split("_")[1]
        category_keyboard = [
            [InlineKeyboardButton(t["hair"], callback_data=f"cat_hair_{selected_area}")],
            [InlineKeyboardButton(t["spa"], callback_data=f"cat_spa_{selected_area}")],
            [InlineKeyboardButton(t["barber"], callback_data=f"cat_barber_{selected_area}")]
        ]
        await query.edit_message_text(
            t["select_category"].format(area=selected_area),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(category_keyboard)
        )

    # Select Category -> Query Salons
    elif data.startswith("cat_"):
        _, cat, area = data.split("_")
        
        with Session(engine) as session:
            salons = session.exec(
                select(Salon).where(Salon.area == area, Salon.category == cat)
            ).all()

            if not salons:
                salons = session.exec(select(Salon).where(Salon.area == area)).all()

            if salons:
                buttons = [
                    [InlineKeyboardButton(f"💈 {s.name} - {s.rating} ⭐", callback_data=f"salon_{s.id}")]
                    for s in salons
                ]
            else:
                all_salons = session.exec(select(Salon)).all()
                buttons = [
                    [InlineKeyboardButton(f"💈 {s.name} - {s.rating} ⭐", callback_data=f"salon_{s.id}")]
                    for s in all_salons[:5]
                ]

            await query.edit_message_text(
                f"📍 Salons in **{area}**:",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(buttons)
            )

    # Select Salon -> Query Services
    elif data.startswith("salon_"):
        salon_id = int(data.split("_")[1])
        
        with Session(engine) as session:
            salon = session.get(Salon, salon_id)
            if not salon:
                return

            context.user_data["draft_booking"] = {
                "salon_id": salon.id, 
                "salon_name": salon.name, 
                "area": salon.area
            }
            
            services = session.exec(select(Service).where(Service.salon_id == salon.id)).all()
            buttons = [
                [InlineKeyboardButton(f"{s.name} ({s.price_etb:.0f} ETB)", callback_data=f"srv_{s.id}")]
                for s in services
            ]
            
            await query.edit_message_text(
                t["select_service"].format(salon_name=salon.name, area=salon.area),
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(buttons)
            )

    # Select Service -> Select Date
    elif data.startswith("srv_"):
        srv_id = int(data.split("_")[1])
        
        with Session(engine) as session:
            service = session.get(Service, srv_id)
            if not service:
                return

            draft = context.user_data.get("draft_booking", {})
            draft["service_id"] = service.id
            draft["service_name"] = service.name
            draft["price"] = service.price_etb
            context.user_data["draft_booking"] = draft

            today = datetime.now()
            d0 = today.strftime("%Y-%m-%d")
            d1 = (today + timedelta(days=1)).strftime("%Y-%m-%d")
            d2 = (today + timedelta(days=2)).strftime("%Y-%m-%d")

            date_buttons = [
                [InlineKeyboardButton(f"Today ({d0})", callback_data=f"dt_{d0}")],
                [InlineKeyboardButton(f"Tomorrow ({d1})", callback_data=f"dt_{d1}")],
                [InlineKeyboardButton(f"In 2 days ({d2})", callback_data=f"dt_{d2}")]
            ]
            await query.edit_message_text(
                t["select_date"].format(
                    salon_name=draft.get("salon_name"),
                    service_name=service.name,
                    price=int(service.price_etb)
                ),
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(date_buttons)
            )

    # Select Date -> Select Time Slot
    elif data.startswith("dt_"):
        selected_date = data.split("_")[1]
        draft = context.user_data.get("draft_booking", {})
        draft["date"] = selected_date
        context.user_data["draft_booking"] = draft

        time_slots = [
            [InlineKeyboardButton("🌅 10:00 AM", callback_data="tm_10:00 AM"), InlineKeyboardButton("☀️ 02:00 PM", callback_data="tm_02:00 PM")],
            [InlineKeyboardButton("🌆 04:30 PM", callback_data="tm_04:30 PM"), InlineKeyboardButton("🌙 06:00 PM", callback_data="tm_06:00 PM")]
        ]
        await query.edit_message_text(
            t["select_time"].format(date=selected_date),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(time_slots)
        )

    # Select Time -> Confirm Prompt
    elif data.startswith("tm_"):
        selected_time = data.split("_")[1]
        draft = context.user_data.get("draft_booking", {})
        draft["time"] = selected_time
        context.user_data["draft_booking"] = draft

        confirm_keyboard = [
            [InlineKeyboardButton(t["btn_confirm"], callback_data="confirm_booking")],
            [InlineKeyboardButton(t["btn_cancel"], callback_data="cancel_booking")]
        ]

        await query.edit_message_text(
            t["confirm_prompt"].format(
                salon=draft.get("salon_name"),
                area=draft.get("area"),
                service=draft.get("service_name"),
                price=int(draft.get("price", 0)),
                date=draft.get("date"),
                time=selected_time
            ),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(confirm_keyboard)
        )

    # Save Booking to Database
    elif data == "confirm_booking":
        draft = context.user_data.get("draft_booking", {})
        ref_code = f"MLK-{uuid.uuid4().hex[:6].upper()}"
        
        with Session(engine) as session:
            db_user = session.exec(select(User).where(User.telegram_id == tg_user.id)).first()
            if db_user:
                booking = Booking(
                    booking_ref=ref_code,
                    user_id=db_user.id,
                    salon_id=draft["salon_id"],
                    service_id=draft["service_id"],
                    booking_date=draft["date"],
                    booking_time=draft["time"],
                    status="confirmed"
                )
                session.add(booking)
                session.commit()

        context.user_data["draft_booking"] = None

        await query.edit_message_text(
            t["booking_success"].format(
                ref=ref_code,
                salon=draft.get("salon_name"),
                service=draft.get("service_name"),
                date=draft.get("date"),
                time=draft.get("time")
            ),
            parse_mode="Markdown"
        )

    # Cancel Booking
    elif data == "cancel_booking":
        context.user_data["draft_booking"] = None
        await query.edit_message_text(t["booking_cancelled"])

# Inline Search
async def handle_inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.inline_query.query.lower()
    results = []
    
    with Session(engine) as session:
        salons = session.exec(select(Salon)).all()
        for salon in salons:
            if not query or query in salon.name.lower() or query in salon.area.lower():
                results.append(
                    InlineQueryResultArticle(
                        id=str(uuid.uuid4()),
                        title=f"{salon.name} ({salon.rating} ⭐)",
                        description=f"Location: {salon.area} | Category: {salon.category.capitalize()}",
                        input_message_content=InputTextMessageContent(
                            f"💈 **{salon.name}**\n📍 Location: {salon.area}\n⭐ Rating: {salon.rating}\n\nTap /start to book an appointment!",
                            parse_mode="Markdown"
                        )
                    )
                )
    await update.inline_query.answer(results[:10])