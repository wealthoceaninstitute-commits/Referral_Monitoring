from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select, text

from . import config
from .db import Base, SessionLocal, engine
from .models import MASTER, User
from .routers import auth, investors, reports, uploads, users
from .security import hash_password


def migrate():
    """Small, repeat-safe upgrades for databases created by an earlier version (PostgreSQL)."""
    if engine.dialect.name != "postgresql":
        return
    steps = [
        "ALTER TABLE uploads ADD COLUMN IF NOT EXISTS as_on DATE",
        # AUM snapshots are now kept per exact date instead of one per month
        "UPDATE aum_snapshots SET as_on = to_date(period || '-01', 'YYYY-MM-DD') WHERE as_on IS NULL",
        "UPDATE aum_snapshots SET period = to_char(as_on, 'YYYY-MM') WHERE period <> to_char(as_on, 'YYYY-MM')",
        "DELETE FROM aum_snapshots a USING aum_snapshots b "
        "WHERE a.investor_id = b.investor_id AND a.as_on = b.as_on AND a.id < b.id",
        "ALTER TABLE aum_snapshots ALTER COLUMN as_on SET NOT NULL",
        "ALTER TABLE aum_snapshots DROP CONSTRAINT IF EXISTS aum_snapshots_investor_id_period_key",
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_aum_investor_as_on') THEN "
        "ALTER TABLE aum_snapshots ADD CONSTRAINT uq_aum_investor_as_on UNIQUE (investor_id, as_on); END IF; END $$",
        "CREATE INDEX IF NOT EXISTS ix_aum_snapshots_as_on ON aum_snapshots (as_on)",
        "UPDATE uploads u SET as_on = s.d, period = to_char(s.d, 'YYYY-MM') FROM "
        "(SELECT upload_id, max(as_on) AS d FROM aum_snapshots GROUP BY upload_id) s "
        "WHERE u.id = s.upload_id AND u.report_type = 'AUM' AND u.as_on IS DISTINCT FROM s.d",
        # history rows of AUM files whose data was replaced by a later upload
        "DELETE FROM uploads WHERE report_type = 'AUM' AND id NOT IN (SELECT upload_id FROM aum_snapshots)",
    ]
    with engine.begin() as conn:
        for sql in steps:
            conn.execute(text(sql))


def bootstrap():
    Base.metadata.create_all(engine)
    migrate()
    with SessionLocal() as db:
        if not db.scalar(select(User.id).where(User.role == MASTER)):
            db.add(User(name=config.MASTER_NAME, email=config.MASTER_EMAIL, role=MASTER,
                        password_hash=hash_password(config.MASTER_PASSWORD)))
            db.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    bootstrap()
    yield


app = FastAPI(title="MF Referral Portal API", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=config.CORS_ORIGINS, allow_credentials=False,
                   allow_methods=["*"], allow_headers=["*"])
for r in (auth.router, users.router, uploads.router, investors.router, reports.router):
    app.include_router(r)


@app.get("/health")
def health():
    return {"ok": True}
