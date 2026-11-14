from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import User
from ..security import current_user, hash_password, make_token, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginIn(BaseModel):
    email: str
    password: str


class PasswordIn(BaseModel):
    current_password: str
    new_password: str


def user_out(u: User, parent: User | None = None) -> dict:
    return {"id": u.id, "name": u.name, "email": u.email, "phone": u.phone, "role": u.role,
            "parent_id": u.parent_id, "parent_name": parent.name if parent else None,
            "is_active": u.is_active, "created_at": u.created_at}


@router.post("/login")
def login(body: LoginIn, db: Session = Depends(get_db)):
    u = db.scalar(select(User).where(User.email == body.email.strip().lower()))
    if not u or not verify_password(body.password, u.password_hash):
        raise HTTPException(401, "Wrong email or password")
    if not u.is_active:
        raise HTTPException(401, "This account has been disabled")
    return {"token": make_token(u), "user": user_out(u)}


@router.get("/me")
def me(u: User = Depends(current_user)):
    return user_out(u)


@router.post("/password")
def change_password(body: PasswordIn, u: User = Depends(current_user), db: Session = Depends(get_db)):
    if not verify_password(body.current_password, u.password_hash):
        raise HTTPException(400, "Current password is wrong")
    if len(body.new_password) < 8:
        raise HTTPException(422, "Password must be at least 8 characters")
    db.get(User, u.id).password_hash = hash_password(body.new_password)
    db.commit()
    return {"ok": True}
