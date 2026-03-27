import logging
import random
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import List

from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from db import init_db, get_db, User, VoteLog
from crypto import encrypt_key, encrypt_raw
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
import gigaverse as gv
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

@app.middleware("http")
async def log_request_body(request: Request, call_next):
    if request.url.path == "/register" and request.method == "POST":
        body_bytes = await request.body()
        print("======== RAW BODY ========", flush=True)
        print(body_bytes.decode('utf-8', errors='replace'), flush=True)
        print("==========================", flush=True)
        async def receive():
            return {"type": "http.request", "body": body_bytes}
        request._receive = receive
    
    response = await call_next(request)
    
    if request.url.path == "/register" and response.status_code == 422:
        print(f"!!! RESPONSE RETURNED 422 !!!", flush=True)
    return response


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Логируем подробности 422 ошибки для дебага."""
    errs = exc.errors()
    body = exc.body
    print(f"\\n!!! VALIDATION ERROR 422 for {request.method} {request.url} !!!", flush=True)
    print(f"Errors: {errs}", flush=True)
    print(f"Body: {body}\\n", flush=True)
    logger.error("Validation error for %s %s: %s\\nBody: %s", request.method, request.url, errs, body)
    return JSONResponse(
        status_code=422,
        content={"detail": errs, "body": body},
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
    if not await verify_signature(req.wallet_address, req.nonce, req.signature):
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

    # Calculate next vote time
    now = datetime.now(timezone.utc)
    now_s = now.hour * 3600 + now.minute * 60
    delta = (user.base_vote_second - now_s) % 86400
    next_vote_in_hours = round(delta / 3600, 1)

    week_queue = generate_week_queue(user.wallet_address, epoch)
    return RegisterResponse(
        wallet_address   = user.wallet_address,
        is_active        = user.is_active,
        base_vote_second = user.base_vote_second,
        current_epoch    = epoch,
        week_queue       = week_queue,
        total_votes      = user.total_votes,
        streak_days      = user.streak_days,
        next_vote_in_hours = next_vote_in_hours
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


# ---------------------------------------------------------------------------
# Gigaverse routes
# ---------------------------------------------------------------------------

class GigaverseConnectRequest(BaseModel):
    """Payload sent from the frontend after the user signs the Gigaverse auth message."""
    signature: str
    message:   str
    timestamp: int


class GigaverseRegisterRequest(BaseModel):
    """Fallback: manual JWT paste (kept for backwards compatibility)."""
    gigaverse_token: str


@app.post("/gigaverse/connect")
async def gigaverse_connect(
    req: GigaverseConnectRequest,
    wallet: str = Depends(get_current_wallet),
    db: AsyncSession = Depends(get_db),
):
    """
    Exchange a wallet signature for a Gigaverse JWT.

    The frontend:
    1. Gets Date.now() as timestamp
    2. Signs the string "Login to Gigaverse at <timestamp>" with AGW
    3. Sends {signature, message, timestamp} to this endpoint

    We forward the signature to Gigaverse /user/auth, receive a JWT,
    encrypt it with Fernet and persist it in the DB.
    """
    result = await db.execute(select(User).where(User.wallet_address == wallet))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(
            status_code=404,
            detail="Register with the upvote bot first before activating Gigaverse",
        )

    try:
        auth_data = await gv.exchange_signature_for_jwt(
            wallet_address=wallet,
            signature=req.signature,
            message=req.message,
            timestamp=req.timestamp,
        )
    except Exception as exc:
        logger.error("Gigaverse auth exchange failed for %s: %s", wallet[:10], exc)
        raise HTTPException(
            status_code=400,
            detail=f"Gigaverse authentication failed: {exc}",
        )

    user.gigaverse_jwt_enc          = encrypt_raw(auth_data["jwt"])
    user.gigaverse_token_expires_at = auth_data["expires_at"]
    await db.commit()
    logger.info("Gigaverse JWT connected for %s (expires %s)", wallet[:10], auth_data["expires_at"])
    return {"connected": True, "expires_at": auth_data["expires_at"]}


@app.post("/gigaverse/register")
async def gigaverse_register(
    req: GigaverseRegisterRequest,
    wallet: str = Depends(get_current_wallet),
    db: AsyncSession = Depends(get_db),
):
    """
    Encrypt the Gigaverse JWT and persist it in the DB (manual / fallback).
    Requires the wallet to be registered with the upvote bot first.
    """
    result = await db.execute(select(User).where(User.wallet_address == wallet))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(
            status_code=404,
            detail="Register with the upvote bot first before activating Gigaverse",
        )
    user.gigaverse_jwt_enc = encrypt_raw(req.gigaverse_token.strip())
    await db.commit()
    logger.info("Gigaverse JWT registered (manual) for %s", wallet[:10])
    return {"registered": True}


@app.get("/gigaverse/status")
async def gigaverse_status(
    wallet: str = Depends(get_current_wallet),
    db: AsyncSession = Depends(get_db),
):
    """Gigaverse bot status for the authenticated wallet."""
    result = await db.execute(select(User).where(User.wallet_address == wallet))
    user = result.scalar_one_or_none()
    if not user:
        return {"active": False, "last_run": None, "expires_at": None}
    return {
        "active": bool(user.gigaverse_jwt_enc),
        "last_run": user.gigaverse_last_run,
        "expires_at": user.gigaverse_token_expires_at,
    }
