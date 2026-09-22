"""A single worker owns this SQLite queue across processes on either host OS."""

import os
from contextlib import contextmanager

from . import db


@contextmanager
def worker_lock():
    db.DATA.mkdir(parents=True, exist_ok=True)
    with (db.DATA / "worker.lock").open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            handle.write(b"0")
            handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
