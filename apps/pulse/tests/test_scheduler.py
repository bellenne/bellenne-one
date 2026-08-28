from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, time, timedelta
from types import SimpleNamespace

from app import scheduler as scheduler_module


class _FakeSession:
    def __init__(self, account):
        self.account = account
        self.commits = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def scalars(self, _statement):
        return [self.account.id]

    def get(self, _model, account_id):
        return self.account if account_id == self.account.id else None

    def commit(self):
        self.commits += 1


class _FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 8, 26, 7, 0, 0)
        return value.replace(tzinfo=tz) if tz is not None else value


def _app_for(account, session):
    state = SimpleNamespace(
        session_factory=lambda: session,
        settings=SimpleNamespace(timezone="Europe/Moscow"),
        credential_cipher=object(),
    )
    return SimpleNamespace(state=state)


def test_scheduler_collects_only_yesterday_and_marks_success(monkeypatch):
    account = SimpleNamespace(
        id=1,
        timezone="Europe/Moscow",
        sync_time=time(6, 15),
        last_scheduled_date=None,
        last_sync_status=None,
        last_sync_at=None,
    )
    session = _FakeSession(account)
    targets: list[date] = []

    async def fake_sync_account(_session, _account, target_date, _cipher, _settings):
        targets.append(target_date)
        return SimpleNamespace(status="success")

    monkeypatch.setattr(scheduler_module, "datetime", _FixedDateTime)
    monkeypatch.setattr(scheduler_module, "sync_account", fake_sync_account)

    asyncio.run(scheduler_module.run_due_accounts(_app_for(account, session)))
    asyncio.run(scheduler_module.run_due_accounts(_app_for(account, session)))

    assert targets == [date(2026, 8, 25)]
    assert account.last_scheduled_date == date(2026, 8, 26)
    assert session.commits == 1


def test_scheduler_retries_warning_after_cooldown_without_collecting_today(monkeypatch):
    fixed_utc = datetime(2026, 8, 26, 4, 0, 0, tzinfo=UTC).replace(tzinfo=None)
    account = SimpleNamespace(
        id=1,
        timezone="Europe/Moscow",
        sync_time=time(6, 15),
        last_scheduled_date=None,
        last_sync_status="warning",
        last_sync_at=fixed_utc - timedelta(minutes=30),
    )
    session = _FakeSession(account)
    targets: list[date] = []

    async def fake_sync_account(_session, _account, target_date, _cipher, _settings):
        targets.append(target_date)
        return SimpleNamespace(status="warning")

    monkeypatch.setattr(scheduler_module, "datetime", _FixedDateTime)
    monkeypatch.setattr(scheduler_module, "utc_now", lambda: fixed_utc)
    monkeypatch.setattr(scheduler_module, "sync_account", fake_sync_account)

    asyncio.run(scheduler_module.run_due_accounts(_app_for(account, session)))
    assert targets == []

    account.last_sync_at = fixed_utc - timedelta(hours=1)
    asyncio.run(scheduler_module.run_due_accounts(_app_for(account, session)))

    assert targets == [date(2026, 8, 25)]
    assert account.last_scheduled_date is None

