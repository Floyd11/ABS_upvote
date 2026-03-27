import random
import time
from typing import List

from config import settings


# ---------------------------------------------------------------------------
# App pool
# ---------------------------------------------------------------------------
# Твои основные + 5 рандомных из реального списка Abstract Portal
# Порядок в пуле не важен — очередь каждый раз перемешивается

APP_POOL: List[int] = [
    # Основные (твои)
    220,   # appId из транзакций
    39,
    207,
    183,
    213,
    155,
    # Дополнительные (реальные appId из Abstract Portal)
    15,
    42,
    67,
    88,
    103,
]

WEEK_SIZE = 7  # голосов в одну эпоху


# ---------------------------------------------------------------------------
# Epoch helpers
# ---------------------------------------------------------------------------

def current_epoch() -> int:
    """
    Вычисляем текущую эпоху без RPC-запроса.
    Формула берётся из timestamp деплоя контракта.
    """
    elapsed = int(time.time()) - settings.epoch_zero_timestamp
    return max(0, elapsed // settings.epoch_duration_seconds)


def seconds_until_next_epoch() -> int:
    """Сколько секунд до смены эпохи."""
    elapsed = int(time.time()) - settings.epoch_zero_timestamp
    return settings.epoch_duration_seconds - (elapsed % settings.epoch_duration_seconds)


# ---------------------------------------------------------------------------
# Queue generation
# ---------------------------------------------------------------------------

def generate_week_queue(wallet_address: str, epoch: int) -> List[int]:
    """
    Генерируем перемешанный список ВСЕХ appId из пула для конкретного кошелька.
    Это дает нам запас для скипов, если за какие-то приложения уже голосовали.
    """
    # Seed = кошелёк + эпоха
    seed_str = f"{wallet_address.lower()}_{epoch}"
    rng = random.Random(seed_str)

    # Копируем весь пул и перемешиваем его
    queue = list(APP_POOL)
    rng.shuffle(queue)
    return queue


def get_app_id_for_today(wallet_address: str, epoch: int, week_app_index: int) -> int:
    """
    Возвращает appId для голосования сегодня.
    week_app_index — сколько дней уже проголосовали в текущей эпохе (0..6).
    """
    queue = generate_week_queue(wallet_address, epoch)
    if week_app_index >= WEEK_SIZE:
        # Эпоха должна была смениться, но на всякий случай
        return queue[WEEK_SIZE - 1]
    return queue[week_app_index]
