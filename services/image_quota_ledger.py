"""图片周期额度与生成租约。只保存稳定引用和计数，绝不保存凭据或请求正文。"""
from __future__ import annotations

import sqlite3
import os
import fcntl
import threading
import time
from contextlib import contextmanager
from pathlib import Path


class ImageQuotaLedger:
    def __init__(self, path: str | Path = ':memory:'):
        self._lock = threading.RLock()
        self._db = None
        self._owner_fd = None
        if str(path) != ':memory:':
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(str(path)+'.lock', os.O_CREAT | os.O_RDWR, 0o600)
            try:
                # 选号并发槽在应用内存中；禁止第二个进程恢复仍由活进程持有的租约。
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(descriptor)
                raise RuntimeError('image quota ledger already has an owner process') from None
            self._owner_fd = descriptor
        self._db = sqlite3.connect(str(path), timeout=5, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute('PRAGMA busy_timeout=5000')
        self._db.execute('PRAGMA journal_mode=WAL')
        self._db.executescript('''
            CREATE TABLE IF NOT EXISTS image_budgets (
                account TEXT PRIMARY KEY, cycle INTEGER NOT NULL DEFAULT 1,
                cap INTEGER, used INTEGER, balance INTEGER NOT NULL,
                upstream INTEGER NOT NULL, reset_at REAL, observed_at REAL NOT NULL, initialized INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS image_leases (
                id TEXT PRIMARY KEY, account TEXT NOT NULL, cycle INTEGER NOT NULL,
                state TEXT NOT NULL, conversation TEXT, created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS image_lease_account ON image_leases(account,cycle,state);
            CREATE INDEX IF NOT EXISTS image_lease_conversation ON image_leases(account,conversation);
        ''')
        # 旧账本只保存了120张快照余额；策略标记防止重启或刷新重复扩充额度。
        columns = {row['name'] for row in self._db.execute('PRAGMA table_info(image_budgets)')}
        if 'local_budget' not in columns:
            with self._db:
                self._db.execute('ALTER TABLE image_budgets ADD COLUMN local_budget INTEGER NOT NULL DEFAULT 0')
        # 单应用进程恢复：未开始提交的预占可释放；开始提交的请求必须保留不确定消耗。
        with self._db:
            self._db.execute("UPDATE image_leases SET state='released' WHERE state='reserved'")
            self._db.execute("UPDATE image_leases SET state='pending' WHERE state='submitted'")
        if str(path) != ':memory:':
            Path(path).chmod(0o600)

    @contextmanager
    def _transaction(self):
        with self._lock:
            self._db.execute('BEGIN IMMEDIATE')
            try:
                yield
                self._db.commit()
            except BaseException:
                self._db.rollback()
                raise

    def close(self):
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None
            if self._owner_fd is not None:
                fcntl.flock(self._owner_fd, fcntl.LOCK_UN)
                os.close(self._owner_fd)
                self._owner_fd = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    @staticmethod
    def _identity(value):
        if not isinstance(value, str) or not 1 <= len(value) <= 160 or not value.isprintable():
            raise ValueError('invalid image ledger identity')
        return value

    def observe(self, account, *, cap, remaining, reset_at=None, now=None, authoritative=True):
        account = self._identity(account)
        if type(remaining) is not int or remaining < 0 or remaining > 10**9:
            raise ValueError('invalid remaining quota')
        if cap is not None and (type(cap) is not int or not 1 <= cap <= 10**9):
            raise ValueError('invalid quota cap')
        now = time.time() if now is None else float(now)
        reset_at = float(reset_at) if type(reset_at) in (int, float) and 0 < reset_at < 253402300800 else None
        with self._transaction():
            row = self._db.execute('SELECT * FROM image_budgets WHERE account=?', (account,)).fetchone()
            if row is None:
                # 历史只有剩余额度，不能把累计成功次数冒充本周期已用量。
                self._db.execute('INSERT INTO image_budgets(account,cap,used,balance,upstream,reset_at,observed_at,initialized) VALUES(?,?,NULL,?,?,?,?,?)',
                                 (account, cap, min(remaining, cap) if cap is not None else remaining, remaining, reset_at, now, int(authoritative)))
            elif authoritative and not row['initialized']:
                balance = cap if row['local_budget'] and cap is not None else min(remaining, cap) if cap is not None else remaining
                self._db.execute('UPDATE image_budgets SET cap=?,balance=?,upstream=?,reset_at=?,observed_at=?,initialized=1 WHERE account=?',
                                 (cap, balance, remaining, reset_at, now, account))
            elif not authoritative or now < row['observed_at'] or (reset_at is not None and row['reset_at'] is not None and reset_at < row['reset_at']):
                return self._view(account)
            elif row['reset_at'] is not None and now >= row['reset_at'] and reset_at is not None and reset_at > now and remaining > 0:
                # 旧周期确已结束，并收到新周期截止及正额度才允许补充周期预算。
                self._db.execute('UPDATE image_budgets SET cycle=cycle+1,cap=?,used=0,balance=?,upstream=?,reset_at=?,observed_at=? WHERE account=?',
                                 (cap, cap if cap is not None else remaining, remaining, reset_at, now, account))
            else:
                balance = min(row['balance'], max(0, cap - (row['used'] or 0))) if cap is not None else row['balance']
                active = self._db.execute("SELECT COUNT(*) FROM image_leases WHERE account=? AND cycle=? AND state IN ('reserved','submitted','pending')", (account, row['cycle'])).fetchone()[0]
                upstream = min(row['upstream'], remaining) if active and not (row['local_budget'] and cap is not None) else remaining
                if cap is None and not active:
                    balance = remaining
                # 上游可能每次刷新都返回“当前时间+24小时”。固定本周期首次确认的截止，
                # 避免旧pending让周期无限延后；新周期只由上面的到期且正额度分支建立。
                self._db.execute('UPDATE image_budgets SET cap=?,balance=?,upstream=?,reset_at=COALESCE(reset_at,?),observed_at=? WHERE account=?',
                                 (cap, balance, upstream, reset_at, now, account))
            return self._view(account)

    def enable_local_budget(self, account):
        """一次性把旧120张台账折算到1000，保留扣减；正数上游快照不再截断本地额度。"""
        self._identity(account)
        with self._transaction():
            row = self._db.execute('SELECT * FROM image_budgets WHERE account=?', (account,)).fetchone()
            if row is None or row['cap'] != 1000 or row['local_budget']:
                return self._view(account)
            successes = self._db.execute(
                "SELECT COUNT(*) FROM image_leases WHERE account=? AND cycle=? AND state='success'",
                (account, row['cycle']),
            ).fetchone()[0]
            # 120→119的旧扣减与成功租约可能重叠，取较大值，不能重复扣除。
            # 历史总成功数不是本周期已用，因此used未知时仍保留NULL。
            deducted = row['used'] if row['used'] is not None else max(0, 120 - row['balance'])
            balance = max(0, row['cap'] - max(deducted, successes)) if row['initialized'] else 0
            self._db.execute('UPDATE image_budgets SET balance=?,local_budget=1 WHERE account=?', (balance, account))
            return self._view(account)

    def consume_untracked(self, account):
        """兼容没有生成租约的旧调用方；正常生成使用finish的幂等结算。"""
        with self._transaction():
            self._db.execute('UPDATE image_budgets SET balance=MAX(0,balance-1),used=CASE WHEN used IS NULL THEN NULL ELSE used+1 END WHERE account=? AND local_budget=1 AND cap IS NOT NULL', (account,))

    def set_cap(self, account, cap):
        if cap is not None and (type(cap) is not int or not 1 <= cap <= 10**9):
            raise ValueError('invalid quota cap')
        with self._transaction():
            row = self._db.execute('SELECT * FROM image_budgets WHERE account=?', (account,)).fetchone()
            if row is not None:
                balance = min(row['balance'], max(0, cap-(row['used'] or 0))) if cap is not None else row['balance']
                self._db.execute('UPDATE image_budgets SET cap=?,balance=? WHERE account=?', (cap,balance,account))
            return self._view(account)

    def _view(self, account):
        row = self._db.execute('SELECT * FROM image_budgets WHERE account=?', (account,)).fetchone()
        if row is None:
            return None
        counts = dict(self._db.execute("SELECT state,COUNT(*) FROM image_leases WHERE account=? AND cycle=? AND state IN ('reserved','submitted','pending') GROUP BY state", (account, row['cycle'])))
        reserved = counts.get('reserved', 0)
        uncertain = counts.get('submitted', 0) + counts.get('pending', 0)
        local_budget = bool(row['local_budget'] and row['cap'] is not None)
        # 本地额度与上游限流分开：正数快照不封顶，明确零额度仍阻断。
        available = row['balance'] if local_budget and row['upstream'] > 0 else min(row['balance'], row['upstream'])
        return {'cap': row['cap'], 'used': row['used'], 'reserved': reserved, 'uncertain': uncertain,
                'available': max(0, available - reserved - uncertain), 'local_budget': bool(row['local_budget']),
                'reset_at': row['reset_at'], 'baseline': 'known' if row['used'] is not None else 'unknown', 'cycle': row['cycle'],
                'balance': row['balance'], 'upstream': row['upstream']}

    def view(self, account):
        with self._lock:
            return self._view(account)

    def reserve(self, account, generation, *, allow_unavailable=False):
        self._identity(account); self._identity(generation)
        with self._transaction():
            old = self._db.execute('SELECT * FROM image_leases WHERE id=?', (generation,)).fetchone()
            if old is not None and old['state'] != 'released':
                return False
            state = self._view(account)
            if not state or (state['available'] < 1 and not allow_unavailable):
                return False
            # 只有持有限流验证槽的调用方可绕过旧零快照；仍禁止与其他提交并行。
            if allow_unavailable and self._db.execute(
                    "SELECT 1 FROM image_leases WHERE account=? AND state IN ('reserved','submitted') LIMIT 1",
                    (account,)).fetchone():
                return False
            if old is not None:
                self._db.execute('DELETE FROM image_leases WHERE id=?', (generation,))
            self._db.execute("INSERT INTO image_leases(id,account,cycle,state,created_at) VALUES(?,?,?,'reserved',?)",
                             (generation, account, state['cycle'], time.time()))
            return True

    def submit(self, generation):
        with self._transaction():
            changed = self._db.execute("UPDATE image_leases SET state='submitted' WHERE id=? AND state='reserved'", (generation,)).rowcount
            if not changed:
                raise RuntimeError('image generation lease is not reserved')

    def bind_conversation(self, generation, conversation):
        self._identity(conversation)
        with self._transaction():
            self._db.execute('UPDATE image_leases SET conversation=? WHERE id=? AND (conversation IS NULL OR conversation=?)', (conversation, generation, conversation))

    def _finish(self, generation, *, success, not_consumed=False, recovery_balance=None, recover_throttle=False):
        lease = self._db.execute('SELECT * FROM image_leases WHERE id=?', (generation,)).fetchone()
        if lease is None or lease['state'] in ('success', 'released') or (lease['state'] == 'pending' and not success and not not_consumed):
            return False
        if success:
            budget = self._db.execute('SELECT * FROM image_budgets WHERE account=?', (lease['account'],)).fetchone()
            if recovery_balance is not None:
                if (type(recovery_balance) is not int or not 1 <= recovery_balance <= 10**9
                        or budget is None or budget['cap'] is not None and recovery_balance > budget['cap']):
                    raise ValueError('invalid verified recovery baseline')
                if lease['cycle'] == budget['cycle']:
                    # 额度冷却后的真实出图才确认新本地周期；原子地计入这一次成功。
                    # 旧pending保留在原周期，迟到结算不能再次扣减新周期。
                    cycle = budget['cycle'] + 1
                    self._db.execute('UPDATE image_budgets SET cycle=?,used=0,balance=?,upstream=?,reset_at=NULL,initialized=1 WHERE account=?',
                                     (cycle, recovery_balance, recovery_balance, lease['account']))
                    self._db.execute('UPDATE image_leases SET cycle=? WHERE id=?', (cycle, generation))
                    lease = {**dict(lease), 'cycle': cycle}
            elif recover_throttle:
                # 读取/请求429恢复只解除陈旧零门禁，绝不补回已用余额或重置周期。
                self._db.execute('UPDATE image_budgets SET upstream=MAX(upstream,balance) WHERE account=? AND cycle=?',
                                 (lease['account'], lease['cycle']))
            self._db.execute("UPDATE image_leases SET state='success' WHERE id=?", (generation,))
            self._db.execute('UPDATE image_budgets SET used=CASE WHEN used IS NULL THEN NULL ELSE used+1 END,balance=MAX(0,balance-1),upstream=CASE WHEN local_budget=1 AND cap IS NOT NULL THEN upstream ELSE MAX(0,upstream-1) END WHERE account=? AND cycle=?', (lease['account'], lease['cycle']))
        elif lease['state'] == 'reserved' or not_consumed:
            self._db.execute("UPDATE image_leases SET state='released' WHERE id=?", (generation,))
        else:
            self._db.execute("UPDATE image_leases SET state='pending' WHERE id=?", (generation,))
        return True

    def finish(self, generation, *, success, not_consumed=False, recovery_balance=None, recover_throttle=False):
        with self._transaction():
            return self._finish(generation, success=success, not_consumed=not_consumed,
                                recovery_balance=recovery_balance, recover_throttle=recover_throttle)

    def complete_conversation(self, account, conversation, *, recovery_balance=None, recover_throttle=False):
        with self._transaction():
            rows = self._db.execute("SELECT id FROM image_leases WHERE account=? AND conversation=? AND state IN ('submitted','pending')", (account, conversation)).fetchall()
            # 有歧义时不能把同会话多个请求全部结算；正常新生成使用独立会话。
            return len(rows) == 1 and self._finish(rows[0]['id'], success=True,
                recovery_balance=recovery_balance, recover_throttle=recover_throttle)
