"""One notice per action: import, uninstall, bot leave, channel delete, and unarchive."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("LOCAL_DEVELOPMENT", "true")
os.environ.setdefault("DATABASE_BACKEND", "sqlite")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")


@pytest.fixture
def real_db(tmp_path):
    import db as db_mod
    from db import initialize_database
    from helpers._cache import clear_all_caches

    url = f"sqlite:///{tmp_path / 'notice_once.db'}"
    old_engine = db_mod.GLOBAL_ENGINE
    old_session = db_mod.GLOBAL_SESSION
    old_schema = db_mod.GLOBAL_SCHEMA
    with patch.dict(os.environ, {"DATABASE_BACKEND": "sqlite", "DATABASE_URL": url}, clear=False):
        try:
            db_mod.GLOBAL_ENGINE = None
            db_mod.GLOBAL_SESSION = None
            db_mod.GLOBAL_SCHEMA = None
            initialize_database()
            from federation import core as federation_core

            federation_core._INSTANCE_ID = None
            federation_core._cached_private_key = None
            federation_core._cached_public_pem = None
            federation_core.get_or_create_instance_keypair()
            clear_all_caches()
            yield
        finally:
            if db_mod.GLOBAL_ENGINE is not None:
                db_mod.GLOBAL_ENGINE.dispose()
            db_mod.GLOBAL_ENGINE = old_engine
            db_mod.GLOBAL_SESSION = old_session
            db_mod.GLOBAL_SCHEMA = old_schema
            clear_all_caches()


def _workspace(team_id="TLOCAL", name="Workspace A"):
    from db import DbManager, schemas
    from federation.core import get_instance_id

    return DbManager.create_record(
        schemas.Workspace(team_id=team_id, workspace_name=name, instance_id=get_instance_id())
    )


def _sync_and_channel(workspace, *, channel_id="C1", status="active"):
    from db import DbManager, schemas

    now = datetime.now(UTC).replace(tzinfo=None)
    group = DbManager.create_record(
        schemas.WorkspaceGroup(name="Group", invite_code="GRP-1", status="active", created_at=now, uid="g-1")
    )
    sync = DbManager.create_record(schemas.Sync(title="general", group_id=group.id, sync_mode="group", uid="s-1"))
    row = DbManager.create_record(
        schemas.SyncChannel(
            sync_id=sync.id,
            workspace_id=workspace.id,
            channel_id=channel_id,
            channel_name="general",
            status=status,
            publishes=True,
            subscribes=True,
            created_at=now,
        )
    )
    return group, sync, row


def test_import_keeps_an_active_channel_and_pends_a_new_one(real_db):
    from db import DbManager, schemas
    from helpers.export_import import import_migration_data

    workspace = _workspace()
    group, sync, row = _sync_and_channel(workspace)
    now = datetime.now(UTC).replace(tzinfo=None)
    DbManager.create_record(
        schemas.WorkspaceGroupMember(
            group_id=group.id,
            workspace_id=workspace.id,
            status="active",
            role="member",
            joined_at=now,
        )
    )
    data = {
        "workspace": {"team_id": "TLOCAL"},
        "groups": [{"uid": group.uid, "name": "Group", "role": "member", "member_workspaces": []}],
        "syncs": [{"uid": sync.uid, "title": "general", "group_uid": group.uid, "sync_mode": "group"}],
        "sync_channels": [
            {"sync_uid": sync.uid, "team_id": "TLOCAL", "channel_id": "C1", "status": "active"},
            {"sync_uid": sync.uid, "team_id": "TLOCAL", "channel_id": "C2", "status": "active"},
            {"sync_uid": sync.uid, "team_id": "TLOCAL", "channel_id": "C3", "status": "paused"},
        ],
        "post_meta": {},
        "user_directory": [],
        "user_mappings": [],
    }
    import_migration_data(data, workspace.id, team_id_to_workspace_id={"TLOCAL": workspace.id})
    rows = {
        item.channel_id: item.status
        for item in DbManager.find_records(schemas.SyncChannel, [schemas.SyncChannel.workspace_id == workspace.id])
    }
    assert rows["C1"] == "active"
    assert rows["C2"] == "pending"
    assert rows["C3"] == "paused"
    assert row.status == "active" or rows["C1"] == "active"


def test_reinstall_holds_resume_while_a_peer_still_has_the_team(real_db):
    from helpers.workspace import resume_restored_sync_channels

    workspace = _workspace()
    _sync_and_channel(workspace)
    client = MagicMock()
    with (
        patch("helpers.workspace.peer_still_hosts_team", return_value=True),
        patch("helpers.sync_lifecycle.deliver_action_notice") as notice,
    ):
        counts = resume_restored_sync_channels(workspace, client=client, source="reinstall")
    notice.assert_not_called()
    assert counts["resumed"] == 0


def test_reinstall_posts_resume_once_when_no_peer_has_the_team(real_db):
    from helpers.workspace import resume_restored_sync_channels

    workspace = _workspace()
    _sync_and_channel(workspace)
    client = MagicMock()
    with (
        patch("helpers.workspace.peer_still_hosts_team", return_value=False),
        patch("helpers.conversations.inspect_bot_channel_access", return_value=(False, True)),
        patch("helpers.sync_lifecycle.deliver_action_notice") as notice,
    ):
        counts = resume_restored_sync_channels(workspace, client=client, source="reinstall")
    assert counts["resumed"] == 1
    assert notice.call_count == 1


def test_uninstall_notifies_once(real_db):
    from helpers.workspace import _soft_delete_uninstalled_workspace

    workspace = _workspace()
    _sync_and_channel(workspace)
    with (
        patch("helpers.sync_lifecycle.deliver_action_notice") as pause,
        patch("helpers.notifications.notify_primary_action") as uninstall,
        patch("helpers.workspace_kind.convert_workspace_to_stub", return_value="missing"),
    ):
        _soft_delete_uninstalled_workspace("TLOCAL")
        _soft_delete_uninstalled_workspace("TLOCAL")
    assert pause.call_count == 1
    assert uninstall.call_count == 1


def test_channel_deleted_posts_once(real_db):
    from helpers.sync_lifecycle import purge_slack_deleted_channel

    workspace = _workspace()
    _sync_and_channel(workspace)
    with (
        patch("helpers.sync_cleanup.purge_sync_channels"),
        patch("helpers.sync_lifecycle.deliver_action_notice") as notice,
        patch("helpers.leave_sync._replicate_one_remove"),
    ):
        purge_slack_deleted_channel(workspace, "C1")
        purge_slack_deleted_channel(workspace, "C1")
    assert notice.call_count == 1


def test_pause_from_a_kick_posts_once(real_db):
    from helpers.sync_lifecycle import pause_sync_channel

    workspace = _workspace()
    _group, _sync, row = _sync_and_channel(workspace)
    with patch("helpers.sync_lifecycle.deliver_action_notice") as notice:
        assert pause_sync_channel(row, via="kick") is True
        assert pause_sync_channel(row, via="kick") is False
    assert notice.call_count == 1


def test_inbound_remove_of_a_missing_row_does_not_post(real_db):
    from federation.api import handle_sync_channel_remove

    workspace = _workspace()
    _group, sync, _row = _sync_and_channel(workspace)
    fed = MagicMock(instance_id="peer")
    with (
        patch("federation.api._workspace_for_replicated_team", return_value=workspace),
        patch("federation.api.is_local_workspace", return_value=True),
        patch("helpers.leave_sync.post_to_other_sync_channels") as posted,
        patch("helpers.sync_lifecycle.post_to_other_sync_channels") as posted_too,
    ):
        status, _resp = handle_sync_channel_remove(
            {
                "sync_uid": sync.uid,
                "team_id": "TLOCAL",
                "channel_id": "CMISSING",
                "notice": ":wave: left",
            },
            fed,
        )
    assert status == 200
    posted.assert_not_called()
    posted_too.assert_not_called()


def test_import_with_no_peer_resumes():
    from types import SimpleNamespace

    from helpers.workspace import consider_migration_release, peer_still_hosts_team

    workspace = SimpleNamespace(id=1, team_id="T1", workspace_name="Workspace A")
    with (
        patch("helpers.workspace.DbManager.get_record", return_value=workspace),
        patch("helpers.workspace_kind.is_local_workspace", return_value=True),
        patch("helpers.workspace.DbManager.find_records", return_value=[]),
        patch("helpers.workspace.restore_migrated_workspace") as restore,
    ):
        assert consider_migration_release("T1") == ("resumed", None)
        restore.assert_called_once_with("T1")
        assert peer_still_hosts_team("T1") is False


def test_import_stays_waiting_when_a_peer_still_has_the_team():
    from types import SimpleNamespace

    from helpers.workspace import consider_migration_release, migration_peer_hold, peer_still_hosts_team

    workspace = SimpleNamespace(id=1, team_id="T1", workspace_name="Workspace A")
    quiet = SimpleNamespace(instance_id="a" * 64, private_key_encrypted=None, status="active")
    live = SimpleNamespace(instance_id="b" * 64, private_key_encrypted=None, status="active")

    def push(peer, payload):
        assert {"team_id": "T1", "name": "Workspace A"} in payload["workspaces"]
        if peer is quiet:
            return None
        return {"results": {"T1": "refused_live"}}

    with (
        patch("helpers.workspace.DbManager.get_record", return_value=workspace),
        patch("helpers.workspace_kind.is_local_workspace", return_value=True),
        patch("helpers.workspace_kind.peer_is_trusted", return_value=True),
        patch("helpers.workspace.DbManager.find_records", return_value=[quiet, live]),
        patch("federation.core.teams_allowlist_payload", return_value={"workspaces": []}),
        patch("federation.core.push_teams", side_effect=push),
        patch("helpers.workspace.restore_migrated_workspace") as restore,
    ):
        assert migration_peer_hold("T1") == ("still_hosted", live)
        assert consider_migration_release("T1") == ("waiting_for_uninstall", live)
        restore.assert_not_called()
        assert peer_still_hosts_team("T1") is True


def test_import_resumes_when_every_peer_has_released_the_team():
    from types import SimpleNamespace

    from helpers.workspace import consider_migration_release, migration_peer_hold, peer_still_hosts_team

    workspace = SimpleNamespace(id=1, team_id="T1", workspace_name="Workspace A")
    peer = SimpleNamespace(instance_id="b" * 64, private_key_encrypted=None, status="active")
    for answer in ("converted", "already_stub", "missing", "ensured"):
        with (
            patch("helpers.workspace.DbManager.get_record", return_value=workspace),
            patch("helpers.workspace_kind.is_local_workspace", return_value=True),
            patch("helpers.workspace_kind.peer_is_trusted", return_value=True),
            patch("helpers.workspace.DbManager.find_records", return_value=[peer]),
            patch("federation.core.teams_allowlist_payload", return_value={"workspaces": []}),
            patch("federation.core.push_teams", return_value={"results": {"T1": answer}}),
            patch("helpers.workspace.restore_migrated_workspace") as restore,
        ):
            assert migration_peer_hold("T1") == ("released", None)
            assert consider_migration_release("T1") == ("resumed", None)
            restore.assert_called_once_with("T1")
            assert peer_still_hosts_team("T1") is False


def test_import_stays_pending_when_a_peer_cannot_be_reached():
    from types import SimpleNamespace

    from helpers.workspace import consider_migration_release, peer_still_hosts_team

    workspace = SimpleNamespace(id=1, team_id="T1", workspace_name="Workspace A")
    peer = SimpleNamespace(instance_id="c" * 64, private_key_encrypted=None, status="active")
    with (
        patch("helpers.workspace.DbManager.get_record", return_value=workspace),
        patch("helpers.workspace_kind.is_local_workspace", return_value=True),
        patch("helpers.workspace_kind.peer_is_trusted", return_value=True),
        patch("helpers.workspace.DbManager.find_records", return_value=[peer]),
        patch("federation.core.teams_allowlist_payload", return_value={"workspaces": []}),
        patch("federation.core.push_teams", return_value=None),
        patch("helpers.workspace.restore_migrated_workspace") as restore,
    ):
        assert consider_migration_release("T1") == ("unreachable", peer)
        restore.assert_not_called()
        assert peer_still_hosts_team("T1") is True


def test_unarchive_without_the_bot_tells_the_other_channels(real_db):
    from helpers.sync_lifecycle import resume_unarchived_channel

    workspace = _workspace()
    _sync_and_channel(workspace, status="paused")
    with (
        patch("helpers.sync_lifecycle.get_bot_token", return_value="xoxb-test"),
        patch("helpers.sync_lifecycle.inspect_bot_channel_access", return_value=(False, False)),
        patch("helpers.sync_lifecycle.deliver_action_notice") as notice,
    ):
        resume_unarchived_channel(workspace, "C1")
    notice.assert_called_once()
    assert notice.call_args.kwargs.get("announce", True) is True
    assert notice.call_args.kwargs["post_in_channel"] is False
