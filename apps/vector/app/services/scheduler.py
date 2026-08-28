from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.models import SchedulerSetting
from app.services.collector import Collector


class SchedulerService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        collector: Collector,
        default_timezone: str,
    ) -> None:
        self.session_factory = session_factory
        self.collector = collector
        self.scheduler = AsyncIOScheduler(
            timezone=ZoneInfo(default_timezone)
        )

    async def start(self) -> None:
        if not self.scheduler.running:
            self.scheduler.start()
        self.reload()

    async def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    def reload(self) -> None:
        self.scheduler.remove_all_jobs()
        with self.session_factory() as session:
            settings = session.scalars(
                select(SchedulerSetting).where(
                    SchedulerSetting.enabled.is_(True)
                )
            ).all()
            for item in settings:
                timezone = ZoneInfo(item.timezone_name)
                trigger = CronTrigger(
                    hour=item.run_time.hour,
                    minute=item.run_time.minute,
                    timezone=timezone,
                )
                self.scheduler.add_job(
                    self._scheduled_collect,
                    trigger=trigger,
                    id=f"account-{item.account_id}",
                    args=[item.account_id, item.lookback_days, item.timezone_name],
                    replace_existing=True,
                    coalesce=True,
                    max_instances=1,
                    misfire_grace_time=3600,
                )

    async def _scheduled_collect(
        self,
        account_id: int,
        lookback_days: int,
        timezone_name: str,
    ) -> None:
        local_today = datetime.now(ZoneInfo(timezone_name)).date()
        end = local_today - timedelta(days=1)
        begin = end - timedelta(days=max(1, lookback_days) - 1)
        await self.collector.collect(
            account_id,
            begin,
            end,
            trigger="scheduler",
        )

    def next_run(self, account_id: int) -> datetime | None:
        job = self.scheduler.get_job(f"account-{account_id}")
        return job.next_run_time if job else None
