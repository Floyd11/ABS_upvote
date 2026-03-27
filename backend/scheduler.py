import asyncio
import logging
import random
from datetime import datetime, timezone, date, timedelta

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


def _already_voted_today(user: User, now_utc: datetime) -> bool:
    """
    Проверяем, было ли уже голосование в текущем 'логическом дне' (15:00 - 15:00 UTC).
    """
    if user.last_voted_at is None:
        return False
    
    # Сдвигаем время на 15 часов назад: 15:00 UTC становится 00:00 'логического дня'
    logic_now_date = (now_utc - timedelta(hours=15)).date()
    voted_at_utc = user.last_voted_at.astimezone(timezone.utc)
    logic_voted_date = (voted_at_utc - timedelta(hours=15)).date()
    
    return logic_voted_date == logic_now_date


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

        queue = generate_week_queue(user.wallet_address, epoch)

        while user.week_app_index < len(queue):
            app_id = queue[user.week_app_index]
            
            # Лог для отладки
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
                    "✓ Successful vote for today: agw=%s app_id=%d tx=%s",
                    user.wallet_address[:10], app_id, tx_hash[:22],
                )
                db.add(vote_log)
                db.add(user)
                await db.commit()
                return True # УСПЕХ: Мы проголосовали один раз сегодня, выходим.

            except Exception as e:
                err_str = str(e).lower()
                # Если транзакция отклонена блокчейном
                if "revert" in err_str:
                    logger.warning(
                        "! App %d REVERTED for agw=%s. Skipping to next in queue.",
                        app_id, user.wallet_address[:10]
                    )
                    vote_log.status = "skip"
                    vote_log.error_msg = f"Reverted/Already voted: {err_str[:100]}"
                    db.add(vote_log)
                    user.week_app_index += 1
                    db.add(user)
                    await db.commit()
                    # ПРОДОЛЖАЕМ цикл: ищем следующее приложение, пока не наступит успех
                    continue
                else:
                    # Другая ошибка (RPC, сеть и т.д.) 
                    # Не увеличиваем индекс, просто записываем фейл и выходим на глобальный retry
                    vote_log.status    = "fail"
                    vote_log.error_msg = str(e)[:500]
                    db.add(vote_log)
                    await db.commit()
                    raise e
        
        logger.error("!!! No more apps in pool to vote for agw=%s in epoch %d", user.wallet_address[:10], epoch)
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

def _is_due_to_vote(u: User, now_utc: datetime) -> bool:
    """Проверяет, пришло ли время голосовать для юзера."""
    # Сдвигаем время на 15 часов чтобы проверить "уже голосовали ли мы в этом логическом цикле"
    if _already_voted_today(u, now_utc):
        return False
    
    now_second = now_utc.hour * 3600 + now_utc.minute * 60 + now_utc.second
    return _is_vote_time(u, now_second, now_utc.date())


async def _tick():
    """
    FIX (аудит п.4): _tick НЕ спит. Просто запускает таски и уходит.
    """
    now_utc = datetime.now(timezone.utc)

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.is_active == True))
        users = result.scalars().all()

    due = [
        u for u in users
        if _is_due_to_vote(u, now_utc)
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
