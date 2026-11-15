"""Parsers for the four NJ Wealth partner-desk Excel exports.

Each parser returns plain dicts; nothing here touches the database. The report type is
detected from the title in the first row, and columns are located by header text so a
re-ordered export still parses.
"""
import io
import re
from datetime import date, datetime
from typing import Any, Optional

import openpyxl

AUM, PURCHASE, REJECTION, REDEMPTION, REGISTRATION = "AUM", "PURCHASE", "REJECTION", "REDEMPTION", "REGISTRATION"

TITLES = [
    ("investor wise aum report", AUM),
    ("processed transaction detail report", PURCHASE),
    ("transaction rejection report", REJECTION),
    ("mf redemption payout detail report", REDEMPTION),
    ("e-wealth reg", REGISTRATION),
]


class ParseError(ValueError):
    pass


def name_key(s: Any) -> str:
    return " ".join(re.sub(r"[^A-Za-z0-9 ]", " ", str(s or "")).upper().split())


def num(v: Any) -> float:
    """'5,12,976' / '-1,300' / 409 / None -> float"""
    if v is None or v == "":
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace(",", "").replace("₹", "").strip()
    if s in ("", "-", "N/A", "NA"):
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def to_date(v: Any) -> Optional[date]:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v).strip()
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d-%b-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def text(v: Any) -> Optional[str]:
    """Folio numbers arrive as int, float or str depending on the AMC."""
    if v is None:
        return None
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    s = str(v).strip()
    return None if s in ("", "N/A", "NA") else s


def _hkey(h: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(h or "").lower())


def _load(content: bytes) -> list[tuple]:
    try:
        wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception as e:
        raise ParseError(f"Not a readable .xlsx file ({e})")
    return [r for r in wb.worksheets[0].iter_rows(values_only=True)]


def _header_index(rows: list[tuple]) -> int:
    for i, r in enumerate(rows[:15]):
        if r and _hkey(r[0]) == "srno":
            return i
    raise ParseError("Could not find the 'Sr. No.' header row")


def _data_rows(rows: list[tuple], start: int):
    for r in rows[start:]:
        if r and isinstance(r[0], (int, float)) and not isinstance(r[0], bool):
            yield r


def _col(headers: dict[str, int], *names: str) -> Optional[int]:
    for n in names:
        if n in headers:
            return headers[n]
    return None


def parse(content: bytes) -> dict:
    rows = _load(content)
    if not rows or not rows[0] or not rows[0][0]:
        raise ParseError("Empty sheet - expected the NJ report title in the first row")
    title = str(rows[0][0]).strip()
    rtype = next((t for needle, t in TITLES if needle in title.lower()), None)
    if not rtype:
        raise ParseError(f"Unrecognised report: '{title[:80]}'")
    hi = _header_index(rows)
    hdr = {_hkey(h): i for i, h in enumerate(rows[hi]) if h}
    get = lambda r, i: r[i] if i is not None and i < len(r) else None  # noqa: E731

    if rtype == AUM:
        inv = _col(hdr, "investor")
        if inv is None:
            raise ParseError("AUM report has no 'Investor' column")
        m = re.search(r"(\d{2})-(\d{2})-(\d{4})", title)
        as_on = date(int(m.group(3)), int(m.group(2)), int(m.group(1))) if m else None
        cats = ["equity", "debt", "hybrid", "physical", "others", "total"]
        out = []
        for r in _data_rows(rows, hi + 1):
            if not get(r, inv):
                continue
            vals = [num(get(r, inv + 1 + k)) for k in range(12)]  # 6 invested + 6 AUM
            row = {"name": str(r[inv]).strip(), "group": text(get(r, _col(hdr, "group")))}
            row.update({f"inv_{c}": vals[k] for k, c in enumerate(cats)})
            row.update({f"aum_{c}": vals[6 + k] for k, c in enumerate(cats)})
            out.append(row)
        return {"type": AUM, "title": title, "as_on": as_on, "rows": out}

    if rtype == REGISTRATION:
        c = dict(name=_col(hdr, "accountnameasperpan", "accountname"), pan=_col(hdr, "accountholderpan"),
                 ref=_col(hdr, "refno"), atype=_col(hdr, "accounttype"), mode=_col(hdr, "modeofoperation"),
                 src=_col(hdr, "clientacquisitionfrom"), group=_col(hdr, "partneremployee"))
        if c["name"] is None or c["ref"] is None:
            raise ParseError("Registration report is missing the 'Account Name' or 'Ref No.' column")
        out = []
        for r in _data_rows(rows, hi + 1):
            ref, name = text(get(r, c["ref"])), text(get(r, c["name"]))
            if not ref or not name:
                continue
            pan = (text(get(r, c["pan"])) or "").upper()
            out.append({"name": name, "pan": pan if len(pan) == 10 else None, "ref": ref,
                        "account_type": text(get(r, c["atype"])), "mode": text(get(r, c["mode"])),
                        "acquired_from": text(get(r, c["src"])), "group": text(get(r, c["group"]))})
        return {"type": REGISTRATION, "title": title, "as_on": None, "rows": out}

    spec = {
        PURCHASE: dict(date=("purchasedate",), type=("type",), folio=("folionodemataC".lower(), "foliono"),
                       units=("purchaseunits",), amount=("grossinvamount",), ref=("trno",)),
        REJECTION: dict(date=("trxndate",), type=("trxntype",), folio=("foliono",),
                        units=("purchaseunits",), amount=("invamount",), remark=("rejectionremark",)),
        REDEMPTION: dict(date=("transactiondate",), mode=("transactionmode",), folio=("foliono",),
                         units=("unitsredeemed",), amount=("redemptionamt",), payout_date=("payoutdate",),
                         payout_mode=("payoutmode",), ref=("consignmentno",)),
    }[rtype]
    c = {k: _col(hdr, *v) for k, v in spec.items()}
    c.update(inv=_col(hdr, "investor"), pan=_col(hdr, "pan"), scheme=_col(hdr, "scheme"), isin=_col(hdr, "isin"),
             nav=_col(hdr, "nav"), group=_col(hdr, "group"))
    c.setdefault("mode", _col(hdr, "trxnmode"))
    for need in ("inv", "date", "amount"):
        if c.get(need) is None:
            raise ParseError(f"{rtype} report is missing the '{need}' column")
    out = []
    for r in _data_rows(rows, hi + 1):
        d = to_date(get(r, c["date"]))
        if not get(r, c["inv"]) or not d:
            continue
        ref = text(get(r, c.get("ref")))
        out.append({
            "name": str(r[c["inv"]]).strip(), "group": text(get(r, c["group"])),
            "pan": (text(get(r, c["pan"])) or "").upper() or None,
            "date": d, "type": text(get(r, c.get("type"))) or ("Redemption" if rtype == REDEMPTION else None),
            "mode": text(get(r, c.get("mode"))), "folio": text(get(r, c["folio"])),
            "scheme": text(get(r, c["scheme"])), "isin": text(get(r, c["isin"])),
            "nav": num(get(r, c["nav"])) or None, "units": num(get(r, c["units"])),
            "amount": num(get(r, c["amount"])), "remark": text(get(r, c.get("remark"))),
            "payout_date": to_date(get(r, c.get("payout_date"))), "payout_mode": text(get(r, c.get("payout_mode"))),
            "ref": None if ref == "0" else ref,
        })
    return {"type": rtype, "title": title, "as_on": None, "rows": out}
