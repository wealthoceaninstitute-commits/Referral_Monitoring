from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import ADMIN, MASTER, RA, AumSnapshot, Investor, Registration, Txn, User
from ..scope import investor_filter, mask_pan, visible_ras
from ..security import current_user

router = APIRouter(tags=["reports"])
KINDS = ("PURCHASE", "REDEMPTION", "REJECTION")


def latest_period(db: Session) -> Optional[str]:
    """Default transaction month: the latest month with completed purchases (the processed
    report is always 'last month'), else the latest month with anything."""
    return (db.scalar(select(func.max(Txn.period)).where(Txn.kind == "PURCHASE"))
            or db.scalar(select(func.max(Txn.period))) or db.scalar(select(func.max(AumSnapshot.period))))


def all_periods(db: Session) -> list[str]:
    return sorted(set(db.scalars(select(Txn.period).distinct())), reverse=True)


def aum_dates(db: Session) -> list[date]:
    return sorted(db.scalars(select(AumSnapshot.as_on).distinct()), reverse=True)


def pick_as_on(db: Session, as_on: Optional[str]) -> Optional[date]:
    """The AUM snapshot to show: the requested date, else the most recent one."""
    if as_on:
        try:
            return date.fromisoformat(as_on)
        except ValueError:
            raise HTTPException(422, "as_on must look like 2026-11-15")
    return db.scalar(select(func.max(AumSnapshot.as_on)))


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
    dates = aum_dates(db)
    return {"periods": all_periods(db), "latest": latest_period(db),
            "aum_dates": dates, "latest_aum": dates[0] if dates else None}


@router.get("/ras")
def ras(user: User = Depends(current_user), db: Session = Depends(get_db)):
    """RAs the caller may filter by (for the RA picker)."""
    admins = {u.id: u.name for u in db.scalars(select(User).where(User.role == ADMIN))}
    return [{"id": u.id, "name": u.name, "admin_id": u.parent_id, "admin_name": admins.get(u.parent_id),
             "is_active": u.is_active} for u in visible_ras(db, user)]


@router.get("/dashboard")
def dashboard(period: Optional[str] = None, as_on: Optional[str] = None, ra_id: Optional[str] = None,
              user: User = Depends(current_user), db: Session = Depends(get_db)):
    """`as_on` picks the AUM snapshot (exact date); `period` picks the transaction month."""
    period = period or latest_period(db)
    on = pick_as_on(db, as_on)
    conds = investor_filter(db, user, ra_id)  # admins: own RAs' clients + the unassigned pool
    scoped = select(Investor.id).where(*conds)
    S = AumSnapshot
    zero = lambda col: func.coalesce(func.sum(col), 0)  # noqa: E731

    r = db.execute(select(func.count(), zero(S.inv_total), zero(S.aum_total), zero(S.aum_equity), zero(S.aum_debt),
                          zero(S.aum_hybrid), zero(S.aum_physical), zero(S.aum_others))
                   .where(S.as_on == on, S.investor_id.in_(scoped))).one()
    totals = {"as_on": on, "active_clients": r[0], "invested": float(r[1]), "aum": float(r[2]), "gain": float(r[2]) - float(r[1]),
              "mix": {"equity": float(r[3]), "debt": float(r[4]), "hybrid": float(r[5]), "physical": float(r[6]), "others": float(r[7])}}
    totals["clients"] = db.scalar(select(func.count()).select_from(Investor).where(*conds))
    totals["not_invested"] = totals["clients"] - totals["active_clients"]
    totals.update(_flow_block(db.execute(select(Txn.kind, func.sum(Txn.amount), func.count())
                                         .where(Txn.period == period, Txn.investor_id.in_(scoped)).group_by(Txn.kind))))

    # every AUM snapshot on file, for the same scope
    aum_history = [{"as_on": d, "active_clients": n, "invested": float(inv or 0), "aum": float(aum or 0)}
                   for d, n, inv, aum in db.execute(select(S.as_on, func.count(), func.sum(S.inv_total), func.sum(S.aum_total))
                                                    .where(S.investor_id.in_(scoped)).group_by(S.as_on).order_by(S.as_on.desc()))]
    # month-wise transactions for the same scope
    months: dict[str, dict] = {}
    for p, kind, amt, n in db.execute(select(Txn.period, Txn.kind, func.sum(Txn.amount), func.count())
                                      .where(Txn.investor_id.in_(scoped)).group_by(Txn.period, Txn.kind)):
        months.setdefault(p, {"period": p})[kind.lower()] = {"amount": float(amt or 0), "count": n}
    for m in months.values():
        for k in KINDS:
            m.setdefault(k.lower(), {"amount": 0.0, "count": 0})
    by_month = [months[p] for p in sorted(months, reverse=True)]

    # per-RA league table (master / admin)
    by_ra = []
    if user.role != RA:
        team = investor_filter(db, user, None, pool=True)
        blank = lambda: {"clients": 0, "holding": 0, "invested": 0.0, "aum": 0.0, **{k.lower(): 0.0 for k in KINDS}}  # noqa: E731
        agg: dict = {}
        for rid, n in db.execute(select(Investor.ra_id, func.count()).where(*team).group_by(Investor.ra_id)):
            agg[rid] = {**blank(), "clients": n}
        for rid, n, inv, aum in db.execute(select(Investor.ra_id, func.count(), func.sum(S.inv_total), func.sum(S.aum_total))
                                           .join(S, S.investor_id == Investor.id)
                                           .where(S.as_on == on, *team).group_by(Investor.ra_id)):
            agg[rid].update(holding=n, invested=float(inv or 0), aum=float(aum or 0))
        for rid, kind, amt in db.execute(select(Investor.ra_id, Txn.kind, func.sum(Txn.amount))
                                         .join(Txn, Txn.investor_id == Investor.id)
                                         .where(Txn.period == period, *team).group_by(Investor.ra_id, Txn.kind)):
            agg[rid][kind.lower()] = float(amt or 0)
        admins = {u.id: u.name for u in db.scalars(select(User).where(User.role == ADMIN))}
        for u in visible_ras(db, user):
            by_ra.append({"ra_id": u.id, "name": u.name, "admin_name": admins.get(u.parent_id), "is_active": u.is_active,
                          **agg.get(u.id, blank())})
        by_ra.sort(key=lambda x: -x["aum"])
        if None in agg:
            by_ra.append({"ra_id": None, "name": "Unassigned", "admin_name": None, "is_active": True, **agg[None]})
        for x in by_ra:
            x["not_invested"] = x["clients"] - x["holding"]

    unmatched = unmatched_regs = 0
    if user.role == MASTER:
        unmatched = db.scalar(select(func.count()).select_from(Txn).where(Txn.investor_id.is_(None)))
        unmatched_regs = db.scalar(select(func.count()).select_from(Registration).where(Registration.investor_id.is_(None)))
    return {"period": period, "as_on": on, "totals": totals, "aum_history": aum_history, "by_month": by_month,
            "by_ra": by_ra, "unmatched_transactions": unmatched, "unmatched_registrations": unmatched_regs}


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
