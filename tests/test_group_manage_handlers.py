"""Authorization tests for the ownership and disband handlers.

These use the mocked-DbManager style of the other handler tests; the real-DB
rule semantics live in ``test_group_roles.py``.
"""

import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("DATABASE_HOST", "localhost")
os.environ.setdefault("DATABASE_USER", "root")
os.environ.setdefault("DATABASE_PASSWORD", "test")
os.environ.setdefault("DATABASE_SCHEMA", "syncbot")
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-0-0")

from db import schemas  # noqa: E402
from handlers.group_manage import (  # noqa: E402
    handle_demote_self_confirm,
    handle_disband_group_confirm,
    handle_leave_group_confirm,
    handle_promote_to_owner,
    handle_promote_to_owner_confirm,
    handle_remove_group_member_confirm,
)
from slack import actions  # noqa: E402

GROUP_ID = 5
SYNC_ID = 7
MEMBER_ID = 12
ACTING_WS_ID = 1
OTHER_WS_ID = 2


def _action_body(prefix, target_id):
    return {
        "user": {"id": "U1", "team_id": "T1"},
        "team": {"id": "T1"},
        "actions": [{"value": str(target_id), "action_id": f"{prefix}_{target_id}"}],
    }


class TestPromoteToOwner:
    def _run(self, *, acting_is_owner, target_eligible=True):
        acting = SimpleNamespace(id=ACTING_WS_ID, team_id="T1", deleted_at=None)
        # get_record is mocked for both the member and the group lookup, so this
        # stands in for either; `name` is the group's.
        target = SimpleNamespace(id=MEMBER_ID, group_id=GROUP_ID, workspace_id=OTHER_WS_ID, role="member", name="G")
        eligible = [target] if target_eligible else []

        with (
            patch("handlers._common._get_authorized_workspace", return_value=("U1", acting)),
            patch("handlers.group_manage.DbManager.get_record", return_value=target),
            patch("handlers.group_manage.helpers.is_workspace_owner", return_value=acting_is_owner),
            patch("handlers.group_manage.helpers.get_promotable_members", return_value=eligible),
            patch("handlers.group_manage.DbManager.update_records") as update_records,
            patch("handlers.group_manage.helpers.get_workspace_by_id", return_value=acting),
            patch("handlers.group_manage.helpers.resolve_workspace_name", return_value="WS"),
            patch("handlers.group_manage.helpers.format_admin_label", return_value="`Ada (WS)`"),
            patch("handlers.group_manage._notify_group_admins"),
            patch("handlers.group_manage.builders.refresh_home_tab_for_workspace"),
            patch("federation.replicate.replicate_group_invite") as replicate,
        ):
            handle_promote_to_owner_confirm(
                _action_body(actions.CONFIG_PROMOTE_TO_OWNER_CONFIRM, MEMBER_ID), MagicMock(), MagicMock(), context={}
            )
        self.replicate = replicate
        return update_records

    def test_owner_may_promote_an_eligible_member(self):
        assert self._run(acting_is_owner=True).called
        assert self.replicate.call_args.kwargs["set_role"] is True
        notice = self.replicate.call_args.kwargs["notice"]
        assert notice.startswith(":key: *SyncBot Action Notice*")
        assert "Action: Promote" in notice
        assert "Workspace: WS" in notice
        assert "Workspaces in Sync" not in notice

    def test_non_owner_may_not_promote(self):
        assert not self._run(acting_is_owner=False).called

    def test_opener_posts_a_confirm_and_does_not_promote(self):
        acting = SimpleNamespace(id=ACTING_WS_ID, team_id="T1", deleted_at=None)
        target = SimpleNamespace(id=MEMBER_ID, group_id=GROUP_ID, workspace_id=OTHER_WS_ID, role="member", name="G")
        client = MagicMock()
        body = _action_body(actions.CONFIG_PROMOTE_TO_OWNER, MEMBER_ID)
        body["trigger_id"] = "tr"
        with (
            patch("handlers._common._get_authorized_workspace", return_value=("U1", acting)),
            patch("handlers.group_manage.DbManager.get_record", return_value=target),
            patch("handlers.group_manage.helpers.is_workspace_owner", return_value=True),
            patch("handlers.group_manage.helpers.get_promotable_members", return_value=[target]),
            patch("handlers.group_manage.DbManager.update_records") as update_records,
            patch("handlers.group_manage.helpers.get_workspace_by_id", return_value=acting),
            patch("handlers.group_manage.helpers.resolve_workspace_name", return_value="Workspace B"),
        ):
            handle_promote_to_owner(body, client, MagicMock(), context={})
        update_records.assert_not_called()
        view = client.views_update.call_args.kwargs["view"]
        assert "submit" not in view
        assert "Make `Workspace B` an Owner of `G`?" in str(view)

    def test_ineligible_target_is_rejected(self):
        """Pending invites are still refused. A stub is eligible upstream."""
        assert not self._run(acting_is_owner=True, target_eligible=False).called


class TestDemoteSelf:
    def _run(self, *, target_workspace_id, owner_count):
        acting = SimpleNamespace(id=ACTING_WS_ID, team_id="T1", deleted_at=None)
        target = SimpleNamespace(
            id=MEMBER_ID, group_id=GROUP_ID, workspace_id=target_workspace_id, role="owner", name="G"
        )
        owners = [SimpleNamespace(id=MEMBER_ID + i, workspace_id=i) for i in range(owner_count)]
        owners[0] = SimpleNamespace(id=MEMBER_ID, workspace_id=target_workspace_id)

        with (
            patch("handlers._common._get_authorized_workspace", return_value=("U1", acting)),
            patch("handlers.group_manage.DbManager.get_record", return_value=target),
            patch("handlers.group_manage.helpers.get_active_owners", return_value=owners),
            patch("handlers.group_manage.DbManager.update_records") as update_records,
            patch("handlers.group_manage.helpers.resolve_workspace_name", return_value="WS"),
            patch("handlers.group_manage.helpers.format_admin_label", return_value="`Ada (WS)`"),
            patch("handlers.group_manage._notify_group_admins"),
            patch("handlers.group_manage.builders.refresh_home_tab_for_workspace"),
            patch("federation.replicate.replicate_group_invite") as replicate,
        ):
            handle_demote_self_confirm(
                _action_body(actions.CONFIG_DEMOTE_SELF_CONFIRM, MEMBER_ID), MagicMock(), MagicMock(), context={}
            )
        self.replicate = replicate
        return update_records

    def test_owner_may_demote_itself_while_another_owner_remains(self):
        assert self._run(target_workspace_id=ACTING_WS_ID, owner_count=2).called
        assert self.replicate.call_args.kwargs["set_role"] is True
        notice = self.replicate.call_args.kwargs["notice"]
        assert notice.startswith(":door: *SyncBot Action Notice*")
        assert "Action: Give Up Ownership" in notice
        assert "Workspace: WS" in notice
        assert "Workspaces in Sync" not in notice
        assert "no longer an Owner" not in notice

    def test_sole_owner_may_not_demote_itself(self):
        assert not self._run(target_workspace_id=ACTING_WS_ID, owner_count=1).called

    def test_an_owner_may_not_demote_a_peer(self):
        """Self-demotion only: letting owners demote each other invites ownership fights."""
        assert not self._run(target_workspace_id=OTHER_WS_ID, owner_count=2).called


class TestDisbandConfirm:
    def _run(self, *, can_disband, blocked=False, delete_history=False):
        acting = SimpleNamespace(
            id=ACTING_WS_ID, team_id="T1", deleted_at=None, workspace_name="Workspace A", instance_id="self"
        )
        stub = SimpleNamespace(
            id=OTHER_WS_ID, team_id="T2", deleted_at=None, workspace_name="Workspace B", instance_id="peer"
        )
        workspaces = {acting.id: acting, stub.id: stub}
        group = SimpleNamespace(id=GROUP_ID, name="G")
        sync = SimpleNamespace(id=SYNC_ID, group_id=GROUP_ID, uid="sync-uid", title="general")
        local_row = SimpleNamespace(id=31, sync_id=SYNC_ID, workspace_id=acting.id, channel_id="C_LOCAL")
        stub_row = SimpleNamespace(id=32, sync_id=SYNC_ID, workspace_id=stub.id, channel_id="C_STUB")
        rows_by_model = {
            schemas.WorkspaceGroup: [group],
            schemas.Sync: [sync],
            schemas.SyncChannel: [local_row, stub_row],
        }
        meta = {"group_id": GROUP_ID, "delete_history": delete_history}
        if blocked:
            meta["blocked"] = True

        with (
            patch("handlers.group_manage.helpers.get_user_id_from_body", return_value="U1"),
            patch("handlers.group_manage.helpers.is_workspace_manager", return_value=True),
            patch("handlers._common._parse_private_metadata", return_value=meta),
            patch("handlers.group_manage.helpers.get_workspace_record", return_value=acting),
            patch(
                "handlers.group_manage.helpers.can_disband",
                return_value=(can_disband, "" if can_disband else "co_owner_exists"),
            ),
            patch(
                "handlers.group_manage.DbManager.find_records",
                side_effect=lambda model, *_args, **_kwargs: list(rows_by_model.get(model, [])),
            ),
            patch("handlers.group_manage.helpers.get_workspace_by_id", side_effect=workspaces.get),
            # Lazy imports inside the handler: patch where each name lives.
            patch("helpers.workspace_kind.is_local_workspace", side_effect=lambda ws: ws is acting),
            patch("federation.replicate.replicate_sync_ended") as replicate_ended,
            patch("handlers.group_manage.soft_delete_sync_channels", return_value=1) as soft_delete,
            patch("helpers.leave_sync.DbManager.get_record", return_value=None),
            patch("handlers.group_manage.helpers.soft_delete_retention_days", return_value=14),
            patch("handlers.group_manage.helpers.format_admin_label", return_value="`A (Workspace A)`"),
            patch(
                "helpers.sync_participation.format_workspaces_in_sync",
                return_value="`Workspace A [Publish and Subscribe]`",
            ),
            patch("helpers.notifications.notify_group_managers", return_value=[]) as group_dms,
            patch("handlers.group_manage.DbManager.update_records") as update_records,
            patch("handlers.group_manage.DbManager.delete_records") as delete_records,
            patch("handlers.group_manage.builders.refresh_home_tab_for_workspace"),
            patch("federation.replicate.replicate_group_upsert") as replicate_group,
        ):
            body = {"view": {"team_id": "T1"}}
            handle_disband_group_confirm(body, MagicMock(), MagicMock(), context={})
        return SimpleNamespace(
            acting=acting,
            stub=stub,
            group=group,
            sync=sync,
            stub_row=stub_row,
            soft_delete=soft_delete,
            replicate_ended=replicate_ended,
            replicate_group=replicate_group,
            group_dms=group_dms,
            update_records=update_records,
            delete_records=delete_records,
        )

    def test_disband_proceeds_when_both_gates_pass(self):
        result = self._run(can_disband=True)
        assert result.delete_records.called
        assert any(c.args[0] is schemas.WorkspaceGroup for c in result.update_records.call_args_list)
        result.replicate_group.assert_called_once()
        assert result.replicate_group.call_args.args[0].status == "disbanded"
        notice = result.replicate_group.call_args.kwargs["notice"]
        assert "*SyncBot Action Notice*" in notice
        assert "Action: Disband" in notice
        assert "Workspaces in Sync: Workspace A [Publish and Subscribe]" in notice
        assert "Data Retention Days: 14" in notice
        assert "Channel Syncs have been removed" not in notice
        assert "has been disbanded" not in notice
        result.group_dms.assert_called_once()
        assert result.group_dms.call_args.kwargs["exclude_workspace_id"] == ACTING_WS_ID
        assert result.group_dms.call_args.args[1] == notice
        hold = result.soft_delete.call_args.kwargs["hold_channels"]
        assert (ACTING_WS_ID, "C_LOCAL") in hold
        assert (OTHER_WS_ID, "C_STUB") in hold

    def test_checked_history_box_purges_every_workspaces_rows(self):
        with patch("helpers.sync_cleanup.purge_sync_channels") as purge:
            result = self._run(can_disband=True, delete_history=True)
        purge.assert_called_once()
        assert sorted(row.channel_id for row in purge.call_args.args[0]) == ["C_LOCAL", "C_STUB"]
        assert result.soft_delete.call_args.kwargs["purge"] is True
        assert "channel_notice" not in result.soft_delete.call_args.kwargs
        notice = result.replicate_group.call_args.kwargs["notice"]
        assert "History: Deleted" in notice
        assert "Data Retention Days" not in notice
        assert "Action: Disband" in result.replicate_ended.call_args.kwargs["notice"]
        assert result.replicate_ended.call_args.kwargs["purge"] is True

    def test_disband_soft_deletes_each_channel_and_tells_peers_about_stub_rows(self):
        """Every live row is soft-deleted; only rows on another instance need replicate_sync_ended."""
        result = self._run(can_disband=True)
        assert result.soft_delete.call_args_list[0].args[:3] == (
            result.acting,
            "C_LOCAL",
            "group_disbanded",
        )
        assert result.soft_delete.call_args_list[1].args[:3] == (result.stub, "C_STUB", "group_disbanded")
        assert result.soft_delete.call_args_list[0].kwargs["dm"] is True
        assert result.soft_delete.call_args_list[1].kwargs["dm"] is False
        assert result.soft_delete.call_args.kwargs["purge"] is False
        assert "channel_notice" not in result.soft_delete.call_args.kwargs
        assert "Action: Disband" in result.replicate_ended.call_args.kwargs["notice"]
        assert "does not come back" not in result.replicate_ended.call_args.kwargs["notice"]
        result.replicate_ended.assert_called_once()
        assert result.replicate_ended.call_args.kwargs["reason"] == "group_disbanded"
        assert result.replicate_ended.call_args.kwargs["purge"] is False
        assert "14 days" not in result.replicate_ended.call_args.kwargs["notice"]

    def test_disband_is_rejected_on_submit_when_a_gate_fails(self):
        """main_response has no auth gate, so the modal-time check is bypassable."""
        result = self._run(can_disband=False)
        assert not result.delete_records.called
        assert not result.soft_delete.called

    def test_the_blocked_explanation_modal_does_not_disband(self):
        result = self._run(can_disband=True, blocked=True)
        assert not result.delete_records.called
        assert not result.soft_delete.called


class TestLeaveGroupConfirmOwnerGuard:
    def _run(self, *, can_leave, blocked=False):
        acting = SimpleNamespace(id=ACTING_WS_ID, team_id="T1", deleted_at=None)
        meta = {"group_id": GROUP_ID}
        if blocked:
            meta["blocked"] = True

        with (
            patch("handlers.group_manage.helpers.get_user_id_from_body", return_value="U1"),
            patch("handlers.group_manage.helpers.is_workspace_manager", return_value=True),
            patch("handlers._common._parse_private_metadata", return_value=meta),
            patch("handlers.group_manage.helpers.get_workspace_record", return_value=acting),
            patch(
                "handlers.group_manage.helpers.can_workspace_leave",
                return_value=(can_leave, "" if can_leave else "sole_owner"),
            ),
            patch("handlers.group_manage.DbManager.find_records", return_value=[]) as find_records,
        ):
            handle_leave_group_confirm({"view": {"team_id": "T1"}}, MagicMock(), MagicMock(), context={})
        return find_records

    def test_a_permitted_departure_proceeds_past_the_guard(self):
        assert self._run(can_leave=True).called

    def test_a_sole_owner_departure_is_rejected_on_submit(self):
        assert not self._run(can_leave=False).called

    def test_the_blocked_explanation_modal_does_not_leave(self):
        assert not self._run(can_leave=True, blocked=True).called


class TestGroupManagerNotices:
    def test_leave_tells_other_workspaces_once_per_sync(self):
        acting = SimpleNamespace(id=ACTING_WS_ID, team_id="T1", workspace_name="Workspace A", deleted_at=None)
        group = SimpleNamespace(id=GROUP_ID, name="G")
        sync = SimpleNamespace(id=SYNC_ID, group_id=GROUP_ID, title="general")
        acting_member = SimpleNamespace(id=1, workspace_id=ACTING_WS_ID, status="active", deleted_at=None)
        other_member = SimpleNamespace(id=2, workspace_id=OTHER_WS_ID, status="active", deleted_at=None)

        def find_records(model, *_args, **_kwargs):
            if model is schemas.WorkspaceGroup:
                return [group]
            if model is schemas.Sync:
                return [sync]
            if model is schemas.WorkspaceGroupMember:
                return [acting_member, other_member]
            return []

        with (
            patch("handlers.group_manage.helpers.get_user_id_from_body", return_value="U1"),
            patch("handlers.group_manage.helpers.is_workspace_manager", return_value=True),
            patch("handlers._common._parse_private_metadata", return_value={"group_id": GROUP_ID}),
            patch("handlers.group_manage.helpers.get_workspace_record", return_value=acting),
            patch("handlers.group_manage.helpers.can_workspace_leave", return_value=(True, "")),
            patch("handlers.group_manage.DbManager.find_records", side_effect=find_records),
            patch("handlers.group_manage.DbManager.update_records"),
            patch("handlers.group_manage.DbManager.delete_records"),
            patch("handlers.group_manage.helpers.format_admin_label", return_value="`Ada (Workspace A)`"),
            patch("handlers.group_manage.helpers.resolve_workspace_name", return_value="Workspace A"),
            patch("handlers.group_manage.helpers.soft_delete_retention_days", return_value=14),
            patch(
                "helpers.sync_participation.format_workspaces_in_sync",
                return_value="`Workspace A [Publish and Subscribe]`",
            ),
            patch("helpers.notifications.notify_group_managers", return_value=[]) as group_dms,
            patch("federation.replicate.replicate_group_leave") as replicate,
            patch("handlers.group_manage.builders.refresh_home_tab_for_workspace"),
            patch("handlers._common._close_modal_done"),
        ):
            handle_leave_group_confirm({"view": {"team_id": "T1"}}, MagicMock(), MagicMock(), context={})

        notice = replicate.call_args.kwargs["notice"]
        assert notice.startswith(":wave: *SyncBot Action Notice*")
        assert "Action: Leave" in notice
        assert "Data Retention Days: 14" in notice
        assert "Workspaces in Sync: Workspace A [Publish and Subscribe]" in notice
        assert "left `" not in notice
        group_dms.assert_called_once()
        assert group_dms.call_args.kwargs["exclude_workspace_id"] == ACTING_WS_ID
        assert group_dms.call_args.args[1] == notice

    @patch("helpers.replace_group_invite_code")
    def test_remove_skips_the_workspace_that_already_gets_channel_detail(self, rotate):
        actor = SimpleNamespace(id=ACTING_WS_ID, team_id="T1", workspace_name="Workspace A", deleted_at=None)
        target = SimpleNamespace(id=OTHER_WS_ID, team_id="T2", workspace_name="Workspace B", deleted_at=None)
        member = SimpleNamespace(
            id=MEMBER_ID,
            group_id=GROUP_ID,
            workspace_id=OTHER_WS_ID,
            role="member",
            status="active",
            deleted_at=None,
        )
        group = SimpleNamespace(id=GROUP_ID, name="G")
        sync = SimpleNamespace(id=SYNC_ID, group_id=GROUP_ID)
        row = SimpleNamespace(sync_id=SYNC_ID, workspace_id=OTHER_WS_ID, channel_id="C1", deleted_at=None)

        def get_record(model, id=None, **_kwargs):
            if model is schemas.WorkspaceGroupMember:
                return member
            if model is schemas.WorkspaceGroup:
                return group
            return None

        def find_records(model, *_args, **_kwargs):
            if model is schemas.Sync:
                return [sync]
            if model is schemas.SyncChannel:
                return [row]
            return []

        with (
            patch("handlers.group_manage.helpers.get_user_id_from_body", return_value="U1"),
            patch("handlers.group_manage.helpers.get_team_id_from_body", return_value="T1"),
            patch("handlers.group_manage.helpers.is_workspace_manager", return_value=True),
            patch("handlers._common._parse_private_metadata", return_value={"member_id": MEMBER_ID}),
            patch("handlers.group_manage.helpers.get_workspace_record", return_value=actor),
            patch("handlers.group_manage.DbManager.get_record", side_effect=get_record),
            patch("handlers.group_manage.helpers.is_workspace_owner", return_value=True),
            patch("handlers.group_manage.helpers.get_workspace_by_id", return_value=target),
            patch("handlers.group_manage.DbManager.find_records", side_effect=find_records),
            patch("handlers.group_manage.DbManager.delete_records"),
            patch("handlers.group_manage.helpers.format_admin_label", return_value="`Ada (Workspace A)`"),
            patch(
                "handlers.group_manage.helpers.resolve_workspace_name",
                side_effect=lambda ws: ws.workspace_name,
            ),
            patch("handlers.group_manage.helpers.soft_delete_retention_days", return_value=14),
            patch(
                "helpers.sync_participation.format_workspaces_in_sync",
                return_value="`Workspace B [Publish and Subscribe]`",
            ),
            patch("helpers.workspace_kind.is_stub_workspace", return_value=False),
            patch("helpers.leave_sync.soft_delete_sync_channels") as soft_delete,
            patch("helpers.notifications.notify_group_managers", return_value=[]) as group_dms,
            patch("helpers.notifications.notify_admins_dm") as extra_dm,
            patch("federation.replicate.replicate_group_member_remove") as replicate,
            patch("handlers.group_manage.builders.refresh_home_tab_for_workspace"),
            patch("handlers._common._close_modal_done"),
        ):
            handle_remove_group_member_confirm({"view": {"team_id": "T1"}}, MagicMock(), MagicMock(), context={})

        notice = replicate.call_args.kwargs["notice"]
        assert notice.startswith(":eject: *SyncBot Action Notice*")
        assert "Action: Remove" in notice
        assert "Data Retention Days: 14" in notice
        assert "removed `" not in notice
        soft_delete.assert_called_once()
        assert soft_delete.call_args.kwargs.get("dm", True) is True
        group_dms.assert_called_once()
        assert group_dms.call_args.kwargs["exclude_workspace_id"] == OTHER_WS_ID
        assert group_dms.call_args.args[1] == notice
        extra_dm.assert_not_called()
        from handlers.group import _generate_invite_code

        rotate.assert_called_once_with(GROUP_ID, _generate_invite_code)


@pytest.fixture
def real_db(tmp_path):
    import db as db_mod
    from db import initialize_database
    from helpers._cache import clear_all_caches

    url = f"sqlite:///{tmp_path / 'group_manage.db'}"
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


def _seed_group_with_two_members():
    import json
    from datetime import UTC, datetime

    from db import DbManager, schemas
    from federation.core import get_instance_id

    now = datetime.now(UTC).replace(tzinfo=None)
    instance_id = get_instance_id()
    actor = DbManager.create_record(
        schemas.Workspace(team_id="TA", workspace_name="Workspace A", instance_id=instance_id)
    )
    target = DbManager.create_record(
        schemas.Workspace(team_id="TB", workspace_name="Workspace B", instance_id=instance_id)
    )
    group = DbManager.create_record(
        schemas.WorkspaceGroup(
            name="B Group",
            invite_code="OLD-CODE",
            status="active",
            created_at=now,
            uid="b-group",
        )
    )
    DbManager.create_record(
        schemas.WorkspaceGroupMember(
            group_id=group.id,
            workspace_id=actor.id,
            status="active",
            role="owner",
            joined_at=now,
        )
    )
    member = DbManager.create_record(
        schemas.WorkspaceGroupMember(
            group_id=group.id,
            workspace_id=target.id,
            status="active",
            role="member",
            joined_at=now,
        )
    )
    body = {
        "user": {"id": "U1"},
        "team": {"id": "TA"},
        "view": {"private_metadata": json.dumps({"member_id": member.id}), "team_id": "TA"},
    }
    return actor, group, member, body


def _remove_patches(actor):
    return (
        patch("handlers.group_manage.helpers.is_workspace_manager", return_value=True),
        patch("handlers.group_manage.helpers.get_workspace_record", return_value=actor),
        patch("handlers.group_manage.helpers.is_workspace_owner", return_value=True),
        patch("handlers.group_manage.helpers.format_admin_label", return_value="Ada Lovelace"),
        patch("handlers.group_manage.helpers.resolve_workspace_name", side_effect=lambda ws: ws.workspace_name),
        patch("helpers.notifications.notify_group_managers", return_value=[]),
        patch("federation.replicate.replicate_group_member_remove"),
        patch("handlers.group_manage.builders.refresh_home_tab_for_workspace"),
        patch("handlers._common._close_modal_done"),
    )


def test_remove_gives_the_group_a_new_invite_code(real_db):
    from contextlib import ExitStack

    from db import DbManager, schemas

    actor, group, _member, body = _seed_group_with_two_members()
    with ExitStack() as stack:
        for patcher in _remove_patches(actor):
            stack.enter_context(patcher)
        handle_remove_group_member_confirm(body, MagicMock(), MagicMock(), {})
    refreshed = DbManager.find_records(schemas.WorkspaceGroup, [schemas.WorkspaceGroup.id == group.id])[0]
    assert refreshed.invite_code != "OLD-CODE"
    assert DbManager.find_records(schemas.WorkspaceGroup, [schemas.WorkspaceGroup.invite_code == "OLD-CODE"]) == []


def test_remove_changes_the_invite_code_first(real_db):
    from db import DbManager, schemas

    actor, group, member, body = _seed_group_with_two_members()
    with (
        patch("handlers.group_manage.helpers.is_workspace_manager", return_value=True),
        patch("handlers.group_manage.helpers.get_workspace_record", return_value=actor),
        patch("handlers.group_manage.helpers.is_workspace_owner", return_value=True),
        patch("handlers.group_manage.helpers.format_admin_label", return_value="Ada Lovelace"),
        patch(
            "handlers.group_manage.helpers.resolve_workspace_name",
            side_effect=lambda ws: ws.workspace_name,
        ),
        patch("helpers.notifications.notify_group_managers", return_value=[]),
        patch("federation.replicate.replicate_group_member_remove") as replicate,
        patch("handlers.group_manage.builders.refresh_home_tab_for_workspace"),
        patch("handlers._common._close_modal_done"),
        patch("handlers.group_manage.helpers.replace_group_invite_code", side_effect=RuntimeError),
        pytest.raises(RuntimeError),
    ):
        handle_remove_group_member_confirm(body, MagicMock(), MagicMock(), {})
    kept = DbManager.find_records(
        schemas.WorkspaceGroupMember,
        [
            schemas.WorkspaceGroupMember.group_id == group.id,
            schemas.WorkspaceGroupMember.workspace_id == member.workspace_id,
        ],
    )
    assert kept
    assert kept[0].id == member.id
    replicate.assert_not_called()
