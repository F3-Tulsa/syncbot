"""Data Tools is limited to an admin on the primary Workspace."""

import os
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


def test_run_while_running_returns_the_error():
    with _allow(), patch("handlers.data_tools._cache_get", return_value=True):
        result = handle_data_tools_submit_ack(_body(), MagicMock(), {})
    assert "already running" in result["errors"]["data_tools_option"]


def test_same_submission_is_not_already_running():
    from helpers._cache import _cache_delete

    other = _body()
    other["view"] = {**other["view"], "id": "V2", "hash": "other"}
    try:
        with _allow():
            first = handle_data_tools_submit_ack(_body(), MagicMock(), {})
            again = handle_data_tools_submit_ack(_body(), MagicMock(), {})
            blocked = handle_data_tools_submit_ack(other, MagicMock(), {})
        assert first["response_action"] == "update"
        assert again["response_action"] == "update"
        assert "already running" in blocked["errors"]["data_tools_option"]
    finally:
        _cache_delete("data_tools_running")


def test_production_repeat_of_the_ack_still_dms_the_report():
    import app as app_module
    from helpers._cache import _cache_delete

    body = _body()
    body["type"] = "view_submission"
    body["view"]["callback_id"] = "data_tools_submit"
    body["view"]["hash"] = "hash-1"
    client = MagicMock()
    try:
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
        client.views_update.assert_not_called()
    finally:
        _cache_delete("data_tools_running")


def test_run_shows_the_running_text():
    labels = {
        "verify": "Verify",
        "repair": "Repair",
        "purge": "Purge Soft-Deleted Data",
        "other": "Verify",
    }
    for option, label in labels.items():
        with (
            _allow(),
            patch("handlers.data_tools._cache_get", return_value=None),
            patch("handlers.data_tools._cache_set"),
        ):
            ack = handle_data_tools_submit_ack(_body(option), MagicMock(), {})
        assert (
            ack["view"]["blocks"][0]["text"]["text"] == f"{label} is running. Close this and SyncBot will DM a report."
        )
        assert ack["view"]["title"]["text"] == "Data Tools"


def _dm_text():
    return patch("handlers._common.helpers.notify_user_dm")


def test_work_does_not_update_the_view():
    client = MagicMock()
    with (
        _allow(),
        _notice_patches(),
        patch("handlers.data_tools.run_repair") as repair,
        _dm_text(),
    ):
        repair.return_value.count.return_value = 0
        handle_data_tools_submit_work(_body(), client, MagicMock(), {})
    client.views_update.assert_not_called()


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
    assert "*SyncBot Action Notice*" in text
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


def test_failure_sends_a_notice():
    from helpers._cache import _cache_get, _cache_set

    _cache_set("data_tools_running", True, ttl=900)
    with (
        _allow(),
        _notice_patches(),
        patch("handlers.data_tools.run_repair", side_effect=RuntimeError("boom")),
        _dm_text() as dm,
    ):
        handle_data_tools_submit_work(_body("repair"), MagicMock(), MagicMock(), {})
    dm.assert_called_once()
    text = dm.call_args.args[2]
    assert "*SyncBot Action Notice*" in text
    assert "Action: Repair" in text
    assert "Result: Something went wrong. Nothing more was changed. Check the logs." in text
    assert _cache_get("data_tools_running") is None


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
    client = MagicMock()
    with (
        _allow(),
        _notice_patches(workspace),
        patch("helpers.notifications.notify_admins_dm"),
        _dm_text(),
    ):
        handle_data_tools_submit_work(_body("purge", team_id=workspace.team_id), client, MagicMock(), {})
    assert DbManager.get_record(schemas.SyncChannel, id=old.id) is None
    assert DbManager.get_record(schemas.SyncChannel, id=live.id) is not None
    client.views_update.assert_not_called()


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
    client = MagicMock()
    with (
        _allow(),
        _notice_patches(workspace),
        patch("helpers.notifications.notify_admins_dm") as dm,
        _dm_text(),
    ):
        handle_data_tools_submit_work(_body("purge", team_id=workspace.team_id), client, MagicMock(), {})
    dm.assert_not_called()
    assert DbManager.get_record(schemas.Workspace, id=workspace.team_id) is not None
    client.views_update.assert_not_called()


def test_purge_now_omits_a_day_count():
    from helpers.notifications import _retention_notice

    text = _retention_notice("Workspace A", 0, None, for_managers=True)
    assert "Data Retention Days" not in text
    assert "Action: Retention" in _retention_notice("Workspace A", 30, None, for_managers=True)
    assert "Data Retention Days: 30" in _retention_notice("Workspace A", 30, None, for_managers=True)
