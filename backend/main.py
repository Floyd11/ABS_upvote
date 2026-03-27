import logging
import random
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import List

from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from db import init_db, get_db, User, VoteLog
from crypto import encrypt_key
from auth import (
    generate_nonce, verify_signature, create_jwt,
    get_current_wallet, verify_admin_key,
    NonceResponse, VerifyRequest, TokenResponse,
)
from models import (
    RegisterRequest, RegisterResponse,
    StatusResponse, VoteLogEntry,
    RevokeResponse,
)
from queue_logic import current_epoch, generate_week_queue, get_app_id_for_today, WEEK_SIZE
from scheduler import start_scheduler, stop_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting up...")
    await init_db()
    start_scheduler()
    yield
    logger.info("Shutting down...")
    stop_scheduler()


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="Abstract Upvote Bot", version="1.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Auth routes (SIWE)
# FIX (аудит п.3): пользователи аутентифицируются подписью кошелька, не API-ключом
# ---------------------------------------------------------------------------

@app.get("/auth/nonce", response_model=NonceResponse)
async def get_nonce(wallet: str):
    """Шаг 1: получить nonce для подписи."""
    nonce, message = generate_nonce(wallet)
    return NonceResponse(nonce=nonce, message=message)


@app.post("/auth/verify", response_model=TokenResponse)
async def verify_login(req: VerifyRequest):
    """Шаг 2: проверить подпись и получить JWT."""
    if not verify_signature(req.wallet_address, req.nonce, req.signature):
        raise HTTPException(status_code=401, detail="Invalid signature")
    token = create_jwt(req.wallet_address)
    return TokenResponse(access_token=token, wallet_address=req.wallet_address.lower())


# ---------------------------------------------------------------------------
# User routes (JWT auth)
# ---------------------------------------------------------------------------

@app.post("/register", response_model=RegisterResponse)
async def register(
    req: RegisterRequest,
    wallet: str = Depends(get_current_wallet),
    db: AsyncSession = Depends(get_db),
):
    """
    Регистрирует пользователя и сохраняет сессионный ключ.

    FIX (аудит п.2): фронтенд передаёт СЫРОЙ приватный ключ.
    Шифрование только на бэкенде (Fernet). Никакого двойного шифрования.

    FIX (аудит п.3): защита JWT токеном, не API-ключом.
    """
    # JWT гарантирует что запрос от владельца кошелька
    if req.wallet_address.lower() != wallet.lower():
        raise HTTPException(status_code=403, detail="Wallet address mismatch")

    result = await db.execute(
        select(User).where(User.wallet_address == wallet)
    )
    existing = result.scalar_one_or_none()
    epoch = current_epoch()

    if existing:
        # Обновляем ключ (переподключение)
        existing.session_key_enc    = encrypt_key(req.session_key_enc.get_secret_value())
        existing.session_expires_at = req.session_expires_at
        existing.session_config_json = req.session_config
        existing.voting_contract    = req.voting_contract or existing.voting_contract
        existing.is_active          = True
        existing.current_epoch      = epoch
        existing.week_app_index     = 0
        await db.commit()
        user = existing
    else:
        user = User(
            wallet_address      = wallet,
            session_key_enc     = encrypt_key(req.session_key_enc.get_secret_value()),
            session_expires_at  = req.session_expires_at,
            session_config_json = req.session_config,
            voting_contract     = req.voting_contract or settings.voting_contract,
            base_vote_second    = random.randint(0, 86399),
            current_epoch       = epoch,
            week_app_index      = 0,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)

    week_queue = generate_week_queue(user.wallet_address, epoch)
    return RegisterResponse(
        wallet_address   = user.wallet_address,
        base_vote_second = user.base_vote_second,
        current_epoch    = epoch,
        week_queue       = week_queue,
    )


@app.get("/status", response_model=StatusResponse)
async def status(
    wallet: str = Depends(get_current_wallet),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).where(User.wallet_address == wallet))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="Wallet not registered")

    epoch      = current_epoch()
    week_queue = generate_week_queue(user.wallet_address, epoch)

    today_app_id = None
    if user.week_app_index < WEEK_SIZE:
        today_app_id = get_app_id_for_today(user.wallet_address, epoch, user.week_app_index)

    next_vote_in_hours = None
    if user.last_voted_at:
        now = datetime.now(timezone.utc)
        now_s = now.hour * 3600 + now.minute * 60
        target = user.base_vote_second
        delta = (target - now_s) % 86400
        next_vote_in_hours = round(delta / 3600, 1)

    return StatusResponse(
        wallet_address     = user.wallet_address,
        is_active          = user.is_active,
        current_epoch      = epoch,
        week_app_index     = user.week_app_index,
        week_queue         = week_queue,
        today_app_id       = today_app_id,
        last_voted_at      = user.last_voted_at,
        streak_days        = user.streak_days,
        total_votes        = user.total_votes,
        next_vote_in_hours = next_vote_in_hours,
    )


@app.get("/history", response_model=List[VoteLogEntry])
async def history(
    limit: int = 20,
    wallet: str = Depends(get_current_wallet),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).where(User.wallet_address == wallet))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="Wallet not registered")

    logs_result = await db.execute(
        select(VoteLog)
        .where(VoteLog.user_id == user.id)
        .order_by(VoteLog.voted_at.desc())
        .limit(limit)
    )
    return [VoteLogEntry.model_validate(log) for log in logs_result.scalars().all()]


@app.post("/revoke", response_model=RevokeResponse)
async def revoke(
    wallet: str = Depends(get_current_wallet),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).where(User.wallet_address == wallet))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="Wallet not registered")

    user.is_active      = False
    user.session_key_enc = ""
    await db.commit()
    return RevokeResponse(wallet_address=user.wallet_address, revoked=True)


# ---------------------------------------------------------------------------
# Admin routes (API key — только серверные вызовы)
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok", "epoch": current_epoch()}


@app.get("/admin/users", dependencies=[Depends(verify_admin_key)])
async def admin_users(db: AsyncSession = Depends(get_db)):
    """Список всех пользователей (только для admin)."""
    result = await db.execute(select(User))
    users = result.scalars().all()
    return [{"id": u.id, "wallet": u.wallet_address, "active": u.is_active,
             "streak": u.streak_days, "total": u.total_votes} for u in users]
