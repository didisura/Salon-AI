import os
from dotenv import load_dotenv

# Load local .env file for local development
load_dotenv()

class Settings:
    PROJECT_NAME: str = "Melkegna"
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    API_BASE_URL: str = os.getenv("API_BASE_URL", "http://127.0.0.1:8000/api/v1")

settings = Settings()