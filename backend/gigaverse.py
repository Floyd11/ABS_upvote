"""
gigaverse.py — Gigaverse dungeon auto-play via REST API.

Uses httpx for async HTTP. No external SDK required.
Reference: https://gigaverse.io/api  (see skill docs at .agent/skills/gigaverse/SKILL.md)

Auth flow (SIWE-style, no nonce from server):
  1. Frontend gets current unix_ms timestamp
  2. Frontend signs message: "Login to Gigaverse at <unix_ms>"
  3. Frontend sends {signature, address, message, timestamp} to our backend
  4. Backend forwards to POST /user/auth → receives JWT + expiresAt
  5. JWT encrypted with Fernet → stored in upvote_bot.users
"""
import asyncio
import logging
import random
from typing import Any

import httpx

logger = logging.getLogger(__name__)

BASE_URL = "https://gigaverse.io/api"
DUNGEON_ID = 1  # Dungetron 5000 — default dungeon
ENERGY_COST = 40  # energy cost per Dungetron 5000 run
MAX_MOVES = 50   # hard cap — prevents infinite loops on unexpected states

# Balanced rotation: Sword→Shield→Spell→Sword…
_COMBAT_MOVES = ["rock", "paper", "scissor"]


async def exchange_signature_for_jwt(
    wallet_address: str,
    signature: str,
    message: str,
    timestamp: int,
) -> dict:
    """
    Exchange a wallet SIWE-style signature for a Gigaverse JWT.

    Returns:
        {"jwt": str, "expires_at": datetime}  — ready to encrypt and store.

    Raises:
        httpx.HTTPStatusError on non-2xx
        ValueError if response is malformed
    """
    payload = {
        "signature": signature,
        "address": wallet_address,
        "message": message,
        "timestamp": timestamp,
        "agent_metadata": {
            "type": "gigaverse-play-skill",
            "model": "abstract-upvote-bot",
        },
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(f"{BASE_URL}/user/auth", json=payload)
        resp.raise_for_status()

    data = resp.json()
    jwt_token = data.get("jwt")
    expires_ms = data.get("expiresAt")

    if not jwt_token:
        raise ValueError(f"Gigaverse auth returned no jwt: {data}")

    from datetime import datetime, timezone
    expires_at = (
        datetime.fromtimestamp(expires_ms / 1000, tz=timezone.utc)
        if expires_ms
        else None
    )

    logger.info(
        "[gigaverse] JWT obtained for %s, expires=%s",
        wallet_address[:10],
        expires_at,
    )
    return {"jwt": jwt_token, "expires_at": expires_at}


def _headers(jwt: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {jwt}",
        "Content-Type": "application/json",
    }


async def check_energy(wallet_address: str, jwt: str) -> int:
    """
    Returns current energy for the player.
    Raises httpx.HTTPStatusError on non-2xx response.
    """
    url = f"{BASE_URL}/offchain/player/energy/{wallet_address}"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, headers=_headers(jwt))
        resp.raise_for_status()
    data: Any = resp.json()
    try:
        return int(data["entities"][0]["parsedData"]["currentEnergy"])
    except (KeyError, IndexError, TypeError, ValueError):
        logger.warning("[gigaverse] Unexpected energy response: %s", data)
        return 0


async def _dungeon_action(client: httpx.AsyncClient, jwt: str, payload: dict) -> dict:
    """Send a single dungeon action and return parsed JSON response."""
    resp = await client.post(
        f"{BASE_URL}/game/dungeon/action",
        headers=_headers(jwt),
        json=payload,
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


async def _resync_state(client: httpx.AsyncClient, jwt: str) -> dict:
    """Fetch current run state to recover actionToken after unexpected errors."""
    resp = await client.get(
        f"{BASE_URL}/game/dungeon/state",
        headers=_headers(jwt),
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def _extract_token(response: dict) -> int | None:
    """Extract actionToken from a dungeon API response."""
    try:
        # Token may sit at response root or inside .data
        tok = response.get("actionToken") or response.get("data", {}).get("actionToken")
        return int(tok) if tok is not None else None
    except (TypeError, ValueError):
        return None


def _extract_run_status(response: dict) -> str | None:
    """Extract run.status from response (dead / completed / cancelled / None)."""
    try:
        return (
            response.get("run", {}).get("status")
            or response.get("data", {}).get("run", {}).get("status")
        )
    except (AttributeError, TypeError):
        return None


def _extract_pending_action(response: dict) -> str | None:
    """
    Extract the pendingAction.type from response.
    Values: 'combat', 'loot', 'heal_or_damage', None (run ended).
    """
    try:
        pa = (
            response.get("pendingAction")
            or response.get("data", {}).get("pendingAction")
        )
        if pa is None:
            return None
        return pa.get("type") if isinstance(pa, dict) else str(pa)
    except (AttributeError, TypeError):
        return None


async def run_dungeon(wallet_address: str, jwt: str) -> dict:
    """
    Execute one full dungeon run for the given player.

    Returns a summary dict: {moves, result, wallet}.
    Raises on non-recoverable errors (lost session, API errors, etc.).
    """
    logger.info("[gigaverse] Starting run for %s", wallet_address[:10])

    # Pre-flight energy check — skip run if not enough energy
    energy = await check_energy(wallet_address, jwt)
    if energy < ENERGY_COST:
        logger.info(
            "[gigaverse] Not enough energy (%d/%d) for %s — skipping run",
            energy, ENERGY_COST, wallet_address[:10],
        )
        return {"moves": 0, "result": "skipped_low_energy", "wallet": wallet_address[:10]}

    async with httpx.AsyncClient(timeout=20) as client:
        # 1. Start the run — actionToken always begins at 0
        start_resp = await _dungeon_action(client, jwt, {
            "action": "start_run",
            "dungeonId": DUNGEON_ID,
            "actionToken": 0,
            "data": {"consumables": [], "isJuiced": False, "index": 0},
        })

        action_token = _extract_token(start_resp)
        if action_token is None:
            # Attempt state resync before giving up
            state = await _resync_state(client, jwt)
            action_token = _extract_token(state)
            if action_token is None:
                raise RuntimeError("Could not obtain actionToken after start_run")

        logger.info("[gigaverse] Run started, token=%d", action_token)

        move_count = 0
        final_status = "unknown"
        move_idx = 0  # index into _COMBAT_MOVES for balanced rotation

        # 2. Game loop
        for _ in range(MAX_MOVES):
            run_status = _extract_run_status(start_resp if move_count == 0 else resp)  # type: ignore[possibly-undefined]
            pending = _extract_pending_action(start_resp if move_count == 0 else resp)  # type: ignore[possibly-undefined]

            # Exit if run already ended (detected from previous response)
            if run_status in {"dead", "completed", "cancelled"}:
                final_status = run_status
                logger.info(
                    "[gigaverse] Run ended: %s after %d moves",
                    final_status, move_count,
                )
                break

            # Determine next action
            if pending == "combat" or pending is None and move_count == 0:
                action = _COMBAT_MOVES[move_idx % len(_COMBAT_MOVES)]
                move_idx += 1
                payload = {
                    "action": action,
                    "dungeonId": DUNGEON_ID,
                    "actionToken": action_token,
                    "data": {},
                }
            elif pending == "loot":
                # Always take loot option 1 (highest chance of rarity-first)
                payload = {
                    "action": "loot_one",
                    "dungeonId": DUNGEON_ID,
                    "actionToken": action_token,
                    "data": {},
                }
            elif pending in {"heal_or_damage"}:
                payload = {
                    "action": "heal_or_damage",
                    "dungeonId": DUNGEON_ID,
                    "actionToken": action_token,
                    "data": {},
                }
            else:
                # Unknown pending state — try to resync once
                logger.warning(
                    "[gigaverse] Unknown pendingAction=%s at move %d, resyncing",
                    pending, move_count,
                )
                try:
                    state = await _resync_state(client, jwt)
                    action_token = _extract_token(state) or action_token
                    rs = _extract_run_status(state)
                    if rs in {"dead", "completed", "cancelled"}:
                        final_status = rs
                        break
                except httpx.HTTPStatusError:
                    pass
                break

            try:
                resp = await _dungeon_action(client, jwt, payload)
            except httpx.HTTPStatusError as exc:
                logger.error(
                    "[gigaverse] Action failed (HTTP %d) at move %d: %s",
                    exc.response.status_code, move_count, exc.response.text[:200],
                )
                break

            # Update token from response
            new_token = _extract_token(resp)
            if new_token is not None:
                action_token = new_token

            move_count += 1

            # Jitter delay between moves — avoids bot-detection signatures
            await asyncio.sleep(random.uniform(1.2, 3.8))

        else:
            # Reached MAX_MOVES without explicit end
            final_status = "max_moves_reached"
            logger.warning(
                "[gigaverse] Hit MAX_MOVES=%d for %s",
                MAX_MOVES, wallet_address[:10],
            )

    result = {"moves": move_count, "result": final_status, "wallet": wallet_address[:10]}
    logger.info("[gigaverse] Run complete: %s", result)
    return result
