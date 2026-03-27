"""
auth.py — SIWE (Sign-In with Ethereum) аутентификация.

FIX (аудит п.3): вместо единого api_secret_key в браузере
используем подпись кошельком → выдаём JWT.

Флоу:
  1. GET /auth/nonce?wallet=0x...  → получаем одноразовый nonce
  2. Фронт подписывает сообщение кошельком (AGW поддерживает personal_sign)
  3. POST /auth/verify {wallet, signature, nonce} → получаем JWT
  4. Все запросы идут с заголовком Authorization: Bearer <jwt>

Внутренние эндпоинты (/health, admin) по-прежнему защищены X-Api-Key.
"""
import secrets
import time
import logging
from typing import Optional

import jwt
from eth_account import Account
from eth_account.messages import encode_defunct
from eth_utils import to_checksum_address
from fastapi import Depends, HTTPException, Header
from pydantic import BaseModel

from config import settings

logger = logging.getLogger(__name__)

# Nonce-кеш (in-memory, TTL 5 минут)
# В production замени на Redis
_nonces: dict[str, float] = {}
NONCE_TTL = 300  # секунды


def _cleanup_nonces():
    now = time.time()
    expired = [k for k, ts in _nonces.items() if now - ts > NONCE_TTL]
    for k in expired:
        del _nonces[k]


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class NonceResponse(BaseModel):
    nonce: str
    message: str  # готовое сообщение для подписи


class VerifyRequest(BaseModel):
    wallet_address: str
    signature: str
    nonce: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    wallet_address: str


# ---------------------------------------------------------------------------
# SIWE helpers
# ---------------------------------------------------------------------------

def _build_siwe_message(wallet: str, nonce: str, issued_at: Optional[float] = None) -> str:
    ts = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(issued_at or time.time()))
    return (
        f"Abstract Upvote Bot wants you to sign in with your Ethereum account:\n"
        f"{wallet}\n\n"
        f"Sign in to automate your daily upvotes.\n\n"
        f"Nonce: {nonce}\n"
        f"Issued At: {ts}"
    )


def generate_nonce(wallet_address: str) -> tuple[str, str]:
    """Генерируем nonce и сохраняем в кеш. Возвращает (nonce, message)."""
    _cleanup_nonces()
    nonce = secrets.token_hex(16)
    now = time.time()
    _nonces[f"{wallet_address.lower()}:{nonce}"] = now
    message = _build_siwe_message(wallet_address, nonce, now)
    return nonce, message


def verify_signature(wallet_address: str, nonce: str, signature: str) -> bool:
    """Проверяем подпись SIWE-сообщения."""
    key = f"{wallet_address.lower()}:{nonce}"

    # Проверяем что nonce существует и не истёк
    issued_at = _nonces.get(key)
    if not issued_at:
        logger.warning("Unknown nonce: %s", key)
        return False
    if time.time() - issued_at > NONCE_TTL:
        del _nonces[key]
        logger.warning("Expired nonce: %s", key)
        return False

    # Удаляем nonce (одноразовый)
    del _nonces[key]

    # Восстанавливаем сообщение и проверяем подпись
    message = _build_siwe_message(wallet_address, nonce, issued_at)
    try:
        msg = encode_defunct(text=message)
        recovered = Account.recover_message(msg, signature=signature)
        return recovered.lower() == wallet_address.lower()
    except Exception as e:
        logger.warning("Signature verification failed: %s", e)
        return False


def create_jwt(wallet_address: str) -> str:
    """Создаём JWT токен (TTL 7 дней)."""
    payload = {
        "sub": wallet_address.lower(),
        "iat": int(time.time()),
        "exp": int(time.time()) + 7 * 24 * 3600,
    }
    return jwt.encode(payload, settings.api_secret_key, algorithm="HS256")


def decode_jwt(token: str) -> Optional[str]:
    """Декодируем JWT, возвращаем wallet_address или None."""
    try:
        payload = jwt.decode(token, settings.api_secret_key, algorithms=["HS256"])
        return payload["sub"]
    except jwt.ExpiredSignatureError:
        return None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------

async def get_current_wallet(authorization: str = Header(...)) -> str:
    """
    Dependency для пользовательских роутов.
    Проверяет JWT из заголовка Authorization: Bearer <token>
    """
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header")
    token = authorization.removeprefix("Bearer ").strip()
    wallet = decode_jwt(token)
    if not wallet:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return wallet


async def verify_admin_key(x_api_key: str = Header(...)) -> None:
    """
    Dependency для внутренних/admin роутов.
    Используем только серверно (не из браузера).
    """
    if x_api_key != settings.api_secret_key:
        raise HTTPException(status_code=401, detail="Invalid API key")
