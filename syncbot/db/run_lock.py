"""A lock every process shares, for work that must not run twice at once.

The in-process cache is per warm container, so two Lambda containers (or a
Data Tools run and the scheduled purge) cannot see each other's flag. This
lock is one ``instance_settings`` row: ``key`` is the primary key, so only one
insert wins. The value is a random token, so only the owner releases it. A row
older than its *ttl* is treated as abandoned (process killed, platform
timeout) and taken over. :func:`hold_lock` refreshes the row while the work
runs, so a run that outlives the TTL on a server without a request timeout
keeps its lock.

Take a lock outside :func:`db.transaction`: the lock commits on its own.
Lock rows start with ``lock:``. Backup and restore skip them.
"""

from __future__ import annotations

import secrets
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from sqlalchemy.exc import IntegrityError

from db.schemas import InstanceSetting
from logger import log_error, log_info, log_warning

LOCK_PREFIX = "lock:"
DATA_MAINTENANCE = "data_maintenance"
# Longer than any platform request timeout (Lambda 120s, Cloud Run 300s). A
# killed run frees it within minutes; a live one refreshes it (hold_lock).
DATA_MAINTENANCE_TTL = timedelta(minutes=10)
_REFRESH_FRACTION = 5


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _key(name: str) -> str:
    return f"{LOCK_PREFIX}{name}"


def _refuse_inside_transaction() -> None:
    from db import in_transaction

    if in_transaction():
        raise RuntimeError("take a run lock outside db.transaction()")


def acquire_lock(name: str, ttl: timedelta) -> str | None:
    """Take *name*. Return the token to release it with, or None if another run holds it."""
    from db import close_session, get_session

    _refuse_inside_transaction()
    key = _key(name)
    token = secrets.token_hex(16)
    for _ in range(2):
        now = _utcnow()
        session = get_session()
        try:
            session.add(InstanceSetting(key=key, value=token, updated_at=now))
            session.commit()
            log_info("run_lock_acquired", lock=name)
            return token
        except IntegrityError:
            session.rollback()
            row = session.query(InstanceSetting).filter(InstanceSetting.key == key).one_or_none()
            if row is None:
                continue
            held_since = _naive_utc(row.updated_at) if row.updated_at is not None else None
            if held_since is not None and now - held_since < ttl:
                return None
            # Delete only the row that was read, so a run that just took the
            # lock is not removed by this one.
            session.query(InstanceSetting).filter(
                InstanceSetting.key == key,
                InstanceSetting.value == row.value,
            ).delete(synchronize_session=False)
            session.commit()
            age = int((now - held_since).total_seconds()) if held_since is not None else None
            log_warning("run_lock_expired", lock=name, age_seconds=age)
        finally:
            close_session(session)
    return None


def release_lock(name: str, token: str | None) -> None:
    """Release *name* if *token* still holds it. Never raises; the lock expires on its own."""
    if not token:
        return
    from db import close_session, get_session

    session = get_session()
    try:
        session.query(InstanceSetting).filter(
            InstanceSetting.key == _key(name),
            InstanceSetting.value == token,
        ).delete(synchronize_session=False)
        session.commit()
        log_info("run_lock_released", lock=name)
    except Exception:
        session.rollback()
        log_error("run_lock_release_failed", lock=name, exc_info=True)
    finally:
        close_session(session)


def refresh_lock(name: str, token: str) -> bool:
    """Move *name*'s time to now if *token* still holds it. False when another run took it over."""
    from db import close_session, get_session

    session = get_session()
    try:
        count = (
            session.query(InstanceSetting)
            .filter(InstanceSetting.key == _key(name), InstanceSetting.value == token)
            .update({InstanceSetting.updated_at: _utcnow()}, synchronize_session=False)
        )
        session.commit()
    except Exception:
        session.rollback()
        log_warning("run_lock_refresh_failed", lock=name, exc_info=True)
        return True
    finally:
        close_session(session)
    if not count:
        log_warning("run_lock_lost", lock=name)
        return False
    return True


def _keep_fresh(name: str, token: str, ttl: timedelta, stop: threading.Event) -> None:
    interval = ttl.total_seconds() / _REFRESH_FRACTION
    while not stop.wait(interval):
        if not refresh_lock(name, token):
            return


@contextmanager
def hold_lock(name: str, ttl: timedelta) -> Iterator[str | None]:
    """Take *name* for the block and release it after. Yields None if another run holds it.

    While the block runs, a background thread refreshes the lock every fifth of
    *ttl*. The thread has its own session, so it never joins a transaction the
    block opens.
    """
    token = acquire_lock(name, ttl)
    if token is None:
        yield None
        return
    stop = threading.Event()
    keeper = threading.Thread(
        target=_keep_fresh,
        args=(name, token, ttl, stop),
        name=f"run-lock-{name}",
        daemon=True,
    )
    keeper.start()
    try:
        yield token
    finally:
        stop.set()
        keeper.join(timeout=5)
        release_lock(name, token)


def is_lock_held(name: str, ttl: timedelta) -> bool:
    """True when a run holds *name* and its row is younger than *ttl*. Reads only."""
    from db import close_session, get_session

    session = get_session()
    try:
        row = session.query(InstanceSetting).filter(InstanceSetting.key == _key(name)).one_or_none()
        if row is None or row.updated_at is None:
            return False
        return _utcnow() - _naive_utc(row.updated_at) < ttl
    finally:
        session.rollback()
        close_session(session)
