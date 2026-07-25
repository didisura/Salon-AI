from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

# Database path (creates salonai.db in your root directory)
DATABASE_URL = "sqlite:///./salonai.db"

# connect_args is necessary ONLY for SQLite to allow multiple threads
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False}
)

# Session factory for handling database sessions
SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine
)

# Base class for your SQLAlchemy models
Base = declarative_base()


# Dependency to yield database sessions per request
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()