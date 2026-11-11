"""构造已知周期消耗，避免把旧120张余额误当成1000张周期的最后一张。"""
import time


def seed_known_local_balance(service, token, remaining):
    account = service._accounts[token]
    ledger = service._image_ledger
    ref = account['management_id']
    now = time.time()
    ledger.observe(ref, cap=1000, remaining=120, reset_at=now - 1, now=now - 10)
    ledger.enable_local_budget(ref)
    ledger.observe(ref, cap=1000, remaining=120, reset_at=now + 3600, now=now)
    for index in range(1000 - remaining):
        generation = f'history-{ref}-{index}'
        assert ledger.reserve(ref, generation)
        ledger.submit(generation)
        ledger.finish(generation, success=True)
    service.get_account(token)
