"""
utils/localization.py
Centralized dictionary and helper functions for multilingual API messages.
"""

MESSAGES = {
    # 1. Appointment & Service Errors
    "service_not_found": {
        "am": "አገልግሎቱ አልተገኘም።",
        "en": "Service not found."
    },
    "staff_not_found": {
        "am": "ባለሙያው አልተገኘም።",
        "en": "Staff member not found."
    },
    "appointment_not_found": {
        "am": "ቀጠሮው አልተገኘም።",
        "en": "Appointment not found."
    },
    "invalid_datetime_format": {
        "am": "የተሳሳተ ቀን ወይም ሰዓት አቀራረብ። እባክዎን YYYY-MM-DD እና HH:MM ይጠቀሙ።",
        "en": "Invalid date or time format. Please use YYYY-MM-DD and HH:MM."
    },
    "staff_busy_slot": {
        "am": "ባለሙያው ከሰዓት {start} እስከ {end} ሌላ ቀጠሮ አላቸው።",
        "en": "Staff member is busy from {start} to {end}."
    },
    
    # 2. Payment & Receipts
    "receipt_uploaded_success": {
        "am": "የክፍያ ስክሪንሾት በትክክል ተልኳል። ክፍያው ሲረጋገጥ ቀጠሮዎ ይፀድቃል።",
        "en": "Receipt uploaded successfully! Appointment pending approval."
    },
    
    # 3. Actions & Lifecycle
    "appointment_cancelled": {
        "am": "ቀጠሮው ተሰርዟል።",
        "en": "Appointment cancelled successfully."
    }
}


def get_message(key: str, lang: str = "am", **kwargs) -> str:
    """
    Fetches the translation for a given message key and language code.
    Falls back to Amharic ("am") if the language code is invalid or missing.
    Supports string formatting via kwargs (e.g., start_time, end_time).
    """
    # Fallback language to "am" if an unsupported locale is passed
    locale = lang if lang in ["am", "en"] else "am"
    
    # Retrieve template dictionary or a generic fallback
    message_dict = MESSAGES.get(key, {})
    template = message_dict.get(locale, message_dict.get("am", "መረጃው አልተገኘም / Information not found"))
    
    # Format dynamics if placeholder arguments were passed
    if kwargs:
        try:
            return template.format(**kwargs)
        except KeyError:
            return template
            
    return template