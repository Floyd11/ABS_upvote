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

TX_SERVICE_URL    = "http://127.0.0.1:3010"
TX_SERVICE_SECRET = settings.tx_service_secret   # shared secret между Python и Node
REQUEST_TIMEOUT   = 400  # секунды — с запасом на нагрузку RPC (6 мин tx + overhead)

VOTING_CONTRACT = "0x3B50dE27506f0a8C1f4122A1e6F470009a76ce2A"


async def send_vote(
    wallet_address: str,
    session_key_enc: str,
    app_id: int,
    voting_contract: str = VOTING_CONTRACT,
) -> str:
    """
    Отправляет voteForApp(appId) через tx-service.
    Возвращает tx_hash, бросает исключение при ошибке.
    """
    # Расшифровываем ключ из БД — tx-service получает сырой hex
    private_key = decrypt_key(session_key_enc)

    logger.info(
        "Sending vote via tx-service: agw=%s app_id=%d",
        wallet_address[:10], app_id,
    )

    payload = {
        "walletAddress":   wallet_address,
        "sessionPrivateKey": private_key,
        "appId":           app_id,
        "votingContract":  voting_contract,
    }

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.post(
            f"{TX_SERVICE_URL}/vote",
            json=payload,
            headers={"x-internal-secret": TX_SERVICE_SECRET},
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
