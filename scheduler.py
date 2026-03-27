import asyncio
import logging
import random
from datetime import datetime, timezone, date

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from config import settings
from db import AsyncSessionLocal, User, VoteLog
from queue_logic import current_epoch, get_app_id_for_today, WEEK_SIZE
from voter import send_vote

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler(timezone=settings.scheduler_timezone)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _vote_target_second(user: User, for_date: date) -> int:
    """
    Целевая секунда суток (UTC) для голосования.
    Jitter детерминирован по дате — стабилен в течение дня, меняется каждый день.
    """
    day_seed = int(user.wallet_address, 16) ^ (for_date.toordinal() * 0xCAFE)
    rng = random.Random(day_seed)
    jitter = rng.randint(-1800, 1800)  # ±30 минут
    return (user.base_vote_second + jitter) % 86400


def _is_vote_time(user: User, now_second: int, now_date: date) -> bool:
    """
    Сравниваем текущую МИНУТУ суток (0..1439) с целевой минутой.

    FIX (критический): предыдущая реализация с окном diff < 60 срабатывала
    на ДВА соседних тика — target=3630: тик на 3600 даёт diff=30, тик на
    3660 тоже даёт diff=30. Это создавало дублирующие таски с nonce collision.

    Сравнение по минутам гарантирует ровно один тик на одно голосование.
    Перенос через полночь решается автоматически.
    """
    target = _vote_target_second(user, now_date)
    return (target // 60) == (now_second // 60)


def _already_voted_today(user: User, now_date: date) -> bool:
    """
    FIX: принимаем now_date явно — нет расхождения между вызовами в одном тике.
    """
    if user.last_voted_at is None:
        return False
    voted_date = user.last_voted_at.astimezone(timezone.utc).date()
    return voted_date == now_date


# ---------------------------------------------------------------------------
# Per-user vote with retry
# ---------------------------------------------------------------------------

async def _attempt_vote(user_id: int) -> bool:
    """Одна попытка проголосовать. Возвращает True при успехе."""
    async with AsyncSessionLocal() as db:
        user = await db.get(User, user_id)
        if not user or not user.is_active:
            return False

        epoch = current_epoch()

        # Смена эпохи
        if user.current_epoch != epoch:
            logger.info("New epoch %d for agw=%s", epoch, user.wallet_address[:10])
            user.current_epoch = epoch
            user.week_app_index = 0

        if user.week_app_index >= WEEK_SIZE:
            return False

        # Перепроверяем — параллельная таска могла уже проголосовать
        today = datetime.now(timezone.utc).date()
        if _already_voted_today(user, today):
            return False

        app_id = get_app_id_for_today(user.wallet_address, epoch, user.week_app_index)

        vote_log = VoteLog(
            user_id=user.id,
            app_id=app_id,
            epoch=epoch,
            voted_at=datetime.now(timezone.utc),
        )

        try:
            tx_hash = await send_vote(
                wallet_address=user.wallet_address,
                session_key_enc=user.session_key_enc,
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
                "✓ Voted: agw=%s app_id=%d epoch=%d tx=%s",
                user.wallet_address[:10], app_id, epoch, tx_hash[:22],
            )
            db.add(vote_log)
            db.add(user)
            await db.commit()
            return True

        except Exception as e:
            vote_log.status    = "fail"
            vote_log.error_msg = str(e)[:500]
            logger.error(
                "✗ Vote failed: agw=%s app_id=%d error=%s",
                user.wallet_address[:10], app_id, e,
            )
            db.add(vote_log)
            db.add(user)
            await db.commit()
            return False


async def _vote_for_user(user_id: int):
    """
    Полный цикл для одного пользователя с retry.

    FIX (аудит п.4): sleep живёт ЗДЕСЬ, не в _tick.
    FIX (аудит п.5): до 3 попыток, повтор через 15-30 минут.
    """
    # Первая попытка — человеческая задержка 15-45 сек
    await asyncio.sleep(random.randint(15, 45))

    for attempt in range(3):
        success = await _attempt_vote(user_id)
        if success:
            return
        if attempt < 2:
            retry_delay = random.randint(900, 1800)  # 15-30 мин
            logger.info("Retry %d/2 for user_id=%d in %ds", attempt + 1, user_id, retry_delay)
            await asyncio.sleep(retry_delay)


# ---------------------------------------------------------------------------
# Main tick — каждую минуту
# ---------------------------------------------------------------------------

async def _tick():
    """
    FIX (аудит п.4): _tick НЕ спит. Просто запускает таски и уходит.
    max_instances=1 нужен только чтобы не накапливать тики при зависшем RPC.
    """
    now_utc    = datetime.now(timezone.utc)
    now_second = now_utc.hour * 3600 + now_utc.minute * 60 + now_utc.second
    now_date   = now_utc.date()

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.is_active == True))
        users = result.scalars().all()

    due = [
        u for u in users
        if _is_vote_time(u, now_second, now_date)
        and not _already_voted_today(u, now_date)
    ]

    if not due:
        return

    logger.info("Tick: %d users due to vote", len(due))
    random.shuffle(due)

    # Запускаем без блокировки — каждая таска спит сама
    for user in due:
        asyncio.create_task(_vote_for_user(user.id))


# ---------------------------------------------------------------------------
# Start / Stop
# ---------------------------------------------------------------------------

def start_scheduler():
    scheduler.add_job(
        _tick,
        trigger="cron",
        second=0,
        id="vote_tick",
        replace_existing=True,
        max_instances=1,
    )
    scheduler.start()
    logger.info("Scheduler started")


def stop_scheduler():
    scheduler.shutdown(wait=False)
    logger.info("Scheduler stopped")
