import os
from dotenv import load_dotenv

# Load local .env file for local development
load_dotenv()

class Settings:
    PROJECT_NAME: str = "Melkegna"
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    # Falls back to Railway URL if API_BASE_URL isn't set in environment
    API_BASE_URL: str = os.getenv("API_BASE_URL", "https://web-production-e8bc1.up.railway.app")

settings = Settings()