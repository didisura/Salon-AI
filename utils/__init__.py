# utils/__init__.py
from .localization import get_message

# Add whichever file has your hash function:
from security import get_password_hash  # or from .security import hash_password

__all__ = ["get_message", "get_password_hash"]