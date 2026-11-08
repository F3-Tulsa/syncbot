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


def _body(option="dry_run"):
    return {
        "user": {"id": "U1"},
        "team_id": "TPRIMARY",
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


def test_modal_offers_three_options():
    with patch("handlers.data_tools.soft_delete_retention_days", return_value=30):
        view = build_data_tools_view()
    radio = view["blocks"][2]["element"]
    labels = [option["text"]["text"] for option in radio["options"]]
    assert labels == ["Repair (Dry Run)", "Repair", "Purge Soft-Deleted Data"]
    assert radio["initial_option"]["value"] == "dry_run"
    assert "30" in radio["options"][2]["description"]["text"]
    assert view["title"]["text"] == "Data Tools"
    assert view["submit"]["text"] == "Run"


def test_run_while_running_returns_the_error():
    with _allow(), patch("handlers.data_tools._cache_get", return_value=True):
        result = handle_data_tools_submit_ack(_body(), MagicMock(), {})
    assert "already running" in result["errors"]["data_tools_option"]


def test_each_option_shows_its_results():
    client = MagicMock()
    with (
        _allow(),
        patch("handlers.data_tools._cache_get", return_value=None),
        patch("handlers.data_tools._cache_set"),
        patch("handlers.data_tools.run_repair") as repair,
    ):
        repair.return_value.count.return_value = 0
        ack = handle_data_tools_submit_ack(_body("dry_run"), client, {})
        assert "Running." in ack["view"]["blocks"][0]["text"]["text"]
        handle_data_tools_submit_work(_body("dry_run"), client, MagicMock(), {})
    text = client.views_update.call_args.kwargs["view"]["blocks"][0]["text"]["text"]
    assert text == "No problems found."

    client = MagicMock()
    with _allow(), patch("handlers.data_tools.run_repair") as repair:
        repair.return_value.count.side_effect = lambda key: 3 if key == "channel_outside_group" else 0
        handle_data_tools_submit_work(_body("repair"), client, MagicMock(), {})
    text = client.views_update.call_args.kwargs["view"]["blocks"][0]["text"]["text"]
    assert text.startswith("*Fixed*")
    assert "Channels from a Workspace outside the Group: 3" in text

    client = MagicMock()
    with _allow(), patch("handlers.data_tools._run_option", return_value="Nothing to purge."):
        handle_data_tools_submit_work(_body("purge"), client, MagicMock(), {})
    text = client.views_update.call_args.kwargs["view"]["blocks"][0]["text"]["text"]
    assert text == "Nothing to purge."


def test_results_are_shown_and_sent_by_dm():
    client = MagicMock()
    with (
        _allow(),
        patch("handlers.data_tools.run_repair") as repair,
        patch("handlers._common.helpers.notify_user_dm") as dm,
    ):
        repair.return_value.count.return_value = 0
        handle_data_tools_submit_work(_body(), client, MagicMock(), {})
    client.views_update.assert_called_once()
    text = client.views_update.call_args.kwargs["view"]["blocks"][0]["text"]["text"]
    assert text == "No problems found."
    dm.assert_called_once()
    assert dm.call_args.args[2] == "No problems found."


def test_purge_description_fits_slack():
    for days in (1, 365):
        with patch("handlers.data_tools.soft_delete_retention_days", return_value=days):
            view = build_data_tools_view()
        text = view["blocks"][2]["element"]["options"][2]["description"]["text"]
        assert text == f"Remove uninstalled Workspaces and left Channels now, not in {days} days."
        assert len(text) <= 75


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
    with _allow(), patch("helpers.notifications.notify_admins_dm"):
        handle_data_tools_submit_work(_body("purge"), client, MagicMock(), {})
    assert DbManager.get_record(schemas.SyncChannel, id=old.id) is None
    assert DbManager.get_record(schemas.SyncChannel, id=live.id) is not None
    text = client.views_update.call_args.kwargs["view"]["blocks"][0]["text"]["text"]
    assert "Channels: 1" in text


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
    with _allow(), patch("helpers.notifications.notify_admins_dm") as dm:
        handle_data_tools_submit_work(_body("purge"), client, MagicMock(), {})
    dm.assert_not_called()
    assert DbManager.get_record(schemas.Workspace, id=workspace.team_id) is not None
    text = client.views_update.call_args.kwargs["view"]["blocks"][0]["text"]["text"]
    assert "Kept: 1 Workspaces still on an External Connection." in text


def test_purge_now_omits_a_day_count():
    from helpers.notifications import _retention_notice

    text = _retention_notice("Workspace A", 0, None, for_managers=True)
    assert "Data Retention Days" not in text
    assert "Action: Retention" in _retention_notice("Workspace A", 30, None, for_managers=True)
    assert "Data Retention Days: 30" in _retention_notice("Workspace A", 30, None, for_managers=True)
