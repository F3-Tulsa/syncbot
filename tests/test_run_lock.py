"""The run lock is one instance_settings row that every process shares."""

import os
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

os.environ.setdefault("DATABASE_HOST", "localhost")
os.environ.setdefault("DATABASE_USER", "root")
os.environ.setdefault("DATABASE_PASSWORD", "test")
os.environ.setdefault("DATABASE_SCHEMA", "syncbot")
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-0-0")

TTL = timedelta(minutes=10)


@pytest.fixture
def real_db(tmp_path):
    import db as db_mod
    from db import initialize_database

    url = f"sqlite:///{tmp_path / 'lock.db'}"
    old_engine = db_mod.GLOBAL_ENGINE
    old_schema = db_mod.GLOBAL_SCHEMA
    with patch.dict(os.environ, {"DATABASE_BACKEND": "sqlite", "DATABASE_URL": url}, clear=False):
        try:
            db_mod.GLOBAL_ENGINE = None
            db_mod.GLOBAL_SCHEMA = None
            initialize_database()
            yield
        finally:
            if db_mod.GLOBAL_ENGINE is not None:
                db_mod.GLOBAL_ENGINE.dispose()
            db_mod.GLOBAL_ENGINE = old_engine
            db_mod.GLOBAL_SCHEMA = old_schema


def _age_lock(name, minutes):
    from db import DbManager, schemas

    DbManager.update_records(
        schemas.InstanceSetting,
        [schemas.InstanceSetting.key == f"lock:{name}"],
        {schemas.InstanceSetting.updated_at: datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=minutes)},
    )


def test_only_one_run_takes_the_lock(real_db):
    from db.run_lock import acquire_lock, is_lock_held, release_lock

    token = acquire_lock("job", TTL)
    assert token
    assert acquire_lock("job", TTL) is None
    assert is_lock_held("job", TTL) is True
    release_lock("job", token)
    assert is_lock_held("job", TTL) is False
    assert acquire_lock("job", TTL)


def test_another_token_cannot_release_the_lock(real_db):
    from db.run_lock import acquire_lock, is_lock_held, release_lock

    acquire_lock("job", TTL)
    release_lock("job", "not-the-owner")
    assert is_lock_held("job", TTL) is True


def test_an_expired_lock_is_taken_over(real_db):
    from db.run_lock import acquire_lock, is_lock_held, release_lock

    stale = acquire_lock("job", TTL)
    _age_lock("job", 11)
    assert is_lock_held("job", TTL) is False
    with patch("db.run_lock.log_warning") as log_warning:
        fresh = acquire_lock("job", TTL)
    assert fresh and fresh != stale
    assert log_warning.call_args.args[0] == "run_lock_expired"
    release_lock("job", stale)
    assert is_lock_held("job", TTL) is True


def test_a_lock_inside_its_ttl_is_not_taken_over(real_db):
    from db.run_lock import acquire_lock

    acquire_lock("job", TTL)
    _age_lock("job", 9)
    assert acquire_lock("job", TTL) is None


def test_release_never_raises():
    from db.run_lock import release_lock

    with (
        patch("db.get_session", side_effect=None) as get_session,
        patch("db.run_lock.log_error") as log_error,
    ):
        get_session.return_value.query.side_effect = RuntimeError("db down")
        release_lock("job", "token")
    assert log_error.call_args.args[0] == "run_lock_release_failed"


def test_release_without_a_token_does_nothing():
    from db.run_lock import release_lock

    with patch("db.get_session") as get_session:
        release_lock("job", None)
    get_session.assert_not_called()


def test_backup_leaves_out_lock_rows(real_db):
    from db import DbManager, schemas
    from db.run_lock import acquire_lock
    from helpers.export_import import build_full_backup

    acquire_lock("data_maintenance", TTL)
    DbManager.create_record(schemas.InstanceSetting(key="soft_delete_retention_days", value="30"))
    keys = {row["key"] for row in build_full_backup()["instance_settings"]}
    assert "soft_delete_retention_days" in keys
    assert "lock:data_maintenance" not in keys


def test_restore_skips_lock_rows(real_db):
    from db import DbManager, schemas
    from helpers.export_import import restore_full_backup

    data = {
        "instance_settings": [
            {"key": "lock:data_maintenance", "value": "old-token", "updated_at": None},
            {"key": "soft_delete_retention_days", "value": "30", "updated_at": None},
        ]
    }
    restore_full_backup(data, skip_hmac_check=True, skip_encryption_key_check=True)
    keys = {
        row.key for row in DbManager.find_records(schemas.InstanceSetting, [schemas.InstanceSetting.key.isnot(None)])
    }
    assert "soft_delete_retention_days" in keys
    assert "lock:data_maintenance" not in keys


def test_scheduled_purge_waits_for_the_lock(real_db):
    from db.run_lock import DATA_MAINTENANCE, acquire_lock, release_lock
    from helpers._cache import _cache_delete

    token = acquire_lock(DATA_MAINTENANCE, TTL)
    _cache_delete("purge_check")
    try:
        with (
            patch("helpers.notifications.purge_soft_deletes") as purge,
            patch("helpers.notifications._cache_set") as cache_set,
        ):
            from helpers.notifications import purge_stale_soft_deletes

            assert purge_stale_soft_deletes() == 0
        purge.assert_not_called()
        assert cache_set.call_args.kwargs["ttl"] == 300
    finally:
        release_lock(DATA_MAINTENANCE, token)
        _cache_delete("purge_check")


def test_scheduled_purge_releases_the_lock(real_db):
    from db.run_lock import DATA_MAINTENANCE, is_lock_held
    from helpers._cache import _cache_delete, _cache_get

    _cache_delete("purge_check")
    seen = []

    def _purge(_days):
        from helpers.notifications import PurgeResult

        seen.append(is_lock_held(DATA_MAINTENANCE, TTL))
        return PurgeResult()

    try:
        with patch("helpers.notifications.purge_soft_deletes", side_effect=_purge):
            from helpers.notifications import purge_stale_soft_deletes

            purge_stale_soft_deletes()
        assert seen == [True]
        assert is_lock_held(DATA_MAINTENANCE, TTL) is False
        assert _cache_get("purge_check") is True
    finally:
        _cache_delete("purge_check")


def test_scheduled_purge_retries_soon_when_the_lock_cannot_be_taken(real_db):
    from helpers._cache import _cache_delete

    _cache_delete("purge_check")
    try:
        with (
            patch("db.run_lock.acquire_lock", side_effect=RuntimeError("db down")),
            patch("helpers.notifications.purge_soft_deletes") as purge,
            patch("helpers.notifications._cache_set") as cache_set,
            patch("helpers.notifications.log_error") as log_error,
        ):
            from helpers.notifications import purge_stale_soft_deletes

            assert purge_stale_soft_deletes() == 0
        purge.assert_not_called()
        assert [call.kwargs["ttl"] for call in cache_set.call_args_list] == [300]
        assert log_error.call_args.args[0] == "purge_stale_soft_deletes_failed"
        assert log_error.call_args.kwargs["started"] is False
    finally:
        _cache_delete("purge_check")


def test_a_failed_scheduled_purge_waits_the_full_day(real_db):
    from db.run_lock import DATA_MAINTENANCE, is_lock_held
    from helpers._cache import _cache_delete

    _cache_delete("purge_check")
    try:
        with (
            patch("helpers.notifications.purge_soft_deletes", side_effect=RuntimeError("boom")),
            patch("helpers.notifications._cache_set") as cache_set,
        ):
            from helpers.notifications import purge_stale_soft_deletes

            assert purge_stale_soft_deletes() == 0
        assert [call.kwargs["ttl"] for call in cache_set.call_args_list] == [86400]
        assert is_lock_held(DATA_MAINTENANCE, TTL) is False
    finally:
        _cache_delete("purge_check")


def test_hold_lock_keeps_the_lock_fresh_and_releases_it(real_db):
    import time

    from db import DbManager, schemas
    from db.run_lock import hold_lock, is_lock_held

    def _taken_at():
        rows = DbManager.find_records(schemas.InstanceSetting, [schemas.InstanceSetting.key == "lock:job"])
        return rows[0].updated_at

    short = timedelta(seconds=0.5)
    with hold_lock("job", short) as token:
        assert token
        first = _taken_at()
        time.sleep(0.4)
        assert _taken_at() > first
        assert is_lock_held("job", short) is True
    assert is_lock_held("job", TTL) is False


def test_hold_lock_yields_none_while_another_run_holds_it(real_db):
    from db.run_lock import acquire_lock, hold_lock, is_lock_held

    acquire_lock("job", TTL)
    with hold_lock("job", TTL) as token:
        assert token is None
    assert is_lock_held("job", TTL) is True


def test_refresh_reports_a_lock_that_was_taken_over(real_db):
    from db.run_lock import acquire_lock, refresh_lock, release_lock

    token = acquire_lock("job", TTL)
    release_lock("job", token)
    acquire_lock("job", TTL)
    with patch("db.run_lock.log_warning") as log_warning:
        assert refresh_lock("job", token) is False
    assert log_warning.call_args.args[0] == "run_lock_lost"


def test_a_lock_cannot_be_taken_inside_a_transaction(real_db):
    from db import transaction
    from db.run_lock import acquire_lock

    with transaction(), pytest.raises(RuntimeError, match="outside db.transaction"):
        acquire_lock("job", TTL)
