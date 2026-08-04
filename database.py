import os
from datetime import datetime
from typing import Optional
from sqlmodel import SQLModel, Field, Relationship, Session, create_engine
from sqlalchemy.orm import sessionmaker

# 1. DATABASE CONFIGURATION & POSTGRES COMPATIBILITY
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./app.db")

# Fix Railway/Heroku postgres:// schema prefix to postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# SQLite requires check_same_thread=False, Postgres does not
connect_args = {"check_same_thread": False} if "sqlite" in DATABASE_URL else {}

engine = create_engine(
    DATABASE_URL, 
    echo=True, 
    connect_args=connect_args
)

# Compatibility for standard SQLAlchemy routers expecting SessionLocal
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


# 2. HELPER FUNCTIONS & DEPENDENCIES
def create_db_and_tables():
    SQLModel.metadata.create_all(engine)

def get_session():
    with Session(engine) as session:
        yield session

# Aliases to fix router imports across the project
get_db = get_session


# 3. DATABASE MODELS
class Appointment(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    customer_name: str
    customer_phone: str
    appointment_time: datetime
    status: str = Field(default="Pending")  # Options: Pending, Confirmed, Completed, Cancelled
    
    service_id: int = Field(foreign_key="service.id")
    staff_id: Optional[int] = Field(default=None, foreign_key="staff.id")