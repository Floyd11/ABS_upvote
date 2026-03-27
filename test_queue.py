"""
Запусти перед деплоем чтобы убедиться что логика очередей работает правильно:
  python test_queue.py
"""
from queue_logic import generate_week_queue, current_epoch, APP_POOL, WEEK_SIZE

# Тестовые кошельки
wallets = [
    "0xa1091c706cb94dddca7942ab3098b5ccad588395",
    "0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
    "0x1234567890abcdef1234567890abcdef12345678",
]

epoch = current_epoch()
print(f"Current epoch: {epoch}")
print(f"App pool size: {len(APP_POOL)} (need >= {WEEK_SIZE})")
print()

for wallet in wallets:
    queue = generate_week_queue(wallet, epoch)
    print(f"Wallet {wallet[:10]}...  queue={queue}")
    assert len(queue) == WEEK_SIZE, "Queue length mismatch"
    assert len(set(queue)) == WEEK_SIZE, "Duplicate appIds in queue!"

print()

# Убеждаемся что разные кошельки получают разный порядок
q1 = generate_week_queue(wallets[0], epoch)
q2 = generate_week_queue(wallets[1], epoch)
assert q1 != q2, "Different wallets got same queue order!"
print("✓ Different wallets get different order")

# Убеждаемся что один кошелёк получает одинаковую очередь при повторном вызове
q1a = generate_week_queue(wallets[0], epoch)
assert q1 == q1a, "Queue is not deterministic!"
print("✓ Queue is deterministic (restart-safe)")

# Убеждаемся что в следующей эпохе очередь другая
q1_next = generate_week_queue(wallets[0], epoch + 1)
assert q1 != q1_next, "Different epochs got same queue!"
print("✓ Different epochs get different queues")

print()
print("All checks passed ✓")
