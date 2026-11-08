"""The application SQLite engine enforces foreign keys unless startup finds bad rows."""

import os

os.environ.setdefault("DATABASE_HOST", "localhost")
os.environ.setdefault("DATABASE_USER", "root")
os.environ.setdefault("DATABASE_PASSWORD", "test")
os.environ.setdefault("DATABASE_SCHEMA", "syncbot")
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-0-0")

from sqlalchemy import text  # noqa: E402


def _fresh():
    import db as db_mod

    db_mod.GLOBAL_ENGINE = None
    db_mod.GLOBAL_SCHEMA = None
    db_mod._SQLITE_FOREIGN_KEYS_ENFORCED = True
    return db_mod


def test_app_engine_enforces_foreign_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_BACKEND", "sqlite")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'fk-on.db'}")
    db_mod = _fresh()
    try:
        engine = db_mod.get_engine()
        with engine.connect() as conn:
            assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1
    finally:
        if db_mod.GLOBAL_ENGINE is not None:
            db_mod.GLOBAL_ENGINE.dispose()
        db_mod.GLOBAL_ENGINE = None
        db_mod._SQLITE_FOREIGN_KEYS_ENFORCED = True


def test_bad_rows_turn_enforcement_off(tmp_path, monkeypatch, caplog):
    import logging

    monkeypatch.setenv("DATABASE_BACKEND", "sqlite")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'fk-off.db'}")
    db_mod = _fresh()
    try:
        db_mod.initialize_database()
        engine = db_mod.get_engine()
        raw = engine.raw_connection()
        try:
            raw.execute("PRAGMA foreign_keys=OFF")
            raw.execute(
                "INSERT INTO workspace_settings (workspace_id, key, value) "
                "VALUES (424242, 'allow_private_channels', 'no')"
            )
            raw.commit()
        finally:
            raw.close()
        with caplog.at_level(logging.ERROR, logger="syncbot"):
            db_mod._review_sqlite_foreign_keys()
        assert db_mod._SQLITE_FOREIGN_KEYS_ENFORCED is False
        assert any(record.message == "sqlite_foreign_key_violations" for record in caplog.records)
        with engine.connect() as conn:
            assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 0
    finally:
        if db_mod.GLOBAL_ENGINE is not None:
            db_mod.GLOBAL_ENGINE.dispose()
        db_mod.GLOBAL_ENGINE = None
        db_mod._SQLITE_FOREIGN_KEYS_ENFORCED = True
