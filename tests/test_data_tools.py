"""Data Tools is limited to an admin on the primary Workspace."""

import os
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("DATABASE_HOST", "localhost")
os.environ.setdefault("DATABASE_USER", "root")
os.environ.setdefault("DATABASE_PASSWORD", "test")
os.environ.setdefault("DATABASE_SCHEMA", "syncbot")
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-0-0")

from handlers.data_tools import (  # noqa: E402
    build_data_tools_view,
    handle_data_tools,
    handle_data_tools_submit_ack,
    handle_data_tools_submit_work,
)


@pytest.fixture
def real_db(tmp_path):
    import db as db_mod
    from db import initialize_database

    url = f"sqlite:///{tmp_path / 'tools.db'}"
    old_engine = db_mod.GLOBAL_ENGINE
    old_schema = db_mod.GLOBAL_SCHEMA
    old_fk = db_mod._SQLITE_FOREIGN_KEYS_ENFORCED
    with patch.dict(os.environ, {"DATABASE_BACKEND": "sqlite", "DATABASE_URL": url}, clear=False):
        try:
            db_mod.GLOBAL_ENGINE = None
            db_mod.GLOBAL_SCHEMA = None
            db_mod._SQLITE_FOREIGN_KEYS_ENFORCED = True
            initialize_database()
            from federation import core as federation_core

            federation_core._INSTANCE_ID = None
            federation_core._cached_private_key = None
            federation_core._cached_public_pem = None
            federation_core.get_or_create_instance_keypair()
            yield
        finally:
            if db_mod.GLOBAL_ENGINE:
                db_mod.GLOBAL_ENGINE.dispose()
            db_mod.GLOBAL_ENGINE = old_engine
            db_mod.GLOBAL_SCHEMA = old_schema
            db_mod._SQLITE_FOREIGN_KEYS_ENFORCED = old_fk


def _body(option="verify", team_id="TPRIMARY"):
    return {
        "user": {"id": "U1"},
        "team_id": team_id,
        "trigger_id": "trig",
        "view": {
            "id": "V1",
            "state": {
                "values": {
                    "data_tools_option": {
                        "data_tools_option": {"selected_option": {"value": option}},
                    }
                }
            },
        },
    }


@pytest.fixture(autouse=True)
def _fake_lock(request):
    """Tests without a database get a lock that is always free. ``real_db`` tests use the real one."""
    if "real_db" in request.fixturenames:
        yield None
        return
    held = {"entered": 0, "exited": 0}

    @contextmanager
    def _free(_name, _ttl):
        held["entered"] += 1
        try:
            yield "token-1"
        finally:
            held["exited"] += 1

    with patch("handlers.data_tools.hold_lock", side_effect=_free):
        yield held


def _hold_lock():
    from db.run_lock import DATA_MAINTENANCE, DATA_MAINTENANCE_TTL, acquire_lock

    token = acquire_lock(DATA_MAINTENANCE, DATA_MAINTENANCE_TTL)
    assert token
    return token


def _lock_is_held():
    from db.run_lock import DATA_MAINTENANCE, DATA_MAINTENANCE_TTL, is_lock_held

    return is_lock_held(DATA_MAINTENANCE, DATA_MAINTENANCE_TTL)


def _allow(admin=True, primary=True):
    return patch.multiple(
        "handlers.data_tools.helpers",
        is_workspace_admin=MagicMock(return_value=admin),
        is_backup_visible_for_workspace=MagicMock(return_value=primary),
    )


def test_data_tools_button_follows_backup():
    from types import SimpleNamespace

    from builders.home import _build_configuration_section

    blocks = []
    workspace = SimpleNamespace(team_id="TPRIMARY")
    with patch("builders.home.helpers.is_backup_visible_for_workspace", return_value=True):
        _build_configuration_section(blocks, workspace, is_admin=True)
    rendered = [block.as_form_field() for block in blocks]
    labels = [
        element["text"]["text"] for block in rendered if block.get("type") == "actions" for element in block["elements"]
    ]
    assert labels.index(":floppy_disk: Backup/Restore") < labels.index(":toolbox: Data Tools")
    blocks = []
    _build_configuration_section(blocks, workspace, is_admin=False)
    text = str([block.as_form_field() for block in blocks])
    assert "Data Tools" not in text


def test_modal_is_hidden_for_a_non_admin_and_a_non_primary_workspace():
    client = MagicMock()
    with _allow(admin=False, primary=True):
        handle_data_tools(_body(), client, MagicMock(), {})
    client.views_update.assert_not_called()
    client = MagicMock()
    with _allow(admin=True, primary=False):
        handle_data_tools(_body(), client, MagicMock(), {})
    client.views_update.assert_not_called()


def _radio(view):
    return next(block["element"] for block in view["blocks"] if block.get("type") == "input")


def _notice_patches(workspace=None):
    record = workspace or MagicMock(workspace_name="Workspace A")
    return patch.multiple(
        "handlers.data_tools.helpers",
        format_admin_label=MagicMock(return_value="`Ada Lovelace (Workspace A)`"),
        get_workspace_record=MagicMock(return_value=record),
    )


def test_modal_explainer_is_one_section():
    view = build_data_tools_view()
    sections = [block for block in view["blocks"] if block.get("type") == "section"]
    assert len(sections) == 1
    assert sections[0]["text"]["text"] == (
        "Verify and repair the SyncBot database. Every Workspace is affected. Make a backup first!"
    )
    assert all(block.get("type") != "context" for block in view["blocks"])
    assert view["blocks"][0]["type"] == "section"
    assert view["blocks"][1]["type"] == "input"


def test_modal_offers_three_options():
    view = build_data_tools_view()
    radio = _radio(view)
    options = radio["options"]
    assert [(option["text"]["text"], option["value"], option["description"]["text"]) for option in options] == [
        ("Verify", "verify", "Check database consistency."),
        ("Repair", "repair", "Fix database issues."),
        ("Purge Soft-Deleted Data", "purge", "Remove soft-deleted data immediately."),
    ]
    assert radio["initial_option"]["value"] == "verify"
    assert view["title"]["text"] == "Data Tools"
    assert view["submit"]["text"] == "Run"


def test_purge_description_fits_slack():
    for option in _radio(build_data_tools_view())["options"]:
        assert len(option["description"]["text"]) < 75


def test_lock_taken_after_the_ack_still_dms(real_db):
    import app as app_module
    from db.run_lock import DATA_MAINTENANCE, release_lock

    body = _body()
    body["type"] = "view_submission"
    body["view"]["callback_id"] = "data_tools_submit"
    with _allow():
        first = handle_data_tools_submit_ack(body, MagicMock(), {})
    assert first["response_action"] == "update"
    token = _hold_lock()
    try:
        with (
            _allow(),
            _notice_patches(),
            patch("handlers.data_tools.run_repair") as repair,
            _dm_text() as dm,
            patch.object(app_module, "LOCAL_DEVELOPMENT", False),
            patch.object(app_module, "emit_metric"),
        ):
            app_module.main_response(body, MagicMock(), MagicMock(), MagicMock(), {})
        repair.assert_not_called()
        dm.assert_called_once()
        assert "Result: A maintenance action is already in progress. Nothing was changed." in dm.call_args.args[2]
    finally:
        release_lock(DATA_MAINTENANCE, token)


def test_ack_does_not_take_the_lock(real_db):
    with _allow():
        first = handle_data_tools_submit_ack(_body(), MagicMock(), {})
        again = handle_data_tools_submit_ack(_body(), MagicMock(), {})
    assert first["response_action"] == "update"
    assert again["response_action"] == "update"
    assert "Close this and SyncBot will DM a report." in first["view"]["blocks"][0]["text"]["text"]
    assert _lock_is_held() is False


def test_ack_ignores_a_held_lock(real_db):
    """The ack never reads the lock, so the first ack and the replay give the same answer."""
    from db.run_lock import DATA_MAINTENANCE, release_lock

    token = _hold_lock()
    try:
        with _allow():
            result = handle_data_tools_submit_ack(_body(), MagicMock(), {})
        assert result["response_action"] == "update"
    finally:
        release_lock(DATA_MAINTENANCE, token)


def test_lock_freed_after_the_ack_runs_and_reports(real_db):
    """The admin saw the wait view, so a run that goes ahead matches what they were told."""
    import app as app_module
    from db.run_lock import DATA_MAINTENANCE, release_lock
    from helpers.data_repair import RepairReport

    body = _body("repair")
    body["type"] = "view_submission"
    body["view"]["callback_id"] = "data_tools_submit"
    token = _hold_lock()
    with _allow():
        first = handle_data_tools_submit_ack(body, MagicMock(), {})
    assert first["response_action"] == "update"
    release_lock(DATA_MAINTENANCE, token)
    with (
        _allow(),
        _notice_patches(),
        patch("handlers.data_tools.run_repair", return_value=RepairReport()) as repair,
        _dm_text() as dm,
        patch.object(app_module, "LOCAL_DEVELOPMENT", False),
        patch.object(app_module, "emit_metric"),
    ):
        app_module.main_response(body, MagicMock(), MagicMock(), MagicMock(), {})
    repair.assert_called_once()
    dm.assert_called_once()
    assert "already in progress" not in dm.call_args.args[2]
    assert _lock_is_held() is False


def test_work_while_running_reports_it_and_keeps_the_lock(real_db):
    from db.run_lock import DATA_MAINTENANCE, release_lock

    token = _hold_lock()
    try:
        with (
            _allow(),
            _notice_patches(),
            patch("handlers.data_tools.run_repair") as repair,
            _dm_text() as dm,
        ):
            handle_data_tools_submit_work(_body("repair"), MagicMock(), MagicMock(), {})
        repair.assert_not_called()
        dm.assert_called_once()
        assert "Result: A maintenance action is already in progress. Nothing was changed." in dm.call_args.args[2]
        assert _lock_is_held() is True
    finally:
        release_lock(DATA_MAINTENANCE, token)


def test_work_holds_the_lock_while_it_runs(real_db):
    seen = []

    def _repair(*, fix):
        seen.append(_lock_is_held())
        report = MagicMock()
        report.count.return_value = 0
        return report

    with _allow(), _notice_patches(), patch("handlers.data_tools.run_repair", side_effect=_repair), _dm_text():
        handle_data_tools_submit_work(_body("verify"), MagicMock(), MagicMock(), {})
    assert seen == [True]
    assert _lock_is_held() is False


def test_a_failed_run_releases_the_lock(real_db):
    with (
        _allow(),
        _notice_patches(),
        patch("handlers.data_tools.run_repair", side_effect=RuntimeError("boom")),
        _dm_text(),
    ):
        handle_data_tools_submit_work(_body("repair"), MagicMock(), MagicMock(), {})
    assert _lock_is_held() is False


def test_a_lock_failure_before_purge_says_nothing_was_changed():
    with (
        _allow(),
        _notice_patches(),
        patch("handlers.data_tools.hold_lock", side_effect=RuntimeError("db down")),
        patch("handlers.data_tools.purge_soft_deletes") as purge,
        _dm_text() as dm,
    ):
        handle_data_tools_submit_work(_body("purge"), MagicMock(), MagicMock(), {})
    purge.assert_not_called()
    assert "Result: Something went wrong. Nothing was changed. Check the logs." in dm.call_args.args[2]


def test_production_repeat_of_the_ack_still_dms_the_report():
    import app as app_module

    body = _body()
    body["type"] = "view_submission"
    body["view"]["callback_id"] = "data_tools_submit"
    body["view"]["hash"] = "hash-1"
    client = MagicMock()
    with (
        _allow(),
        _notice_patches(),
        patch("handlers.data_tools.run_repair") as repair,
        _dm_text() as dm,
        patch.object(app_module, "LOCAL_DEVELOPMENT", False),
        patch.object(app_module, "emit_metric"),
    ):
        repair.return_value.count.return_value = 0
        handle_data_tools_submit_ack(body, client, {})
        app_module.main_response(body, MagicMock(), client, MagicMock(), {})
    dm.assert_called_once()
    assert "Result: No issues found. Nothing was changed." in dm.call_args.args[2]


def test_run_shows_the_running_text():
    labels = {
        "verify": "Verify",
        "repair": "Repair",
        "purge": "Purge Soft-Deleted Data",
        "other": "Verify",
    }
    for option, label in labels.items():
        with _allow():
            ack = handle_data_tools_submit_ack(_body(option), MagicMock(), {})
        assert (
            ack["view"]["blocks"][0]["text"]["text"] == f"{label} is running. Close this and SyncBot will DM a report."
        )
        assert ack["view"]["title"]["text"] == "Data Tools"


def _dm_text():
    return patch("handlers._common.helpers.notify_user_dm")


def test_verify_reports_every_check_when_nothing_is_found():
    client = MagicMock()
    with (
        _allow(),
        _notice_patches(),
        patch("handlers.data_tools.run_repair") as repair,
        _dm_text() as dm,
    ):
        repair.return_value.count.return_value = 0
        handle_data_tools_submit_work(_body("verify"), client, MagicMock(), {})
    dm.assert_called_once()
    text = dm.call_args.args[2]
    for label in (
        "Syncs whose Group is gone: 0",
        "Channels in a Sync that is gone: 0",
        "Channels from a Workspace that is gone: 0",
        "Channels from a Workspace outside the Group: 0",
        "Duplicate Channels in one Sync: 0",
        "Message records with no Channel: 0",
        "Rows for a Workspace that is gone: 0",
    ):
        assert label in text
    assert "Result: No issues found. Nothing was changed." in text


def test_report_is_an_action_notice():
    client = MagicMock()
    with (
        _allow(),
        _notice_patches(),
        patch("handlers.data_tools.run_repair") as repair,
        _dm_text() as dm,
    ):
        repair.return_value.count.return_value = 0
        handle_data_tools_submit_work(_body(), client, MagicMock(), {})
    text = dm.call_args.args[2]
    assert text.startswith(":toolbox: *SyncBot Action Notice*")
    inside = text.split("```", 2)[1]
    assert "Acting Manager:" in inside
    assert "Action: Verify" in inside
    assert "Workspace: Workspace A" in inside


def _outside_channel():
    from db import DbManager, schemas
    from federation.core import get_instance_id

    now = datetime.now(UTC).replace(tzinfo=None)
    group = DbManager.create_record(
        schemas.WorkspaceGroup(name="Group", invite_code="GRP-TOOLS", status="active", created_at=now, uid="g-tools")
    )
    workspace = DbManager.create_record(
        schemas.Workspace(team_id="T0000000A", workspace_name="Workspace A", instance_id=get_instance_id())
    )
    other = DbManager.create_record(
        schemas.Workspace(team_id="T0000000C", workspace_name="Workspace C", instance_id=get_instance_id())
    )
    DbManager.create_record(
        schemas.WorkspaceGroupMember(
            group_id=group.id,
            workspace_id=workspace.id,
            status="active",
            role="member",
            joined_at=now,
        )
    )
    sync = DbManager.create_record(schemas.Sync(title="general", sync_mode="group", group_id=group.id, uid="s-tools"))
    channel = DbManager.create_record(
        schemas.SyncChannel(
            sync_id=sync.id,
            workspace_id=other.id,
            channel_id="CC",
            status="active",
            publishes=True,
            subscribes=True,
            created_at=now,
        )
    )
    return workspace, channel


def test_verify_reports_issues_and_changes_nothing(real_db):
    from db import DbManager, schemas

    workspace, channel = _outside_channel()
    with _allow(), _notice_patches(workspace), _dm_text() as dm:
        handle_data_tools_submit_work(_body("verify", team_id=workspace.team_id), MagicMock(), MagicMock(), {})
    assert DbManager.get_record(schemas.SyncChannel, id=channel.id) is not None
    text = dm.call_args.args[2]
    assert "Channels from a Workspace outside the Group: 1" in text
    assert "Nothing was changed" in text
    assert "Result: 1 issue found. Nothing was changed. Run Repair to fix it." in text


def test_repair_reports_what_it_fixed(real_db):
    from db import DbManager, schemas

    workspace, channel = _outside_channel()
    with _allow(), _notice_patches(workspace), _dm_text() as dm:
        handle_data_tools_submit_work(_body("repair", team_id=workspace.team_id), MagicMock(), MagicMock(), {})
    assert DbManager.get_record(schemas.SyncChannel, id=channel.id) is None
    text = dm.call_args.args[2]
    assert "Channels from a Workspace outside the Group: 1" in text
    assert "Result: Fixed 1 issue." in text


def test_purge_reports_nothing_to_purge(real_db):
    with _allow(), _notice_patches(), _dm_text() as dm:
        handle_data_tools_submit_work(_body("purge"), MagicMock(), MagicMock(), {})
    assert dm.call_args.args[2].startswith(":toolbox: *SyncBot Action Notice*")
    assert "Result: Nothing to purge. Nothing was changed." in dm.call_args.args[2]
    assert "Workspaces Removed: 0" in dm.call_args.args[2]
    assert "Workspaces Kept: 0" in dm.call_args.args[2]


def test_purge_reports_a_kept_workspace(real_db):
    from db import DbManager, schemas
    from federation.core import get_instance_id

    workspace = DbManager.create_record(
        schemas.Workspace(team_id="T0000000A", workspace_name="Workspace A", instance_id=get_instance_id())
    )
    DbManager.update_records(
        schemas.Workspace,
        [schemas.Workspace.id == workspace.id],
        {schemas.Workspace.deleted_at: datetime.now(UTC) - timedelta(days=1)},
    )
    DbManager.create_record(
        schemas.FederationWorkspaceAllowlist(instance_id=get_instance_id(), workspace_id=workspace.id)
    )
    with (
        _allow(),
        _notice_patches(workspace),
        patch("helpers.notifications.notify_admins_dm"),
        _dm_text() as dm,
    ):
        handle_data_tools_submit_work(_body("purge", team_id=workspace.team_id), MagicMock(), MagicMock(), {})
    text = dm.call_args.args[2]
    assert "Workspaces Kept: 1" in text
    assert "Result: 1 Workspace is still on an External Connection, so it was kept. Nothing was changed." in text
    assert DbManager.get_record(schemas.Workspace, id=workspace.team_id) is not None


def test_failure_sends_a_notice(_fake_lock):
    with (
        _allow(),
        _notice_patches(),
        patch("handlers.data_tools.run_repair", side_effect=RuntimeError("boom")),
        _dm_text() as dm,
    ):
        handle_data_tools_submit_work(_body("repair"), MagicMock(), MagicMock(), {})
    dm.assert_called_once()
    text = dm.call_args.args[2]
    assert text.startswith(":toolbox: *SyncBot Action Notice*")
    assert "Action: Repair" in text
    assert "Result: Something went wrong. Nothing was changed. Check the logs." in text
    assert _fake_lock == {"entered": 1, "exited": 1}


def test_purge_failure_says_data_may_be_removed():
    with (
        _allow(),
        _notice_patches(),
        patch("handlers.data_tools.purge_soft_deletes", side_effect=RuntimeError("boom")),
        _dm_text() as dm,
    ):
        handle_data_tools_submit_work(_body("purge"), MagicMock(), MagicMock(), {})
    text = dm.call_args.args[2]
    assert "Action: Purge Soft-Deleted Data" in text
    assert "Result: Something went wrong. Some data may already be removed. Check the logs." in text
    assert "Nothing was changed" not in text


def test_purge_reports_a_workspace_it_could_not_remove():
    from helpers.notifications import PurgeResult

    result = PurgeResult(channels_purged=2, workspaces_failed=1)
    with (
        _allow(),
        _notice_patches(),
        patch("handlers.data_tools.purge_soft_deletes", return_value=result),
        _dm_text() as dm,
    ):
        handle_data_tools_submit_work(_body("purge"), MagicMock(), MagicMock(), {})
    text = dm.call_args.args[2]
    assert "Workspaces Failed: 1" in text
    assert "Result: Removed 2 rows. 1 Workspace could not be removed. Check the logs." in text


def test_a_run_logs_start_and_finish():
    from helpers.data_repair import RepairReport

    with (
        _allow(),
        _notice_patches(),
        patch("handlers.data_tools.run_repair", return_value=RepairReport(counts={})),
        patch("handlers.data_tools.log_info") as log_info,
        _dm_text(),
    ):
        handle_data_tools_submit_work(_body("verify"), MagicMock(), MagicMock(), {})
    events = [call.args[0] for call in log_info.call_args_list]
    assert events == ["data_tools_started", "data_tools_finished"]
    finished = log_info.call_args_list[1].kwargs
    assert finished["option"] == "verify"
    assert finished["result"] == "Result: No issues found. Nothing was changed."
    assert "duration_ms" in finished


def test_a_failed_run_logs_the_error():
    with (
        _allow(),
        _notice_patches(),
        patch("handlers.data_tools.run_repair", side_effect=RuntimeError("boom")),
        patch("handlers.data_tools.log_error") as log_error,
        _dm_text(),
    ):
        handle_data_tools_submit_work(_body("repair"), MagicMock(), MagicMock(), {})
    log_error.assert_called_once()
    assert log_error.call_args.args[0] == "data_tools_failed"
    assert log_error.call_args.kwargs["option"] == "repair"
    assert log_error.call_args.kwargs["exc_info"] is True


def test_a_refused_run_logs_a_warning():
    with (
        patch("handlers.data_tools._allowed", return_value=False),
        patch("handlers.data_tools.log_warning") as log_warning,
        _dm_text() as dm,
    ):
        handle_data_tools_submit_work(_body("purge"), MagicMock(), MagicMock(), {})
    dm.assert_not_called()
    assert log_warning.call_args.args[0] == "data_tools_refused"


def test_purge_now_ignores_the_retention_window(real_db):
    from db import DbManager, schemas
    from federation.core import get_instance_id

    workspace = DbManager.create_record(
        schemas.Workspace(team_id="T0000000A", workspace_name="Workspace A", instance_id=get_instance_id())
    )
    sync = DbManager.create_record(schemas.Sync(title="general", sync_mode="group", uid="s-purge-now"))
    old = DbManager.create_record(
        schemas.SyncChannel(
            sync_id=sync.id,
            workspace_id=workspace.id,
            channel_id="COLD",
            status="deleted",
            created_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(days=2),
            deleted_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(days=1),
        )
    )
    live = DbManager.create_record(
        schemas.SyncChannel(
            sync_id=sync.id,
            workspace_id=workspace.id,
            channel_id="CLIVE",
            status="active",
            created_at=datetime.now(UTC).replace(tzinfo=None),
        )
    )
    with (
        _allow(),
        _notice_patches(workspace),
        patch("helpers.notifications.notify_admins_dm"),
        _dm_text(),
    ):
        handle_data_tools_submit_work(_body("purge", team_id=workspace.team_id), MagicMock(), MagicMock(), {})
    assert DbManager.get_record(schemas.SyncChannel, id=old.id) is None
    assert DbManager.get_record(schemas.SyncChannel, id=live.id) is not None


def test_purge_now_keeps_a_workspace_on_an_allowlist(real_db):
    from datetime import UTC, datetime, timedelta

    from db import DbManager, schemas
    from federation.core import get_instance_id

    workspace = DbManager.create_record(
        schemas.Workspace(team_id="T0000000A", workspace_name="Workspace A", instance_id=get_instance_id())
    )
    DbManager.update_records(
        schemas.Workspace,
        [schemas.Workspace.id == workspace.id],
        {schemas.Workspace.deleted_at: datetime.now(UTC) - timedelta(days=1)},
    )
    DbManager.create_record(
        schemas.FederationWorkspaceAllowlist(instance_id=get_instance_id(), workspace_id=workspace.id)
    )
    with (
        _allow(),
        _notice_patches(workspace),
        patch("helpers.notifications.notify_admins_dm") as dm,
        _dm_text(),
    ):
        handle_data_tools_submit_work(_body("purge", team_id=workspace.team_id), MagicMock(), MagicMock(), {})
    dm.assert_not_called()
    assert DbManager.get_record(schemas.Workspace, id=workspace.team_id) is not None


def test_purge_now_omits_a_day_count():
    from helpers.notifications import _retention_notice

    text = _retention_notice("Workspace A", 0, None, for_managers=True)
    assert "Data Retention Days" not in text
    assert "Action: Retention" in _retention_notice("Workspace A", 30, None, for_managers=True)
    assert "Data Retention Days: 30" in _retention_notice("Workspace A", 30, None, for_managers=True)
