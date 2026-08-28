from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from app.clock import utc_now
from app.models import MarketplaceAccount
from app.services.sync import sync_account


logger = logging.getLogger(__name__)
RETRY_INTERVAL = timedelta(hours=1)


async def run_due_accounts(app) -> None:
    session_factory = app.state.session_factory
    settings = app.state.settings
    cipher = app.state.credential_cipher
    with session_factory() as session:
        account_ids = list(
            session.scalars(
                select(MarketplaceAccount.id).where(
                    MarketplaceAccount.is_active.is_(True),
                    MarketplaceAccount.schedule_enabled.is_(True),
                )
            )
        )
    for account_id in account_ids:
        with session_factory() as session:
            account = session.get(MarketplaceAccount, account_id)
            if account is None:
                continue
            try:
                local_now = datetime.now(ZoneInfo(account.timezone or settings.timezone))
            except Exception:
                local_now = datetime.now(ZoneInfo(settings.timezone))
            if local_now.time().replace(second=0, microsecond=0) < account.sync_time.replace(second=0, microsecond=0):
                continue
            if account.last_scheduled_date == local_now.date():
                continue
            if (
                account.last_sync_status in {"error", "warning"}
                and account.last_sync_at is not None
                and utc_now() - account.last_sync_at < RETRY_INTERVAL
            ):
                continue
            target_date = local_now.date() - timedelta(days=1)
            run = await sync_account(session, account, target_date, cipher, settings)
            if run.status == "success":
                account.last_scheduled_date = local_now.date()
                session.commit()


def build_scheduler(app) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=app.state.settings.timezone)
    scheduler.add_job(
        run_due_accounts,
        "interval",
        minutes=1,
        args=[app],
        id="marketplace-sync-tick",
        coalesce=True,
        max_instances=1,
        misfire_grace_time=120,
    )
    return scheduler
