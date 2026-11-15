"""Turns parsed report rows into investors, AUM snapshots and transactions.

Linking rules (see README "How the four files are linked"):
  * Investor name is the only field present in all four reports -> primary link.
  * PAN (processed report only) is stored on the investor and separates namesakes.
  * Folio (three transaction reports) links rejections/redemptions of namesakes back to
    the right person, using folios already seen against a PAN.
  * The E-Wealth registration report lists every registered account with its PAN; it adds
    not-yet-invested clients and fills in PANs for the rest.
"""
import hashlib
from collections import defaultdict
from datetime import date
from typing import Optional

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from . import parsers
from .models import AumSnapshot, Investor, Registration, Txn, Upload, User


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


def _ingest_aum(db: Session, parsed: dict, up: Upload, res: Resolver, as_on: date):
    """One snapshot per exact date. Uploading the same date again replaces that date only."""
    db.execute(delete(AumSnapshot).where(AumSnapshot.as_on == as_on))
    prev = {}
    last = db.scalar(select(func.max(AumSnapshot.as_on)).where(AumSnapshot.as_on < as_on)) \
        or db.scalar(select(func.min(AumSnapshot.as_on)).where(AumSnapshot.as_on > as_on))
    if last:
        prev = {s.investor_id: float(s.inv_total) for s in db.scalars(select(AumSnapshot).where(AumSnapshot.as_on == last))}
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in parsed["rows"]:
        groups[parsers.name_key(row["name"])].append(row)
    for key, rows in groups.items():
        free = list(res.by_key[key])
        # namesakes: pair each row with the existing investor whose nearest invested total is closest
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
            db.add(AumSnapshot(investor_id=inv.id, period=_period(as_on), as_on=as_on, upload_id=up.id,
                               **{k: v for k, v in row.items() if k.startswith(("inv_", "aum_"))}))
    up.period, up.as_on, up.rows_new = _period(as_on), as_on, len(parsed["rows"])
    db.flush()
    # history rows of AUM uploads that this one has fully replaced are no longer meaningful
    live = select(AumSnapshot.upload_id).distinct()
    db.execute(delete(Upload).where(Upload.report_type == parsers.AUM, Upload.id != up.id, Upload.id.not_in(live)))


def _ingest_registrations(db: Session, parsed: dict, up: Upload, res: Resolver):
    """Full registered-client list. Every row carries a PAN, so this also fills in PANs."""
    rows = parsed["rows"]
    existing = set(db.scalars(select(Registration.ref_no)))
    pans_for: dict[str, set] = defaultdict(set)
    for r in rows:
        if r["pan"]:
            pans_for[parsers.name_key(r["name"])].add(r["pan"])
    for r in rows:
        if r["ref"] in existing:
            continue
        existing.add(r["ref"])
        key = parsers.name_key(r["name"])
        cands = res.by_key[key]
        hit = next((c for c in cands if r["pan"] and c.pan == r["pan"]), None)
        if not hit:
            blank = [c for c in cands if not c.pan]
            unowned = pans_for[key] - {c.pan for c in cands}
            if blank and len(unowned) > 1:
                hit = None  # same name, several unplaced PANs, PAN-less clients on file: a person must decide
            else:
                hit = res.for_txn({"name": r["name"], "pan": r["pan"], "group": r.get("group")})
        if not hit:
            up.rows_unmatched += 1
        db.add(Registration(ref_no=r["ref"], investor_id=hit.id if hit else None, name_key=key, account_name=r["name"],
                            pan=r["pan"], account_type=r["account_type"], mode=r["mode"],
                            acquired_from=r["acquired_from"], upload_id=up.id))
        up.rows_new += 1


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


def link_registrations(db: Session, res: Resolver, key: Optional[str] = None) -> int:
    """Attach registration rows that could not be placed when they were uploaded. Runs after
    every upload and PAN edit, because a later file (or a person) may have supplied the PAN
    that tells two namesakes apart."""
    db.flush()
    q = select(Registration).where(Registration.investor_id.is_(None), Registration.pan.is_not(None))
    if key:
        q = q.where(Registration.name_key == key)
    todo = list(db.scalars(q.order_by(Registration.id)))
    fixed = 0
    for g in todo:
        cands = res.by_key[g.name_key]
        hit = next((c for c in cands if c.pan == g.pan), None)
        if not hit:
            blank = [c for c in cands if not c.pan]
            if not blank:
                hit = res.create(g.account_name, g.name_key, g.pan)  # every namesake has another PAN -> a new person
            else:
                unowned = {x.pan for x in todo if x.name_key == g.name_key} - {c.pan for c in cands}
                if len(blank) == 1 and unowned == {g.pan}:
                    blank[0].pan = g.pan  # the only PAN-less client and the only unplaced PAN
                    hit = blank[0]
        if hit:
            g.investor_id, fixed = hit.id, fixed + 1
    db.flush()
    return fixed


def relink(db: Session, key: str) -> int:
    """Re-attach orphan transactions and registrations for a name once PANs have been set."""
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
    return fixed + link_registrations(db, res, key)


def purge_orphans(db: Session) -> int:
    """Remove clients left with nothing at all (no AUM, no transactions, no registration, no agent)."""
    db.flush()
    q = delete(Investor).where(Investor.ra_id.is_(None),
                               Investor.id.not_in(select(AumSnapshot.investor_id)),
                               Investor.id.not_in(select(Txn.investor_id).where(Txn.investor_id.is_not(None))),
                               Investor.id.not_in(select(Registration.investor_id).where(Registration.investor_id.is_not(None))))
    return db.execute(q).rowcount or 0


def delete_upload(db: Session, up: Upload) -> dict:
    """Undo one upload: remove exactly the rows that file added, then any clients left empty."""
    n = 0
    for model in (AumSnapshot, Txn, Registration):
        n += db.execute(delete(model).where(model.upload_id == up.id)).rowcount or 0
    db.delete(up)
    gone = purge_orphans(db)
    db.commit()
    return {"rows_removed": n, "clients_removed": gone}


def ingest(db: Session, filename: str, content: bytes, user: User, as_on: Optional[date] = None) -> Upload:
    parsed = parsers.parse(content)
    up = Upload(report_type=parsed["type"], filename=filename[:300], title=parsed["title"][:300],
                rows_total=len(parsed["rows"]), uploaded_by=user.id)
    db.add(up)
    db.flush()
    res = Resolver(db)
    if parsed["type"] == parsers.AUM:
        d = as_on or parsed["as_on"]
        if not d:
            raise parsers.ParseError("Could not read the 'As on' date from the AUM report - choose the date on the upload page")
        _ingest_aum(db, parsed, up, res, d)
    elif parsed["type"] == parsers.REGISTRATION:
        _ingest_registrations(db, parsed, up, res)
    else:
        _ingest_txns(db, parsed, up, res)
    link_registrations(db, res)
    up.investors_created = res.created
    db.commit()
    return up
