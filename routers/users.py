from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

# Local imports
import models
from database import get_db
from security import get_password_hash  # <-- IMPORTS FROM YOUR NEW utils.py FILE!


router = APIRouter(prefix="/users", tags=["Users"])


# =====================================================================
# Pydantic Schemas
# =====================================================================

class UserCreate(BaseModel):
    email: EmailStr
    name: str
    phone: str
    password: str
    role: Optional[str] = "customer"

    class Config:
        # Pre-fills example data in Swagger UI
        json_schema_extra = {
            "example": {
                "email": "johndoe@example.com",
                "name": "John Doe",
                "phone": "+1234567890",
                "password": "StrongPassword123!",
                "role": "customer"
            }
        }


class UserResponse(BaseModel):
    id: int
    name: str
    phone: str
    email: EmailStr
    role: str

    class Config:
        from_attributes = True


# =====================================================================
# API Endpoints
# =====================================================================

# 1. CREATE USER (POST /users/)
@router.post(
    "/",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create User",
    description="Registers a new user in the database with a hashed password."
)
def create_user(user: UserCreate, db: Session = Depends(get_db)):
    # Check if user already exists
    existing_user = db.query(models.User).filter(models.User.email == user.email).first()
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A user with this email already exists."
        )

    # Hash the password using utils.py
    hashed_pwd = hash_password(user.password)

    # Create ORM Instance
    new_user = models.User(
        email=user.email,
        name=user.name,
        phone=user.phone,
        password=hashed_pwd,  # <-- MATCHES models.py
        role=user.role if hasattr(user, "role") and user.role else "customer"
    )

    # Save to Database
    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    return new_user


# 2. GET ALL USERS (GET /users/)
@router.get(
    "/",
    response_model=list[UserResponse],
    status_code=status.HTTP_200_OK,
    summary="Get All Users",
    description="Retrieves a list of all registered users."
)
def get_all_users(skip: int = 0, limit: int = 100, db: Session = Depends(get_db)):
    users = db.query(models.User).offset(skip).limit(limit).all()
    return users


# 3. GET SINGLE USER BY ID (GET /users/{user_id})
@router.get(
    "/{user_id}",
    response_model=UserResponse,
    status_code=status.HTTP_200_OK,
    summary="Get User By ID",
    description="Retrieves profile information for a single user by their ID."
)
def get_user_by_id(user_id: int, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User with ID {user_id} not found."
        )
    return user