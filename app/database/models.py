from datetime import datetime
from typing import Optional
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy import BigInteger, ForeignKey, String, Float, DateTime, Boolean


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    prompt_vibe: Mapped[str] = mapped_column(String, default="Заботливый друг")
    state: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    monthly_income: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    envelopes = relationship("Envelope", back_populates="user", cascade="all, delete-orphan")
    transactions = relationship("Transaction", back_populates="user", cascade="all, delete-orphan")


class Envelope(Base):
    __tablename__ = "envelopes"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.telegram_id"))
    name: Mapped[str] = mapped_column(String)
    target_amount: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    current_amount: Mapped[float] = mapped_column(Float, default=0.0)
    is_debt: Mapped[bool] = mapped_column(Boolean, default=False)
    is_goal: Mapped[bool] = mapped_column(Boolean, default=False)
    priority: Mapped[int] = mapped_column(default=1)

    user = relationship("User", back_populates="envelopes")
    transactions = relationship("Transaction", back_populates="envelope")


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.telegram_id"))
    amount: Mapped[float] = mapped_column(Float)
    envelope_id: Mapped[Optional[int]] = mapped_column(ForeignKey("envelopes.id"), nullable=True)
    description: Mapped[str] = mapped_column(String)
    datetime_created: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="transactions")
    envelope = relationship("Envelope", back_populates="transactions")


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.telegram_id"))
    role: Mapped[str] = mapped_column(String)
    content: Mapped[str] = mapped_column(String)
    datetime_created: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    user = relationship("User")
