from typing import Optional

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import ADMIN, MASTER, RA, AumSnapshot, Investor, Txn, User
from ..scope import investor_filter, mask_pan, visible_ras
from ..security import current_user

router = APIRouter(tags=["reports"])
KINDS = ("PURCHASE", "REDEMPTION", "REJECTION")


def latest_period(db: Session) -> Optional[str]:
    return db.scalar(select(func.max(AumSnapshot.period))) or db.scalar(select(func.max(Txn.period)))


def all_periods(db: Session) -> list[str]:
    a = set(db.scalars(select(AumSnapshot.period).distinct()))
    t = set(db.scalars(select(Txn.period).distinct()))
    return sorted(a | t, reverse=True)


def txn_out(t: Txn, user: User, ra_name: Optional[str] = None) -> dict:
    return {"id": t.id, "kind": t.kind, "period": t.period, "investor_id": t.investor_id, "investor": t.investor_name,
            "pan": mask_pan(t.pan, user), "date": t.txn_date, "type": t.txn_type, "mode": t.txn_mode, "folio": t.folio,
            "scheme": t.scheme, "isin": t.isin, "nav": float(t.nav) if t.nav is not None else None,
            "units": float(t.units) if t.units is not None else None, "amount": float(t.amount or 0),
            "remark": t.remark, "payout_date": t.payout_date, "payout_mode": t.payout_mode, "ref_no": t.ref_no,
            "ra_name": ra_name}


def _flow_block(rows) -> dict:
    out = {k.lower(): {"amount": 0.0, "count": 0} for k in KINDS}
    for kind, amt, n in rows:
        out[kind.lower()] = {"amount": float(amt or 0), "count": n}
    return out


@router.get("/periods")
def periods(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return {"periods": all_periods(db), "latest": latest_period(db)}


@router.get("/ras")
def ras(user: User = Depends(current_user), db: Session = Depends(get_db)):
    """RAs the caller may filter by (for the RA picker)."""
    admins = {u.id: u.name for u in db.scalars(select(User).where(User.role == ADMIN))}
    return [{"id": u.id, "name": u.name, "admin_id": u.parent_id, "admin_name": admins.get(u.parent_id),
             "is_active": u.is_active} for u in visible_ras(db, user)]


@router.get("/dashboard")
def dashboard(period: Optional[str] = None, ra_id: Optional[str] = None,
              user: User = Depends(current_user), db: Session = Depends(get_db)):
    period = period or latest_period(db)
    conds = investor_filter(db, user, ra_id)  # admins: own RAs' clients + the unassigned pool
    scoped = select(Investor.id).where(*conds)

    def snap_totals(p):
        r = db.execute(select(func.count(), func.coalesce(func.sum(AumSnapshot.inv_total), 0),
                              func.coalesce(func.sum(AumSnapshot.aum_total), 0),
                              func.coalesce(func.sum(AumSnapshot.aum_equity), 0), func.coalesce(func.sum(AumSnapshot.aum_debt), 0),
                              func.coalesce(func.sum(AumSnapshot.aum_hybrid), 0), func.coalesce(func.sum(AumSnapshot.aum_physical), 0),
                              func.coalesce(func.sum(AumSnapshot.aum_others), 0))
                       .where(AumSnapshot.period == p, AumSnapshot.investor_id.in_(scoped))).one()
        return {"active_clients": r[0], "invested": float(r[1]), "aum": float(r[2]), "gain": float(r[2]) - float(r[1]),
                "mix": {"equity": float(r[3]), "debt": float(r[4]), "hybrid": float(r[5]), "physical": float(r[6]), "others": float(r[7])}}

    totals = snap_totals(period)
    totals["as_on"] = db.scalar(select(func.max(AumSnapshot.as_on)).where(AumSnapshot.period == period))
    totals["clients"] = db.scalar(select(func.count()).select_from(Investor).where(*conds))
    totals.update(_flow_block(db.execute(select(Txn.kind, func.sum(Txn.amount), func.count())
                                         .where(Txn.period == period, Txn.investor_id.in_(scoped)).group_by(Txn.kind))))

    # month-wise history for the same scope
    months: dict[str, dict] = {}
    for p, n, inv, aum in db.execute(select(AumSnapshot.period, func.count(), func.sum(AumSnapshot.inv_total), func.sum(AumSnapshot.aum_total))
                                     .where(AumSnapshot.investor_id.in_(scoped)).group_by(AumSnapshot.period)):
        months[p] = {"period": p, "active_clients": n, "invested": float(inv or 0), "aum": float(aum or 0)}
    for p, kind, amt, n in db.execute(select(Txn.period, Txn.kind, func.sum(Txn.amount), func.count())
                                      .where(Txn.investor_id.in_(scoped)).group_by(Txn.period, Txn.kind)):
        m = months.setdefault(p, {"period": p, "active_clients": 0, "invested": None, "aum": None})
        m[kind.lower()] = {"amount": float(amt or 0), "count": n}
    for m in months.values():
        for k in KINDS:
            m.setdefault(k.lower(), {"amount": 0.0, "count": 0})
    by_month = [months[p] for p in sorted(months, reverse=True)]

    # per-RA league table (master / admin)
    by_ra = []
    if user.role != RA:
        team = investor_filter(db, user, None, pool=True)
        agg: dict = {}
        for rid, n in db.execute(select(Investor.ra_id, func.count()).where(*team).group_by(Investor.ra_id)):
            agg[rid] = {"clients": n, "invested": 0.0, "aum": 0.0, **{k.lower(): 0.0 for k in KINDS}}
        for rid, inv, aum in db.execute(select(Investor.ra_id, func.sum(AumSnapshot.inv_total), func.sum(AumSnapshot.aum_total))
                                        .join(AumSnapshot, AumSnapshot.investor_id == Investor.id)
                                        .where(AumSnapshot.period == period, *team).group_by(Investor.ra_id)):
            agg[rid].update(invested=float(inv or 0), aum=float(aum or 0))
        for rid, kind, amt in db.execute(select(Investor.ra_id, Txn.kind, func.sum(Txn.amount))
                                         .join(Txn, Txn.investor_id == Investor.id)
                                         .where(Txn.period == period, *team).group_by(Investor.ra_id, Txn.kind)):
            agg[rid][kind.lower()] = float(amt or 0)
        admins = {u.id: u.name for u in db.scalars(select(User).where(User.role == ADMIN))}
        empty = {"clients": 0, "invested": 0.0, "aum": 0.0, **{k.lower(): 0.0 for k in KINDS}}
        for u in visible_ras(db, user):
            by_ra.append({"ra_id": u.id, "name": u.name, "admin_name": admins.get(u.parent_id), "is_active": u.is_active,
                          **agg.get(u.id, empty)})
        by_ra.sort(key=lambda r: -r["aum"])
        if None in agg:
            by_ra.append({"ra_id": None, "name": "Unassigned", "admin_name": None, "is_active": True, **agg[None]})

    unmatched = db.scalar(select(func.count()).select_from(Txn).where(Txn.investor_id.is_(None))) if user.role == MASTER else 0
    return {"period": period, "totals": totals, "by_month": by_month, "by_ra": by_ra, "unmatched_transactions": unmatched}


@router.get("/transactions")
def transactions(kind: Optional[str] = None, period: Optional[str] = None, ra_id: Optional[str] = None,
                 q: Optional[str] = None, unmatched: bool = False, limit: int = 200, offset: int = 0,
                 user: User = Depends(current_user), db: Session = Depends(get_db)):
    conds = []
    if unmatched and user.role == MASTER:
        conds.append(Txn.investor_id.is_(None))
    else:
        conds.append(Txn.investor_id.in_(select(Investor.id).where(*investor_filter(db, user, ra_id))))
    if kind:
        conds.append(Txn.kind == kind.upper())
    if period:
        conds.append(Txn.period == period)
    if q:
        like = f"%{q.strip().upper()}%"
        conds.append(Txn.name_key.like(like) | func.upper(Txn.scheme).like(like))
    total, amount = db.execute(select(func.count(), func.coalesce(func.sum(Txn.amount), 0)).where(*conds)).one()
    rows = db.execute(select(Txn, User.name).outerjoin(Investor, Investor.id == Txn.investor_id)
                      .outerjoin(User, User.id == Investor.ra_id).where(*conds)
                      .order_by(Txn.txn_date.desc(), Txn.id.desc()).limit(min(max(limit, 1), 1000)).offset(max(offset, 0))).all()
    return {"total": total, "amount": float(amount), "items": [txn_out(t, user, ra) for t, ra in rows]}
