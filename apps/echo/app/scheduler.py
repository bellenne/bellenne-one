import asyncio
import os

from apscheduler.schedulers.background import BackgroundScheduler

from .db import SessionLocal
from .worker import process_due_users


def start_scheduler() -> BackgroundScheduler:
    interval_seconds = int(os.getenv("CHECK_INTERVAL_SECONDS", "60"))
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(_run_due_users, "interval", seconds=interval_seconds, id="reply-worker", max_instances=1)
    scheduler.start()
    return scheduler


def _run_due_users() -> None:
    db = SessionLocal()
    try:
        asyncio.run(process_due_users(db))
    finally:
        db.close()
