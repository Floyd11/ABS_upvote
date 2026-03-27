"""
voter.py — делегирует отправку транзакции Node.js tx-service.

Почему не Python напрямую:
  AGW использует SessionKeyValidator модуль Privy.
  Подпись должна быть упакована как abi.encode(validatorAddress, ecdsaSignature).
  Официальный SDK только для JS: @abstract-foundation/agw-client.
  tx-service (Node.js) использует createSessionClient() из этого SDK.

Этот файл просто:
  1. Расшифровывает сессионный ключ из БД
  2. Делает HTTP POST на localhost:3010/vote
  3. Возвращает tx_hash или бросает исключение
"""

import logging
import httpx

from config import settings
from crypto import decrypt_key

logger = logging.getLogger(__name__)

TX_SERVICE_SECRET = settings.tx_service_secret   # shared secret между Python и Node
REQUEST_TIMEOUT   = 400  # секунды — с запасом на нагрузку RPC (6 мин tx + overhead)


async def send_vote(
    wallet_address: str,
    session_key_enc: str,
    session_config: dict,
    app_id: int,
    voting_contract: str = settings.voting_contract,
) -> str:
    """
    Отправляет voteForApp(appId) через tx-service.
    Возвращает tx_hash, бросает исключение при ошибке.
    """
    # Расшифровываем ключ из БД — tx-service получает сырой hex
    private_key = decrypt_key(session_key_enc)

    if not session_config:
        raise RuntimeError(
            f"session_config is missing for wallet {wallet_address[:10]}. "
            "User must re-authenticate and renew the bot session."
        )

    logger.info(
        "Sending vote via tx-service: agw=%s app_id=%d",
        wallet_address[:10], app_id,
    )

    payload = {
        "walletAddress":   wallet_address,
        "sessionPrivateKey": private_key,
        "sessionConfig":   session_config,
        "appId":           app_id,
        "votingContract":  voting_contract,
    }

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.post(
            f"{settings.tx_service_url}/vote",
            json=payload,
            headers={"x-internal-secret": settings.tx_service_secret},
        )

    if resp.status_code != 200:
        body = resp.text[:300]
        raise RuntimeError(f"tx-service error {resp.status_code}: {body}")

    data = resp.json()
    tx_hash = data.get("txHash", "")
    if not tx_hash:
        raise RuntimeError(f"tx-service returned no txHash: {data}")

    logger.info("✓ tx_hash=%s agw=%s app_id=%d", tx_hash[:22], wallet_address[:10], app_id)
    return tx_hash
