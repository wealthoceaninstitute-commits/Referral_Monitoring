from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select

from . import config
from .db import Base, SessionLocal, engine
from .models import MASTER, User
from .routers import auth, investors, reports, uploads, users
from .security import hash_password


def bootstrap():
    Base.metadata.create_all(engine)
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
