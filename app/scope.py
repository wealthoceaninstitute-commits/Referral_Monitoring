"""Row-level visibility. Every query that touches investors goes through here."""
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .models import ADMIN, MASTER, RA, Investor, User


def my_ra_ids(user: User):
    return select(User.id).where(User.parent_id == user.id, User.role == RA)


def visible_ras(db: Session, user: User) -> list[User]:
    q = select(User).where(User.role == RA).order_by(User.name)
    if user.role == ADMIN:
        q = q.where(User.parent_id == user.id)
    elif user.role == RA:
        q = q.where(User.id == user.id)
    return list(db.scalars(q))


def investor_filter(db: Session, user: User, ra_id: Optional[str] = None, pool: bool = True) -> list:
    """SQL conditions limiting Investor rows to what `user` may see.

    MASTER: everyone.  ADMIN: clients of their own RAs (+ the unassigned pool when `pool`).
    RA: only their own clients.  `ra_id` narrows further: a user id, 'assigned' or 'unassigned'.
    """
    conds = []
    if user.role == RA:
        conds.append(Investor.ra_id == user.id)
    elif user.role == ADMIN:
        mine = Investor.ra_id.in_(my_ra_ids(user))
        conds.append(or_(mine, Investor.ra_id.is_(None)) if pool or ra_id == "unassigned" else mine)
    if ra_id == "unassigned":
        if user.role == RA:
            raise HTTPException(403, "Not allowed")
        conds.append(Investor.ra_id.is_(None))
    elif ra_id == "assigned":
        conds.append(Investor.ra_id.is_not(None))
    elif ra_id:
        if not ra_id.isdigit():
            raise HTTPException(422, "ra_id must be a user id, 'assigned' or 'unassigned'")
        conds.append(Investor.ra_id == int(ra_id))
    return conds


def mask_pan(pan: Optional[str], user: User) -> Optional[str]:
    if not pan or user.role in (MASTER, ADMIN):
        return pan
    return pan[:2] + "*" * (len(pan) - 4) + pan[-2:]
