import os
from sqlmodel import SQLModel, create_engine, Session
from sqlalchemy.orm import sessionmaker

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