# db.py
from sqlalchemy import create_engine, Column, Integer, BigInteger, String, Boolean, DateTime, ForeignKey
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from datetime import datetime
import os

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///airdrop.db")

engine = create_engine(
    DATABASE_URL,
    future=True,
    pool_pre_ping=True,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Base = declarative_base()

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # REQUIRED BY YOUR CODE:
    telegram_id = Column(BigInteger, unique=True, index=True, nullable=False)
    username = Column(String(255), default="")

    # Airdrop fields your bot uses
    bsc_address = Column(String(64), nullable=True)
    balance_beam = Column(Integer, default=0)          # accumulated BEAM
    referrals_count = Column(Integer, default=0)       # number of successful referrals
    referred_by = Column(BigInteger, nullable=True)    # telegram_id of referrer

    created_at = Column(DateTime, default=datetime.utcnow)

    # relationships
    referrals = relationship("Referral", back_populates="referrer", cascade="all, delete-orphan")
    payouts = relationship("Payout", back_populates="user", cascade="all, delete-orphan")

class Referral(Base):
    __tablename__ = "referrals"

    id = Column(Integer, primary_key=True, autoincrement=True)
    referrer_id = Column(BigInteger, index=True, nullable=False)  # telegram_id of referrer
    referee_id = Column(BigInteger, index=True, nullable=False)   # telegram_id of referee
    created_at = Column(DateTime, default=datetime.utcnow)

    # not strictly needed for queries, but nice to have
    referrer_db_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    referrer = relationship("User", back_populates="referrals", foreign_keys=[referrer_db_id])

class Payout(Base):
    __tablename__ = "payouts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    telegram_id = Column(BigInteger, index=True, nullable=False)  # who got paid (telegram_id)
    amount = Column(Integer, nullable=False)
    tx_hash = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    user = relationship("User", back_populates="payouts")

def init_db():
    Base.metadata.create_all(bind=engine)
