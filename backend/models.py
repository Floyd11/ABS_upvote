from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel, field_validator, SecretStr
import re


class RegisterRequest(BaseModel):
    """
    Фронтенд отправляет после создания Session Key.

    session_key_enc — СЫРОЙ приватный ключ сессии (hex, 0x + 64 символа).
    Передаётся по HTTPS. Шифрование только на бэкенде (Fernet в crypto.py).
    Никакого AES-GCM на фронтенде — это вызвало бы двойное шифрование.
    """
    wallet_address: str
    session_key_enc: SecretStr          # Маскируем в логах
    session_expires_at: Optional[datetime] = None
    session_config: Optional[dict] = None # Конфиг сессии для AGW SDK
    voting_contract: Optional[str] = "0x3B50dE27506f0a8C1f4122A1e6F470009a76ce2A"

    @field_validator("wallet_address")
    @classmethod
    def validate_address(cls, v: str) -> str:
        if not re.match(r"^0x[0-9a-fA-F]{40}$", v):
            raise ValueError("Invalid Ethereum address")
        return v.lower()

    @field_validator("session_key_enc", mode="before")
    @classmethod
    def validate_session_key(cls, v: any) -> any:
        """Убеждаемся что это именно сырой hex-ключ, не зашифрованная строка."""
        if not isinstance(v, str):
            return v
        clean = v.removeprefix("0x").strip()
        if len(clean) != 64:
            raise ValueError(
                f"session_key_enc должен быть hex-ключом (64 символа без 0x), "
                f"получено {len(clean)} символов. "
                f"Убедись что фронтенд передаёт сырой приватный ключ, а не зашифрованную строку."
            )
        if not re.match(r"^[0-9a-fA-F]{64}$", clean):
            raise ValueError("session_key_enc содержит не-hex символы")
        return v


class RegisterResponse(BaseModel):
    wallet_address: str
    base_vote_second: int
    current_epoch: int
    week_queue: List[int]


class StatusResponse(BaseModel):
    wallet_address: str
    is_active: bool
    current_epoch: int
    week_app_index: int
    week_queue: List[int]
    today_app_id: Optional[int]
    last_voted_at: Optional[datetime]
    streak_days: int
    total_votes: int
    next_vote_in_hours: Optional[float]


class VoteLogEntry(BaseModel):
    app_id: int
    epoch: int
    tx_hash: Optional[str]
    voted_at: datetime
    status: str
    error_msg: Optional[str]

    class Config:
        from_attributes = True


class RevokeResponse(BaseModel):
    wallet_address: str
    revoked: bool
