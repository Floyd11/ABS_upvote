import asyncio
import logging
import random
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from config import settings
from db import AsyncSessionLocal, User, VoteLog
from crypto import decrypt_raw
import gigaverse as gv
from queue_logic import (
    current_epoch, get_app_id_for_today, WEEK_SIZE,
    current_vote_day_id, current_vote_week_id, VOTE_RESET_HOUR,
)
from voter import send_vote

logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler(timezone=settings.scheduler_timezone)


def _vote_target_minute(user: User) -> int:
    """
    Целевая минута суток (0..1439) для голосования этого пользователя.
    Jitter детерминирован по vote_day_id — меняется каждый день-окно, не по полуночи.
    """
    day_seed = int(user.wallet_address, 16) ^ (current_vote_day_id() * 0xCAFE)
    rng = random.Random(day_seed)
    jitter_minutes = rng.randint(-30, 30)
    base_minute = (user.base_vote_second // 60) % 1440
    return (base_minute + jitter_minutes) % 1440


def _already_voted_this_window(user: User) -> bool:
    """
    Проверяем голосовал ли пользователь в текущем окне 15:00-15:00 UTC.
    Заменяет старый _already_voted_today который проверял по календарной дате.
    """
    if user.last_voted_at is None:
        return False
    from datetime import timedelta
    voted_at = user.last_voted_at.astimezone(timezone.utc)
    if voted_at.hour < VOTE_RESET_HOUR:
        voted_day = (voted_at - timedelta(hours=VOTE_RESET_HOUR)).date().toordinal()
    else:
        voted_day = voted_at.date().toordinal()
    return voted_day == current_vote_day_id()


async def _attempt_vote(user_id: int, app_id_override: int | None = None) -> bool:
    async with AsyncSessionLocal() as db:
        user = await db.get(User, user_id)
        if not user or not user.is_active:
            return False

        vote_week = current_vote_week_id()
        if user.current_epoch != vote_week:
            logger.info(
                "New vote week %d for agw=%s (was %d)",
                vote_week, user.wallet_address[:10], user.current_epoch,
            )
            user.current_epoch = vote_week
            user.week_app_index = 0

        if user.week_app_index >= WEEK_SIZE:
            logger.debug("Week complete: agw=%s", user.wallet_address[:10])
            return False

        if _already_voted_this_window(user):
            return False

        contract_epoch = current_epoch()
        if app_id_override is not None:
            app_id = app_id_override
        else:
            app_id = get_app_id_for_today(user.wallet_address, contract_epoch, user.week_app_index)

        vote_log = VoteLog(
            user_id=user.id,
            app_id=app_id,
            epoch=contract_epoch,
            voted_at=datetime.now(timezone.utc),
        )

        try:
            tx_hash = await send_vote(
                wallet_address=user.wallet_address,
                session_key_enc=user.session_key_enc,
                session_config=user.session_config_json,
                app_id=app_id,
                voting_contract=user.voting_contract,
            )
            vote_log.tx_hash = tx_hash
            vote_log.status  = "ok"
            user.last_voted_at  = datetime.now(timezone.utc)
            user.week_app_index += 1
            user.total_votes    += 1
            user.streak_days    += 1
            logger.info(
                "✓ Voted: agw=%s app_id=%d week=%d tx=%s",
                user.wallet_address[:10], app_id, vote_week, tx_hash[:22],
            )
            db.add(vote_log)
            db.add(user)
            await db.commit()
            return True

        except Exception as e:
            vote_log.status    = "fail"
            vote_log.error_msg = str(e)[:500]
            logger.error("✗ Vote failed: agw=%s app_id=%d error=%s",
                         user.wallet_address[:10], app_id, e)
            db.add(vote_log)
            db.add(user)
            await db.commit()
            return False


async def _vote_for_user(user_id: int, is_catchup: bool = False):
    """
    Ежедневный цикл голосования с умным retry.
    При неудаче переходим к СЛЕДУЮЩЕМУ appId из очереди — не повторяем тот же.
    Максимум 3 попытки с разными appId за один день.
    Логика ежедневного запуска из _tick НЕ меняется.
    """
    initial_delay = random.randint(5, 15) if is_catchup else random.randint(15, 45)
    await asyncio.sleep(initial_delay)

    for attempt in range(3):
        # Читаем свежее состояние пользователя перед каждой попыткой
        async with AsyncSessionLocal() as db:
            user = await db.get(User, user_id)
            if not user or not user.is_active:
                return

            vote_week = current_vote_week_id()
            contract_epoch = current_epoch()

            # Смена недели — сбрасываем индекс
            if user.current_epoch != vote_week:
                user.current_epoch = vote_week
                user.week_app_index = 0
                db.add(user)
                await db.commit()

            # Все appId этой недели исчерпаны
            if user.week_app_index >= WEEK_SIZE:
                logger.debug("Week complete: agw=%s", user.wallet_address[:10])
                return

            # Уже проголосовали сегодня
            if _already_voted_this_window(user):
                logger.debug("Already voted this window: agw=%s", user.wallet_address[:10])
                return

            # Берём appId для текущего индекса
            app_id = get_app_id_for_today(
                user.wallet_address, contract_epoch, user.week_app_index
            )
            current_index = user.week_app_index

        logger.info(
            "Attempt %d/3: agw=%s app_id=%d index=%d",
            attempt + 1, user.wallet_address[:10], app_id, current_index,
        )

        success = await _attempt_vote(user_id, app_id_override=app_id)

        if success:
            return

        # Попытка не удалась — пропускаем этот appId, берём следующий завтра
        async with AsyncSessionLocal() as db:
            user = await db.get(User, user_id)
            if user and user.week_app_index < WEEK_SIZE:
                user.week_app_index += 1
                db.add(user)
                await db.commit()
                logger.warning(
                    "Skipped app_id=%d (attempt %d/3 failed), "
                    "next index=%d: agw=%s",
                    app_id, attempt + 1,
                    user.week_app_index, user.wallet_address[:10],
                )

        # Если все 3 попытки исчерпаны
        if attempt == 2:
            logger.error(
                "All 3 attempts failed today for agw=%s, "
                "no vote recorded this window",
                user.wallet_address[:10],
            )
            return

        # Пауза перед следующей попыткой с другим appId
        retry_delay = random.randint(30, 90)
        logger.info(
            "Trying next app_id in %ds (attempt %d/3 next)",
            retry_delay, attempt + 2,
        )
        await asyncio.sleep(retry_delay)


async def _tick():
    now_utc    = datetime.now(timezone.utc)
    now_minute = now_utc.hour * 60 + now_utc.minute  # 0..1439

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.is_active == True))
        users = result.scalars().all()

    # Основной список: пришло запланированное время голосования
    due = [
        u for u in users
        if (_vote_target_minute(u) == now_minute)
        and not _already_voted_this_window(u)
    ]

    # Catch-up: пользователь зарегистрировался менее 60 минут назад и ещё не голосовал.
    # Запускаем каждый тик — _attempt_vote сам не даст дублей через _already_voted_this_window.
    catchup_ids = set(u.id for u in due)
    catchup = []
    for u in users:
        if u.id in catchup_ids:
            continue
        if _already_voted_this_window(u):
            continue
        if u.created_at is None:
            continue
        created_ago_minutes = (now_utc - u.created_at.astimezone(timezone.utc)).total_seconds() / 60
        if 0 <= created_ago_minutes <= 60:
            catchup.append(u)

    if due:
        logger.info("Tick: %d users due to vote (scheduled)", len(due))
    if catchup:
        logger.info("Tick: %d users catch-up (registered today)", len(catchup))

    random.shuffle(due)
    for user in due:
        asyncio.create_task(_vote_for_user(user.id, is_catchup=False))

    random.shuffle(catchup)
    for user in catchup:
        asyncio.create_task(_vote_for_user(user.id, is_catchup=True))


def start_scheduler():
    scheduler.add_job(
        _tick,
        trigger="cron",
        second=0,
        id="vote_tick",
        replace_existing=True,
        max_instances=1,
    )
    scheduler.add_job(
        _gigaverse_tick,
        trigger="cron",
        second=30,  # offset from vote_tick to spread DB load
        id="gigaverse_tick",
        replace_existing=True,
        max_instances=1,
    )
    scheduler.start()
    logger.info("Scheduler started")


def stop_scheduler():
    scheduler.shutdown(wait=False)
    logger.info("Scheduler stopped")


# ---------------------------------------------------------------------------
# Gigaverse scheduler tasks
# ---------------------------------------------------------------------------

async def _run_gigaverse(user_id: int) -> None:
    """
    Execute one Gigaverse dungeon run for the given user.
    Updates gigaverse_last_run on success; logs and skips on failure.
    """
    async with AsyncSessionLocal() as db:
        user = await db.get(User, user_id)
        if not user or not user.gigaverse_jwt_enc:
            return
        jwt = decrypt_raw(user.gigaverse_jwt_enc)
        wallet = user.wallet_address

    try:
        result = await gv.run_dungeon(wallet, jwt)
        status = result.get("result", "unknown")

        # Only advance last_run if the run actually executed (not skipped due to energy)
        if status != "skipped_low_energy":
            async with AsyncSessionLocal() as db:
                user = await db.get(User, user_id)
                if user:
                    user.gigaverse_last_run = datetime.now(timezone.utc)
                    db.add(user)
                    await db.commit()
            logger.info(
                "[gigaverse] Run complete for %s: moves=%d result=%s",
                wallet[:10], result.get("moves", 0), status,
            )
        else:
            logger.info("[gigaverse] Skipped run for %s (low energy)", wallet[:10])

    except Exception as exc:
        # Do NOT update last_run — let the scheduler retry next cycle
        logger.error(
            "[gigaverse] Run failed for %s: %s",
            wallet[:10], exc,
        )


async def _gigaverse_tick() -> None:
    """
    Called every minute at :30 seconds.
    For each user with a Gigaverse JWT, checks if a new run is due.

    Run interval = 2 hours + deterministic per-user jitter (5–45 min).
    The jitter seed changes daily so timing shifts every day — mimics human behaviour.
    """
    now = datetime.now(timezone.utc)
    today_ordinal = now.toordinal()

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(User).where(User.gigaverse_jwt_enc.isnot(None))
        )
        users = result.scalars().all()

    for user in users:
        # Per-user deterministic daily jitter (5–45 minutes)
        jitter_min = random.Random(
            int(user.wallet_address, 16) ^ today_ordinal
        ).randint(5, 45)
        interval_seconds = (2 * 3600) + (jitter_min * 60)

        if user.gigaverse_last_run is None:
            # First ever run — add a small random initial delay (10–60 s)
            # handled by asyncio.sleep inside _run_gigaverse; trigger immediately
            asyncio.create_task(_run_gigaverse(user.id))
            logger.info(
                "[gigaverse] Scheduling first run for %s", user.wallet_address[:10]
            )
        else:
            since_last = (
                now - user.gigaverse_last_run.astimezone(timezone.utc)
            ).total_seconds()
            if since_last >= interval_seconds:
                asyncio.create_task(_run_gigaverse(user.id))
                logger.info(
                    "[gigaverse] Scheduling run for %s (since_last=%.0fs interval=%ds)",
                    user.wallet_address[:10], since_last, interval_seconds,
                )
