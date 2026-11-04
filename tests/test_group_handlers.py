"""Focused unit tests for group handler edge branches."""

import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import constants

os.environ.setdefault("DATABASE_HOST", "localhost")
os.environ.setdefault("DATABASE_USER", "root")
os.environ.setdefault("DATABASE_PASSWORD", "test")
os.environ.setdefault("DATABASE_SCHEMA", "syncbot")
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-0-0")

from handlers.group import (  # noqa: E402
    handle_accept_group_invite,
    handle_cancel_group_invite_confirm,
    handle_create_group_submit,
    handle_decline_group_invite,
    handle_join_group_submit,
)
from slack import actions  # noqa: E402

INVITED_WS_ID = 1
INVITER_WS_ID = 2
THIRD_PARTY_WS_ID = 99
MEMBER_ID = 7
GROUP_ID = 5


def _pending_member():
    return SimpleNamespace(
        id=MEMBER_ID,
        workspace_id=INVITED_WS_ID,
        invited_by_workspace_id=INVITER_WS_ID,
        group_id=GROUP_ID,
        status="pending",
        dm_messages=None,
    )


def _invite_body(action_id: str):
    return {
        "user": {"id": "U1", "team_id": "T1"},
        "team": {"id": "T1"},
        "actions": [{"value": str(MEMBER_ID), "action_id": f"{action_id}_{MEMBER_ID}"}],
    }


class TestAcceptGroupInviteAuthorization:
    """Accept is an invitee action: only the invited workspace may activate the invite."""

    def _run(self, acting_workspace_id):
        member = _pending_member()
        group = SimpleNamespace(id=GROUP_ID, name="Test Group")
        acting = SimpleNamespace(id=acting_workspace_id, team_id="T1", deleted_at=None)

        with (
            patch("handlers.group._get_authorized_workspace", return_value=("U1", acting)),
            patch("handlers.group.DbManager.get_record", side_effect=[member, group]),
            patch("handlers.group.DbManager.update_records") as update_records,
            patch("handlers.group.DbManager.find_records", return_value=[]),
            patch("handlers.group.helpers.get_workspace_by_id", return_value=acting),
            patch("handlers.group.helpers.resolve_workspace_name", return_value="WS"),
            patch("handlers.group.helpers.format_admin_label", return_value="`Ada Lovelace (Workspace A)`"),
            patch("handlers.group._activate_group_membership"),
            patch("handlers.group.helpers.update_group_invite_dms"),
            patch("handlers.group.builders.refresh_home_tab_for_workspace"),
        ):
            handle_accept_group_invite(
                _invite_body(actions.CONFIG_ACCEPT_GROUP_INVITE), MagicMock(), MagicMock(), context={}
            )
        return update_records

    def test_invited_workspace_may_accept(self):
        assert self._run(INVITED_WS_ID).called

    def test_third_party_workspace_may_not_accept(self):
        assert not self._run(THIRD_PARTY_WS_ID).called

    def test_inviting_workspace_may_not_accept_on_behalf_of_invitee(self):
        assert not self._run(INVITER_WS_ID).called

    def test_unauthorized_user_is_rejected(self):
        """When the user is not authorized, the handler returns without reading the invite."""
        with (
            patch("handlers.group._get_authorized_workspace", return_value=None),
            patch("handlers.group.DbManager.get_record") as get_record,
            patch("handlers.group.DbManager.update_records") as update_records,
        ):
            handle_accept_group_invite(
                _invite_body(actions.CONFIG_ACCEPT_GROUP_INVITE), MagicMock(), MagicMock(), context={}
            )

        assert not get_record.called
        assert not update_records.called

    def test_accept_uses_the_join_group_notice(self):
        member = _pending_member()
        group = SimpleNamespace(id=GROUP_ID, name="Group")
        acting = SimpleNamespace(id=INVITED_WS_ID, team_id="T1", deleted_at=None)
        with (
            patch("handlers.group._get_authorized_workspace", return_value=("U1", acting)),
            patch("handlers.group.DbManager.get_record", side_effect=[member, group]),
            patch("handlers.group.DbManager.update_records"),
            patch("handlers.group.DbManager.find_records", return_value=[]),
            patch("handlers.group.helpers.get_workspace_by_id", return_value=acting),
            patch("handlers.group.helpers.resolve_workspace_name", return_value="Workspace A"),
            patch("handlers.group.helpers.format_admin_label", return_value="`Ada Lovelace (Workspace A)`"),
            patch("handlers.group._activate_group_membership"),
            patch("handlers.group.helpers.update_group_invite_dms") as updated,
            patch("handlers.group.builders.refresh_home_tab_for_workspace"),
            patch("federation.replicate.replicate_group_invite") as replicate,
            patch("helpers.notifications.notify_group_managers") as group_dms,
        ):
            handle_accept_group_invite(
                _invite_body(actions.CONFIG_ACCEPT_GROUP_INVITE), MagicMock(), MagicMock(), context={}
            )
        text = updated.call_args.args[2]
        assert text.startswith(":punch: *SyncBot Action Notice*")
        assert "Action: Join Group" in text
        assert "Workspace: Workspace A" in text
        assert "Workspaces in Sync" not in text
        assert group_dms.call_args.args[1] == text
        assert group_dms.call_args.kwargs["exclude_workspace_id"] == INVITED_WS_ID
        assert replicate.call_args.kwargs["notice"] == text
        assert replicate.call_args.kwargs["announce"] is True


class TestDeclineGroupInviteAuthorization:
    """Decline is an invitee action; cancel is an inviter action. Both destroy the row."""

    def _run(self, action_id, acting_workspace_id):
        member = _pending_member()
        group = SimpleNamespace(id=GROUP_ID, name="Test Group")
        acting = SimpleNamespace(id=acting_workspace_id, team_id="T1", deleted_at=None)

        with (
            patch("handlers.group._get_authorized_workspace", return_value=("U1", acting)),
            patch("handlers.group.DbManager.get_record", side_effect=[member, group]),
            patch("handlers.group.DbManager.delete_records") as delete_records,
            patch("handlers.group.DbManager.find_records", return_value=[]),
            patch("handlers.group.helpers.get_workspace_by_id", return_value=acting),
            patch("handlers.group.helpers.resolve_workspace_name", return_value="Workspace A"),
            patch("handlers.group.helpers.format_admin_label", return_value="`Ada Lovelace (Workspace A)`"),
            patch("handlers.group.helpers.update_group_invite_dms"),
            patch("helpers.notifications.notify_group_managers"),
            patch("handlers.group.builders.refresh_home_tab_for_workspace"),
        ):
            fn = (
                handle_cancel_group_invite_confirm
                if action_id == actions.CONFIG_CANCEL_GROUP_INVITE
                else handle_decline_group_invite
            )
            fn(_invite_body(action_id), MagicMock(), MagicMock(), context={})
        return delete_records

    def test_invited_workspace_may_decline(self):
        assert self._run(actions.CONFIG_DECLINE_GROUP_INVITE, INVITED_WS_ID).called

    def test_third_party_may_not_decline(self):
        assert not self._run(actions.CONFIG_DECLINE_GROUP_INVITE, THIRD_PARTY_WS_ID).called

    def test_inviter_may_not_decline(self):
        assert not self._run(actions.CONFIG_DECLINE_GROUP_INVITE, INVITER_WS_ID).called

    def test_inviting_workspace_may_cancel(self):
        assert self._run(actions.CONFIG_CANCEL_GROUP_INVITE, INVITER_WS_ID).called

    def test_third_party_may_not_cancel(self):
        assert not self._run(actions.CONFIG_CANCEL_GROUP_INVITE, THIRD_PARTY_WS_ID).called

    def test_invitee_may_not_cancel(self):
        assert not self._run(actions.CONFIG_CANCEL_GROUP_INVITE, INVITED_WS_ID).called

    def test_group_owner_may_cancel(self):
        """A group owner has standing over membership, so it may cancel a pending invite."""
        member = _pending_member()
        group = SimpleNamespace(id=GROUP_ID, name="Test Group")
        acting = SimpleNamespace(id=THIRD_PARTY_WS_ID, team_id="T1", deleted_at=None)

        with (
            patch("handlers.group._get_authorized_workspace", return_value=("U1", acting)),
            patch("handlers.group.DbManager.get_record", side_effect=[member, group]),
            patch("handlers.group.DbManager.delete_records") as delete_records,
            patch("handlers.group.DbManager.find_records", return_value=[]),
            patch("handlers.group.helpers.is_workspace_owner", return_value=True),
            patch("handlers.group.helpers.get_workspace_by_id", return_value=acting),
            patch("handlers.group.helpers.resolve_workspace_name", return_value="Workspace A"),
            patch("handlers.group.helpers.format_admin_label", return_value="`Ada Lovelace (Workspace A)`"),
            patch("handlers.group.helpers.update_group_invite_dms"),
            patch("helpers.notifications.notify_group_managers"),
            patch("handlers.group.builders.refresh_home_tab_for_workspace"),
        ):
            handle_cancel_group_invite_confirm(
                _invite_body(actions.CONFIG_CANCEL_GROUP_INVITE), MagicMock(), MagicMock(), context={}
            )

        assert delete_records.called

    def test_unauthorized_user_is_rejected(self):
        with (
            patch("handlers.group._get_authorized_workspace", return_value=None),
            patch("handlers.group.DbManager.get_record") as get_record,
            patch("handlers.group.DbManager.delete_records") as delete_records,
        ):
            handle_decline_group_invite(
                _invite_body(actions.CONFIG_DECLINE_GROUP_INVITE), MagicMock(), MagicMock(), context={}
            )

        assert not get_record.called
        assert not delete_records.called

    def test_decline_rewrites_the_invite_with_the_action_notice(self):
        member = _pending_member()
        group = SimpleNamespace(id=GROUP_ID, name="Group")
        acting = SimpleNamespace(id=INVITED_WS_ID, team_id="T1", deleted_at=None)
        with (
            patch("handlers.group._get_authorized_workspace", return_value=("U1", acting)),
            patch("handlers.group.DbManager.get_record", side_effect=[member, group]),
            patch("handlers.group.DbManager.delete_records"),
            patch("handlers.group.DbManager.find_records", return_value=[]),
            patch("handlers.group.helpers.get_workspace_by_id", return_value=acting),
            patch("handlers.group.helpers.resolve_workspace_name", return_value="Workspace A"),
            patch("handlers.group.helpers.format_admin_label", return_value="`Ada Lovelace (Workspace A)`"),
            patch("handlers.group.helpers.update_group_invite_dms") as updated,
            patch("helpers.notifications.notify_group_managers") as group_dms,
            patch("handlers.group.builders.refresh_home_tab_for_workspace"),
        ):
            handle_decline_group_invite(
                _invite_body(actions.CONFIG_DECLINE_GROUP_INVITE), MagicMock(), MagicMock(), context={}
            )
        text = updated.call_args.args[2]
        assert text.startswith(":eject: *SyncBot Action Notice*")
        assert "Action: Decline" in text
        assert "Workspace: Workspace A" in text
        assert "Workspaces in Sync" not in text
        assert "invitation to join" not in text
        assert group_dms.call_args.args[1] == text
        assert group_dms.call_args.kwargs["exclude_workspace_id"] == INVITED_WS_ID

    def test_cancel_rewrites_the_invite_with_the_action_notice(self):
        member = _pending_member()
        group = SimpleNamespace(id=GROUP_ID, name="Group")
        acting = SimpleNamespace(id=INVITER_WS_ID, team_id="T1", deleted_at=None)
        with (
            patch("handlers.group._get_authorized_workspace", return_value=("U1", acting)),
            patch("handlers.group.DbManager.get_record", side_effect=[member, group]),
            patch("handlers.group.DbManager.delete_records"),
            patch("handlers.group.DbManager.find_records", return_value=[]),
            patch("handlers.group.helpers.is_workspace_owner", return_value=False),
            patch("handlers.group.helpers.get_workspace_by_id", return_value=acting),
            patch("handlers.group.helpers.resolve_workspace_name", return_value="Workspace B"),
            patch("handlers.group.helpers.format_admin_label", return_value="`Ada Lovelace (Workspace A)`"),
            patch("handlers.group.helpers.update_group_invite_dms") as updated,
            patch("helpers.notifications.notify_group_managers"),
            patch("handlers.group.builders.refresh_home_tab_for_workspace"),
        ):
            handle_cancel_group_invite_confirm(
                _invite_body(actions.CONFIG_CANCEL_GROUP_INVITE), MagicMock(), MagicMock(), context={}
            )
        text = updated.call_args.args[2]
        assert text.startswith(":eject: *SyncBot Action Notice*")
        assert "Action: Cancel Invite" in text
        assert "Workspace: Workspace B" in text
        assert "Workspaces in Sync" not in text


class TestWorkspaceManagerGate:
    """Non-admins are never managers unless listed as extra managers in Settings."""

    def test_extra_manager_is_a_manager(self):
        import helpers

        client = MagicMock()
        with (
            patch("helpers.slack_api._users_info", return_value={"user": {"is_admin": False, "is_owner": False}}),
            patch("helpers.workspace_settings.extra_manager_user_ids", return_value=["U1"]),
        ):
            assert helpers.is_workspace_manager(client, "U1", "T1") is True

    def test_require_admin_env_is_ignored(self):
        import helpers
        from helpers import core as core_mod

        client = MagicMock()
        core_mod._REQUIRE_ADMIN_WARNED = False
        with (
            patch.dict(os.environ, {constants.REQUIRE_ADMIN: "false"}),
            patch("helpers.slack_api._users_info", return_value={"user": {"is_admin": False, "is_owner": False}}),
            patch("helpers.workspace_settings.extra_manager_user_ids", return_value=[]),
        ):
            assert helpers.is_workspace_manager(client, "U1", "T1") is False
        assert core_mod._REQUIRE_ADMIN_WARNED is True


class TestJoinGroupSubmit:
    def test_invalid_group_code_log_is_sanitized(self):
        from handlers.group import handle_join_group_submit_ack

        client = MagicMock()
        workspace = SimpleNamespace(id=42)

        body = {
            "user": {"id": "U1"},
            "view": {"state": {"values": {}}},
        }

        with (
            patch("handlers.group._get_authorized_workspace", return_value=("U1", workspace)),
            patch("handlers.group.helpers._cache_get", return_value=0),
            patch("handlers.group.helpers._cache_set"),
            patch("handlers.group.DbManager.find_records", return_value=[]),
            patch("handlers.group.log_warning") as warn_log,
        ):
            result = handle_join_group_submit_ack(body, client, context={})

        matched = [call for call in warn_log.call_args_list if call.args and call.args[0] == "group_code_invalid"]
        assert matched, "Expected group_code_invalid warning"
        extra = matched[0].kwargs
        assert "code" not in extra
        assert extra["workspace_id"] == workspace.id
        assert extra["attempt"] == 1
        assert "code_length" in extra
        assert actions.CONFIG_JOIN_GROUP_CODE in result["errors"]

    def test_join_group_ack_stops_after_five_tries(self):
        from handlers.group import handle_join_group_submit_ack

        workspace = SimpleNamespace(id=42)
        with (
            patch("handlers.group._get_authorized_workspace", return_value=("U1", workspace)),
            patch("handlers.group.helpers._cache_get", return_value=5),
            patch("handlers.group.DbManager.find_records") as find_records,
        ):
            result = handle_join_group_submit_ack({"user": {"id": "U1"}, "view": {}}, MagicMock(), {})
        assert result["response_action"] == "errors"
        find_records.assert_not_called()

    def test_join_group_ack_accepts_a_matching_code(self):
        from handlers.group import handle_join_group_submit_ack

        workspace = SimpleNamespace(id=42)
        group = SimpleNamespace(id=7)
        with (
            patch("handlers.group._get_authorized_workspace", return_value=("U1", workspace)),
            patch("handlers.group.helpers._cache_get", return_value=0),
            patch("handlers.group._get_text_input_value", return_value="ABC-DEF"),
            patch("handlers.group.DbManager.find_records", return_value=[group]),
        ):
            result = handle_join_group_submit_ack({"user": {"id": "U1"}, "view": {}}, MagicMock(), {})
        assert result is None

    def test_join_group_ack_counts_each_submission_once(self):
        from handlers.group import handle_join_group_submit_ack

        workspace = SimpleNamespace(id=42)
        store: dict = {}

        def cache_get(key):
            return store.get(key)

        def cache_set(key, value, ttl=900):
            store[key] = value

        body = {"trigger_id": "T1", "user": {"id": "U1"}, "view": {"id": "V1", "state": {"values": {}}}}
        again = {"trigger_id": "T2", "user": {"id": "U1"}, "view": {"id": "V1", "state": {"values": {}}}}
        with (
            patch("handlers.group._get_authorized_workspace", return_value=("U1", workspace)),
            patch("handlers.group.helpers._cache_get", side_effect=cache_get),
            patch("handlers.group.helpers._cache_set", side_effect=cache_set),
            patch("handlers.group.DbManager.find_records", return_value=[]),
        ):
            handle_join_group_submit_ack(body, MagicMock(), {})
            handle_join_group_submit_ack(body, MagicMock(), {})
            assert store[f"group_join_attempts:{workspace.id}"]["count"] == 1
            handle_join_group_submit_ack(again, MagicMock(), {})
        assert store[f"group_join_attempts:{workspace.id}"]["count"] == 2

    def test_join_group_ack_counts_a_submission_with_no_trigger_id(self):
        from handlers.group import handle_join_group_submit_ack

        workspace = SimpleNamespace(id=42)
        store: dict = {}

        def cache_get(key):
            return store.get(key)

        def cache_set(key, value, ttl=900):
            store[key] = value

        body = {"user": {"id": "U1"}, "view": {"id": "V1", "state": {"values": {}}}}
        with (
            patch("handlers.group._get_authorized_workspace", return_value=("U1", workspace)),
            patch("handlers.group.helpers._cache_get", side_effect=cache_get),
            patch("handlers.group.helpers._cache_set", side_effect=cache_set),
            patch("handlers.group.DbManager.find_records", return_value=[]),
        ):
            handle_join_group_submit_ack(body, MagicMock(), {})
            handle_join_group_submit_ack(body, MagicMock(), {})
        assert store[f"group_join_attempts:{workspace.id}"]["count"] == 2

    def test_join_by_code_tells_connected_instances(self):
        workspace = SimpleNamespace(id=42, team_id="T1", workspace_name="Workspace A", deleted_at=None)
        group = SimpleNamespace(id=7, name="Shared")
        body = {"user": {"id": "U1"}, "view": {"state": {"values": {}}}}

        def create_record(record):
            record.id = 3
            return record

        with (
            patch("handlers.group._get_authorized_workspace", return_value=("U1", workspace)),
            patch("handlers.group._get_text_input_value", return_value="ABC-DEF"),
            patch("handlers.group.helpers._cache_get", return_value=0),
            patch("handlers.group.DbManager.find_records", side_effect=[[group], []]),
            patch("handlers.group.DbManager.create_record", side_effect=create_record),
            patch("handlers.group.helpers.format_admin_label", return_value="Ada Lovelace"),
            patch("handlers.group.helpers.resolve_workspace_name", return_value="Workspace A"),
            patch("handlers.group._activate_group_membership"),
            patch("helpers.notifications.notify_group_managers", return_value=[]),
            patch("handlers.group.builders.refresh_home_tab_for_workspace"),
            patch("federation.replicate.replicate_group_invite") as replicate,
        ):
            handle_join_group_submit(body, MagicMock(), MagicMock(), {})
        replicate.assert_called_once()
        assert replicate.call_args.args[0] is group
        assert replicate.call_args.args[1] is workspace
        assert replicate.call_args.kwargs["announce"] is True
        assert "Join Group" in replicate.call_args.kwargs["notice"]


def test_decline_of_an_invite_from_another_instance_tells_that_instance(real_db):
    from datetime import UTC, datetime

    from db import DbManager, schemas
    from federation.core import get_instance_id, public_key_fingerprint
    from handlers.group import handle_decline_group_invite

    now = datetime.now(UTC).replace(tzinfo=None)
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private = Ed25519PrivateKey.generate()
    public = (
        private.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    fed = DbManager.create_record(
        schemas.Instance(
            instance_id=public_key_fingerprint(public),
            webhook_url="https://peer.example/api/federation",
            public_key=public,
            status="active",
            trust_status="trusted",
            name="Partner Org",
            created_at=now,
        )
    )
    invited = DbManager.create_record(
        schemas.Workspace(team_id="TB", workspace_name="Workspace B", instance_id=get_instance_id())
    )
    inviter = DbManager.create_record(
        schemas.Workspace(team_id="TSTUB", workspace_name="Workspace A", instance_id=fed.instance_id)
    )
    group = DbManager.create_record(
        schemas.WorkspaceGroup(name="Shared", invite_code="GRP-TEST01", status="active", created_at=now, uid="g-dec")
    )
    member = DbManager.create_record(
        schemas.WorkspaceGroupMember(
            group_id=group.id,
            workspace_id=invited.id,
            status="pending",
            role="member",
            invited_by_workspace_id=inviter.id,
        )
    )
    body = {"user": {"id": "U1"}, "actions": [{"value": str(member.id)}]}
    with (
        patch("handlers.group._get_authorized_workspace", return_value=("U1", invited)),
        patch("handlers.group.helpers.format_admin_label", return_value="Ada Lovelace"),
        patch("handlers.group.helpers.resolve_workspace_name", side_effect=lambda ws: ws.workspace_name),
        patch("helpers.notifications.notify_group_managers", return_value=[]),
        patch("handlers.group.helpers.update_group_invite_dms"),
        patch("handlers.group.builders.refresh_home_tab_for_workspace"),
        patch("federation.replicate.replicate_group_leave") as leave,
    ):
        handle_decline_group_invite(body, MagicMock(), MagicMock(), {})
    leave.assert_called_once()
    assert leave.call_args.args[1].id == invited.id
    assert "Decline" in leave.call_args.kwargs["notice"]


def test_decline_of_a_same_instance_invite_stays_here(real_db):
    from datetime import UTC, datetime

    from db import DbManager, schemas
    from federation.core import get_instance_id
    from handlers.group import handle_decline_group_invite

    now = datetime.now(UTC).replace(tzinfo=None)
    instance_id = get_instance_id()
    invited = DbManager.create_record(
        schemas.Workspace(team_id="TB", workspace_name="Workspace B", instance_id=instance_id)
    )
    inviter = DbManager.create_record(
        schemas.Workspace(team_id="TA", workspace_name="Workspace A", instance_id=instance_id)
    )
    group = DbManager.create_record(
        schemas.WorkspaceGroup(name="Shared", invite_code="GRP-TEST01", status="active", created_at=now, uid="g-same")
    )
    member = DbManager.create_record(
        schemas.WorkspaceGroupMember(
            group_id=group.id,
            workspace_id=invited.id,
            status="pending",
            role="member",
            invited_by_workspace_id=inviter.id,
        )
    )
    body = {"user": {"id": "U1"}, "actions": [{"value": str(member.id)}]}
    with (
        patch("handlers.group._get_authorized_workspace", return_value=("U1", invited)),
        patch("handlers.group.helpers.format_admin_label", return_value="Ada Lovelace"),
        patch("handlers.group.helpers.resolve_workspace_name", side_effect=lambda ws: ws.workspace_name),
        patch("helpers.notifications.notify_group_managers", return_value=[]),
        patch("handlers.group.helpers.update_group_invite_dms"),
        patch("handlers.group.builders.refresh_home_tab_for_workspace"),
        patch("federation.replicate.replicate_group_leave") as leave,
    ):
        handle_decline_group_invite(body, MagicMock(), MagicMock(), {})
    leave.assert_not_called()


def test_cancel_of_an_invite_to_another_instance_tells_that_instance(real_db):
    from datetime import UTC, datetime

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from db import DbManager, schemas
    from federation.core import get_instance_id, public_key_fingerprint
    from handlers.group import handle_cancel_group_invite_confirm

    now = datetime.now(UTC).replace(tzinfo=None)
    private = Ed25519PrivateKey.generate()
    public = (
        private.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    fed = DbManager.create_record(
        schemas.Instance(
            instance_id=public_key_fingerprint(public),
            webhook_url="https://peer.example/api/federation",
            public_key=public,
            status="active",
            trust_status="trusted",
            name="Partner Org",
            created_at=now,
        )
    )
    inviter = DbManager.create_record(
        schemas.Workspace(team_id="TA", workspace_name="Workspace A", instance_id=get_instance_id())
    )
    target = DbManager.create_record(
        schemas.Workspace(team_id="TSTUB", workspace_name="Workspace B", instance_id=fed.instance_id)
    )
    group = DbManager.create_record(
        schemas.WorkspaceGroup(name="Shared", invite_code="GRP-TEST01", status="active", created_at=now, uid="g-can")
    )
    member = DbManager.create_record(
        schemas.WorkspaceGroupMember(
            group_id=group.id,
            workspace_id=target.id,
            status="pending",
            role="member",
            invited_by_workspace_id=inviter.id,
        )
    )
    body = {"user": {"id": "U1"}, "actions": [{"value": str(member.id)}]}
    with (
        patch("handlers.group._get_authorized_workspace", return_value=("U1", inviter)),
        patch("handlers.group.helpers.format_admin_label", return_value="Ada Lovelace"),
        patch("handlers.group.helpers.resolve_workspace_name", side_effect=lambda ws: ws.workspace_name),
        patch("handlers.group.helpers.is_workspace_owner", return_value=False),
        patch("helpers.notifications.notify_group_managers", return_value=[]),
        patch("handlers.group.helpers.update_group_invite_dms"),
        patch("handlers.group.builders.refresh_home_tab_for_workspace"),
        patch("federation.replicate.replicate_group_leave") as leave,
    ):
        handle_cancel_group_invite_confirm(body, MagicMock(), MagicMock(), {})
    leave.assert_called_once()
    assert leave.call_args.args[1].id == target.id
    assert "Cancel Invite" in leave.call_args.kwargs["notice"]


def test_invite_workspace_modal_has_one_invite_code_section():
    from handlers.group import handle_invite_workspace

    client = MagicMock()
    workspace = SimpleNamespace(id=1, team_id="T1")
    group = SimpleNamespace(id=5, invite_code="GRP-ABC")
    other = SimpleNamespace(id=2, team_id="T2", workspace_name="Other")
    body = {
        "trigger_id": "tr",
        "actions": [{"value": "5"}],
        "user": {"id": "U1"},
        "team": {"id": "T1"},
    }
    with (
        patch("handlers.group._get_authorized_workspace", return_value=("U1", workspace)),
        patch("handlers.group.DbManager.get_record", return_value=group),
        patch("handlers.group.DbManager.find_records", side_effect=[[], [other]]),
        patch("handlers.group.helpers.federation_enabled", return_value=True),
        patch("helpers.workspace_kind.is_deliverable_workspace", return_value=True),
        patch("helpers.workspace_kind.is_stub_workspace", return_value=False),
        patch("handlers.group.helpers.resolve_workspace_name", return_value="Other"),
    ):
        handle_invite_workspace(body, client, MagicMock(), {})
    view = client.views_update.call_args.kwargs["view"]
    text = repr(view)
    assert text.count("*Invite Code*") == 1
    assert "External Workspace" not in text
    assert "immediately" not in text
    assert "on this SyncBot instance" in text


@pytest.fixture
def real_db(tmp_path):
    import db as db_mod
    from db import initialize_database
    from helpers._cache import clear_all_caches

    url = f"sqlite:///{tmp_path / 'group_handlers.db'}"
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


def test_invite_to_a_workspace_on_another_instance_waits_for_accept(real_db):
    import json
    from datetime import UTC, datetime

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from db import DbManager, schemas
    from federation.core import get_instance_id, public_key_fingerprint
    from handlers.group import handle_invite_workspace_submit
    from slack.actions import CONFIG_INVITE_WORKSPACE_SELECT

    now = datetime.now(UTC).replace(tzinfo=None)
    private = Ed25519PrivateKey.generate()
    public = (
        private.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    fed = DbManager.create_record(
        schemas.Instance(
            instance_id=public_key_fingerprint(public),
            webhook_url="https://peer.example/api/federation",
            public_key=public,
            status="active",
            trust_status="trusted",
            name="Partner Org",
            created_at=now,
        )
    )
    inviter = DbManager.create_record(
        schemas.Workspace(team_id="TLOCAL", workspace_name="Workspace A", instance_id=get_instance_id())
    )
    target = DbManager.create_record(
        schemas.Workspace(team_id="TSTUB", workspace_name="Workspace B", instance_id=fed.instance_id)
    )
    group = DbManager.create_record(
        schemas.WorkspaceGroup(name="Shared", invite_code="GRP-TEST01", status="active", created_at=now, uid="g-1")
    )
    body = {
        "user": {"id": "U1"},
        "view": {
            "private_metadata": json.dumps({"group_id": group.id}),
            "state": {
                "values": {"b": {CONFIG_INVITE_WORKSPACE_SELECT: {"selected_option": {"value": str(target.id)}}}}
            },
        },
    }
    with (
        patch("handlers.group._get_authorized_workspace", return_value=("U1", inviter)),
        patch("handlers.group.helpers.format_admin_label", return_value="Ada Lovelace"),
        patch("handlers.group.helpers.resolve_workspace_name", side_effect=lambda ws: ws.workspace_name),
        patch("handlers.group.helpers.send_group_invite_dm") as dm,
        patch("federation.replicate.replicate_group_invite") as replicate,
        patch("handlers.group.builders.refresh_home_tab_for_workspace"),
    ):
        handle_invite_workspace_submit(body, MagicMock(), MagicMock(), {})
    rows = DbManager.find_records(
        schemas.WorkspaceGroupMember,
        [
            schemas.WorkspaceGroupMember.group_id == group.id,
            schemas.WorkspaceGroupMember.workspace_id == target.id,
            schemas.WorkspaceGroupMember.deleted_at.is_(None),
        ],
    )
    assert len(rows) == 1
    assert rows[0].status == "pending"
    assert replicate.call_count == 2
    first, second = replicate.call_args_list
    assert first.args[0].id == group.id
    assert first.args[1].id == inviter.id
    assert first.kwargs["peer"].instance_id == fed.instance_id
    assert second.args[0].id == group.id
    assert second.args[1].id == target.id
    assert second.kwargs["peer"].instance_id == fed.instance_id
    assert second.kwargs["invited_by"].id == inviter.id
    assert "notice" in second.kwargs
    dm.assert_not_called()


def test_invite_picker_lists_a_peer_workspace_only_when_this_workspace_is_shared():
    from handlers.group import handle_invite_workspace

    workspace = SimpleNamespace(id=1, team_id="T1", instance_id="self")
    group = SimpleNamespace(id=5, invite_code="GRP-ABC")
    shared = SimpleNamespace(id=2, team_id="TSHARE", workspace_name="Workspace B", instance_id="peer-b")
    hidden = SimpleNamespace(id=3, team_id="THIDE", workspace_name="Workspace C", instance_id="peer-c")
    peer_b = SimpleNamespace(instance_id="peer-b")
    peer_c = SimpleNamespace(instance_id="peer-c")
    body = {
        "trigger_id": "tr",
        "actions": [{"value": "5"}],
        "user": {"id": "U1"},
        "team": {"id": "T1"},
    }

    def allowed(_workspace, peer):
        return getattr(peer, "instance_id", None) == "peer-b"

    def peer_for(ws):
        return peer_b if ws is shared else peer_c

    client = MagicMock()
    with (
        patch("handlers.group._get_authorized_workspace", return_value=("U1", workspace)),
        patch("handlers.group.DbManager.get_record", return_value=group),
        patch("handlers.group.DbManager.find_records", side_effect=[[], [shared, hidden]]),
        patch("handlers.group.helpers.federation_enabled", return_value=True),
        patch("helpers.workspace_kind.is_deliverable_workspace", return_value=True),
        patch("helpers.workspace_kind.is_stub_workspace", return_value=True),
        patch("helpers.workspace_kind.workspace_allowed_for_peer", side_effect=allowed),
        patch("helpers.workspace_kind.peer_for_workspace", side_effect=peer_for),
        patch("handlers.group.helpers.resolve_workspace_name", side_effect=lambda ws: ws.workspace_name),
    ):
        handle_invite_workspace(body, client, MagicMock(), {})
    view = client.views_update.call_args.kwargs["view"]
    text = repr(view)
    assert "Workspace B" in text
    assert "Workspace C" not in text


class TestCreateGroupSubmitMintsUid:
    def test_create_group_mints_a_uid(self):
        created = []

        def create_record(record):
            record.id = 1 + len(created)
            created.append(record)
            return record

        workspace = SimpleNamespace(id=10, team_id="T1", deleted_at=None)
        client = MagicMock()
        client.conversations_open.return_value = {"channel": {"id": "D1"}}
        with (
            patch("handlers.group._get_authorized_workspace", return_value=("U1", workspace)),
            patch("handlers.group._get_text_input_value", return_value="Shared"),
            patch("handlers.group.DbManager.create_record", side_effect=create_record),
            patch("handlers.group.builders.refresh_home_tab_for_workspace"),
        ):
            handle_create_group_submit({"view": {"team_id": "T1"}}, client, MagicMock(), {})

        assert created[0].__class__.__name__ == "WorkspaceGroup"
        assert created[0].uid
        assert len(created[0].uid) == 36

    def test_group_created_is_logged_without_an_invite_code(self, caplog):
        import logging

        workspace = SimpleNamespace(id=10, team_id="T1", deleted_at=None)
        client = MagicMock()
        client.conversations_open.return_value = {"channel": {"id": "D1"}}

        def create_record(record):
            record.id = 4
            return record

        with (
            patch("handlers.group._get_authorized_workspace", return_value=("U1", workspace)),
            patch("handlers.group._get_text_input_value", return_value="Shared"),
            patch("handlers.group.DbManager.create_record", side_effect=create_record),
            patch("handlers.group.builders.refresh_home_tab_for_workspace"),
            caplog.at_level(logging.INFO),
        ):
            handle_create_group_submit({"view": {"team_id": "T1"}}, client, MagicMock(), {})
        created_logs = [record for record in caplog.records if record.message == "group_created"]
        assert created_logs
        assert "invite_code" not in created_logs[0].__dict__
