"""Explicit opt-in network smoke, isolated temporary DB; not discovered by unittest.

Run in browser-test image. Uses the real application queue/collector pipeline.
No seller tokens, existing DB or personal browser profile is read.
"""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from sqlalchemy import select
from app.config import Settings
from app.db import database, migrate
from app.models import Account, Job, Product, Snapshot
from app.service import claim, enqueue, ensure_owner, run_job

with TemporaryDirectory() as directory:
    settings = Settings(Path(directory), f"sqlite:///{directory}/smoke.db", "/parity", "")
    engine, factory = database(settings)
    migrate(engine)
    with factory() as session:
        ensure_owner(session, 1)
        for account in session.scalars(select(Account)):
            session.add(Product(owner_id=1, account_id=account.id, marketplace=account.marketplace,
                                external_id="701955470" if account.marketplace == "wb" else "3244447271",
                                sku="701955470" if account.marketplace == "wb" else "3267204518",
                                seller_article="FFO-039_300x270", name="Live smoke", status="active"))
        job = enqueue(session, 1)
        session.commit()
        job_id = job.id
    claim(factory, settings)
    run_job(factory, settings, job_id)
    with factory() as session:
        job = session.get(Job, job_id)
        print(json.dumps({"status": job.status, "results": job.results, "snapshots": [
            {"product_id": s.product_id, "loyalty_price": str(s.loyalty_price), "context": s.context}
            for s in session.scalars(select(Snapshot))]}, ensure_ascii=False))
    engine.dispose()
