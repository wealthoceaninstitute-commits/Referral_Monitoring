from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import ADMIN, MASTER, RA, Investor, User
from ..security import hash_password, require
from .auth import user_out

router = APIRouter(prefix="/users", tags=["users"])


class UserIn(BaseModel):
    name: str
    email: str
    password: str
    role: str
    phone: Optional[str] = None
    admin_id: Optional[int] = None  # master creating an RA says which admin owns it


class UserPatch(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None
    is_active: Optional[bool] = None
    password: Optional[str] = None
    admin_id: Optional[int] = None  # master moving an RA to another admin


def _can_manage(actor: User, target: User) -> bool:
    if actor.role == MASTER:
        return target.role != MASTER
    return target.role == RA and target.parent_id == actor.id


@router.get("")
def list_users(role: Optional[str] = None, actor: User = Depends(require(MASTER, ADMIN)), db: Session = Depends(get_db)):
    q = select(User).where(User.role != MASTER).order_by(User.role, User.name)
    if actor.role == ADMIN:
        q = q.where(User.role == RA, User.parent_id == actor.id)
    if role:
        q = q.where(User.role == role)
    users = list(db.scalars(q))
    parents = {u.id: u for u in db.scalars(select(User).where(User.role.in_([MASTER, ADMIN])))}
    clients = dict(db.execute(select(Investor.ra_id, func.count()).group_by(Investor.ra_id)).all())
    ras = dict(db.execute(select(User.parent_id, func.count()).where(User.role == RA).group_by(User.parent_id)).all())
    return [{**user_out(u, parents.get(u.parent_id)), "clients": clients.get(u.id, 0), "ras": ras.get(u.id, 0)} for u in users]


@router.post("", status_code=201)
def create_user(body: UserIn, actor: User = Depends(require(MASTER, ADMIN)), db: Session = Depends(get_db)):
    email = body.email.strip().lower()
    if "@" not in email:
        raise HTTPException(422, "Enter a valid email")
    if len(body.password) < 8:
        raise HTTPException(422, "Password must be at least 8 characters")
    if not body.name.strip():
        raise HTTPException(422, "Name is required")
    if body.role == ADMIN:
        if actor.role != MASTER:
            raise HTTPException(403, "Only the master can create admins")
        parent_id = actor.id
    elif body.role == RA:
        if actor.role == ADMIN:
            parent_id = actor.id
        else:
            admin = db.get(User, body.admin_id) if body.admin_id else None
            if not admin or admin.role != ADMIN:
                raise HTTPException(422, "Choose the admin this RA reports to")
            parent_id = admin.id
    else:
        raise HTTPException(422, "role must be ADMIN or RA")
    if db.scalar(select(User.id).where(User.email == email)):
        raise HTTPException(409, "That email is already in use")
    u = User(name=body.name.strip(), email=email, phone=body.phone, role=body.role, parent_id=parent_id,
             password_hash=hash_password(body.password))
    db.add(u)
    db.commit()
    return user_out(u, db.get(User, parent_id))


@router.patch("/{user_id}")
def update_user(user_id: int, body: UserPatch, actor: User = Depends(require(MASTER, ADMIN)), db: Session = Depends(get_db)):
    u = db.get(User, user_id)
    if not u or not _can_manage(actor, u):
        raise HTTPException(404, "User not found")
    if body.name is not None and body.name.strip():
        u.name = body.name.strip()
    if body.phone is not None:
        u.phone = body.phone or None
    if body.is_active is not None:
        u.is_active = body.is_active
    if body.password:
        if len(body.password) < 8:
            raise HTTPException(422, "Password must be at least 8 characters")
        u.password_hash = hash_password(body.password)
    if body.admin_id is not None:
        admin = db.get(User, body.admin_id)
        if actor.role != MASTER or u.role != RA or not admin or admin.role != ADMIN:
            raise HTTPException(422, "Only the master can move an RA to another admin")
        u.parent_id = admin.id
    db.commit()
    return user_out(u, db.get(User, u.parent_id) if u.parent_id else None)
