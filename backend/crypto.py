"""
crypto.py — шифрование сессионных ключей.

FIX (аудит п.2): фронтенд передаёт СЫРОЙ приватный ключ по HTTPS.
Шифрованием занимается ТОЛЬКО бэкенд (Fernet).
Никакого двойного шифрования — decrypt_key возвращает готовый hex-ключ.
"""
from cryptography.fernet import Fernet
from config import settings

_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(settings.encryption_key.encode())
    return _fernet


def encrypt_key(raw_private_key_hex: str) -> str:
    """
    Шифруем сырой приватный ключ перед сохранением в БД.
    Принимает hex строку (с 0x или без).
    """
    clean = raw_private_key_hex.removeprefix("0x").strip()
    if len(clean) != 64:
        raise ValueError(f"Invalid private key length: {len(clean)} chars (expected 64 hex chars)")
    return _get_fernet().encrypt(clean.encode()).decode()


def decrypt_key(encrypted: str) -> str:
    """
    Расшифровываем перед отправкой транзакции.
    Возвращает '0x' + 64 hex chars.
    """
    clean = _get_fernet().decrypt(encrypted.encode()).decode()
    return "0x" + clean
