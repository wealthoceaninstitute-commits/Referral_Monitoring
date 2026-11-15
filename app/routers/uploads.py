from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import parsers
from ..db import get_db
from ..ingest import delete_upload, ingest
from ..models import ADMIN, MASTER, Upload, User
from ..security import require

router = APIRouter(prefix="/uploads", tags=["uploads"])
MAX_BYTES = 25 * 1024 * 1024


def upload_out(u: Upload, by: Optional[str] = None, can_delete: bool = False) -> dict:
    return {"id": u.id, "report_type": u.report_type, "filename": u.filename, "title": u.title, "period": u.period,
            "as_on": u.as_on, "rows_total": u.rows_total, "rows_new": u.rows_new, "rows_unmatched": u.rows_unmatched,
            "investors_created": u.investors_created, "uploaded_at": u.uploaded_at, "uploaded_by": by,
            "can_delete": can_delete}


@router.post("")
async def upload(files: list[UploadFile] = File(...), as_on: Optional[str] = Form(None),
                 actor: User = Depends(require(MASTER, ADMIN)), db: Session = Depends(get_db)):
    """Accepts any mix of the NJ reports; each file's type is detected from its title row.
    `as_on` (YYYY-MM-DD) overrides the date an AUM report is filed under; by default the
    report's own "As on" date is used."""
    on: Optional[date] = None
    if as_on:
        try:
            on = date.fromisoformat(as_on)
        except ValueError:
            raise HTTPException(422, "as_on must look like 2026-11-15")
    blobs = []
    for f in files:
        data = await f.read()
        if len(data) > MAX_BYTES:
            raise HTTPException(413, f"{f.filename} is larger than 25 MB")
        blobs.append((f.filename or "upload.xlsx", data))
    # AUM first, then the registration list and purchases (both carry PANs), then the rest
    order = {parsers.AUM: 0, parsers.REGISTRATION: 1, parsers.PURCHASE: 2}
    results, typed = [], []
    for name, data in blobs:
        try:
            typed.append((order.get(parsers.parse(data)["type"], 3), name, data))
        except parsers.ParseError as e:
            results.append({"filename": name, "ok": False, "error": str(e)})
    if on and sum(1 for o, _, _ in typed if o == 0) > 1:
        raise HTTPException(422, "A date override applies to one AUM file at a time - upload the AUM files separately")
    for _, name, data in sorted(typed, key=lambda t: t[0]):
        try:
            results.append({"ok": True, **upload_out(ingest(db, name, data, actor, on))})
        except parsers.ParseError as e:
            db.rollback()
            results.append({"filename": name, "ok": False, "error": str(e)})
        except Exception:
            db.rollback()
            raise
    return results


@router.get("")
def history(actor: User = Depends(require(MASTER, ADMIN)), db: Session = Depends(get_db)):
    rows = db.execute(select(Upload, User.name).join(User, User.id == Upload.uploaded_by)
                      .order_by(Upload.uploaded_at.desc()).limit(300)).all()
    return [upload_out(u, by, actor.role == MASTER or u.uploaded_by == actor.id) for u, by in rows]


@router.delete("/{upload_id}")
def remove(upload_id: int, actor: User = Depends(require(MASTER, ADMIN)), db: Session = Depends(get_db)):
    """Deletes the file's record and exactly the data it added. The master can delete any
    upload; an admin only their own."""
    up = db.get(Upload, upload_id)
    if not up:
        raise HTTPException(404, "Upload not found")
    if actor.role != MASTER and up.uploaded_by != actor.id:
        raise HTTPException(403, "Only the master can delete another person's upload")
    return delete_upload(db, up)
