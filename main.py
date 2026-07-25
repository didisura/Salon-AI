import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from database import engine, Base

# Import all routers
from routers import users, auth, salons, services, staff, appointments

# 1. Create all database tables automatically if they don't exist
Base.metadata.create_all(bind=engine)

# 2. Initialize FastAPI Application
app = FastAPI(
    title="Melkegna API",
    description="Digital Appointment Platform for Beauty and Wellness Centers",
    version="1.0.0"
)

# 3. Configure CORS (Allows Mobile Apps, Telegram Bots, and Web Frontends to connect)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Adjust in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 4. Ensure upload folder exists for C2B deposit screenshots
os.makedirs("uploads/receipts", exist_ok=True)

# 5. Mount Static Files directory (Allows viewing receipts via http://127.0.0.1:8000/uploads/receipts/...)
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")


# 6. Root / Health-Check Endpoint
@app.get("/")
def root():
    return {
        "message": "እንኳን ወደ መልከኛ በደህና መጡ! (Welcome to Melkegna API)",
        "status": "online",
        "docs_url": "/docs"
    }


# 7. Register Routers
app.include_router(users.router)
app.include_router(auth.router)
app.include_router(salons.router)
app.include_router(services.router)
app.include_router(staff.router)
app.include_router(appointments.router)