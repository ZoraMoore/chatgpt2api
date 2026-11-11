"""离线恢复指定Edu账号的停滞账本；保留全部租约、限流记录和账号文件。"""
from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import re
import sqlite3
import time
from pathlib import Path


def _read_json(path):
    path = Path(path)
    if not path.is_file() or path.stat().st_size > 50 * 1024 * 1024:
        raise ValueError('invalid recovery input file')
    return json.loads(path.read_text(encoding='utf-8'))


def recover(accounts_path, database_path, account_refs, *, backup_path, apply=False, now=None):
    if (not isinstance(account_refs, list) or not 1 <= len(account_refs) <= 1000
            or any(not isinstance(ref, str) or re.fullmatch(r'[a-f0-9]{64}', ref) is None for ref in account_refs)
            or len(set(account_refs)) != len(account_refs)):
        raise ValueError('invalid recovery account scope')
    now = time.time() if now is None else float(now)
    if not math.isfinite(now) or now <= 0:
        raise ValueError('invalid recovery time')
    database = Path(database_path).resolve(strict=True)
    backup = Path(backup_path).resolve()
    if database == backup:
        raise ValueError('backup cannot overwrite ledger')
    owner = os.open(str(database) + '.lock', os.O_CREAT | os.O_RDWR, 0o600)
    db = None
    try:
        try:
            fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('stop the ledger owner process before recovery') from None
        rows = _read_json(accounts_path)
        if not isinstance(rows, list) or len(rows) > 10000 or any(not isinstance(row, dict) for row in rows):
            raise ValueError('invalid accounts document')
        indexed = {}
        for row in rows:
            ref = row.get('management_id')
            if ref in account_refs:
                if ref in indexed:
                    raise ValueError('duplicate recovery account identity')
                indexed[ref] = row
        db = sqlite3.connect(database.as_uri() + '?mode=rw', uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        report = {'applied': bool(apply), 'scope_count': len(account_refs), 'eligible': 0,
                  'restored': 0, 'already_available': 0, 'skipped': [], 'entries': []}
        for ref in account_refs:
            account = indexed.get(ref)
            reason = ''
            if account is None:
                reason = 'account_missing'
            elif account.get('status') != '正常' or account.get('type') != 'edu_plus':
                reason = 'account_not_normal_edu'
            else:
                limit = account.get('image_limit')
                if limit and (not isinstance(limit, dict) or limit.get('state') != 'normal'):
                    reason = 'image_limit_protected'
                elif account.get('last_refresh_error'):
                    reason = 'profile_refresh_failed'
            if reason:
                report['skipped'].append({'account_ref': ref, 'reason': reason})
                continue
            budget = db.execute('SELECT * FROM image_budgets WHERE account=?', (ref,)).fetchone()
            snapshots = account.get('limits_progress')
            snapshot = next((item for item in snapshots if isinstance(item, dict)
                             and item.get('feature_name') == 'image_gen'), {}) if isinstance(snapshots, list) else {}
            remaining = snapshot.get('remaining')
            states = dict(db.execute('SELECT state,COUNT(*) FROM image_leases WHERE account=? AND cycle=? GROUP BY state',
                                     (ref, budget['cycle']))) if budget else {}
            pending = states.get('pending', 0)
            if budget is None or budget['cap'] is not None or budget['local_budget'] or not budget['initialized']:
                reason = 'unsupported_budget'
            elif not -60 <= now - budget['observed_at'] <= 900:
                reason = 'stale_snapshot'
            elif type(remaining) is not int or not 0 < remaining <= 10**9:
                reason = 'no_positive_upstream_snapshot'
            elif states.get('reserved', 0) or states.get('submitted', 0):
                reason = 'generation_inflight'
            elif remaining <= pending:
                reason = 'snapshot_cannot_cover_uncertain_usage'
            if reason:
                report['skipped'].append({'account_ref': ref, 'reason': reason})
                continue
            available = max(0, min(budget['balance'], budget['upstream']) - pending)
            if available > 0:
                report['already_available'] += 1
                continue
            report['entries'].append({'account_ref': ref, 'email': account.get('email'),
                                      'before': dict(budget), 'snapshot_remaining': remaining,
                                      'pending_retained': pending, 'available_before': available,
                                      'available_after': remaining - pending})
        report['eligible'] = len(report['entries'])
        if apply and report['entries']:
            # 先保留完整数据库；只在应用已停止且目标全部完成预检后写入。
            backup.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
            destination = sqlite3.connect(backup)
            try:
                db.backup(destination)
            finally:
                destination.close()
            db.execute('BEGIN IMMEDIATE')
            try:
                for entry in report['entries']:
                    before = entry['before']
                    changed = db.execute(
                        'UPDATE image_budgets SET balance=?,upstream=? '
                        'WHERE account=? AND cycle=? AND balance=? AND upstream=? AND observed_at=?',
                        (entry['snapshot_remaining'], entry['snapshot_remaining'], entry['account_ref'],
                         before['cycle'], before['balance'], before['upstream'], before['observed_at'])).rowcount
                    if changed != 1:
                        raise RuntimeError('ledger changed after recovery preflight')
                db.commit()
            except BaseException:
                db.rollback()
                raise
            report['restored'] = len(report['entries'])
        return report
    finally:
        if db is not None:
            db.close()
        os.close(owner)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--accounts', required=True)
    parser.add_argument('--database', required=True)
    parser.add_argument('--scope', required=True)
    parser.add_argument('--backup', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    scope = _read_json(args.scope)
    if not isinstance(scope, dict) or scope.get('expected_count') != len(scope.get('account_refs', [])):
        raise ValueError('invalid recovery scope document')
    report = recover(args.accounts, args.database, scope['account_refs'], backup_path=args.backup, apply=args.apply)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 2 if report['skipped'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
