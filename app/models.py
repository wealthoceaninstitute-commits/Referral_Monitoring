from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base

MASTER, ADMIN, RA = "MASTER", "ADMIN", "RA"
PURCHASE, REJECTION, REDEMPTION = "PURCHASE", "REJECTION", "REDEMPTION"
Money = Numeric(18, 2)


def now() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    phone: Mapped[Optional[str]] = mapped_column(String(20))
    password_hash: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(10), index=True)
    # MASTER -> None, ADMIN -> the master who created it, RA -> the admin it reports to
    parent_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Investor(Base):
    """One row per investor. NJ's AUM report carries only the name, so the natural key is
    (name_key, dup_seq); dup_seq separates genuine namesakes. PAN is filled in when a
    processed-transaction report reveals it."""
    __tablename__ = "investors"
    __table_args__ = (UniqueConstraint("name_key", "dup_seq"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    name_key: Mapped[str] = mapped_column(String(200), index=True)
    dup_seq: Mapped[int] = mapped_column(Integer, default=1)
    pan: Mapped[Optional[str]] = mapped_column(String(10), index=True)
    group_name: Mapped[Optional[str]] = mapped_column(String(200))
    ra_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), index=True)
    assigned_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    assigned_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Upload(Base):
    __tablename__ = "uploads"
    id: Mapped[int] = mapped_column(primary_key=True)
    report_type: Mapped[str] = mapped_column(String(12))
    filename: Mapped[str] = mapped_column(String(300))
    title: Mapped[Optional[str]] = mapped_column(String(300))
    period: Mapped[Optional[str]] = mapped_column(String(7))  # AUM only: month of as_on
    as_on: Mapped[Optional[date]] = mapped_column(Date)  # AUM only: the snapshot date
    rows_total: Mapped[int] = mapped_column(Integer, default=0)
    rows_new: Mapped[int] = mapped_column(Integer, default=0)
    rows_unmatched: Mapped[int] = mapped_column(Integer, default=0)
    investors_created: Mapped[int] = mapped_column(Integer, default=0)
    uploaded_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AumSnapshot(Base):
    """Position per investor on one exact date, straight from the Investor Wise AUM report.
    Any number of dates can be kept; uploading the same date again replaces it."""
    __tablename__ = "aum_snapshots"
    __table_args__ = (UniqueConstraint("investor_id", "as_on", name="uq_aum_investor_as_on"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    investor_id: Mapped[int] = mapped_column(ForeignKey("investors.id"), index=True)
    period: Mapped[str] = mapped_column(String(7), index=True)  # YYYY-MM of as_on
    as_on: Mapped[date] = mapped_column(Date, index=True)
    upload_id: Mapped[int] = mapped_column(ForeignKey("uploads.id"))
    inv_equity: Mapped[float] = mapped_column(Money, default=0)
    inv_debt: Mapped[float] = mapped_column(Money, default=0)
    inv_hybrid: Mapped[float] = mapped_column(Money, default=0)
    inv_physical: Mapped[float] = mapped_column(Money, default=0)
    inv_others: Mapped[float] = mapped_column(Money, default=0)
    inv_total: Mapped[float] = mapped_column(Money, default=0)
    aum_equity: Mapped[float] = mapped_column(Money, default=0)
    aum_debt: Mapped[float] = mapped_column(Money, default=0)
    aum_hybrid: Mapped[float] = mapped_column(Money, default=0)
    aum_physical: Mapped[float] = mapped_column(Money, default=0)
    aum_others: Mapped[float] = mapped_column(Money, default=0)
    aum_total: Mapped[float] = mapped_column(Money, default=0)


class Txn(Base):
    """Purchases (processed), rejections and redemptions in one table, bucketed by txn month."""
    __tablename__ = "transactions"
    id: Mapped[int] = mapped_column(primary_key=True)
    dedupe_key: Mapped[str] = mapped_column(String(64), unique=True)
    kind: Mapped[str] = mapped_column(String(12), index=True)
    period: Mapped[str] = mapped_column(String(7), index=True)
    investor_id: Mapped[Optional[int]] = mapped_column(ForeignKey("investors.id"), index=True)
    name_key: Mapped[str] = mapped_column(String(200), index=True)
    investor_name: Mapped[str] = mapped_column(String(200))
    pan: Mapped[Optional[str]] = mapped_column(String(10))
    txn_date: Mapped[date] = mapped_column(Date, index=True)
    txn_type: Mapped[Optional[str]] = mapped_column(String(40))
    txn_mode: Mapped[Optional[str]] = mapped_column(String(40))
    folio: Mapped[Optional[str]] = mapped_column(String(40), index=True)
    scheme: Mapped[Optional[str]] = mapped_column(String(300))
    isin: Mapped[Optional[str]] = mapped_column(String(20))
    nav: Mapped[Optional[float]] = mapped_column(Numeric(18, 4))
    units: Mapped[Optional[float]] = mapped_column(Numeric(18, 4))
    amount: Mapped[float] = mapped_column(Money, default=0)
    remark: Mapped[Optional[str]] = mapped_column(String(300))
    payout_date: Mapped[Optional[date]] = mapped_column(Date)
    payout_mode: Mapped[Optional[str]] = mapped_column(String(60))
    ref_no: Mapped[Optional[str]] = mapped_column(String(40))
    upload_id: Mapped[int] = mapped_column(ForeignKey("uploads.id"))


class Registration(Base):
    """One row per E-Wealth account from the Reg. Status report - the full registered client
    list, including people who have not invested yet."""
    __tablename__ = "registrations"
    id: Mapped[int] = mapped_column(primary_key=True)
    ref_no: Mapped[str] = mapped_column(String(40), unique=True)
    investor_id: Mapped[Optional[int]] = mapped_column(ForeignKey("investors.id"), index=True)
    name_key: Mapped[str] = mapped_column(String(200), index=True)
    account_name: Mapped[str] = mapped_column(String(200))
    pan: Mapped[Optional[str]] = mapped_column(String(10))
    account_type: Mapped[Optional[str]] = mapped_column(String(40))
    mode: Mapped[Optional[str]] = mapped_column(String(40))
    acquired_from: Mapped[Optional[str]] = mapped_column(String(60))
    upload_id: Mapped[int] = mapped_column(ForeignKey("uploads.id"), index=True)
