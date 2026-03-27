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
    Генерируем список из WEEK_SIZE уникальных appId для конкретного кошелька
    на конкретную эпоху.

    Ключевые свойства:
    1. Детерминированный для той же пары (wallet, epoch) — если бэкенд
       перезапустится, очередь пересоздастся идентично.
    2. Разные кошельки получают разный порядок — защита от паттерна.
    3. Пул достаточно большой чтобы выбрать 7 уникальных appId.
    """
    if len(APP_POOL) < WEEK_SIZE:
        raise ValueError(f"APP_POOL содержит меньше {WEEK_SIZE} элементов")

    # Seed = кошелёк + эпоха → каждый кошелёк получает свой уникальный порядок
    # Строковый seed надёжнее XOR: учитывает все 160 бит адреса, не только младшие 32
    seed_str = f"{wallet_address.lower()}_{epoch}"
    rng = random.Random(seed_str)

    queue = rng.sample(APP_POOL, WEEK_SIZE)
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
