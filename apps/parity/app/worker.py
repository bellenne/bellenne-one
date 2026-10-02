import logging
import time
from .config import Settings
from .db import database, migrate
from .service import claim, run_job, schedule_due


def main():
    settings = Settings.from_env()
    engine, factory = database(settings)
    migrate(engine)
    while True:
        try:
            schedule_due(factory)
            job_id = claim(factory, settings)
            if job_id:
                run_job(factory, settings, job_id)
            else:
                time.sleep(settings.worker_idle)
        except Exception:
            # Never emit external response bodies, headers, or exception strings.
            logging.error("Parity worker operation failed; see durable job journal")
            time.sleep(settings.worker_idle)


if __name__ == "__main__":
    main()
