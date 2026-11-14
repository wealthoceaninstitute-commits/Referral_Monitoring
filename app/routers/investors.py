from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..ingest import relink
from ..models import ADMIN, MASTER, RA, AumSnapshot, Investor, Txn, User
from ..scope import investor_filter, mask_pan, visible_ras
from ..security import current_user, require
from .reports import latest_period, txn_out

router = APIRouter(prefix="/investors", tags=["investors"])
SNAP_FIELDS = ["inv_equity", "inv_debt", "inv_hybrid", "inv_physical", "inv_others", "inv_total",
               "aum_equity", "aum_debt", "aum_hybrid", "aum_physical", "aum_others", "aum_total"]


class AssignIn(BaseModel):
    investor_ids: list[int]
    ra_id: Optional[int] = None  # None = move back to the unassigned pool


class InvestorPatch(BaseModel):
    pan: Optional[str] = None


def snap_out(s: Optional[AumSnapshot]) -> dict:
    d = {f: float(getattr(s, f)) if s else 0.0 for f in SNAP_FIELDS}
    d["gain"] = d["aum_total"] - d["inv_total"]
    return d


@router.get("")
def list_investors(period: Optional[str] = None, ra_id: Optional[str] = None, q: Optional[str] = None,
                   user: User = Depends(current_user), db: Session = Depends(get_db)):
    period = period or latest_period(db)
    conds = investor_filter(db, user, ra_id)
    if q:
        like = f"%{q.strip().upper()}%"
        conds.append(or_(Investor.name_key.like(like), Investor.pan.like(like)) if user.role != RA else Investor.name_key.like(like))
    rows = db.execute(
        select(Investor, AumSnapshot)
        .outerjoin(AumSnapshot, (AumSnapshot.investor_id == Investor.id) & (AumSnapshot.period == period))
        .where(*conds).order_by(Investor.name).limit(5000)).all()
    ids = [i.id for i, _ in rows]
    flows: dict[int, dict] = {}
    if ids:
        for iid, kind, amt in db.execute(select(Txn.investor_id, Txn.kind, func.sum(Txn.amount))
                                         .where(Txn.period == period, Txn.investor_id.in_(ids))
                                         .group_by(Txn.investor_id, Txn.kind)):
            flows.setdefault(iid, {})[kind] = float(amt or 0)
    ras = {u.id: u.name for u in db.scalars(select(User).where(User.role == RA))}
    namesakes = {k for k, in db.execute(select(Investor.name_key).group_by(Investor.name_key).having(func.count() > 1))}
    return {"period": period, "items": [{
        "id": i.id, "name": i.name, "pan": mask_pan(i.pan, user), "ra_id": i.ra_id, "ra_name": ras.get(i.ra_id),
        "namesake": i.name_key in namesakes, "active": s is not None, **snap_out(s),
        "purchases": flows.get(i.id, {}).get("PURCHASE", 0.0), "redemptions": flows.get(i.id, {}).get("REDEMPTION", 0.0),
        "rejections": flows.get(i.id, {}).get("REJECTION", 0.0)} for i, s in rows]}


@router.post("/assign")
def assign(body: AssignIn, actor: User = Depends(require(MASTER, ADMIN)), db: Session = Depends(get_db)):
    if not body.investor_ids:
        raise HTTPException(422, "Select at least one client")
    if body.ra_id is not None:
        if body.ra_id not in {u.id for u in visible_ras(db, actor) if u.is_active}:
            raise HTTPException(403, "You can only assign clients to your own active RAs")
    # an admin can move only clients that are unassigned or already under one of their RAs
    allowed = list(db.scalars(select(Investor).where(Investor.id.in_(body.investor_ids), *investor_filter(db, actor))))
    if len(allowed) != len(set(body.investor_ids)):
        raise HTTPException(403, "Some of those clients belong to another admin's RA")
    for inv in allowed:
        inv.ra_id, inv.assigned_by, inv.assigned_at = body.ra_id, actor.id, datetime.now(timezone.utc)
    db.commit()
    return {"updated": len(allowed)}


def _get(db: Session, user: User, investor_id: int) -> Investor:
    inv = db.scalar(select(Investor).where(Investor.id == investor_id, *investor_filter(db, user)))
    if not inv:
        raise HTTPException(404, "Client not found")
    return inv


@router.get("/{investor_id}")
def detail(investor_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    inv = _get(db, user, investor_id)
    snaps = list(db.scalars(select(AumSnapshot).where(AumSnapshot.investor_id == inv.id).order_by(AumSnapshot.period.desc())))
    flows: dict[str, dict] = {}
    for p, kind, amt, n in db.execute(select(Txn.period, Txn.kind, func.sum(Txn.amount), func.count())
                                      .where(Txn.investor_id == inv.id).group_by(Txn.period, Txn.kind)):
        flows.setdefault(p, {})[kind] = {"amount": float(amt or 0), "count": n}
    by_p = {s.period: s for s in snaps}
    months = [{"period": p, "active": p in by_p, "as_on": by_p[p].as_on if p in by_p else None, **snap_out(by_p.get(p)),
               **{k.lower(): flows.get(p, {}).get(k, {"amount": 0.0, "count": 0}) for k in ("PURCHASE", "REDEMPTION", "REJECTION")}}
              for p in sorted(set(by_p) | set(flows), reverse=True)]
    txns = list(db.scalars(select(Txn).where(Txn.investor_id == inv.id).order_by(Txn.txn_date.desc(), Txn.id.desc()).limit(1000)))
    ra = db.get(User, inv.ra_id) if inv.ra_id else None
    return {"id": inv.id, "name": inv.name, "pan": mask_pan(inv.pan, user), "group": inv.group_name,
            "ra_id": inv.ra_id, "ra_name": ra.name if ra else None, "assigned_at": inv.assigned_at,
            "months": months, "transactions": [txn_out(t, user) for t in txns]}


@router.patch("/{investor_id}")
def update(investor_id: int, body: InvestorPatch, actor: User = Depends(require(MASTER, ADMIN)), db: Session = Depends(get_db)):
    """Set/correct a PAN - this is how namesakes ("Srinivasa L" x2) are told apart."""
    inv = _get(db, actor, investor_id)
    pan = (body.pan or "").strip().upper() or None
    if pan and (len(pan) != 10 or not pan.isalnum()):
        raise HTTPException(422, "PAN must be 10 letters/digits")
    if pan and db.scalar(select(Investor.id).where(Investor.pan == pan, Investor.id != inv.id)):
        raise HTTPException(409, "Another client already has this PAN")
    inv.pan = pan
    fixed = relink(db, inv.name_key)
    db.commit()
    return {"id": inv.id, "pan": inv.pan, "relinked": fixed}
