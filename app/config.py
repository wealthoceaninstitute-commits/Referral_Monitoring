import os


def _db_url() -> str:
    url = os.environ.get("DATABASE_URL", "sqlite:///./dev.db")
    # Railway gives postgres:// or postgresql:// - point SQLAlchemy at psycopg 3
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


DATABASE_URL = _db_url()
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-only-secret-change-me-in-production")
TOKEN_HOURS = int(os.environ.get("TOKEN_HOURS", "12"))
CORS_ORIGINS = [o.strip() for o in os.environ.get("CORS_ORIGINS", "http://localhost:3000").split(",") if o.strip()]
MASTER_EMAIL = os.environ.get("MASTER_EMAIL", "master@example.com").lower()
MASTER_PASSWORD = os.environ.get("MASTER_PASSWORD", "change-me")
MASTER_NAME = os.environ.get("MASTER_NAME", "Master")
