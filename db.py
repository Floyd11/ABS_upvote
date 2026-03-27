from datetime import datetime
from typing import Optional, List
from sqlalchemy import (
    Integer, String, Boolean, DateTime, BigInteger,
    ForeignKey, ARRAY, Index, text
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.schema import CreateSchema

from config import settings


# ---------------------------------------------------------------------------
# Engine & Session
# ---------------------------------------------------------------------------

engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_pre_ping=True,
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


# ---------------------------------------------------------------------------
# Base with schema
# ---------------------------------------------------------------------------

SCHEMA = settings.db_schema


class Base(DeclarativeBase):
    __table_args__ = {"schema": SCHEMA}


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    wallet_address: Mapped[str] = mapped_column(String(42), unique=True, nullable=False, index=True)

    # Сессионный ключ зашифрован Fernet
    session_key_enc: Mapped[str] = mapped_column(String, nullable=False)
    session_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    # Контракт голосования (можно переопределить на уровне юзера)
    voting_contract: Mapped[str] = mapped_column(
        String(42), default="0x3B50dE27506f0a8C1f4122A1e6F470009a76ce2A"
    )

    # Расписание — базовое время в секундах от начала суток (UTC)
    # например 33300 = 09:15:00
    base_vote_second: Mapped[int] = mapped_column(Integer, nullable=False)

    # Текущая эпоха и позиция в очереди
    current_epoch: Mapped[int] = mapped_column(Integer, default=0)
    week_app_ids: Mapped[List[int]] = mapped_column(ARRAY(Integer), default=list)
    week_app_index: Mapped[int] = mapped_column(Integer, default=0)

    # Статистика
    last_voted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    streak_days: Mapped[int] = mapped_column(Integer, default=0)
    total_votes: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow
    )

    # Relations
    vote_logs: Mapped[List["VoteLog"]] = relationship(back_populates="user")

    def __repr__(self):
        return f"<User {self.wallet_address[:10]}... epoch={self.current_epoch}>"


class VoteLog(Base):
    __tablename__ = "vote_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey(f"{SCHEMA}.users.id"), nullable=False)
    app_id: Mapped[int] = mapped_column(Integer, nullable=False)
    epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    tx_hash: Mapped[Optional[str]] = mapped_column(String(66))
    voted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    status: Mapped[str] = mapped_column(String(10), default="ok")  # ok | fail
    error_msg: Mapped[Optional[str]] = mapped_column(String)

    user: Mapped["User"] = relationship(back_populates="vote_logs")

    __table_args__ = (
        Index("ix_vote_log_user_epoch", "user_id", "epoch"),
        {"schema": SCHEMA},
    )


# ---------------------------------------------------------------------------
# Init DB (создаёт схему и таблицы если не существуют)
# ---------------------------------------------------------------------------

async def init_db():
    async with engine.begin() as conn:
        # Создаём схему если нет
        await conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}"))
        # Создаём таблицы
        await conn.run_sync(Base.metadata.create_all)
