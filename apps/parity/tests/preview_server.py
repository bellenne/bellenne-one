"""Synthetic UI preview only. Never included in the runtime Docker stage."""
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from time import sleep
from unittest.mock import patch
from app.config import Settings
from app.db import migrate
from app.domain import now
from app.main import create_app
from app.models import Account, Policy, Product, Job
from app.security import cipher
from app.service import claim, enqueue, ensure_owner, run_job
from tests.test_parity import FakeAdapter, product
from sqlalchemy import select

directory = TemporaryDirectory(prefix="parity-preview-")
settings = Settings(Path(directory.name), f"sqlite:///{Path(directory.name) / 'preview.db'}", "/parity", "")
inner = create_app(settings)
factory = inner.state.factory
with factory() as session:
    migrate(session.bind)
    ensure_owner(session, 1).price_source = "seller_api"
    for account in session.scalars(select(Account)):
        account.client_id = "123456"
        account.secret = cipher(settings).encrypt(b"SYNTHETIC-PREVIEW-KEY").decode()
    session.commit()


def adapter(account, day=0):
    return FakeAdapter([product(i if account.marketplace == "wb" else i+1000, f"ART-{i:03}",
        value=str(1499+100*i+day*25+(200 if i%3 and account.marketplace == "ozon" else 0)),
        loyalty=str(1400+100*i) if account.marketplace == "wb" and day != 1 else None)
        for i in range(1,13)])


for day in range(4):
    captured = now()-timedelta(days=3-day)
    with factory() as session:
        job = enqueue(session, 1)
        session.commit()
        job_id = job.id
    claim(factory, settings)
    with patch("app.service.now", return_value=captured):
        run_job(factory, settings, job_id, lambda a: adapter(a, day))


from app.storefront import CollectionError, Observation, product_url
from app.domain import NormalizedPrice
from decimal import Decimal

class PreviewCollector:
    def collect(self, product):
        if product.marketplace == "wb" and product.external_id == "1":
            raise CollectionError("blocked")
        value = Decimal(2900 + int(product.external_id) % 1000 * 10 + (126 if product.marketplace == "ozon" else 0))
        return Observation(NormalizedPrice(Decimal("15060"), value+100, value, "RUB", {}, {}),
                           product_url(product.marketplace, product.external_id, product.sku),
                           {"city": "Москва", "session": "guest", "payment": "wb_wallet" if product.marketplace == "wb" else "ozon_card"})
    def close(self):
        pass

with factory() as session:
    session.get(Policy, 1).price_source = "storefront"
    for item in session.scalars(select(Product)):
        item.sku = item.external_id
    job = enqueue(session, 1)
    session.commit()
    storefront_job_id = job.id
claim(factory, settings)
run_job(factory, settings, storefront_job_id, collector_factory=lambda *args: PreviewCollector())


def worker():
    while True:
        job = claim(factory, settings)
        if job:
            run_job(factory, settings, job, lambda a: adapter(a, 3), collector_factory=lambda *args: PreviewCollector())
        sleep(1)


Thread(target=worker, daemon=True).start()


async def app(scope, receive, send):
    if scope["type"] == "http":
        scope = dict(scope)
        scope["path"] = scope["path"].removeprefix("/parity") or "/"
        scope["headers"] = list(scope["headers"]) + [(b"x-bellenne-user-id",b"1"),(b"x-bellenne-username",b"Preview"),(b"x-bellenne-csrf-token",b"preview-csrf")]
    await inner(scope, receive, send)
