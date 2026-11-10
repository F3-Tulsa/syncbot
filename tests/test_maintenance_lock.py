"""Restore, Data Migration import, and Reset Database wait for the run lock."""

import os
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("DATABASE_HOST", "localhost")
os.environ.setdefault("DATABASE_USER", "root")
os.environ.setdefault("DATABASE_PASSWORD", "test")
os.environ.setdefault("DATABASE_SCHEMA", "syncbot")
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-0-0")

BUSY = "A maintenance action is already in progress. Try again in a few minutes."


@contextmanager
def _busy(_name, _ttl):
    yield None


def test_restore_waits_for_the_lock():
    from handlers import export_import

    with (
        patch("handlers.export_import.hold_lock", side_effect=_busy),
        patch("handlers.export_import._do_restore") as restore,
        patch("handlers.export_import._close_modal_done") as done,
    ):
        export_import._finish_restore({}, MagicMock(), {}, "U0000000A")
    restore.assert_not_called()
    assert done.call_args.args[2] == f":warning: Restore did not run. {BUSY}"


def test_import_waits_for_the_lock():
    from handlers import export_import

    workspace = SimpleNamespace(id=1, team_id="T0000000A", workspace_name="Workspace A")
    meta = {"data": {"version": 1}, "group_id": 0, "workspace_id": 1, "team_id_to_workspace_id": {}}
    with (
        patch("helpers._cache._cache_get", return_value=meta),
        patch("handlers.export_import.helpers.get_workspace_by_id", return_value=workspace),
        patch("handlers.export_import.ei.get_migration_file_status", return_value=("this_install", None)),
        patch("handlers.export_import.hold_lock", side_effect=_busy),
        patch("handlers.export_import._run_migration_import") as run_import,
        patch("handlers.export_import._notify_import_result") as notify,
    ):
        export_import._complete_data_migration_import({}, MagicMock(), MagicMock(), {}, user_id="U0000000A")
    run_import.assert_not_called()
    assert notify.call_args.args[3] == f":warning: Import stopped. {BUSY}"


def test_reset_waits_for_the_lock():
    from handlers.sync import handle_db_reset_proceed

    body = {"user": {"id": "U0000000A"}, "team": {"id": "T0000000A"}, "view": {"id": "V1"}}
    with (
        patch("handlers.sync.helpers.is_db_reset_visible_for_workspace", return_value=True),
        patch("handlers.sync.helpers.is_workspace_admin", return_value=True),
        patch("db.run_lock.hold_lock", side_effect=_busy),
        patch("db.drop_and_init_db") as reset,
        patch("handlers.sync._close_modal_done") as done,
        patch("handlers.sync.helpers.clear_all_caches") as clear,
    ):
        handle_db_reset_proceed(body, MagicMock(), MagicMock(), {})
    reset.assert_not_called()
    clear.assert_not_called()
    assert done.call_args.args[2] == f":warning: Reset did not run. {BUSY}"
