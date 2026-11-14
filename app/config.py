import os


def _db_url() -> str:
    raw = os.environ.get("DATABASE_URL")
    url = (raw or "").strip().strip("'\"").strip()
    if not url:
        if raw is not None:  # variable exists but is empty -> a mistake, not "use the local default"
            raise RuntimeError("DATABASE_URL is set but empty. In Railway set it to ${{Postgres.DATABASE_URL}} "
                               "(use your database service's exact name).")
        return "sqlite:///./dev.db"
    if "${{" in url or "{{" in url:
        raise RuntimeError("DATABASE_URL still contains an unresolved Railway reference: " + url[:60] +
                           " - the service name inside ${{ }} must match your Postgres service exactly.")
    # Railway gives postgres:// or postgresql:// - point SQLAlchemy at psycopg 3
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    if not url.startswith(("postgresql+psycopg://", "sqlite:")):
        raise RuntimeError("DATABASE_URL must start with postgresql:// - got: " + url[:30] + "...")
    return url


DATABASE_URL = _db_url()
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-only-secret-change-me-in-production")
TOKEN_HOURS = int(os.environ.get("TOKEN_HOURS", "12"))
CORS_ORIGINS = [o.strip() for o in os.environ.get("CORS_ORIGINS", "http://localhost:3000").split(",") if o.strip()]
MASTER_EMAIL = os.environ.get("MASTER_EMAIL", "master@example.com").lower()
MASTER_PASSWORD = os.environ.get("MASTER_PASSWORD", "change-me")
MASTER_NAME = os.environ.get("MASTER_NAME", "Master")
