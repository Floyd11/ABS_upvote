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
import httpx
from typing import Optional

import jwt
from eth_account import Account
from eth_account.messages import encode_defunct
from eth_utils import to_checksum_address, to_bytes, keccak
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


async def verify_signature(wallet_address: str, nonce: str, signature: str) -> bool:
    """Проверяем подпись SIWE-сообщения. Поддерживает EOA и Smart Accounts (EIP-1271)."""
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
    
    # 1. Попытка EOA recovery (стандартная подпись)
    try:
        msg = encode_defunct(text=message)
        recovered = Account.recover_message(msg, signature=signature)
        if recovered.lower() == wallet_address.lower():
            return True
    except Exception:
        pass

    # 2. Попытка EIP-1271 (Smart Accounts / AGW)
    return await _verify_eip1271(wallet_address, message, signature)


async def _verify_eip1271(wallet: str, message: str, signature: str) -> bool:
    """
    EIP-1271 verification for Smart Contract Wallets (AGW).
    Calls isValidSignature(bytes32, bytes) on the wallet contract via RPC.
    The hash must be the EIP-191 personal_sign hash:
      keccak256("\x19Ethereum Signed Message:\n" + len(message) + message)
    """
    try:
        # Compute EIP-191 personal_sign hash manually
        # (SignableMessage from encode_defunct has no .message_hash attribute)
        message_bytes = message.encode("utf-8")
        prefix = f"\x19Ethereum Signed Message:\n{len(message_bytes)}".encode("utf-8")
        msg_hash: bytes = keccak(prefix + message_bytes)
        
        logger.debug(
            "EIP-1271 check wallet=%s msg_hash=%s sig=%s",
            wallet, msg_hash.hex(), signature[:20]
        )

        sig_bytes = to_bytes(hexstr=signature)
        # Pad signature to 32-byte boundary
        padded_sig_len = ((len(sig_bytes) + 31) // 32) * 32
        padded_sig = sig_bytes.ljust(padded_sig_len, b"\x00")

        # ABI-encode call to isValidSignature(bytes32 hash, bytes memory sig)
        # Selector: 0x1626ba7e
        # Slot 0 (32 bytes): hash
        # Slot 1 (32 bytes): offset to dynamic bytes param = 0x40 (64)
        # Slot 2 (32 bytes): byte length of signature
        # Slot 3+ (N*32 bytes): signature data
        call_data = (
            "1626ba7e"
            + msg_hash.hex()  # already 32 bytes = 64 hex chars
            + "0000000000000000000000000000000000000000000000000000000000000040"
            + hex(len(sig_bytes))[2:].zfill(64)
            + padded_sig.hex()
        )
        data = "0x" + call_data

        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                settings.abstract_rpc_url,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "eth_call",
                    "params": [{"to": to_checksum_address(wallet), "data": data}, "latest"],
                }
            )
            body = resp.json()
            result = body.get("result", "")
            rpc_error = body.get("error")
            
            if rpc_error:
                logger.warning("EIP-1271 RPC error: %s", rpc_error)
            else:
                logger.info("EIP-1271 result for %s: %s", wallet, result)

            # Magic value: 0x1626ba7e (first 4 bytes of the 32-byte return)
            return isinstance(result, str) and result.lower().startswith("0x1626ba7e")
    except Exception as e:
        logger.warning("EIP-1271 check failed: %s", e)
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
