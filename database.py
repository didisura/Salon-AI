from dataclasses import Field
import os
from sqlmodel import SQLModel, create_engine, Session
from sqlalchemy.orm import sessionmaker
from datetime import datetime
from typing import Optional
from sqlmodel import SQLModel, Field, Relationship

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./app.db")

engine = create_engine(
    DATABASE_URL, 
    echo=True, 
    connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {}
)

# Compatibility for standard SQLAlchemy routers expecting SessionLocal
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def create_db_and_tables():
    SQLModel.metadata.create_all(engine)

def get_session():
    with Session(engine) as session:
        yield session

# Aliases to fix router imports across the project
get_db = get_session

class Appointment(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    customer_name: str
    customer_phone: str
    appointment_time: datetime
    status: str = Field(default="Pending") # Pending, Confirmed, Completed, Cancelled
    
    service_id: int = Field(foreign_key="service.id")
    staff_id: Optional[int] = Field(default=None, foreign_key="staff.id")