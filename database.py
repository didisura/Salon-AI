import os

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

# Loads variables from a .env file in the project root (if present) into
# the environment, so DATABASE_URL doesn't need to be set manually every
# time you open a new terminal.
load_dotenv()

# Set DATABASE_URL in your environment (or .env file) in production, e.g.:
# postgresql+psycopg2://user:password@host:5432/melkegna
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg2://postgres:postgres@localhost:5432/melkegna",
)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    """FastAPI dependency that yields a request-scoped DB session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()