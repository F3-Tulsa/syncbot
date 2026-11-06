"""A Channel Sync soft-deletes when the person it runs under leaves, and pauses only from Home or a kick."""

import contextlib
import os
from contextlib import ExitStack
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


@pytest.fixture
def real_db(tmp_path):
    import db as db_mod
    from db import initialize_database

    url = f"sqlite:///{tmp_path / 'private.db'}"
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


def _workspace(team_id="T1", name="Workspace A"):
    from federation.core import get_instance_id

    return DbManager.create_record(
        schemas.Workspace(team_id=team_id, workspace_name=name, instance_id=get_instance_id())
    )


def _sync(group_id=None, title="general"):
    return DbManager.create_record(schemas.Sync(title=title, sync_mode="group", group_id=group_id, uid=str(uuid4())))


def _channel(
    sync,
    workspace,
    *,
    user_id="UADA",
    status="active",
    channel_id="C1",
    is_private=None,
    publishes=True,
    deleted_at=None,
):
    return DbManager.create_record(
        schemas.SyncChannel(
            sync_id=sync.id,
            workspace_id=workspace.id,
            channel_id=channel_id,
            channel_name="general",
            status=status,
            publishes=publishes,
            subscribes=True,
            synced_by_user_id=user_id,
            is_private=is_private,
            created_at=_now(),
            deleted_at=deleted_at,
        )
    )


def _reload(row):
    return DbManager.get_record(schemas.SyncChannel, id=row.id)


def _message_body(subtype, channel_id="C1"):
    return {
        "team_id": "T1",
        "event_id": f"Ev{subtype}",
        "event": {"type": "message", "subtype": subtype, "channel": channel_id, "ts": "1.0"},
    }


@contextlib.contextmanager
def _slack_pass(*, token=None):
    """Stub the Slack pass of a soft-delete so the test reads what it would have posted."""
    with ExitStack() as stack:
        stack.enter_context(patch("helpers.leave_sync.get_bot_token", return_value=token))
        if token is not None:
            stack.enter_context(patch("helpers.sync_lifecycle.get_bot_token", return_value=token))
        stack.enter_context(patch("helpers.leave_sync.get_user_info", return_value=("Ada Lovelace", None)))
        stack.enter_context(patch("helpers.leave_sync.soft_delete_retention_days", return_value=30))
        notice = stack.enter_context(
            patch("helpers.sync_lifecycle.notify_channel_or_managers", return_value=bool(token))
        )
        stack.enter_context(patch("helpers.leave_sync.notify_channel_or_managers", notice))
        managers = stack.enter_context(patch("helpers.sync_lifecycle.notify_managers"))
        stack.enter_context(patch("helpers.workspace_kind.is_local_workspace", return_value=True))
        others = MagicMock()
        stack.enter_context(patch("helpers.leave_sync.post_to_other_sync_channels", others))
        stack.enter_context(patch("helpers.sync_lifecycle.post_to_other_sync_channels", others))
        leave = stack.enter_context(patch("helpers.leave_sync._leave_channel_id"))
        stack.enter_context(patch("helpers.conversations.authorize_url", return_value=None))
        yield SimpleNamespace(notice=notice, others=others, leave=leave, managers=managers)


def _wrapped_soft_delete():
    """Record the reason handed to ``soft_delete_sync_channels`` while the real one still runs."""
    from helpers import leave_sync

    return patch("helpers.leave_sync.soft_delete_sync_channels", wraps=leave_sync.soft_delete_sync_channels)


class _SlackResponse:
    """Same shape as slack_sdk.web.SlackResponse: .get, not a dict."""

    def __init__(self, data: dict):
        self.data = data

    def get(self, key, default=None):
        return self.data.get(key, default)

    def __getitem__(self, key):
        return self.data.get(key)


class TestSlackResponse:
    def test_get_slack_channel_private_reads_a_slack_response(self):
        from helpers.channel_state import get_slack_channel_private

        client = MagicMock()
        client.conversations_info.return_value = _SlackResponse({"channel": {"is_private": True, "name": "general"}})
        assert get_slack_channel_private(client, "C1") is True
        client.conversations_info.return_value = _SlackResponse({"channel": {"is_private": False}})
        assert get_slack_channel_private(client, "C2") is False


class TestEndAssignment:
    def test_person_rows_soft_delete_once(self, real_db):
        from helpers.leave_sync import soft_delete_sync_channels

        workspace = _workspace()
        row = _channel(_sync(), workspace, user_id="UADA")
        with _slack_pass() as slack:
            assert soft_delete_sync_channels(workspace, "C1", "manager_left", synced_by_user_id="UADA") == 1
            saved = _reload(row)
            assert saved.deleted_at is not None
            assert saved.status == "deleted"
            assert saved.synced_by_user_id is None
            assert soft_delete_sync_channels(workspace, "C1", "manager_left", synced_by_user_id="UADA") == 0
        assert slack.notice.call_count == 1
        assert slack.others.call_count == 1
        assert _reload(row).deleted_at == saved.deleted_at

    def test_manager_leaving_ends_only_rows_that_person_runs(self, real_db):
        from handlers.channel_events import handle_member_left_channel

        workspace = _workspace()
        mine = _channel(_sync(), workspace, user_id="UADA", is_private=True)
        theirs = _channel(_sync(title="random"), workspace, user_id="UBOB", is_private=True)
        body = {"team_id": "T1", "event": {"type": "member_left_channel", "user": "UADA", "channel": "C1"}}
        client = MagicMock()
        with (
            patch("handlers.channel_events.get_own_bot_user_id", return_value="UBOT"),
            patch("handlers.channel_events.helpers.note_private_channel_membership") as note,
            patch("helpers.synced_by.has_user_token", return_value=True) as authorized,
            _slack_pass(token="xoxb-test") as slack,
        ):
            handle_member_left_channel(body, client, MagicMock(), {})
        note.assert_called_once_with(client, "T1", "UADA", "C1")
        saved = _reload(mine)
        assert saved.status == "deleted"
        assert saved.deleted_at is not None
        assert saved.synced_by_user_id is None
        kept = _reload(theirs)
        assert kept.status == "active"
        assert kept.deleted_at is None
        assert kept.synced_by_user_id == "UBOB"
        authorized.assert_not_called()
        assert "Acting Manager: Ada Lovelace (Workspace A)" in slack.notice.call_args.args[2]
        assert "Data Retention Days: 30" in slack.managers.call_args.args[1]

    @pytest.mark.parametrize("other_publishes", [False, True])
    def test_other_channels_hear_the_leave(self, real_db, other_publishes):
        from helpers.leave_sync import soft_delete_sync_channels

        workspace = _workspace()
        other = _workspace(team_id="T2", name="Workspace B")
        sync = _sync()
        _channel(sync, workspace, user_id="UADA")
        _channel(sync, other, user_id=None, channel_id="C2", publishes=other_publishes)
        with _slack_pass(token="xoxb-test") as slack:
            soft_delete_sync_channels(workspace, "C1", "manager_left", synced_by_user_id="UADA", person="Ada Lovelace")
        slack.others.assert_called_once()
        posted_workspace, sync_id, text = slack.others.call_args.args
        assert posted_workspace.id == workspace.id
        assert sync_id == sync.id
        assert "Acting Manager: Ada Lovelace (Workspace A)" in text
        assert "Action: Leave" in text
        assert "Workspaces in Sync:" in text
        assert "Data Retention Days" not in text
        assert "no publisher" not in text

    @pytest.mark.parametrize(("sibling_status", "leaves"), [("active", False), ("paused", True)])
    def test_leaves_the_channel_only_when_no_other_active_row_uses_it(self, real_db, sibling_status, leaves):
        from helpers.leave_sync import soft_delete_sync_channels

        workspace = _workspace()
        _channel(_sync(), workspace, user_id="UADA")
        sibling = _channel(_sync(title="random"), workspace, user_id="UBOB", status=sibling_status)
        with _slack_pass(token="xoxb-test") as slack:
            marked = soft_delete_sync_channels(
                workspace, "C1", "manager_left", synced_by_user_id="UADA", person="Ada Lovelace"
            )
        assert marked == 1
        assert _reload(sibling).status == sibling_status
        assert _reload(sibling).deleted_at is None
        assert slack.leave.called is leaves
        notice = slack.notice.call_args.args[2]
        assert "SyncBot stays" not in notice
        if leaves:
            assert slack.leave.call_args.args[1] == "C1"
            assert slack.leave.call_args.kwargs == {"token": "xoxb-test"}
            assert "Acting Manager: Ada Lovelace (Workspace A)" in notice
            assert "Data Retention Days: 30" in slack.managers.call_args.args[1]

    def test_the_channel_notice_names_the_person(self, real_db):
        from helpers.leave_sync import soft_delete_sync_channels

        workspace = _workspace()
        _channel(_sync(), workspace, user_id="UADA")
        with _slack_pass(token="xoxb-test") as slack:
            soft_delete_sync_channels(workspace, "C1", "manager_left", synced_by_user_id="UADA", person="Ada Lovelace")
        notice = slack.notice.call_args.args[2]
        assert "Acting Manager: Ada Lovelace (Workspace A)" in notice
        assert "Data Retention Days: 30" in slack.managers.call_args.args[1]
        assert "Workspaces in Sync:" in notice


class TestRetention:
    def test_home_pause_sets_no_deleted_at(self, real_db):
        from helpers.sync_lifecycle import pause_sync_channel

        workspace = _workspace()
        row = _channel(_sync(), workspace, user_id="UADA")
        with (
            patch("helpers.sync_lifecycle.notify_channel_or_managers") as posted,
            patch("helpers.sync_lifecycle.announce_sync_channels") as announce,
            patch("helpers.leave_sync._leave_channel_id") as leave,
            patch("helpers.workspace_kind.is_local_workspace", return_value=True),
            patch("helpers.sync_participation.format_workspaces_in_sync", return_value="`Workspace A [Paused]`"),
        ):
            paused = pause_sync_channel(row, via="home", leave=True, who="Ada Lovelace")
        assert paused is True
        saved = _reload(row)
        assert saved.status == "paused"
        assert saved.deleted_at is None
        assert saved.synced_by_user_id == "UADA"
        posted.assert_called_once()
        text = posted.call_args.args[2]
        assert "Action: Pause" in text
        assert "Acting Manager: Ada Lovelace (Workspace A)" in text
        leave.assert_called_once()
        assert leave.call_args.args[1] == "C1"
        announce.assert_called_once()
        assert announce.call_args.args[2] == text


class TestArchive:
    @pytest.mark.parametrize("subtype", ["channel_archive", "group_archive"])
    def test_archive_subtype_pauses_and_keeps_history(self, real_db, subtype):
        from handlers.message import respond_to_message_event

        workspace = _workspace()
        row = _channel(_sync(), workspace, user_id="UADA")
        with (
            patch("handlers.message.run_claimed", side_effect=lambda _body, work: work()),
            patch("helpers.sync_lifecycle.notify_channel_or_managers") as notice,
            patch("helpers.sync_lifecycle.notify_managers") as managers,
            patch("helpers.sync_lifecycle.announce_sync_channels") as announce,
            patch("helpers.sync_lifecycle.get_bot_token", return_value="xoxb"),
            patch("helpers.workspace_kind.is_local_workspace", return_value=True),
        ):
            respond_to_message_event(_message_body(subtype), MagicMock(), MagicMock(), {})
        saved = _reload(row)
        assert saved.status == "paused"
        assert saved.deleted_at is None
        assert saved.synced_by_user_id == "UADA"
        notice.assert_not_called()
        announce.assert_called()
        assert "Action: Archive" in announce.call_args.args[2]
        assert "Syncing resumes when the Channel is unarchived." in managers.call_args.args[1]


class TestChannelState:
    @pytest.mark.parametrize(
        ("stored", "live", "reason"), [(False, True, "became_private"), (True, False, "became_public")]
    )
    def test_a_flip_from_a_known_flag_soft_deletes(self, real_db, stored, live, reason):
        from helpers.channel_state import update_channel_privacy

        workspace = _workspace()
        row = _channel(_sync(), workspace, user_id="UADA", is_private=stored)
        with _wrapped_soft_delete() as ended, _slack_pass():
            assert update_channel_privacy(row, is_private=live) is True
        ended.assert_called_once()
        assert ended.call_args.args[1:] == ("C1", reason)
        assert ended.call_args.kwargs["is_private"] is live
        saved = _reload(row)
        assert saved.is_private is live
        assert saved.status == "deleted"
        assert saved.deleted_at is not None
        assert saved.synced_by_user_id is None

    def test_a_null_flag_becoming_known_is_a_plain_store(self, real_db):
        from helpers.channel_state import update_channel_privacy

        workspace = _workspace()
        row = _channel(_sync(), workspace, user_id="UADA", is_private=None)
        with patch("helpers.leave_sync.soft_delete_sync_channels") as ended:
            assert update_channel_privacy(row, is_private=True) is True
            assert update_channel_privacy(row, is_private=True) is False
        ended.assert_not_called()
        saved = _reload(row)
        assert saved.is_private is True
        assert saved.status == "active"
        assert saved.deleted_at is None
        assert saved.synced_by_user_id == "UADA"

    def test_heal_applies_when_info_disagrees(self, real_db):
        from helpers.channel_state import refresh_channel_privacy

        workspace = _workspace()
        row = _channel(_sync(), workspace, user_id="UADA", is_private=False)
        client = MagicMock()
        client.conversations_info.return_value = _SlackResponse({"channel": {"is_private": True, "name": "general"}})
        with _wrapped_soft_delete() as ended, _slack_pass():
            assert refresh_channel_privacy(row, client) is None
        client.conversations_info.assert_called_once_with(channel="C1")
        ended.assert_called_once()
        assert ended.call_args.args[1:] == ("C1", "became_private")
        saved = _reload(row)
        assert saved.is_private is True
        assert saved.status == "deleted"
        assert saved.deleted_at is not None

    def test_heal_leaves_a_matching_flag_alone(self, real_db):
        from helpers.channel_state import refresh_channel_privacy

        workspace = _workspace()
        row = _channel(_sync(), workspace, user_id="UADA", is_private=True)
        client = MagicMock()
        client.conversations_info.return_value = _SlackResponse({"channel": {"is_private": True}})
        with patch("helpers.channel_state.update_channel_privacy") as apply:
            refresh_channel_privacy(row, client)
        apply.assert_not_called()
        assert _reload(row).status == "active"

    def test_failed_info_returns_none_and_heal_keeps_the_stored_flag(self, real_db):
        from helpers.channel_state import get_slack_channel_private, refresh_channel_privacy

        workspace = _workspace()
        row = _channel(_sync(), workspace, user_id="UADA", is_private=False)
        client = MagicMock()
        client.conversations_info.side_effect = SlackApiError(
            "channel_not_found", {"ok": False, "error": "channel_not_found"}
        )
        assert get_slack_channel_private(client, "C1") is None
        with patch("helpers.channel_state.update_channel_privacy") as apply:
            refresh_channel_privacy(row, client)
        apply.assert_not_called()
        saved = _reload(row)
        assert saved.is_private is False
        assert saved.status == "active"
        assert saved.deleted_at is None

    def test_became_public_does_not_revive_anything(self, real_db):
        from helpers.channel_state import update_channel_privacy

        workspace = _workspace()
        paused = _channel(_sync(), workspace, user_id="UADA", status="paused", is_private=True)
        gone = _channel(
            _sync(title="random"),
            workspace,
            user_id=None,
            status="deleted",
            is_private=True,
            deleted_at=_now(),
        )
        with _wrapped_soft_delete() as ended, _slack_pass():
            update_channel_privacy(paused, is_private=False)
        assert ended.call_args.args[1:] == ("C1", "became_public")
        saved = _reload(paused)
        assert saved.is_private is False
        assert saved.status == "deleted"
        assert saved.deleted_at is not None
        untouched = _reload(gone)
        assert untouched.status == "deleted"
        assert untouched.is_private is True
        assert untouched.deleted_at == gone.deleted_at

    @pytest.mark.parametrize(
        ("subtype", "reason", "flag"),
        [("channel_convert_to_private", "became_private", True), ("channel_convert_to_public", "became_public", False)],
    )
    def test_message_subtypes_route_to_the_right_reason(self, real_db, subtype, reason, flag):
        from handlers.message import respond_to_message_event

        workspace = _workspace()
        row = _channel(_sync(), workspace, user_id="UADA", is_private=not flag)
        with (
            patch("handlers.message.run_claimed", side_effect=lambda _body, work: work()),
            _wrapped_soft_delete() as ended,
            _slack_pass(),
        ):
            respond_to_message_event(_message_body(subtype), MagicMock(), MagicMock(), {})
        ended.assert_called_once()
        assert ended.call_args.args[1:] == ("C1", reason)
        assert ended.call_args.kwargs == {"is_private": flag}
        saved = _reload(row)
        assert saved.is_private is flag
        assert saved.status == "deleted"
        assert saved.deleted_at is not None


class TestSettingsOff:
    def test_off_soft_deletes_private_rows_after_one_heal(self, real_db):
        from helpers.sync_lifecycle import soft_delete_private_syncs_for_settings

        workspace = _workspace()
        private = _channel(_sync(), workspace, user_id="UADA", is_private=None)
        unknown = _channel(_sync(title="random"), workspace, user_id="UADA", channel_id="Cunk", is_private=None)
        public = _channel(_sync(title="town-square"), workspace, user_id=None, channel_id="Cpub", is_private=False)
        client = MagicMock()

        def _info(channel):
            if channel == "C1":
                return _SlackResponse({"channel": {"is_private": True, "name": "general"}})
            raise SlackApiError("channel_not_found", {"ok": False, "error": "channel_not_found"})

        client.conversations_info.side_effect = _info
        with _slack_pass(token="xoxb-test") as slack:
            soft_delete_private_syncs_for_settings(workspace, client)
        assert sorted(call.kwargs["channel"] for call in client.conversations_info.call_args_list) == ["C1", "Cunk"]
        saved = _reload(private)
        assert saved.is_private is True
        assert saved.status == "deleted"
        assert saved.deleted_at is not None
        assert saved.synced_by_user_id is None
        skipped = _reload(unknown)
        assert skipped.is_private is None
        assert skipped.status == "active"
        assert skipped.synced_by_user_id == "UADA"
        kept = _reload(public)
        assert kept.is_private is False
        assert kept.status == "active"
        slack.notice.assert_called_once()
        assert slack.notice.call_args.args[1] == "C1"
        assert "Action: Privacy" in slack.notice.call_args.args[2]
        assert "Note: Private Channel syncing was turned off." in slack.managers.call_args.args[1]

    def test_nothing_private_sends_no_dm(self, real_db):
        from helpers.sync_lifecycle import soft_delete_private_syncs_for_settings

        workspace = _workspace()
        _channel(_sync(), workspace, user_id="UADA", is_private=False)
        client = MagicMock()
        with _slack_pass() as slack:
            soft_delete_private_syncs_for_settings(workspace, client)
        client.conversations_info.assert_not_called()
        slack.notice.assert_not_called()


class TestRevokeAndDeleted:
    def test_end_user_authorization_clears_the_token_and_ends_only_that_persons_rows(self, real_db):
        from helpers.sync_lifecycle import end_user_authorization

        workspace = _workspace()
        mine = _channel(_sync(), workspace, user_id="UADA", is_private=True)
        theirs = _channel(_sync(title="random"), workspace, user_id="UBOB", channel_id="C2", is_private=True)
        unowned = _channel(_sync(title="town-square"), workspace, user_id=None, channel_id="C3", is_private=True)
        client = MagicMock()
        with patch("helpers.sync_lifecycle.clear_user_authorization") as clear, _slack_pass(token="xoxb-test") as slack:
            assert end_user_authorization("T1", "UADA", client=client) == 1
        clear.assert_called_once_with("T1", "UADA")
        saved = _reload(mine)
        assert saved.status == "deleted"
        assert saved.deleted_at is not None
        assert saved.synced_by_user_id is None
        kept = _reload(theirs)
        assert kept.status == "active"
        assert kept.synced_by_user_id == "UBOB"
        untouched = _reload(unowned)
        assert untouched.status == "active"
        assert untouched.deleted_at is None
        assert untouched.synced_by_user_id is None
        slack.notice.assert_called_once()
        assert slack.notice.call_args.args[1] == "C1"
        assert "Action: Leave" in slack.notice.call_args.args[2]
        assert "Note: SyncBot is no longer authorized for this Channel." in slack.managers.call_args.args[1]

    def test_unknown_workspace_still_clears_the_token(self, real_db):
        from helpers.sync_lifecycle import end_user_authorization

        with patch("helpers.sync_lifecycle.clear_user_authorization") as clear, _slack_pass() as slack:
            assert end_user_authorization("T9", "UADA", client=MagicMock()) == 0
        clear.assert_called_once_with("T9", "UADA")
        slack.notice.assert_not_called()

    def test_tokens_revoked_calls_end_user_authorization(self):
        from handlers.tokens import handle_tokens_revoked

        client = MagicMock()
        body = {"team_id": "T1", "event": {"type": "tokens_revoked", "tokens": {"oauth": ["UADA"], "bot": []}}}
        with (
            patch("handlers.tokens.end_user_authorization") as end,
            patch("handlers.tokens._republish_home_after_user_revoke") as republish,
        ):
            handle_tokens_revoked(body, client, MagicMock(), {})
        end.assert_called_once_with("T1", "UADA", client=client)
        republish.assert_called_once()
        assert republish.call_args.args[:2] == ("T1", "UADA")

    def test_deleted_profile_calls_end_user_authorization(self):
        from handlers.users import handle_user_profile_changed

        client = MagicMock()
        body = {"team_id": "T1", "event": {"type": "user_profile_changed", "user": {"id": "UADA", "deleted": True}}}
        with patch("handlers.users.end_user_authorization") as end:
            handle_user_profile_changed(body, client, MagicMock(), {})
        end.assert_called_once_with("T1", "UADA", client=client)


class TestResumeModal:
    BODY = {
        "trigger_id": "trig",
        "team": {"id": "T1"},
        "user": {"id": "U1"},
        "actions": [{"action_id": "resume_sync", "value": "2"}],
    }

    def _open(self, *, member, token):
        from handlers import channel_sync

        channel = SimpleNamespace(
            id=1, sync_id=2, workspace_id=3, channel_id="C1", status="paused", is_private=True, deleted_at=None
        )
        workspace = SimpleNamespace(id=3, team_id="T1", workspace_name="Workspace A")
        seen: dict = {}

        def _post(self, **kwargs):
            seen["blocks"] = list(self.blocks)
            seen["kwargs"] = kwargs

        from helpers.synced_by import Viewer

        viewer = Viewer(
            team_id="T1",
            user_id="U1",
            workspace_id=3,
            is_manager=True,
            allow_private=True,
            has_user_token=token,
            private_channel_ids=frozenset({"C1"}) if member else frozenset(),
        )
        with (
            patch("handlers.channel_sync._get_authorized_workspace", return_value=("U1", workspace)),
            patch("handlers.channel_sync.viewer_context", return_value=viewer),
            patch("handlers.channel_sync.DbManager.get_record", return_value=None),
            patch("handlers.channel_sync.DbManager.find_records", return_value=[channel]),
            patch.object(channel_sync.orm.BlockView, "post_modal", _post),
        ):
            channel_sync.handle_resume_sync(self.BODY, MagicMock(), MagicMock(), {})
        return seen

    @staticmethod
    def _text(seen):
        return " ".join(getattr(block, "label", "") or "" for block in seen["blocks"])

    @staticmethod
    def _buttons(seen):
        return [element for block in seen["blocks"] for element in (getattr(block, "elements", None) or [])]

    def test_not_a_member_gets_a_close_only_modal(self):
        seen = self._open(member=False, token=True)
        text = self._text(seen)
        assert "You are not a member of this private Channel, so you cannot resume it." in text
        assert seen["kwargs"]["close_button_text"] == "Close"
        assert seen["kwargs"]["submit_button_text"] is None
        assert seen["kwargs"]["parent_metadata"] == {"sync_id": 2}
        assert self._buttons(seen) == []

    def test_member_without_authorization_is_told_to_authorize(self):
        seen = self._open(member=True, token=False)
        assert "Authorize SyncBot" in self._text(seen)
        assert seen["kwargs"]["close_button_text"] == "Close"
        assert seen["kwargs"]["submit_button_text"] is None
        assert self._buttons(seen) == []

    def test_authorized_member_gets_the_resume_button(self):
        from slack import actions

        seen = self._open(member=True, token=True)
        buttons = self._buttons(seen)
        assert [button.label for button in buttons] == [":arrow_forward: Resume Sync"]
        assert buttons[0].action == actions.CONFIG_RESUME_SYNC_CONFIRM
        assert buttons[0].value == "2"
        assert "Resume the Sync in this Channel?" in self._text(seen)
        assert seen["kwargs"]["close_button_text"] == "Cancel"
        assert seen["kwargs"]["parent_metadata"] == {"sync_id": 2}


class TestResumeFailClosed:
    def test_private_resume_writes_paused_back_and_dms_the_person(self, real_db):
        from helpers.conversations import ConversationAccessError
        from helpers.sync_lifecycle import resume_sync_channel

        workspace = _workspace()
        row = _channel(_sync(), workspace, user_id="UADA", status="paused", is_private=True)
        client = MagicMock()
        message = "SyncBot could not be added to that private Channel (`not_in_channel`)."

        def _fail(*_args, **_kwargs):
            raise ConversationAccessError(message) from SlackApiError(
                "not_in_channel", {"ok": False, "error": "not_in_channel"}
            )

        from helpers.synced_by import Viewer

        with (
            patch("helpers.sync_lifecycle.is_workspace_manager", return_value=True),
            patch(
                "helpers.sync_lifecycle.viewer_context",
                return_value=Viewer(
                    team_id="T1",
                    user_id="UADA",
                    workspace_id=workspace.id,
                    is_manager=True,
                    allow_private=True,
                    has_user_token=True,
                    private_channel_ids=frozenset({"C1"}),
                ),
            ),
            patch("helpers.sync_lifecycle.get_bot_token", return_value="xoxb-test"),
            patch("helpers.sync_lifecycle.ensure_bot_in_conversation", side_effect=_fail) as invite,
            patch("helpers.sync_lifecycle.notify_user_dm") as dm,
            patch("helpers.sync_lifecycle.notify_channel_or_managers") as notice,
            patch("helpers.sync_lifecycle.post_to_other_sync_channels") as others,
            patch("helpers.sync_lifecycle.replicate_sync_channel_status") as replicate,
            _slack_pass() as slack,
        ):
            denied = resume_sync_channel(row, workspace, "UADA", client=client, join=True, context={})
        assert denied == f":warning: {message}"
        assert invite.call_args.args[1] == "C1"
        assert invite.call_args.kwargs["team_id"] == "T1"
        assert invite.call_args.kwargs["acting_user_id"] == "UADA"
        saved = _reload(row)
        assert saved.status == "paused"
        assert saved.deleted_at is None
        assert saved.synced_by_user_id == "UADA"
        dm.assert_called_once_with(client, "UADA", f":warning: {message}")
        notice.assert_not_called()
        others.assert_not_called()
        replicate.assert_not_called()
        slack.notice.assert_not_called()


class TestActionNotice:
    def test_names_the_person_and_keeps_the_days_on_the_manager_dm(self):
        from helpers.sync_participation import format_action_notice

        with patch(
            "helpers.sync_participation.format_workspaces_in_sync",
            return_value="`Workspace B [Publish and Subscribe]`",
        ):
            channel = format_action_notice(
                action="Leave",
                workspace_name="Workspace A",
                manager="Ada Lovelace",
                sync_id=5,
            )
            detail = format_action_notice(
                action="Leave",
                workspace_name="Workspace A",
                manager="Ada Lovelace",
                sync_id=5,
                channel_name="general",
                retention_days=30,
                for_managers=True,
            )
        assert "Acting Manager: Ada Lovelace (Workspace A)" in channel
        assert "Workspaces in Sync: Workspace B [Publish and Subscribe]" in channel
        assert "Data Retention Days" not in channel
        assert "Data Retention Days: 30" in detail
        assert "Affected Channel: #general" in detail
        assert "Acting Manager: Ada Lovelace" in detail

    def test_authorization_ended_names_the_person_on_the_manager_dm(self):
        from helpers.sync_participation import format_action_notice

        with patch("helpers.sync_participation.format_workspaces_in_sync", return_value="`none`"):
            detail = format_action_notice(
                action="Leave",
                workspace_name="Workspace A",
                manager="Ada Lovelace",
                sync_id=5,
                for_managers=True,
                channel_name="general",
                note="SyncBot is no longer authorized for this Channel.",
            )
        assert "Action: Leave" in detail
        assert "Note: SyncBot is no longer authorized for this Channel." in detail
        assert "Home tab" not in detail


class TestBotAndDelete:
    def test_bot_kick_pauses_once(self, real_db):
        from handlers.channel_events import handle_member_left_channel
        from helpers.sync_lifecycle import pause_sync_channel

        workspace = _workspace()
        row = _channel(_sync(), workspace, user_id="UADA")
        body = {"team_id": "T1", "event": {"type": "member_left_channel", "user": "UBOT", "channel": "C1"}}
        with (
            patch("handlers.channel_events.get_own_bot_user_id", return_value="UBOT"),
            patch("handlers.channel_events.helpers.note_private_channel_membership") as note,
            patch("helpers.sync_lifecycle.notify_channel_or_managers") as notice,
            patch("helpers.sync_lifecycle.notify_managers") as managers,
            patch("helpers.sync_lifecycle.post_to_other_sync_channels") as others,
            patch("helpers.leave_sync._leave_channel_id") as leave,
            patch("helpers.sync_lifecycle.get_bot_token", return_value="xoxb"),
            patch("helpers.workspace_kind.is_local_workspace", return_value=True),
        ):
            handle_member_left_channel(body, MagicMock(), MagicMock(), {})
            handle_member_left_channel(body, MagicMock(), MagicMock(), {})
            assert pause_sync_channel(_reload(row), via="kick") is False
        note.assert_not_called()
        saved = _reload(row)
        assert saved.status == "paused"
        assert saved.deleted_at is None
        assert saved.synced_by_user_id == "UADA"
        notice.assert_not_called()
        others.assert_called_once()
        assert "Action: Removed" in others.call_args.args[2]
        assert "Affected Channel:" in managers.call_args.args[1]
        assert "no publisher" not in others.call_args.args[2]
        leave.assert_not_called()

    def _pause_patches(self):
        return (
            patch("helpers.sync_lifecycle.notify_channel_or_managers"),
            patch("helpers.sync_lifecycle.notify_managers"),
            patch("helpers.sync_lifecycle.post_to_other_sync_channels"),
            patch("helpers.leave_sync._leave_channel_id"),
            patch("helpers.sync_lifecycle.get_bot_token", return_value="xoxb"),
            patch("helpers.workspace_kind.is_local_workspace", return_value=True),
        )

    def test_bot_left_pauses_the_install_workspace(self, real_db):
        from handlers.channel_events import handle_member_left_channel

        workspace_a = _workspace(team_id="T0000000A", name="Workspace A")
        workspace_b = _workspace(team_id="T0000000B", name="Workspace B")
        row_a = _channel(_sync(), workspace_a, channel_id="C0SHARED", user_id="UADA")
        row_b = _channel(_sync(title="other"), workspace_b, channel_id="C0SHARED", user_id="UBOB")
        body = {
            "team_id": "T0000000B",
            "event": {"type": "member_left_channel", "user": "U0BOTA", "channel": "C0SHARED"},
        }
        context = {"team_id": "T0000000A", "bot_user_id": "U0BOTA"}
        with ExitStack() as stack:
            for item in self._pause_patches():
                stack.enter_context(item)
            handle_member_left_channel(body, MagicMock(), MagicMock(), context)
        assert _reload(row_a).status == "paused"
        assert _reload(row_b).status == "active"

    def test_channel_left_pauses_the_install_workspace(self, real_db):
        from handlers.channel_events import handle_channel_left

        workspace_a = _workspace(team_id="T0000000A", name="Workspace A")
        workspace_b = _workspace(team_id="T0000000B", name="Workspace B")
        row_a = _channel(_sync(), workspace_a, channel_id="C0SHARED", user_id="UADA")
        row_b = _channel(_sync(title="other"), workspace_b, channel_id="C0SHARED", user_id="UBOB")
        body = {"team_id": "T0000000B", "event": {"type": "channel_left", "channel": "C0SHARED"}}
        context = {"team_id": "T0000000A", "bot_user_id": "U0BOTA"}
        with ExitStack() as stack:
            for item in self._pause_patches():
                stack.enter_context(item)
            handle_channel_left(body, MagicMock(), MagicMock(), context)
        assert _reload(row_a).status == "paused"
        assert _reload(row_b).status == "active"

    def test_bot_left_without_install_team_uses_the_event_workspace(self, real_db):
        from handlers.channel_events import handle_member_left_channel

        workspace = _workspace(team_id="T0000000A", name="Workspace A")
        row = _channel(_sync(), workspace, channel_id="C0SHARED", user_id="UADA")
        body = {
            "team_id": "T0000000A",
            "event": {"type": "member_left_channel", "user": "U0BOTA", "channel": "C0SHARED"},
        }
        with ExitStack() as stack:
            stack.enter_context(patch("handlers.channel_events.get_own_bot_user_id", return_value="U0BOTA"))
            for item in self._pause_patches():
                stack.enter_context(item)
            handle_member_left_channel(body, MagicMock(), MagicMock(), {})
        assert _reload(row).status == "paused"

    def test_channel_deleted_purges_only_this_workspaces_live_rows(self, real_db):
        from handlers import channel_events

        workspace = _workspace()
        other = _workspace(team_id="T2", name="Workspace B")
        first = _channel(_sync(), workspace, user_id="UADA")
        second = _channel(_sync(title="random"), workspace, user_id="UBOB")
        theirs = _channel(_sync(title="theirs"), other, channel_id="C2", user_id="UB")
        gone = _channel(_sync(title="old"), workspace, user_id=None, status="deleted", deleted_at=_now())
        body = {"team_id": "T1", "event": {"type": "channel_deleted", "channel": "C1"}}
        with (
            patch("helpers.sync_lifecycle.get_bot_token", return_value="xoxb"),
            patch("helpers.sync_lifecycle.post_to_other_sync_channels") as others,
            patch("helpers.leave_sync._replicate_one_remove"),
            patch("helpers.sync_lifecycle.notify_managers") as dm,
        ):
            channel_events.handle_channel_deleted(body, MagicMock(), MagicMock(), {})
        assert _reload(first) is None
        assert _reload(second) is None
        assert _reload(theirs).channel_id == "C2"
        kept = _reload(gone)
        assert kept is not None
        assert kept.deleted_at == gone.deleted_at
        assert dm.call_count == 2
        assert others.call_count == 2
        for call in dm.call_args_list:
            assert "History: Deleted" in call.args[1]
            assert "Affected Channel:" in call.args[1]
        for call in others.call_args_list:
            assert "Action: Channel deleted" in call.args[2]
            assert "History:" not in call.args[2]


class TestStampPrivateChannel:
    def test_siblings_get_the_flag_but_not_the_person(self, real_db):
        from helpers.synced_by import set_channel_private

        workspace = _workspace()
        row = _channel(_sync(), workspace, user_id=None, is_private=None)
        sibling = _channel(_sync(title="random"), workspace, user_id=None, is_private=None)
        elsewhere = _channel(_sync(title="town-square"), workspace, user_id=None, channel_id="C2", is_private=None)
        gone = _channel(_sync(title="old"), workspace, user_id=None, status="deleted", deleted_at=_now())
        set_channel_private(row, "UADA")
        assert (row.is_private, row.synced_by_user_id) == (True, "UADA")
        saved = _reload(row)
        assert (saved.is_private, saved.synced_by_user_id) == (True, "UADA")
        stamped = _reload(sibling)
        assert (stamped.is_private, stamped.synced_by_user_id) == (True, None)
        untouched = _reload(elsewhere)
        assert (untouched.is_private, untouched.synced_by_user_id) == (None, None)
        assert _reload(gone).is_private is None

    def test_no_person_still_flags_the_channel(self, real_db):
        from helpers.synced_by import set_channel_private

        workspace = _workspace()
        row = _channel(_sync(), workspace, user_id=None, is_private=None)
        set_channel_private(row, None)
        saved = _reload(row)
        assert saved.is_private is True
        assert saved.synced_by_user_id is None
