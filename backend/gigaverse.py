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
import re
from typing import Any

import httpx

logger = logging.getLogger(__name__)

BASE_URL = "https://gigaverse.io/api"
ENERGY_COST = 40  # minimum energy required to start a run (base cost, Dungetron 5000)
MAX_MOVES = 120   # total iterations (including resyncs/errors). Dungeons can be long.
MAX_CONSECUTIVE_RESYNCS = 5  # abort after this many back-to-back resync failures

# Known dungeon IDs
DUNGEON_DUNGETRON = 1   # Dungetron: 5000 (main, always available)
DUNGEON_UNDERHAUL = 2   # Underhaul (alternative)

# Balanced combat rotation: Sword → Shield → Spell → …
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

    Gigaverse API has returned different field names over time; we use a
    fallback chain to stay resilient to API changes:
      currentEnergy → energyValue → energy // 1e9
    """
    url = f"{BASE_URL}/offchain/player/energy/{wallet_address}"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, headers=_headers(jwt))
        resp.raise_for_status()
    data: Any = resp.json()
    try:
        pe = data["entities"][0]["parsedData"]
        current_en = (
            pe.get("currentEnergy")
            or pe.get("energyValue")
            or (pe.get("energy", 0) // 1_000_000_000)
        )
        logger.info("[gigaverse] Parsed energy: %s from pe=%s", current_en, pe)
        return int(current_en)
    except Exception as e:
        logger.warning(
            "[gigaverse] Unexpected energy response (%s): %s",
            str(e), data if "data" in dir() else "no data",
        )
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
    """
    Fetch current run state from /game/dungeon/state.

    This is the canonical source of truth for:
      - Whether a run is active (entity field present)
      - Whether the run has ended (entity.COMPLETE_CID == true)
      - What action is needed next (run.lootPhase)
      - The current actionToken
    """
    resp = await client.get(
        f"{BASE_URL}/game/dungeon/state",
        headers=_headers(jwt),
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def _extract_token(response: dict) -> int | None:
    """Extract actionToken from any dungeon API response (action or state)."""
    try:
        tok = response.get("actionToken") or response.get("data", {}).get("actionToken")
        return int(tok) if tok is not None else None
    except (TypeError, ValueError):
        return None


def _extract_token_from_error(error_body: dict | str) -> int | None:
    """
    Try to extract the expected actionToken from a 400 error response.
    Message format often is: "... Invalid action token 123 != 456"
    where 456 is the one we want.
    """
    msg = ""
    if isinstance(error_body, dict):
        msg = error_body.get("message", "")
    else:
        msg = str(error_body)

    # Looking for numbers in "token 123 != 456" or "token null != 456"
    matches = re.findall(r"(\d+)", msg)
    if matches:
        try:
            # We assume the last number is the expected token
            return int(matches[-1])
        except ValueError:
            pass
    return None


def _is_run_complete(state: dict) -> bool:
    """
    Determine if the current run has ended, using /state response.

    The /state endpoint does NOT expose a 'run.status' field in the
    same way action responses do. The reliable signal is:
      - entity field missing → no active run (complete / never started)
      - entity.COMPLETE_CID == true → run finished (victory or death)

    NOTE: actionToken can legitimately be 0 for an active stuck run,
    so we never use actionToken as a proxy for completeness.
    """
    entity = state.get("entity") or state.get("data", {}).get("entity")
    if entity is None:
        return True  # No active run
    return bool(entity.get("COMPLETE_CID", False))


def _has_active_run(state: dict) -> bool:
    """True if there is an active (not yet complete) run in /state response."""
    entity = state.get("entity") or state.get("data", {}).get("entity")
    if entity is None:
        return False
    return not bool(entity.get("COMPLETE_CID", False))


def _get_pending_action(state: dict) -> str | None:
    """
    Determine what action is needed next from /state data.

    Priority:
      1. 'pendingAction' field (appears in some action responses)
      2. run.lootPhase: True → 'loot', False → 'combat'
    Returns None if we cannot determine (triggers resync).
    """
    # Some action responses include pendingAction directly
    pa = state.get("pendingAction") or state.get("data", {}).get("pendingAction")
    if pa is not None:
        if isinstance(pa, dict):
            return pa.get("type")
        return str(pa)

    # /state response: use run.lootPhase
    run = state.get("run") or state.get("data", {}).get("run")
    if run is not None:
        return "loot" if run.get("lootPhase") else "combat"

    return None


async def run_dungeon(
    wallet_address: str,
    jwt: str,
    dungeon_id: int = DUNGEON_DUNGETRON,
    is_juiced: bool = False,
) -> dict:
    """
    Execute one full dungeon run for the given player.

    Antifragile design:
      1. Energy pre-flight: skip if insufficient (accounts for 3x juiced cost).
      2. State check: if an active (incomplete) run exists, resume it instead
         of trying to start a new one and getting "already in dungeon" errors.
      3. Game loop: uses /state as canonical truth after every action.
         On HTTP errors or unknown pending actions, resyncs iteratively.
         Handles "Invalid action token" errors by extracting the correct token
         from the error message.
      4. Termination: detected via entity.COMPLETE_CID == true from /state.

    Args:
        dungeon_id:  1 = Dungetron 5000, 2 = Underhaul
        is_juiced:   True = 3× energy cost + boosted rewards (requires juice)

    Returns a summary dict: {moves, result, wallet, dungeon_id, is_juiced}.
    Raises on non-recoverable errors (auth failure, persistent API errors).
    """
    logger.info(
        "[gigaverse] Starting run for %s  dungeon=%d  juiced=%s",
        wallet_address[:10], dungeon_id, is_juiced,
    )

    # ── 1. Energy pre-flight ────────────────────────────────────────────────
    energy = await check_energy(wallet_address, jwt)
    required_energy = ENERGY_COST * 3 if is_juiced else ENERGY_COST
    if energy < required_energy:
        logger.info(
            "[gigaverse] Not enough energy (%d/%d) for %s — skipping run",
            energy, required_energy, wallet_address[:10],
        )
        return {
            "moves": 0, "result": "skipped_low_energy",
            "wallet": wallet_address[:10], "dungeon_id": dungeon_id, "is_juiced": is_juiced,
        }

    async with httpx.AsyncClient(timeout=20) as client:

        # ── 2. Check for an existing active run ─────────────────────────────
        try:
            current_state = await _resync_state(client, jwt)
        except httpx.HTTPStatusError as exc:
            logger.error(
                "[gigaverse] Failed to fetch state before run: HTTP %d %s",
                exc.response.status_code, exc.response.text[:200],
            )
            raise

        resuming = False

        if _has_active_run(current_state):
            # There is an incomplete run — figure out the token and resume
            action_token = _extract_token(current_state)
            entity = current_state.get("entity", {})
            room_num = entity.get("ROOM_NUM_CID", "?")
            logger.info(
                "[gigaverse] Resuming active run for %s at room=%s, state_token=%s",
                wallet_address[:10], room_num, action_token,
            )
            # If the state token is 0 or potentially stale, probe it
            if not action_token:
                logger.info("[gigaverse] state_token=0 — probing for real token")
                try:
                    await _dungeon_action(client, jwt, {
                        "action": "rock",
                        "dungeonId": dungeon_id,
                        "actionToken": 0,
                        "data": {},
                    })
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code == 400:
                        try:
                            error_data = exc.response.json()
                            new_tok = _extract_token_from_error(error_data)
                            if new_tok:
                                action_token = new_tok
                                logger.info("[gigaverse] Got real token from 400: %d", action_token)
                        except Exception:
                            pass
            resuming = True
        else:
            # No active run — start fresh
            logger.info("[gigaverse] Starting fresh run for %s", wallet_address[:10])
            try:
                await _dungeon_action(client, jwt, {
                    "action": "start_run",
                    "dungeonId": dungeon_id,
                    "actionToken": 0,
                    "data": {"consumables": [], "isJuiced": is_juiced, "index": 0},
                })
            except httpx.HTTPStatusError as exc:
                body = exc.response.text[:300]
                # If "already in dungeon" — resync and resume
                if exc.response.status_code in (400, 409):
                    current_state = await _resync_state(client, jwt)
                    if _has_active_run(current_state):
                        resuming = True
                        action_token = _extract_token(current_state)
                    else:
                        raise RuntimeError(f"Cannot start or resume run: {body}") from exc
                else:
                    raise

            if not resuming:
                # Resync after start_run to get canonical state
                current_state = await _resync_state(client, jwt)
                action_token = _extract_token(current_state)
                if not action_token:
                    raise RuntimeError("Could not obtain actionToken after start_run")

        # ── 3. Game loop ────────────────────────────────────────────────────
        move_count = 0
        final_status = "unknown"
        move_idx = 0            # round-robin index into _COMBAT_MOVES
        consecutive_resyncs = 0

        for _ in range(MAX_MOVES):

            # ── Terminal check ─────────────────────────────────────────────
            if _is_run_complete(current_state):
                run_obj = current_state.get("run") or current_state.get("data", {}).get("run", {}) or {}
                hp = None
                try:
                    for p in (run_obj.get("players") or []):
                        if isinstance(p, dict) and wallet_address.lower() in str(p.get("address", "")).lower():
                            hp = p.get("health", {}).get("current")
                except Exception:
                    pass
                final_status = "dead" if (hp is not None and hp <= 0) else "completed"
                logger.info(
                    "[gigaverse] Run ended: %s after %d moves",
                    final_status, move_count,
                )
                break

            pending = _get_pending_action(current_state)
            logger.debug(
                "[gigaverse] iter=%d pending=%s token=%s",
                move_count, pending, action_token,
            )

            # ── Build action payload ───────────────────────────────────────
            if pending in ("combat", None):
                action = _COMBAT_MOVES[move_idx % len(_COMBAT_MOVES)]
                move_idx += 1
                payload = {
                    "action": action,
                    "dungeonId": dungeon_id,
                    "actionToken": action_token,
                    "data": {},
                }
            elif pending == "loot":
                payload = {
                    "action": "loot_one",
                    "dungeonId": dungeon_id,
                    "actionToken": action_token,
                    "data": {},
                }
            elif pending == "heal_or_damage":
                payload = {
                    "action": "heal_or_damage",
                    "dungeonId": dungeon_id,
                    "actionToken": action_token,
                    "data": {},
                }
            else:
                logger.warning("[gigaverse] Unknown pending=%s — resyncing", pending)
                consecutive_resyncs += 1
                if consecutive_resyncs >= MAX_CONSECUTIVE_RESYNCS:
                    break
                await asyncio.sleep(2)
                current_state = await _resync_state(client, jwt)
                continue

            # ── Submit the action ──────────────────────────────────────────
            try:
                await _dungeon_action(client, jwt, payload)
                consecutive_resyncs = 0
            except httpx.HTTPStatusError as exc:
                logger.warning(
                    "[gigaverse] Action '%s' failed (HTTP %d): %s",
                    payload["action"], exc.response.status_code, exc.response.text[:150],
                )
                consecutive_resyncs += 1
                if consecutive_resyncs >= MAX_CONSECUTIVE_RESYNCS:
                    break

                # 400 errors often mean invalid token. Try to recover token from body.
                if exc.response.status_code == 400:
                    try:
                        new_tok = _extract_token_from_error(exc.response.json())
                        if new_tok:
                            action_token = new_tok
                            logger.info("[gigaverse] Recovered token from 400: %d", action_token)
                    except Exception:
                        pass
                
                # Always resync state after an error
                await asyncio.sleep(2)
                try:
                    current_state = await _resync_state(client, jwt)
                    # If we didn't get a token from error body, try from state
                    if not action_token or action_token == payload["actionToken"]:
                        st_tok = _extract_token(current_state)
                        if st_tok:
                            action_token = st_tok
                except Exception:
                    pass
                continue

            move_count += 1
            await asyncio.sleep(random.uniform(1.2, 3.0))

            # ── Post-action resync ────────────────────────────────────────
            try:
                current_state = await _resync_state(client, jwt)
                new_tok = _extract_token(current_state)
                if new_tok:
                    action_token = new_tok
            except Exception:
                pass

        else:
            final_status = "max_moves_reached"

    result = {
        "moves": move_count, "result": final_status,
        "wallet": wallet_address[:10], "dungeon_id": dungeon_id, "is_juiced": is_juiced,
    }
    logger.info("[gigaverse] Final result: %s", result)
    return result
