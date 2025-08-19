from datetime import datetime
from sqlalchemy import create_engine, Column, Integer, String, BigInteger, DateTime, Boolean
from sqlalchemy.orm import DeclarativeBase, sessionmaker

# SQLite DB setup
engine = create_engine("sqlite:///airdrop.db", echo=False, future=True)
SessionLocal = sessionmaker(bind=engine, future=True)

class Base(DeclarativeBase):
    pass

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    tg_user_id = Column(BigInteger, unique=True, index=True, nullable=False)
    bsc_address = Column(String, nullable=True, unique=True)
    joined_verified = Column(Boolean, default=False)
    welcomed_paid = Column(Boolean, default=False)
    referrals_count = Column(Integer, default=0)
    owed_beam = Column(Integer, default=0)
    referred_by = Column(BigInteger, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class Referral(Base):
    __tablename__ = "referrals"
    id = Column(Integer, primary_key=True)
    referrer_tg = Column(BigInteger, index=True, nullable=False)
    referee_tg = Column(BigInteger, unique=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

class Payout(Base):
    __tablename__ = "payouts"
    id = Column(Integer, primary_key=True)
    tg_user_id = Column(BigInteger, index=True)
    tx_hash = Column(String, nullable=False)
    amount_beam = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

def init_db():
    Base.metadata.create_all(bind=engine)
