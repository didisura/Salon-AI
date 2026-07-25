from fastapi import APIRouter, Depends
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from database import get_db, SessionLocal
from models import User
from schemas import UserCreate
from security import SECRET_KEY, ALGORITHM, create_access_token
import jwt

router = APIRouter(
    tags=["Authentication"]
)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/login")


def get_current_user(
    token: str = Depends(oauth2_scheme)
):
    db = SessionLocal()

    try:
        payload = jwt.decode(
            token,
            SECRET_KEY,
            algorithms=[ALGORITHM]
        )

        user_id = payload.get("user_id")

        if user_id is None:
            raise HTTPException(
                status_code=401,
                detail="Invalid token"
            )

        user = db.query(User).filter(
            User.id == user_id
        ).first()

        if user is None:
            raise Exception("User not found")

        return user

    except jwt.PyJWTError:
        raise Exception("Invalid token")

    finally:
        db.close()


@router.post("/signup")
def signup(
    user: UserCreate,
    db: Session = Depends(get_db)
):
    print("SIGNUP STARTED")
    print("Email:", user.email)

    try:
        existing_user = db.query(User).filter(
            User.email == user.email
        ).first()

        if existing_user:
            return {"error": "Email already exists"}

        new_user = User(
            name=user.name,
            phone=user.phone,
            email=user.email,
            password=user.password,
            role="owner"
        )

        db.add(new_user)
        db.commit()
        db.refresh(new_user)

        print("USER CREATED:", new_user.id)

        return {
            "message": "User created successfully",
            "user_id": new_user.id
        }

    except Exception as e:
        print("SIGNUP ERROR:", e)
        db.rollback()

        return {
            "error": str(e)
        }
    
@router.post("/login")
def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db)
):
    existing_user = db.query(User).filter(
        User.email == form_data.username
    ).first()

    if not existing_user:
        return {"error": "User not found"}

    if existing_user.password != form_data.password:
        return {"error": "Invalid password"}

    token = create_access_token(
        {
            "user_id": existing_user.id,
            "email": existing_user.email,
            "role": existing_user.role
        }
    )

    return {
        "access_token": token,
        "token_type": "bearer"
    }