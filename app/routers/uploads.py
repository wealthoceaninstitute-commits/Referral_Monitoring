import re
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import parsers
from ..db import get_db
from ..ingest import ingest
from ..models import MASTER, Upload, User
from ..security import require

router = APIRouter(prefix="/uploads", tags=["uploads"])
MAX_BYTES = 25 * 1024 * 1024


def upload_out(u: Upload, by: Optional[str] = None) -> dict:
    return {"id": u.id, "report_type": u.report_type, "filename": u.filename, "title": u.title, "period": u.period,
            "rows_total": u.rows_total, "rows_new": u.rows_new, "rows_unmatched": u.rows_unmatched,
            "investors_created": u.investors_created, "uploaded_at": u.uploaded_at, "uploaded_by": by}


@router.post("")
async def upload(files: list[UploadFile] = File(...), period: Optional[str] = Form(None),
                 actor: User = Depends(require(MASTER)), db: Session = Depends(get_db)):
    """Accepts any mix of the four NJ reports; each file's type is detected from its title row.
    `period` (YYYY-MM) overrides the month an AUM report is filed under."""
    if period and not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", period):
        raise HTTPException(422, "period must look like 2026-11")
    blobs = []
    for f in files:
        data = await f.read()
        if len(data) > MAX_BYTES:
            raise HTTPException(413, f"{f.filename} is larger than 25 MB")
        blobs.append((f.filename or "upload.xlsx", data))
    # AUM first so the investor master exists before transactions are linked to it
    order = {parsers.AUM: 0, parsers.PURCHASE: 1}
    results = []
    typed = []
    for name, data in blobs:
        try:
            typed.append((order.get(parsers.parse(data)["type"], 2), name, data))
        except parsers.ParseError as e:
            results.append({"filename": name, "ok": False, "error": str(e)})
    for _, name, data in sorted(typed, key=lambda t: t[0]):
        try:
            results.append({"ok": True, **upload_out(ingest(db, name, data, actor, period))})
        except parsers.ParseError as e:
            db.rollback()
            results.append({"filename": name, "ok": False, "error": str(e)})
        except Exception:
            db.rollback()
            raise
    return results


@router.get("")
def history(actor: User = Depends(require(MASTER)), db: Session = Depends(get_db)):
    rows = db.execute(select(Upload, User.name).join(User, User.id == Upload.uploaded_by)
                      .order_by(Upload.uploaded_at.desc()).limit(200)).all()
    return [upload_out(u, by) for u, by in rows]
