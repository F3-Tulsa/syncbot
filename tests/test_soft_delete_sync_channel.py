"""Soft-deleting one workspace's Channel Sync: the database pass and the Slack pass."""

import os
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from slack_sdk.errors import SlackApiError

os.environ.setdefault("DATABASE_HOST", "localhost")
os.environ.setdefault("DATABASE_USER", "root")
os.environ.setdefault("DATABASE_PASSWORD", "test")
os.environ.setdefault("DATABASE_SCHEMA", "syncbot")
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-0-0")

from db import DbManager, schemas  # noqa: E402
from helpers.leave_sync import (  # noqa: E402
    _leave_channel_id,
    soft_delete_sync_channel,
    soft_delete_sync_channels,
)

PEER_INSTANCE_ID = "f" * 64


@pytest.fixture
def real_db(tmp_path):
    import db as db_mod
    from db import initialize_database

    url = f"sqlite:///{tmp_path / 'soft_delete.db'}"
    old_engine = db_mod.GLOBAL_ENGINE
    old_schema = db_mod.GLOBAL_SCHEMA
    with patch.dict(os.environ, {"DATABASE_BACKEND": "sqlite", "DATABASE_URL": url}, clear=False):
        try:
            db_mod.GLOBAL_ENGINE = None
            db_mod.GLOBAL_SCHEMA = None
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


def _now():
    return datetime.now(UTC).replace(tzinfo=None)


def _workspace(team_id="T1", name="Workspace A", instance_id=None):
    from federation.core import get_instance_id

    return DbManager.create_record(
        schemas.Workspace(team_id=team_id, workspace_name=name, instance_id=instance_id or get_instance_id())
    )


def _peer_instance():
    return DbManager.create_record(
        schemas.Instance(
            instance_id=PEER_INSTANCE_ID,
            public_key="-----BEGIN PUBLIC KEY-----\npeer\n-----END PUBLIC KEY-----\n",
            webhook_url="https://peer.example/api/federation",
            status="active",
            trust_status="trusted",
            name="Peer",
            created_at=_now(),
        )
    )


def _group():
    return DbManager.create_record(
        schemas.WorkspaceGroup(
            name="Group", invite_code=uuid4().hex[:12], status="active", created_at=_now(), uid=str(uuid4())
        )
    )


def _sync(title="general", group_id=None):
    return DbManager.create_record(schemas.Sync(title=title, sync_mode="group", group_id=group_id, uid=str(uuid4())))


def _channel(
    sync,
    workspace,
    *,
    channel_id="C1",
    channel_name="general",
    user_id="UADA",
    status="active",
    publishes=True,
    is_private=None,
):
    return DbManager.create_record(
        schemas.SyncChannel(
            sync_id=sync.id,
            workspace_id=workspace.id,
            channel_id=channel_id,
            channel_name=channel_name,
            status=status,
            publishes=publishes,
            subscribes=True,
            synced_by_user_id=user_id,
            is_private=is_private,
            created_at=_now(),
        )
    )


def _reload(row):
    return DbManager.get_record(schemas.SyncChannel, id=row.id)


@contextmanager
def _slack_pass(**overrides):
    """Patch every Slack-facing collaborator of the Slack pass where leave_sync looks it up."""
    values = {
        "get_bot_token": "xoxb-test",
        "notify_channel_or_managers": True,
        "invalidate_home_tab_caches_for_team": None,
        "invalidate_group_member_homes": None,
        "_replicate_one_remove": None,
        "get_user_info": ("Ada Lovelace", None),
        "soft_delete_retention_days": 30,
    }
    values.update(overrides)
    with ExitStack() as stack:
        mocks = {
            name: stack.enter_context(patch(f"helpers.leave_sync.{name}", return_value=value))
            for name, value in values.items()
        }
        mocks["WebClient"] = stack.enter_context(patch("helpers.leave_sync.WebClient"))
        others = MagicMock()
        stack.enter_context(patch("helpers.leave_sync.post_to_other_sync_channels", others))
        stack.enter_context(patch("helpers.sync_lifecycle.post_to_other_sync_channels", others))
        mocks["post_to_other_sync_channels"] = others
        notice = MagicMock(return_value=values["notify_channel_or_managers"])
        stack.enter_context(patch("helpers.leave_sync.notify_channel_or_managers", notice))
        stack.enter_context(patch("helpers.sync_lifecycle.notify_channel_or_managers", notice))
        mocks["notify_channel_or_managers"] = notice
        managers = MagicMock()
        stack.enter_context(patch("helpers.sync_lifecycle.notify_managers", managers))
        stack.enter_context(patch("helpers.notifications.notify_managers", managers))
        mocks["notify_managers"] = managers
        stack.enter_context(patch("helpers.sync_lifecycle.get_bot_token", return_value=values["get_bot_token"]))
        stack.enter_context(patch("helpers.notifications.get_bot_token", return_value=values["get_bot_token"]))
        yield mocks


def _other_channel_texts(mocks):
    return {call.args[1]: call.args[2] for call in mocks["post_to_other_sync_channels"].call_args_list}


class TestSoftDeleteSyncChannel:
    def test_marks_the_row_deleted_and_clears_the_person(self, real_db):
        workspace = _workspace()
        row = _channel(_sync(), workspace, user_id="UADA")
        with patch("helpers.leave_sync._replicate_one_remove"):
            assert soft_delete_sync_channel(row, "left_sync") is True
        saved = _reload(row)
        assert saved.deleted_at is not None
        assert saved.status == "deleted"
        assert saved.synced_by_user_id is None
        assert saved.is_private is None
        assert row.deleted_at == saved.deleted_at
        assert row.status == "deleted"
        assert row.synced_by_user_id is None

    def test_writes_is_private_when_given(self, real_db):
        workspace = _workspace()
        row = _channel(_sync(), workspace)
        with patch("helpers.leave_sync._replicate_one_remove"):
            soft_delete_sync_channel(row, "became_private", is_private=True)
        assert _reload(row).is_private is True
        assert row.is_private is True

    def test_invalidates_fan_out_for_the_sync(self, real_db):
        workspace = _workspace()
        sync = _sync()
        row = _channel(sync, workspace)
        with (
            patch("helpers.leave_sync._replicate_one_remove"),
            patch("helpers.leave_sync.invalidate_sync_fanout_for_syncs") as fanout,
        ):
            soft_delete_sync_channel(row, "left_sync")
        fanout.assert_called_once_with([sync.id])

    def test_second_call_returns_false_and_does_not_replicate_again(self, real_db):
        workspace = _workspace()
        row = _channel(_sync(group_id=_group().id), workspace)
        with patch("helpers.leave_sync._replicate_one_remove") as replicate:
            assert soft_delete_sync_channel(row, "left_sync") is True
            assert soft_delete_sync_channel(row, "left_sync") is False
            assert soft_delete_sync_channel(_reload(row), "left_sync") is False
        assert replicate.call_count == 0

    def test_local_remove_replicates_from_the_wrapper(self, real_db):
        workspace = _workspace()
        group = _group()
        sync = _sync(group_id=group.id)
        row = _channel(sync, workspace)
        with _slack_pass(), patch("helpers.leave_sync._replicate_one_remove") as replicate:
            soft_delete_sync_channels(workspace, row.channel_id, "manager_left")
        replicate.assert_called_once()
        replicated_sync, group_id, replicated_workspace, replicated_row = replicate.call_args.args
        assert replicated_sync.id == sync.id
        assert group_id == group.id
        assert replicated_workspace.id == workspace.id
        assert replicated_row.id == row.id
        assert replicate.call_args.kwargs["reason"] == "manager_left"
        assert replicate.call_args.kwargs["notice"]

    def test_stub_workspace_does_not_replicate(self, real_db):
        _peer_instance()
        stub = _workspace(team_id="T2", name="Workspace B", instance_id=PEER_INSTANCE_ID)
        row = _channel(_sync(group_id=_group().id), stub)
        with patch("helpers.leave_sync._replicate_one_remove") as replicate:
            assert soft_delete_sync_channel(row, "remote_ended") is True
        replicate.assert_not_called()
        assert _reload(row).status == "deleted"

    def test_wrapper_replicate_false_skips_replication_for_a_local_row(self, real_db):
        workspace = _workspace()
        row = _channel(_sync(group_id=_group().id), workspace)
        with _slack_pass() as mocks:
            assert soft_delete_sync_channels(workspace, "C1", "left_sync", replicate=False) == 1
        mocks["_replicate_one_remove"].assert_not_called()
        assert _reload(row).status == "deleted"

    def test_missing_or_unsaved_row_returns_false(self):
        with patch("helpers.leave_sync._replicate_one_remove") as replicate:
            assert soft_delete_sync_channel(None, "left_sync") is False
            assert soft_delete_sync_channel(SimpleNamespace(id=None, deleted_at=None), "left_sync") is False
        replicate.assert_not_called()


class TestSoftDeleteSyncChannels:
    def test_returns_zero_when_nothing_is_live(self, real_db):
        workspace = _workspace()
        with _slack_pass() as mocks:
            assert soft_delete_sync_channels(workspace, "C1", "left_sync") == 0
        row = _channel(_sync(), workspace)
        with patch("helpers.leave_sync._replicate_one_remove"):
            soft_delete_sync_channel(row, "left_sync")
        with _slack_pass() as mocks:
            assert soft_delete_sync_channels(workspace, "C1", "left_sync") == 0
        mocks["notify_channel_or_managers"].assert_not_called()
        mocks["post_to_other_sync_channels"].assert_not_called()
        assert soft_delete_sync_channels(None, "C1", "left_sync") == 0
        assert soft_delete_sync_channels(workspace, "", "left_sync") == 0

    def test_filters_by_the_person_who_runs_the_row(self, real_db):
        workspace = _workspace()
        mine = _channel(_sync("general"), workspace, user_id="UADA")
        other = _channel(_sync("random"), workspace, user_id="UBOB")
        with _slack_pass() as mocks:
            assert soft_delete_sync_channels(workspace, "C1", "manager_left", synced_by_user_id="UADA") == 1
        assert _reload(mine).status == "deleted"
        assert _reload(other).status == "active"
        assert _reload(other).synced_by_user_id == "UBOB"
        assert "Ada Lovelace" in mocks["notify_channel_or_managers"].call_args.args[2]

    def test_filters_by_sync_ids(self, real_db):
        workspace = _workspace()
        first = _sync("general")
        second = _sync("random")
        kept = _channel(first, workspace)
        ended = _channel(second, workspace)
        with _slack_pass() as mocks:
            assert soft_delete_sync_channels(workspace, "C1", "left_sync", sync_ids={second.id}) == 1
        assert _reload(kept).status == "active"
        assert _reload(ended).status == "deleted"
        assert [call.args[1] for call in mocks["post_to_other_sync_channels"].call_args_list] == [second.id]

    def test_explicit_exclude_user_id_is_forwarded_over_the_default(self, real_db):
        workspace = _workspace()
        _channel(_sync(), workspace, user_id="UADA")
        with _slack_pass() as mocks:
            soft_delete_sync_channels(workspace, "C1", "left_sync", synced_by_user_id="UADA", person="Ada Lovelace")
        notice = mocks["notify_channel_or_managers"].call_args.args[2]
        assert "Acting Manager: Ada Lovelace (Workspace A)" in notice
        assert "Workspaces in Sync:" in notice

    def test_person_is_resolved_through_get_user_info_when_only_the_id_is_known(self, real_db):
        workspace = _workspace()
        _channel(_sync(), workspace, user_id="UADA")
        with _slack_pass() as mocks:
            soft_delete_sync_channels(workspace, "C1", "manager_left", synced_by_user_id="UADA")
        mocks["get_user_info"].assert_called_once()
        assert mocks["get_user_info"].call_args.args[0] is mocks["WebClient"].return_value
        assert mocks["get_user_info"].call_args.args[1] == "UADA"
        notice = mocks["notify_channel_or_managers"].call_args.args[2]
        assert "Acting Manager: Ada Lovelace (Workspace A)" in notice
        assert "Action: Leave" in notice
        detail = mocks["notify_managers"].call_args.args[1]
        assert "Data Retention Days: 30" in detail
        assert "Home tab" not in notice

    def test_person_name_given_skips_get_user_info(self, real_db):
        workspace = _workspace()
        _channel(_sync(), workspace, user_id="UADA")
        with _slack_pass() as mocks:
            soft_delete_sync_channels(workspace, "C1", "manager_left", synced_by_user_id="UADA", person="Grace Hopper")
        mocks["get_user_info"].assert_not_called()
        assert "Grace Hopper" in mocks["notify_channel_or_managers"].call_args.args[2]

    def test_no_person_id_means_no_lookup_and_a_generic_manager(self, real_db):
        workspace = _workspace()
        _channel(_sync(), workspace, user_id="UADA")
        with _slack_pass() as mocks:
            soft_delete_sync_channels(workspace, "C1", "left_sync")
        mocks["get_user_info"].assert_not_called()
        assert "Acting Manager: SyncBot (Workspace A)" in mocks["notify_channel_or_managers"].call_args.args[2]

    def test_no_publisher_note_only_for_syncs_this_workspace_last_published(self, real_db):
        workspace = _workspace()
        twin_workspace = _workspace(team_id="T2", name="Workspace B")
        last = _sync("general")
        shared = _sync("random")
        _channel(last, workspace)
        _channel(last, twin_workspace, channel_id="C2", publishes=False)
        _channel(shared, workspace)
        _channel(shared, twin_workspace, channel_id="C2", publishes=True)
        with _slack_pass() as mocks:
            assert soft_delete_sync_channels(workspace, "C1", "left_sync") == 2
        texts = _other_channel_texts(mocks)
        assert set(texts) == {last.id, shared.id}
        assert texts[last.id].startswith(":wave: *SyncBot Action Notice*")
        assert "no publisher" not in texts[last.id]
        assert "no publisher" not in texts[shared.id]
        for text in texts.values():
            assert "Action: Leave" in text
            assert "Workspace A" in text
            assert "Workspaces in Sync:" in text
        assert mocks["post_to_other_sync_channels"].call_count == 2
        assert all(call.args[0].id == workspace.id for call in mocks["post_to_other_sync_channels"].call_args_list)

    def test_two_syncs_on_one_channel_get_one_notice_with_both_titles_when_syncbot_stays(self, real_db):
        workspace = _workspace()
        first = _sync("general")
        second = _sync("random")
        third = _sync("announcements")
        _channel(first, workspace)
        _channel(second, workspace)
        staying = _channel(third, workspace)
        with _slack_pass() as mocks:
            assert soft_delete_sync_channels(workspace, "C1", "left_sync", sync_ids={first.id, second.id}) == 2
        post = mocks["notify_channel_or_managers"]
        assert post.call_count == 2
        notices = [call.args[2] for call in post.call_args_list]
        assert all("Action: Leave" in notice for notice in notices)
        assert all("Data Retention Days" not in notice for notice in notices)
        assert all("announcements" not in notice for notice in notices)
        mocks["WebClient"].return_value.conversations_leave.assert_not_called()
        assert _reload(staying).status == "active"

    def test_leaves_and_uses_the_left_template_when_the_only_other_row_is_paused(self, real_db):
        workspace = _workspace()
        ended = _channel(_sync("general"), workspace)
        paused = _channel(_sync("random"), workspace, status="paused")
        with _slack_pass() as mocks, patch("helpers.leave_sync.log_info") as log:
            assert soft_delete_sync_channels(workspace, "C1", "left_sync", sync_ids={ended.sync_id}) == 1
        notice = mocks["notify_channel_or_managers"].call_args.args[2]
        assert notice.startswith(":wave: *SyncBot Action Notice*")
        assert "Action: Leave" in notice
        assert "Data Retention Days: 30" in mocks["notify_managers"].call_args.args[1]
        mocks["WebClient"].return_value.conversations_leave.assert_called_once_with(channel="C1")
        assert ("sync_channel_left_channel",) in [call.args for call in log.call_args_list]
        left = next(call for call in log.call_args_list if call.args == ("sync_channel_left_channel",))
        assert left.kwargs == {"channel_id": "C1", "workspace_id": workspace.id}
        assert _reload(paused).status == "paused"

    def test_channel_name_comes_from_a_row_before_slack(self, real_db):
        workspace = _workspace()
        _channel(_sync(), workspace, channel_name="#general")
        with _slack_pass() as mocks, patch("helpers.sync_lifecycle.lookup_channel_name") as lookup:
            soft_delete_sync_channels(workspace, "C1", "left_sync")
        lookup.assert_not_called()
        detail = mocks["notify_managers"].call_args.args[1]
        assert "Affected Channel: #general" in detail
        assert "Workspaces in Sync:" in mocks["notify_channel_or_managers"].call_args.args[2]

    def test_channel_name_falls_back_to_lookup_when_rows_have_none(self, real_db):
        workspace = _workspace()
        _channel(_sync(), workspace, channel_name=None)
        with (
            _slack_pass() as mocks,
            patch("helpers.sync_lifecycle.lookup_channel_name", return_value="random") as lookup,
        ):
            soft_delete_sync_channels(workspace, "C1", "left_sync")
        lookup.assert_called()
        assert lookup.call_args.args[0] == "C1"
        assert "Affected Channel: #random" in mocks["notify_managers"].call_args.args[1]

    def test_invalidates_this_home_and_the_group_members_homes(self, real_db):
        workspace = _workspace()
        group = _group()
        _channel(_sync(group_id=group.id), workspace)
        with _slack_pass() as mocks:
            soft_delete_sync_channels(workspace, "C1", "left_sync")
        mocks["invalidate_home_tab_caches_for_team"].assert_called_once_with("T1")
        mocks["invalidate_group_member_homes"].assert_called_once_with(group.id, exclude_workspace_id=workspace.id)

    def test_manager_dm_needs_a_landed_post_and_a_bot_token(self, real_db):
        workspace = _workspace()
        _channel(_sync(), workspace)
        with _slack_pass(notify_channel_or_managers=False) as mocks:
            assert soft_delete_sync_channels(workspace, "C1", "left_sync") == 1
        mocks["notify_channel_or_managers"].assert_called_once()
        mocks["WebClient"].return_value.conversations_leave.assert_called_once_with(channel="C1")

        _channel(_sync("random"), workspace)
        with _slack_pass(get_bot_token=None) as mocks:
            assert soft_delete_sync_channels(workspace, "C1", "left_sync", synced_by_user_id="UADA") == 1
        mocks["get_user_info"].assert_not_called()
        mocks["WebClient"].return_value.conversations_leave.assert_not_called()
        mocks["notify_channel_or_managers"].assert_called_once()

    def test_dm_text_uses_the_unreachable_template_and_the_notice_the_reason(self, real_db):
        workspace = _workspace()
        _channel(_sync(), workspace)
        with _slack_pass() as mocks, patch("helpers.conversations.authorize_url", return_value=None):
            soft_delete_sync_channels(workspace, "C1", "became_private", is_private=True)
        post = mocks["notify_channel_or_managers"]
        assert post.call_args.args[0].id == workspace.id
        assert post.call_args.args[1] == "C1"
        assert "Action: Privacy" in post.call_args.args[2]
        detail = mocks["notify_managers"].call_args.args[1]
        assert "Data Retention Days: 30" in detail
        assert "Note: The Channel is now private." in detail
        assert "Workspaces in Sync:" in post.call_args.args[2]

    def test_is_private_and_replicate_reach_every_row(self, real_db):
        workspace = _workspace()
        first = _channel(_sync("general"), workspace)
        second = _channel(_sync("random"), workspace)
        with _slack_pass() as mocks:
            assert soft_delete_sync_channels(workspace, "C1", "became_public", is_private=False, replicate=False) == 2
        assert _reload(first).is_private is False
        assert _reload(second).is_private is False
        mocks["_replicate_one_remove"].assert_not_called()

    def test_each_marked_row_replicates_with_the_reason(self, real_db):
        workspace = _workspace()
        group = _group()
        sync = _sync(group_id=group.id)
        row = _channel(sync, workspace)
        with _slack_pass() as mocks:
            soft_delete_sync_channels(workspace, "C1", "left_group", acting_workspace="Workspace B")
        replicate = mocks["_replicate_one_remove"]
        replicate.assert_called_once()
        assert replicate.call_args.args[0].id == sync.id
        assert replicate.call_args.args[3].id == row.id
        assert replicate.call_args.kwargs["reason"] == "left_group"
        assert "Acting Manager: Workspace B" in replicate.call_args.kwargs["notice"]
        assert "Data Retention Days" not in replicate.call_args.kwargs["notice"]
        assert "Action: Leave" in mocks["notify_channel_or_managers"].call_args.args[2]


class TestLeaveChannelId:
    def test_leave_ok_errors_log_at_info_and_others_warn(self):
        workspace = SimpleNamespace(id=1, team_id="T1")
        with (
            patch("helpers.leave_sync.WebClient") as web_client,
            patch("helpers.leave_sync.log_info") as info,
            patch("helpers.leave_sync.log_warning") as warning,
        ):
            web_client.return_value.conversations_leave.side_effect = SlackApiError(
                "is_archived", {"ok": False, "error": "is_archived"}
            )
            _leave_channel_id(workspace, "C1", token="xoxb-test")
            web_client.return_value.conversations_leave.side_effect = SlackApiError(
                "ratelimited", {"ok": False, "error": "ratelimited"}
            )
            _leave_channel_id(workspace, "C1", token="xoxb-test")
        assert info.call_args.args == ("channel_leave_failed",)
        assert info.call_args.kwargs["channel_id"] == "C1"
        assert warning.call_args.args == ("channel_leave_failed",)

    def test_no_token_means_no_slack_call(self):
        with (
            patch("helpers.leave_sync.get_bot_token", return_value=None),
            patch("helpers.leave_sync.WebClient") as web_client,
        ):
            _leave_channel_id(SimpleNamespace(id=1, team_id="T1"), "C1")
        web_client.assert_not_called()


class TestNotifyChannelOrManagers:
    def test_falls_back_to_the_managers_dm_when_the_post_does_not_land(self):
        from helpers.notifications import notify_channel_or_managers

        workspace = SimpleNamespace(id=1, team_id="T1")
        with (
            patch("helpers.notifications.get_bot_token", return_value="xoxb-test"),
            patch("helpers.notifications.notify_synced_channels", return_value=0) as post,
            patch("helpers.notifications.notify_managers") as managers,
        ):
            landed = notify_channel_or_managers(workspace, "C1", "channel text")
        assert landed is False
        assert post.call_args.args[1:] == (["C1"], "channel text")
        managers.assert_called_once()
        assert managers.call_args.args[1] == "channel text"
        assert managers.call_args.kwargs["team_id"] == "T1"

    def test_dm_falls_back_to_the_channel_text_without_dm_text(self):
        from helpers.notifications import notify_channel_or_managers

        with (
            patch("helpers.notifications.get_bot_token", return_value="xoxb-test"),
            patch("helpers.notifications.notify_synced_channels", return_value=0),
            patch("helpers.notifications.notify_managers") as managers,
        ):
            assert notify_channel_or_managers(SimpleNamespace(id=1, team_id="T1"), "C1", "channel text") is False
        assert managers.call_args.args[1] == "channel text"

    def test_a_landed_post_does_not_dm(self):
        from helpers.notifications import notify_channel_or_managers

        with (
            patch("helpers.notifications.get_bot_token", return_value="xoxb-test"),
            patch("helpers.notifications.notify_synced_channels", return_value=1),
            patch("helpers.notifications.notify_managers") as managers,
        ):
            assert notify_channel_or_managers(SimpleNamespace(id=1, team_id="T1"), "C1", "text") is True
        managers.assert_not_called()

    def test_no_token_or_channel_posts_nothing(self):
        from helpers.notifications import notify_channel_or_managers

        with (
            patch("helpers.workspace.get_bot_token", return_value=None),
            patch("helpers.notifications.notify_synced_channels") as post,
            patch("helpers.notifications.notify_managers") as managers,
        ):
            assert notify_channel_or_managers(SimpleNamespace(id=1, team_id="T1"), "C1", "text") is False
            assert notify_channel_or_managers(None, "C1", "text") is False
        post.assert_not_called()
        managers.assert_not_called()
        with (
            patch("helpers.notifications.get_bot_token", return_value="xoxb-test"),
            patch("helpers.notifications.notify_synced_channels") as post,
            patch("helpers.notifications.notify_managers") as managers,
        ):
            assert notify_channel_or_managers(SimpleNamespace(id=1, team_id="T1"), None, "text") is False
        post.assert_not_called()
        managers.assert_not_called()


class TestNotifyTwinSyncChannels:
    def test_a_missed_post_is_logged_and_not_sent_as_a_dm(self):
        from helpers.sync_lifecycle import post_to_other_sync_channels

        origin = SimpleNamespace(id=1, team_id="T1")
        other = SimpleNamespace(id=2, team_id="T2")
        twin = SimpleNamespace(id=9, sync_id=5, workspace_id=2, channel_id="C2", deleted_at=None, status="active")
        with (
            patch("helpers.sync_lifecycle.DbManager.find_records", return_value=[twin]),
            patch("helpers.sync_lifecycle.get_workspace_by_id", return_value=other),
            patch("helpers.sync_lifecycle.get_bot_token", return_value="xoxb-test"),
            patch("helpers.sync_lifecycle.notify_synced_channels", return_value=0) as posted,
            patch("helpers.notifications.notify_managers") as managers,
        ):
            post_to_other_sync_channels(origin, 5, "joined")
        posted.assert_called_once()
        managers.assert_not_called()

    def test_a_workspace_without_a_bot_token_is_skipped(self):
        from helpers.sync_lifecycle import post_to_other_sync_channels

        origin = SimpleNamespace(id=1, team_id="T1")
        stub = SimpleNamespace(id=2, team_id="T2")
        twin = SimpleNamespace(id=9, sync_id=5, workspace_id=2, channel_id="C2", deleted_at=None)
        with (
            patch("helpers.sync_lifecycle.DbManager.find_records", return_value=[twin]),
            patch("helpers.sync_lifecycle.get_workspace_by_id", return_value=stub),
            patch("helpers.sync_lifecycle.get_bot_token", return_value=None),
            patch("helpers.notifications.notify_synced_channels") as post,
            patch("helpers.notifications.notify_managers") as managers,
        ):
            post_to_other_sync_channels(origin, 5, "joined")
        post.assert_not_called()
        managers.assert_not_called()


class TestReviveSyncChannel:
    def test_revive_keeps_the_row_and_its_history(self, real_db):
        from decimal import Decimal

        from helpers.sync_lifecycle import revive_sync_channel

        workspace = _workspace()
        sync = _sync(group_id=_group().id)
        row = _channel(sync, workspace, status="deleted")
        DbManager.update_records(
            schemas.SyncChannel,
            [schemas.SyncChannel.id == row.id],
            {schemas.SyncChannel.deleted_at: _now(), schemas.SyncChannel.synced_by_user_id: "UOLD"},
        )
        DbManager.create_record(
            schemas.PostMeta(post_id="P-revive", sync_channel_id=row.id, ts=Decimal("1700000000.000100"))
        )
        row = DbManager.get_record(schemas.SyncChannel, id=row.id)
        with patch("helpers.sync_lifecycle.ensure_bot_in_conversation", return_value=True):
            assert (
                revive_sync_channel(
                    row,
                    client=MagicMock(),
                    workspace=workspace,
                    acting_user_id="UADA",
                    publishes=True,
                    subscribes=False,
                    reaction_style="direct",
                    via="join",
                )
                is True
            )
        saved = DbManager.get_record(schemas.SyncChannel, id=row.id)
        assert saved.status == "active"
        assert saved.deleted_at is None
        assert saved.publishes is True
        assert saved.subscribes is False
        assert saved.reaction_style == "direct"
        assert saved.is_private is True
        assert saved.synced_by_user_id == "UADA"
        assert DbManager.find_records(schemas.PostMeta, [schemas.PostMeta.post_id == "P-revive"])

    def test_failed_invite_puts_the_deleted_row_back(self, real_db):
        from helpers.conversations import ConversationAccessError
        from helpers.sync_lifecycle import revive_sync_channel

        workspace = _workspace()
        sync = _sync(group_id=_group().id)
        ended = _now()
        row = _channel(sync, workspace, status="deleted")
        DbManager.update_records(
            schemas.SyncChannel,
            [schemas.SyncChannel.id == row.id],
            {schemas.SyncChannel.deleted_at: ended},
        )
        row = DbManager.get_record(schemas.SyncChannel, id=row.id)
        with (
            patch(
                "helpers.sync_lifecycle.ensure_bot_in_conversation",
                side_effect=ConversationAccessError("no dice"),
            ),
            patch("helpers.sync_lifecycle.notify_user_dm") as dm,
        ):
            assert (
                revive_sync_channel(
                    row,
                    client=MagicMock(),
                    workspace=workspace,
                    acting_user_id="UADA",
                    publishes=True,
                    subscribes=True,
                    reaction_style=None,
                    via="create",
                )
                is None
            )
        saved = DbManager.get_record(schemas.SyncChannel, id=row.id)
        assert saved.status == "deleted"
        assert saved.deleted_at == ended
        assert saved.synced_by_user_id is None
        dm.assert_called_once()
        assert "no dice" in dm.call_args.args[2]

    def test_join_reuses_the_newest_deleted_row(self, real_db):
        from datetime import timedelta

        from handlers.channel_sync import handle_join_sync_submit

        workspace = _workspace()
        sync = _sync(group_id=_group().id)
        older = _channel(sync, workspace, status="deleted")
        newer = _channel(sync, workspace, status="deleted")
        DbManager.update_records(
            schemas.SyncChannel,
            [schemas.SyncChannel.id == older.id],
            {schemas.SyncChannel.deleted_at: _now() - timedelta(days=2)},
        )
        DbManager.update_records(
            schemas.SyncChannel,
            [schemas.SyncChannel.id == newer.id],
            {schemas.SyncChannel.deleted_at: _now()},
        )
        with (
            patch("handlers.channel_sync._get_authorized_workspace", return_value=("UADA", workspace)),
            patch("handlers.channel_sync._parse_private_metadata", return_value={"sync_id": sync.id}),
            patch("handlers.channel_sync._get_selected_conversation_or_option", return_value="C1"),
            patch("handlers.channel_sync._validate_channel_selection", return_value=(None, False)),
            patch("handlers.channel_sync._privacy_mismatch", return_value=False),
            patch("helpers.sync_lifecycle.ensure_bot_in_conversation", return_value=False),
            patch("handlers.channel_sync.helpers.lookup_channel_name", return_value="general"),
            patch("handlers.channel_sync.helpers.format_admin_label", return_value="`Ada Lovelace (Workspace A)`"),
            patch("helpers.sync_lifecycle.notify_channel_or_managers", return_value=True),
            patch("handlers.channel_sync.deliver_action_notice"),
            patch("federation.replicate.replicate_sync_channel_upsert"),
            patch("handlers.channel_sync.builders.refresh_home_tab_for_workspace"),
            patch("handlers.channel_sync.invalidate_group_member_homes"),
        ):
            handle_join_sync_submit({}, MagicMock(), MagicMock(), {})
        assert DbManager.get_record(schemas.SyncChannel, id=newer.id).status == "active"
        assert DbManager.get_record(schemas.SyncChannel, id=older.id).status == "deleted"
        assert len(DbManager.find_records(schemas.SyncChannel, [schemas.SyncChannel.sync_id == sync.id])) == 2

    def test_join_does_not_insert_beside_a_paused_row(self, real_db):
        from handlers.channel_sync import handle_join_sync_submit

        workspace = _workspace()
        sync = _sync(group_id=_group().id)
        paused = _channel(sync, workspace, status="paused")
        with (
            patch("handlers.channel_sync._get_authorized_workspace", return_value=("UADA", workspace)),
            patch("handlers.channel_sync._parse_private_metadata", return_value={"sync_id": sync.id}),
            patch("handlers.channel_sync._get_selected_conversation_or_option", return_value="C1"),
            patch("handlers.channel_sync._validate_channel_selection", return_value=(None, False)),
            patch("handlers.channel_sync._privacy_mismatch", return_value=False),
            patch("handlers.channel_sync.DbManager.create_record") as create,
            patch("handlers.channel_sync.builders.refresh_home_tab_for_workspace"),
            patch("handlers.channel_sync.invalidate_group_member_homes"),
        ):
            handle_join_sync_submit({}, MagicMock(), MagicMock(), {})
        create.assert_not_called()
        assert DbManager.get_record(schemas.SyncChannel, id=paused.id).status == "paused"

    def test_create_revives_the_row_even_when_a_partner_is_already_back(self, real_db):
        from handlers.channel_sync import _revive_deleted_create

        workspace = _workspace()
        group = _group()
        empty = _sync(group_id=group.id)
        occupied = _sync("random", group_id=group.id)
        alone = _channel(empty, workspace, status="deleted")
        DbManager.update_records(
            schemas.SyncChannel,
            [schemas.SyncChannel.id == alone.id],
            {schemas.SyncChannel.deleted_at: _now()},
        )
        shared = _channel(occupied, workspace, status="deleted", channel_id="C2")
        DbManager.update_records(
            schemas.SyncChannel,
            [schemas.SyncChannel.id == shared.id],
            {schemas.SyncChannel.deleted_at: _now()},
        )
        _channel(occupied, _workspace(team_id="T2", name="Workspace B"), channel_id="C9")
        with patch("helpers.sync_lifecycle.ensure_bot_in_conversation", return_value=False):
            revived = _revive_deleted_create(MagicMock(), workspace, "UADA", group.id, "C1", True, True, None, None)
            also = _revive_deleted_create(MagicMock(), workspace, "UADA", group.id, "C2", True, True, None, None)
        assert revived.id == alone.id
        assert also.id == shared.id
        assert DbManager.get_record(schemas.SyncChannel, id=alone.id).status == "active"
        assert DbManager.get_record(schemas.SyncChannel, id=shared.id).status == "active"

    def test_creates_in_either_order_reuse_one_sync_and_its_history(self, real_db):
        from datetime import timedelta
        from decimal import Decimal

        from handlers.channel_sync import _revive_deleted_create
        from helpers.post_meta import get_post_records

        group = _group()
        sync = _sync(group_id=group.id)
        first = _workspace()
        second = _workspace(team_id="T2", name="Workspace B")
        third = _workspace(team_id="T3", name="Workspace C")
        left = [
            _channel(sync, first, status="deleted"),
            _channel(sync, second, status="deleted", channel_id="C2"),
            _channel(sync, third, status="deleted", channel_id="C3"),
        ]
        stamp = _now()
        for offset, row in enumerate(left):
            DbManager.update_records(
                schemas.SyncChannel,
                [schemas.SyncChannel.id == row.id],
                {schemas.SyncChannel.deleted_at: stamp - timedelta(minutes=offset)},
            )
        DbManager.create_record(
            schemas.PostMeta(post_id="P-kept", sync_channel_id=left[0].id, ts=Decimal("1700000000.000100"))
        )
        with patch("helpers.sync_lifecycle.ensure_bot_in_conversation", return_value=False):
            second_first = _revive_deleted_create(MagicMock(), second, "UADA", group.id, "C2", True, True, None, None)
            first_next = _revive_deleted_create(MagicMock(), first, "UADA", group.id, "C1", True, True, None, None)
            third_next = _revive_deleted_create(MagicMock(), third, "UADA", group.id, "C3", True, True, None, None)
            fresh = _revive_deleted_create(MagicMock(), first, "UADA", group.id, "CNEW", True, True, None, None)
        assert second_first.sync_id == first_next.sync_id == third_next.sync_id == sync.id
        assert fresh is None
        assert len(DbManager.find_records(schemas.Sync, [schemas.Sync.group_id == group.id])) == 1
        found = get_post_records("1700000000.000100")
        assert found and found[0][1].id == left[0].id

    def test_join_of_the_revived_sync_uses_the_same_row(self, real_db):
        from handlers.channel_sync import handle_join_sync_submit

        group = _group()
        sync = _sync(group_id=group.id)
        other = _sync("other", group_id=group.id)
        workspace = _workspace()
        partner = _workspace(team_id="T2", name="Workspace B")
        ours = _channel(sync, workspace, status="deleted")
        DbManager.update_records(
            schemas.SyncChannel,
            [schemas.SyncChannel.id == ours.id],
            {schemas.SyncChannel.deleted_at: _now()},
        )
        _channel(sync, partner, channel_id="C9")
        elsewhere = _channel(other, workspace, status="deleted", channel_id="C1")
        DbManager.update_records(
            schemas.SyncChannel,
            [schemas.SyncChannel.id == elsewhere.id],
            {schemas.SyncChannel.deleted_at: _now()},
        )
        with (
            patch("handlers.channel_sync._get_authorized_workspace", return_value=("UADA", workspace)),
            patch("handlers.channel_sync._parse_private_metadata", return_value={"sync_id": sync.id}),
            patch("handlers.channel_sync._get_selected_conversation_or_option", return_value="C1"),
            patch("handlers.channel_sync._validate_channel_selection", return_value=(None, False)),
            patch("handlers.channel_sync._privacy_mismatch", return_value=False),
            patch("helpers.sync_lifecycle.ensure_bot_in_conversation", return_value=False),
            patch("handlers.channel_sync.helpers.lookup_channel_name", return_value="general"),
            patch("handlers.channel_sync.helpers.format_admin_label", return_value="`Ada Lovelace (Workspace A)`"),
            patch("helpers.sync_lifecycle.notify_channel_or_managers", return_value=True),
            patch("handlers.channel_sync.deliver_action_notice"),
            patch("federation.replicate.replicate_sync_channel_upsert"),
            patch("handlers.channel_sync.builders.refresh_home_tab_for_workspace"),
            patch("handlers.channel_sync.invalidate_group_member_homes"),
            patch("handlers.channel_sync.DbManager.create_record") as create,
        ):
            handle_join_sync_submit({}, MagicMock(), MagicMock(), {})
        create.assert_not_called()
        assert DbManager.get_record(schemas.SyncChannel, id=ours.id).status == "active"
        assert DbManager.get_record(schemas.SyncChannel, id=elsewhere.id).status == "deleted"


class TestRetentionPurge:
    def _purge(self):
        from helpers.notifications import purge_stale_soft_deletes

        with (
            patch("helpers.notifications._cache_get", return_value=None),
            patch("helpers.notifications._cache_set"),
            patch("helpers.settings.soft_delete_retention_days", return_value=30),
        ):
            return purge_stale_soft_deletes()

    def test_old_deleted_row_and_its_history_are_removed_and_an_empty_sync_goes_too(self, real_db):
        from datetime import timedelta
        from decimal import Decimal

        workspace = _workspace()
        sync = _sync(group_id=_group().id)
        row = _channel(sync, workspace, status="deleted")
        DbManager.update_records(
            schemas.SyncChannel,
            [schemas.SyncChannel.id == row.id],
            {schemas.SyncChannel.deleted_at: _now() - timedelta(days=40)},
        )
        DbManager.create_record(
            schemas.PostMeta(post_id="P-old", sync_channel_id=row.id, ts=Decimal("1700000000.000100"))
        )
        self._purge()
        assert DbManager.get_record(schemas.SyncChannel, id=row.id) is None
        assert DbManager.find_records(schemas.PostMeta, [schemas.PostMeta.post_id == "P-old"]) == []
        assert DbManager.get_record(schemas.Sync, id=sync.id) is None

    def test_a_recent_deleted_row_and_an_uninstalled_row_stay(self, real_db):
        from datetime import timedelta

        workspace = _workspace()
        recent = _channel(_sync(), workspace, status="deleted")
        uninstalled = _channel(_sync("random"), workspace, status="uninstalled", channel_id="C2")
        DbManager.update_records(
            schemas.SyncChannel,
            [schemas.SyncChannel.id == recent.id],
            {schemas.SyncChannel.deleted_at: _now() - timedelta(days=2)},
        )
        DbManager.update_records(
            schemas.SyncChannel,
            [schemas.SyncChannel.id == uninstalled.id],
            {schemas.SyncChannel.deleted_at: _now() - timedelta(days=40)},
        )
        self._purge()
        assert DbManager.get_record(schemas.SyncChannel, id=recent.id).status == "deleted"
        assert DbManager.get_record(schemas.SyncChannel, id=uninstalled.id).status == "uninstalled"

    def test_a_live_sibling_keeps_the_sync(self, real_db):
        from datetime import timedelta

        workspace = _workspace()
        sync = _sync()
        old = _channel(sync, workspace, status="deleted")
        live = _channel(sync, workspace, channel_id="C2")
        DbManager.update_records(
            schemas.SyncChannel,
            [schemas.SyncChannel.id == old.id],
            {schemas.SyncChannel.deleted_at: _now() - timedelta(days=40)},
        )
        self._purge()
        assert DbManager.get_record(schemas.SyncChannel, id=old.id) is None
        assert DbManager.get_record(schemas.SyncChannel, id=live.id).status == "active"
        assert DbManager.get_record(schemas.Sync, id=sync.id) is not None

    def test_the_last_sync_of_a_disbanded_group_removes_the_group(self, real_db):
        from datetime import timedelta

        workspace = _workspace()
        group = _group()
        DbManager.update_records(
            schemas.WorkspaceGroup,
            [schemas.WorkspaceGroup.id == group.id],
            {schemas.WorkspaceGroup.status: "disbanded"},
        )
        sync = _sync(group_id=group.id)
        row = _channel(sync, workspace, status="deleted")
        DbManager.update_records(
            schemas.SyncChannel,
            [schemas.SyncChannel.id == row.id],
            {schemas.SyncChannel.deleted_at: _now() - timedelta(days=40)},
        )
        self._purge()
        assert DbManager.get_record(schemas.WorkspaceGroup, id=group.id) is None

    def test_a_removed_workspace_tells_each_sync_once_and_replicates_the_remove(self, real_db):
        from datetime import timedelta

        from helpers.sync_lifecycle import post_to_other_sync_channels

        workspace = _workspace()
        other = _workspace(team_id="T2", name="Workspace B")
        group = _group()
        sync = _sync(group_id=group.id)
        row = _channel(sync, workspace)
        _channel(sync, other, channel_id="C2")
        DbManager.update_records(
            schemas.Workspace,
            [schemas.Workspace.id == workspace.id],
            {schemas.Workspace.deleted_at: datetime.now(UTC) - timedelta(days=40)},
        )
        with (
            patch("helpers.sync_lifecycle.post_to_other_sync_channels", wraps=post_to_other_sync_channels) as twins,
            patch("helpers.leave_sync._replicate_one_remove") as replicate,
            patch("helpers.notifications.get_bot_token", return_value=None),
        ):
            self._purge()
        twins.assert_called_once()
        assert twins.call_args.args[1] == sync.id
        posted = twins.call_args.args[2]
        assert posted.startswith(":wastebasket: *SyncBot Action Notice*")
        assert "Action: Retention" in posted
        assert "Workspace A" in posted
        assert "was removed after" not in posted
        assert "30 days" not in posted
        replicate.assert_called_once()
        assert replicate.call_args.kwargs["notice"] == posted
        assert replicate.call_args.args[3].id == row.id

    def test_a_removed_workspace_direct_messages_retention_with_the_day_count(self, real_db):
        from datetime import timedelta

        workspace = _workspace()
        other = _workspace(team_id="T2", name="Workspace B")
        group = _group()
        sync = _sync(group_id=group.id)
        _channel(sync, workspace)
        _channel(sync, other, channel_id="C2")
        for ws in (workspace, other):
            DbManager.create_record(
                schemas.WorkspaceGroupMember(
                    group_id=group.id,
                    workspace_id=ws.id,
                    status="active",
                    role="member",
                    joined_at=_now(),
                )
            )
        DbManager.update_records(
            schemas.Workspace,
            [schemas.Workspace.id == workspace.id],
            {schemas.Workspace.deleted_at: datetime.now(UTC) - timedelta(days=40)},
        )
        with (
            patch("helpers.sync_lifecycle.post_to_other_sync_channels"),
            patch("helpers.leave_sync._replicate_one_remove"),
            patch("helpers.notifications.get_bot_token", return_value="xoxb-test"),
            patch("helpers.notifications.notify_admins_dm") as dm,
        ):
            self._purge()
        dm.assert_called_once()
        text = dm.call_args.args[1]
        assert text.startswith(":wastebasket: *SyncBot Action Notice*")
        assert "Action: Retention" in text
        assert "Data Retention Days: 30" in text
        assert "permanently removed" not in text
        assert "Workspaces in Sync" in text

    def test_a_workspace_on_an_allowlist_is_kept_and_not_announced(self, real_db):
        from datetime import timedelta

        from federation.core import get_instance_id, get_or_create_instance_keypair

        get_or_create_instance_keypair()
        workspace = _workspace()
        DbManager.create_record(
            schemas.FederationWorkspaceAllowlist(instance_id=get_instance_id(), workspace_id=workspace.id)
        )
        DbManager.update_records(
            schemas.Workspace,
            [schemas.Workspace.id == workspace.id],
            {schemas.Workspace.deleted_at: datetime.now(UTC) - timedelta(days=40)},
        )
        with patch("helpers.notifications.notify_admins_dm") as dm:
            from helpers.notifications import purge_soft_deletes

            result = purge_soft_deletes(30)
        dm.assert_not_called()
        assert result.workspaces_kept == 1
        assert result.workspaces_purged == 0
        assert DbManager.get_record(schemas.Workspace, id=workspace.team_id) is not None
