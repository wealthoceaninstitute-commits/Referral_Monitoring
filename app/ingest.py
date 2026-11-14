"""Turns parsed report rows into investors, AUM snapshots and transactions.

Linking rules (see README "How the four files are linked"):
  * Investor name is the only field present in all four reports -> primary link.
  * PAN (processed report only) is stored on the investor and separates namesakes.
  * Folio (three transaction reports) links rejections/redemptions of namesakes back to
    the right person, using folios already seen against a PAN.
"""
import hashlib
from collections import defaultdict
from typing import Optional

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from . import parsers
from .models import AumSnapshot, Investor, Txn, Upload, User


class Resolver:
    def __init__(self, db: Session):
        self.db = db
        self.by_key: dict[str, list[Investor]] = defaultdict(list)
        for inv in db.scalars(select(Investor).order_by(Investor.name_key, Investor.dup_seq)):
            self.by_key[inv.name_key].append(inv)
        self.created = 0
        self._folios: dict[str, dict[str, int]] = {}

    def create(self, name: str, key: str, pan: Optional[str] = None, group: Optional[str] = None) -> Investor:
        inv = Investor(name=name, name_key=key, dup_seq=len(self.by_key[key]) + 1, pan=pan, group_name=group)
        self.db.add(inv)
        self.db.flush()
        self.by_key[key].append(inv)
        self.created += 1
        return inv

    def folio_owner(self, key: str, folio: Optional[str]) -> Optional[int]:
        if not folio:
            return None
        if key not in self._folios:
            q = select(Txn.folio, Txn.investor_id).where(Txn.name_key == key, Txn.investor_id.is_not(None)).distinct()
            self._folios[key] = {f: i for f, i in self.db.execute(q) if f}
        return self._folios[key].get(folio)

    def for_txn(self, row: dict) -> Optional[Investor]:
        key = parsers.name_key(row["name"])
        cands, pan = self.by_key[key], row.get("pan")
        if pan:
            hit = next((c for c in cands if c.pan == pan), None)
            if hit:
                return hit
            blank = [c for c in cands if not c.pan]
            if len(blank) == 1:
                blank[0].pan = pan  # first time we learn this investor's PAN (or last namesake left)
                return blank[0]
            if not blank:
                return self.create(row["name"], key, pan, row.get("group"))  # new namesake
            return None  # several PAN-less namesakes: needs a human to say which is which
        if len(cands) == 1:
            return cands[0]
        if not cands:
            return self.create(row["name"], key, None, row.get("group"))
        owner = self.folio_owner(key, row.get("folio"))
        return next((c for c in cands if c.id == owner), None)


def _period(d) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def aum_period(as_on) -> str:
    """Month an AUM report is filed under. The reports are pulled at the start of a month
    together with *last month's* transactions, so an "As on" date in the first half of a
    month is treated as the closing position of the previous month. Override on upload."""
    if as_on.day <= 15:
        y, m = (as_on.year, as_on.month - 1) if as_on.month > 1 else (as_on.year - 1, 12)
        return f"{y:04d}-{m:02d}"
    return _period(as_on)


def _ingest_aum(db: Session, parsed: dict, up: Upload, res: Resolver, period: str):
    db.execute(delete(AumSnapshot).where(AumSnapshot.period == period))  # latest upload for a month wins
    prev = {}
    last = db.scalar(select(AumSnapshot.period).where(AumSnapshot.period < period).order_by(AumSnapshot.period.desc()).limit(1))
    if last:
        prev = {s.investor_id: float(s.inv_total) for s in db.scalars(select(AumSnapshot).where(AumSnapshot.period == last))}
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in parsed["rows"]:
        groups[parsers.name_key(row["name"])].append(row)
    for key, rows in groups.items():
        free = list(res.by_key[key])
        # namesakes: pair each row with the existing investor whose last invested total is closest
        pairs = []
        if len(rows) > 1 and free and prev:
            for row in sorted(rows, key=lambda r: -r["inv_total"]):
                known = [c for c in free if c.id in prev]
                if known:
                    best = min(known, key=lambda c: abs(prev[c.id] - row["inv_total"]))
                    free.remove(best)
                    pairs.append((row, best))
        paired = {id(r) for r, _ in pairs}
        for row in rows:
            if id(row) in paired:
                continue
            pairs.append((row, free.pop(0) if free else res.create(row["name"], key, None, row.get("group"))))
        for row, inv in pairs:
            if row.get("group") and not inv.group_name:
                inv.group_name = row["group"]
            db.add(AumSnapshot(investor_id=inv.id, period=period, as_on=parsed["as_on"], upload_id=up.id,
                               **{k: v for k, v in row.items() if k.startswith(("inv_", "aum_"))}))
    up.period, up.rows_new = period, len(parsed["rows"])


def _dedupe_keys(kind: str, rows: list[dict]) -> list[str]:
    seen: dict[str, int] = defaultdict(int)
    keys = []
    for r in rows:
        if kind == parsers.PURCHASE and r.get("ref"):
            keys.append(f"P:{r['ref']}")  # NJ transaction number is unique
            continue
        base = "|".join(str(x) for x in (r["date"], parsers.name_key(r["name"]), r["folio"], r["isin"], r["units"], r["amount"]))
        h = hashlib.sha1(base.encode()).hexdigest()[:40]
        if kind == parsers.REDEMPTION:
            keys.append(f"R:{h}")  # NJ lists each payout twice (with/without consignment no.) - collapse
        else:
            seen[h] += 1
            keys.append(f"{kind[0]}:{h}:{seen[h]}")  # keep genuinely identical same-day SIPs apart
    return keys


def _ingest_txns(db: Session, parsed: dict, up: Upload, res: Resolver):
    kind, rows = parsed["type"], parsed["rows"]
    keys = _dedupe_keys(kind, rows)
    existing: set[str] = set()
    for i in range(0, len(keys), 500):
        existing.update(db.scalars(select(Txn.dedupe_key).where(Txn.dedupe_key.in_(keys[i:i + 500]))))
    for r, k in zip(rows, keys):
        if k in existing:
            continue
        existing.add(k)
        inv = res.for_txn(r)
        if not inv:
            up.rows_unmatched += 1
        db.add(Txn(dedupe_key=k, kind=kind, period=_period(r["date"]), investor_id=inv.id if inv else None,
                   name_key=parsers.name_key(r["name"]), investor_name=r["name"], pan=r.get("pan"), txn_date=r["date"],
                   txn_type=r["type"], txn_mode=r["mode"], folio=r["folio"], scheme=r["scheme"], isin=r["isin"],
                   nav=r["nav"], units=r["units"], amount=r["amount"], remark=r["remark"],
                   payout_date=r["payout_date"], payout_mode=r["payout_mode"], ref_no=r["ref"], upload_id=up.id))
        up.rows_new += 1


def relink(db: Session, key: str) -> int:
    """Re-attach orphan transactions for a name once PANs have been set on the namesakes."""
    db.flush()
    res, fixed = Resolver(db), 0
    for with_pan in (True, False):  # PAN rows first; they reveal the folios the PAN-less rows need
        res._folios.clear()
        for t in db.scalars(select(Txn).where(Txn.name_key == key, Txn.investor_id.is_(None))):
            if bool(t.pan) != with_pan:
                continue
            hit = res.for_txn({"name": t.investor_name, "pan": t.pan, "folio": t.folio})
            if hit:
                t.investor_id, fixed = hit.id, fixed + 1
        db.flush()
    return fixed


def ingest(db: Session, filename: str, content: bytes, user: User, period: Optional[str] = None) -> Upload:
    parsed = parsers.parse(content)
    up = Upload(report_type=parsed["type"], filename=filename[:300], title=parsed["title"][:300],
                rows_total=len(parsed["rows"]), uploaded_by=user.id)
    db.add(up)
    db.flush()
    res = Resolver(db)
    if parsed["type"] == parsers.AUM:
        p = period or (aum_period(parsed["as_on"]) if parsed["as_on"] else None)
        if not p:
            raise parsers.ParseError("Could not read the 'As on' date from the AUM report - pass the month explicitly")
        _ingest_aum(db, parsed, up, res, p)
    else:
        _ingest_txns(db, parsed, up, res)
    up.investors_created = res.created
    db.commit()
    return up
