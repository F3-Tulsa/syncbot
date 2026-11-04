"""Group management — leave, remove, promote, demote, disband, and the Disband history checkbox."""

from datetime import UTC, datetime
from logging import Logger

from slack_sdk.web import WebClient

import builders
import helpers
from db import DbManager, schemas
from handlers._common import _counted
from helpers.leave_sync import soft_delete_sync_channels
from logger import log_info, log_warning
from slack import actions, orm


def handle_leave_group(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Show a confirmation modal before leaving a workspace group."""
    user_id = helpers.get_user_id_from_body(body)
    team_id = helpers.get_team_id_from_body(body)
    if not user_id or not team_id or not helpers.is_workspace_manager(client, user_id, team_id):
        log_warning("authorization_denied", user_id=user_id, action="leave_group")
        return

    action_data = helpers.safe_get(body, "actions", 0) or {}
    action_id: str = action_data.get("action_id", "")
    group_id_str = action_id.replace(f"{actions.CONFIG_LEAVE_GROUP}_", "")

    try:
        group_id = int(group_id_str)
    except (TypeError, ValueError):
        log_warning("leave_group_invalid_id", action_id=action_id)
        return

    groups = DbManager.find_records(schemas.WorkspaceGroup, [schemas.WorkspaceGroup.id == group_id])
    if not groups:
        return
    group = groups[0]

    trigger_id = helpers.safe_get(body, "trigger_id")
    if not trigger_id:
        return

    workspace_record = helpers.get_workspace_record(
        team_id,
        body,
        context,
        client,
    )

    # An owner may not abandon a group that would be left with no owner. Show
    # the reason in the modal rather than failing on submit.
    if workspace_record:
        allowed, reason = helpers.can_workspace_leave(group_id, workspace_record.id)
        if not allowed:
            blocked_form = orm.BlockView(
                blocks=[
                    orm.SectionBlock(
                        label=(
                            f':lock: *You are the only Owner of "{group.name}", so you cannot leave it yet.*\n\n'
                            "Every group needs at least one Owner. Promote another Workspace to Owner "
                            "from the group's member list, and then you will be able to leave.\n\n"
                            "_If you would rather shut the whole group down, and you are the only "
                            "Workspace publishing a Channel into it, use Disband Group instead._"
                        ),
                    ),
                ]
            )
            log_info("leave_group_blocked", group_id=group_id, workspace_id=workspace_record.id, reason=reason)
            blocked_form.post_modal(
                client=client,
                trigger_id=trigger_id,
                callback_id=actions.CONFIG_LEAVE_GROUP_CONFIRM,
                title_text="Leave Group",
                submit_button_text=None,
                close_button_text="Close",
                parent_metadata={"group_id": group_id, "blocked": True},
                body=body,
            )
            return

    days = helpers.soft_delete_retention_days()
    confirm_form = orm.BlockView(
        blocks=[
            orm.SectionBlock(
                label=(
                    f':warning: *Are you sure you want to leave the group "{group.name}"?*\n\n'
                    "This will:\n"
                    "\u2022 Stop all channel syncs you have in this group\n"
                    f"\u2022 Keep this Workspace's synced message history in this Group for {days} days. "
                    "It comes back if this Workspace rejoins the Group and joins the same Channels\n"
                    "\u2022 Delete this Group's user mappings now\n\n"
                    "_Other members will continue syncing. Mappings can be built again. Message history cannot._"
                ),
            ),
            orm.ActionsBlock(
                elements=[
                    orm.ButtonElement(
                        label=":wave: Leave Group",
                        action=actions.CONFIG_LEAVE_GROUP_CONFIRM,
                        value=str(group_id),
                        style="danger",
                    ),
                ]
            ),
        ]
    )

    confirm_form.post_modal(
        client=client,
        trigger_id=trigger_id,
        callback_id=actions.CONFIG_LEAVE_GROUP_CONFIRM,
        title_text="Leave Group",
        submit_button_text=None,
        close_button_text="Cancel",
        parent_metadata={"group_id": group_id},
        body=body,
    )


def handle_leave_group_confirm(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Soft-delete this workspace's rows in the group's Syncs, remove its mappings, mark the membership inactive, replicate, and tell the remaining members."""
    from handlers._common import _close_modal_done, _parse_private_metadata

    user_id = helpers.get_user_id_from_body(body)
    team_id = helpers.get_team_id_from_body(body)
    if not user_id or not team_id or not helpers.is_workspace_manager(client, user_id, team_id):
        log_warning("authorization_denied", user_id=user_id, action="leave_group_confirm")
        return

    meta = _parse_private_metadata(body)
    group_id = meta.get("group_id")
    if not group_id:
        log_warning("leave_group_confirm", reason="missing group_id in metadata")
        return

    if meta.get("blocked"):
        # The sole-owner explanation modal is close-only and never routes here;
        # this guards a forged payload.
        return

    workspace_record = helpers.get_workspace_record(team_id, body, context, client)
    if not workspace_record:
        return

    # Re-checked on submit: main_response has no authorization gate, so the
    # modal-time check alone is bypassable with a forged view submission.
    allowed, reason = helpers.can_workspace_leave(group_id, workspace_record.id)
    if not allowed:
        log_warning("leave_group_denied", group_id=group_id, workspace_id=workspace_record.id, reason=reason)
        return

    groups = DbManager.find_records(schemas.WorkspaceGroup, [schemas.WorkspaceGroup.id == group_id])
    if not groups:
        return
    group = groups[0]

    members = DbManager.find_records(
        schemas.WorkspaceGroupMember,
        [
            schemas.WorkspaceGroupMember.group_id == group_id,
            schemas.WorkspaceGroupMember.workspace_id == workspace_record.id,
            schemas.WorkspaceGroupMember.deleted_at.is_(None),
        ],
    )
    if not members:
        log_warning("leave_group_confirm_not_member", group_id=group_id)
        return

    acting_user_id = user_id
    person = helpers.format_admin_label(client, acting_user_id, workspace_record)
    acting_workspace = helpers.resolve_workspace_name(workspace_record)

    syncs_in_group = DbManager.find_records(schemas.Sync, [schemas.Sync.group_id == group_id])
    notices = _sync_manager_notices(
        syncs_in_group,
        action="Leave",
        workspace_name=acting_workspace,
        group_name=group.name,
        manager=person,
    )
    sync_ids = {sync.id for sync in syncs_in_group}
    leaving: list[str] = []
    for sync in syncs_in_group:
        my_channels = DbManager.find_records(
            schemas.SyncChannel,
            [
                schemas.SyncChannel.sync_id == sync.id,
                schemas.SyncChannel.workspace_id == workspace_record.id,
                schemas.SyncChannel.deleted_at.is_(None),
            ],
        )
        for ch in my_channels:
            if ch.channel_id and ch.channel_id not in leaving:
                leaving.append(ch.channel_id)
    hold = {(workspace_record.id, channel_id) for channel_id in leaving}
    for channel_id in leaving:
        soft_delete_sync_channels(
            workspace_record,
            channel_id,
            "left_group",
            sync_ids=sync_ids,
            person=person,
            hold_channels=hold,
        )

    DbManager.delete_records(
        schemas.UserMapping,
        [
            schemas.UserMapping.group_id == group_id,
            (
                (schemas.UserMapping.source_workspace_id == workspace_record.id)
                | (schemas.UserMapping.target_workspace_id == workspace_record.id)
            ),
        ],
    )

    now = datetime.now(UTC)
    for member in members:
        DbManager.update_records(
            schemas.WorkspaceGroupMember,
            [schemas.WorkspaceGroupMember.id == member.id],
            {
                schemas.WorkspaceGroupMember.status: "inactive",
                schemas.WorkspaceGroupMember.role: helpers.MEMBER,
                schemas.WorkspaceGroupMember.deleted_at: now,
            },
        )

    try:
        from federation.replicate import replicate_group_leave

        replicate_group_leave(group, workspace_record, notice="\n\n".join(notices))
    except Exception:
        log_warning("federation_replicate_group_leave_failed", group_id=group_id)

    log_info("group_left", workspace_id=workspace_record.id, group_id=group_id, group_name=group.name)

    remaining_members = DbManager.find_records(
        schemas.WorkspaceGroupMember,
        [
            schemas.WorkspaceGroupMember.group_id == group_id,
            schemas.WorkspaceGroupMember.status == "active",
            schemas.WorkspaceGroupMember.deleted_at.is_(None),
        ],
    )

    if not remaining_members:
        # Keep the group when an uninstalled owner is still in retention.
        helpers.succeed_ownership(group_id)
    else:
        _tell_other_member_workspaces(group_id, notices, exclude_workspace_id=workspace_record.id, logger=logger)

    builders.refresh_home_tab_for_workspace(workspace_record, logger, context=context, user_id=user_id)
    _close_modal_done(client, body, f":wave: You have left *{group.name}*. You can close this now.")


def _member_id_from_action(body: dict, prefix: str) -> int | None:
    """Read the trailing member id off a prefix-matched action id."""
    action_data = helpers.safe_get(body, "actions", 0) or {}
    raw = action_data.get("value") or (action_data.get("action_id", "") or "").replace(f"{prefix}_", "")
    try:
        return int(raw)
    except (TypeError, ValueError):
        log_warning("invalid_member_id", action=prefix, raw_length=len(str(raw)))
        return None


def _sync_manager_notices(
    syncs,
    *,
    action: str,
    workspace_name: str,
    group_name: str | None,
    manager: str | None,
    history_deleted: bool = False,
) -> list[str]:
    """One Action Notice per Sync. With no Sync, one notice about the Workspace."""
    from helpers.sync_participation import format_action_notice

    if not syncs:
        return [
            format_action_notice(
                action=action,
                workspace_name=workspace_name,
                group_name=group_name,
                manager=manager,
                for_managers=True,
                workspace_line=True,
            )
        ]
    days = None if history_deleted else helpers.soft_delete_retention_days()
    return [
        format_action_notice(
            action=action,
            workspace_name=workspace_name,
            group_name=group_name,
            manager=manager,
            sync_id=sync.id,
            for_managers=True,
            retention_days=days,
            history_deleted=history_deleted,
        )
        for sync in syncs
    ]


def _tell_other_member_workspaces(
    group_id: int, notices: list[str], *, exclude_workspace_id: int | None, logger: Logger
) -> None:
    """Direct-message the Action Notice to other local members. One call per Sync."""
    from helpers.notifications import notify_group_managers

    for text in notices:
        for member_ws in notify_group_managers(group_id, text, exclude_workspace_id=exclude_workspace_id):
            builders.refresh_home_tab_for_workspace(member_ws, logger, context=None)


def _notify_group_admins(
    group_id: int, message: str, logger: Logger, *, exclude_workspace_id: int | None = None
) -> None:
    """DM every active local member's admins and refresh their Home tabs."""
    from helpers.notifications import notify_group_managers

    for member_ws in notify_group_managers(group_id, message, exclude_workspace_id=exclude_workspace_id):
        builders.refresh_home_tab_for_workspace(member_ws, logger, context=None)


def _post_role_modal(client, body, trigger_id, *, title, text, button, action, member_id, callback_id, style="danger"):
    elements = []
    if button:
        element = orm.ButtonElement(label=button, action=action, value=str(member_id))
        if style:
            element.style = style
        elements = [element]
    orm.BlockView(
        blocks=[orm.SectionBlock(label=text)] + ([orm.ActionsBlock(elements=elements)] if elements else [])
    ).post_modal(
        client=client,
        trigger_id=trigger_id,
        callback_id=callback_id,
        title_text=title,
        submit_button_text=None,
        close_button_text="Cancel" if button else "Close",
        parent_metadata={"member_id": member_id},
        body=body,
    )


def handle_promote_to_owner(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Ask before promoting another member to Owner."""
    from handlers._common import _get_authorized_workspace

    auth_result = _get_authorized_workspace(body, client, context, "promote_to_owner")
    if not auth_result:
        return
    _, workspace_record = auth_result
    member_id = _member_id_from_action(body, actions.CONFIG_PROMOTE_TO_OWNER)
    trigger_id = helpers.safe_get(body, "trigger_id")
    if member_id is None or not trigger_id:
        return
    target = DbManager.get_record(schemas.WorkspaceGroupMember, id=member_id)
    if not target:
        return
    group = DbManager.get_record(schemas.WorkspaceGroup, id=target.group_id)
    group_name = group.name if group else "this Group"
    target_ws = helpers.get_workspace_by_id(target.workspace_id) if target.workspace_id else None
    target_name = helpers.resolve_workspace_name(target_ws) if target_ws else "Workspace"
    if not helpers.is_workspace_owner(target.group_id, workspace_record.id):
        text = f":lock: *Only an Owner of `{group_name}` can promote.*"
        button = None
    elif member_id not in {member.id for member in helpers.get_promotable_members(target.group_id)}:
        text = f":lock: *`{target_name}` cannot be promoted.*"
        button = None
    else:
        text = (
            f":key: *Make `{target_name}` an Owner of `{group_name}`?*\n\n"
            "An Owner can remove members and promote other Owners. "
            "You cannot take this back from here. They give it up themselves."
        )
        button = ":key: Promote to Owner"
    _post_role_modal(
        client,
        body,
        trigger_id,
        title="Promote to Owner",
        text=text,
        button=button,
        action=actions.CONFIG_PROMOTE_TO_OWNER_CONFIRM,
        member_id=member_id,
        callback_id=actions.CONFIG_PROMOTE_TO_OWNER_CONFIRM,
        style=None,
    )


def handle_promote_to_owner_confirm(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Promote another member of a group to Owner.

    Owner-only. Eligible targets are active members, including a workspace on
    another instance. Pending invites stay ineligible.
    """
    from handlers._common import _close_modal_done, _get_authorized_workspace

    auth_result = _get_authorized_workspace(body, client, context, "promote_to_owner_confirm")
    if not auth_result:
        return
    user_id, workspace_record = auth_result

    member_id = _member_id_from_action(body, actions.CONFIG_PROMOTE_TO_OWNER_CONFIRM)
    if member_id is None:
        return

    target = DbManager.get_record(schemas.WorkspaceGroupMember, id=member_id)
    if not target:
        return

    if not helpers.is_workspace_owner(target.group_id, workspace_record.id):
        log_warning(
            "authorization_denied",
            action="promote_to_owner",
            group_id=target.group_id,
            acting_workspace_id=workspace_record.id,
        )
        return

    eligible_ids = {member.id for member in helpers.get_promotable_members(target.group_id)}
    if member_id not in eligible_ids:
        log_warning("promote_to_owner_ineligible", member_id=member_id, group_id=target.group_id)
        return

    DbManager.update_records(
        schemas.WorkspaceGroupMember,
        [schemas.WorkspaceGroupMember.id == member_id],
        {schemas.WorkspaceGroupMember.role: helpers.OWNER},
    )

    log_info(
        "group_owner_promoted",
        group_id=target.group_id,
        member_id=member_id,
        workspace_id=target.workspace_id,
        promoted_by_workspace_id=workspace_record.id,
    )

    group = DbManager.get_record(schemas.WorkspaceGroup, id=target.group_id)
    target_ws = helpers.get_workspace_by_id(target.workspace_id) if target.workspace_id else None
    target_name = helpers.resolve_workspace_name(target_ws) if target_ws else "Workspace"
    from helpers.sync_participation import format_action_notice

    sentence = format_action_notice(
        action="Promote",
        workspace_name=target_name,
        group_name=group.name if group else None,
        manager=helpers.format_admin_label(client, user_id, workspace_record),
        for_managers=True,
        workspace_line=True,
    )
    _notify_group_admins(target.group_id, sentence, logger, exclude_workspace_id=workspace_record.id)
    if group and target_ws:
        try:
            from federation.replicate import replicate_group_invite

            replicate_group_invite(group, target_ws, set_role=True, notice=sentence)
        except Exception:
            log_warning("federation_replicate_group_invite_failed", group_id=target.group_id)

    builders.refresh_home_tab_for_workspace(workspace_record, logger, context=context, user_id=user_id)
    _close_modal_done(client, body, f":key: `{target_name}` is now an Owner. You can close this now.")


def handle_demote_self(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Ask before this workspace gives up ownership."""
    from handlers._common import _get_authorized_workspace

    auth_result = _get_authorized_workspace(body, client, context, "demote_self")
    if not auth_result:
        return
    _, workspace_record = auth_result
    member_id = _member_id_from_action(body, actions.CONFIG_DEMOTE_SELF)
    trigger_id = helpers.safe_get(body, "trigger_id")
    if member_id is None or not trigger_id:
        return
    target = DbManager.get_record(schemas.WorkspaceGroupMember, id=member_id)
    if not target:
        return
    group = DbManager.get_record(schemas.WorkspaceGroup, id=target.group_id)
    group_name = group.name if group else "this Group"
    if target.workspace_id != workspace_record.id:
        text = ":lock: *You can only give up your own ownership.*"
        button = None
    else:
        owners = helpers.get_active_owners(target.group_id)
        if not any(owner.id == member_id for owner in owners) or len(owners) < 2:
            text = (
                f":lock: *You are the only Owner of `{group_name}`, so you cannot give it up yet.*\n\n"
                "Promote another Workspace to Owner first."
            )
            button = None
        else:
            text = (
                f":door: *Give up ownership of `{group_name}`?*\n\n"
                "This Workspace stays in the Group as a member. Another Owner can promote it again."
            )
            button = ":door: Give Up Ownership"
    _post_role_modal(
        client,
        body,
        trigger_id,
        title="Give Up Ownership",
        text=text,
        button=button,
        action=actions.CONFIG_DEMOTE_SELF_CONFIRM,
        member_id=member_id,
        callback_id=actions.CONFIG_DEMOTE_SELF_CONFIRM,
    )


def handle_demote_self_confirm(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Give up this workspace's own ownership of a group.

    Self-demotion only, under the same "another owner remains" guard as leaving.
    """
    from handlers._common import _close_modal_done, _get_authorized_workspace

    auth_result = _get_authorized_workspace(body, client, context, "demote_self_confirm")
    if not auth_result:
        return
    user_id, workspace_record = auth_result

    member_id = _member_id_from_action(body, actions.CONFIG_DEMOTE_SELF_CONFIRM)
    if member_id is None:
        return

    target = DbManager.get_record(schemas.WorkspaceGroupMember, id=member_id)
    if not target:
        return

    if target.workspace_id != workspace_record.id:
        log_warning(
            "authorization_denied", action="demote_self", member_id=member_id, acting_workspace_id=workspace_record.id
        )
        return

    owners = helpers.get_active_owners(target.group_id)
    if not any(owner.id == member_id for owner in owners):
        return
    if len(owners) < 2:
        log_info("demote_self_blocked_sole_owner", group_id=target.group_id, workspace_id=workspace_record.id)
        return

    DbManager.update_records(
        schemas.WorkspaceGroupMember,
        [schemas.WorkspaceGroupMember.id == member_id],
        {schemas.WorkspaceGroupMember.role: helpers.MEMBER},
    )

    log_info("group_owner_demoted", group_id=target.group_id, member_id=member_id, workspace_id=workspace_record.id)

    group = DbManager.get_record(schemas.WorkspaceGroup, id=target.group_id)
    ws_name = helpers.resolve_workspace_name(workspace_record)
    from helpers.sync_participation import format_action_notice

    sentence = format_action_notice(
        action="Give Up Ownership",
        workspace_name=ws_name,
        group_name=group.name if group else None,
        manager=helpers.format_admin_label(client, user_id, workspace_record),
        for_managers=True,
        workspace_line=True,
    )
    _notify_group_admins(target.group_id, sentence, logger, exclude_workspace_id=workspace_record.id)
    if group:
        try:
            from federation.replicate import replicate_group_invite

            replicate_group_invite(group, workspace_record, set_role=True, notice=sentence)
        except Exception:
            log_warning("federation_replicate_group_invite_failed", group_id=target.group_id)

    builders.refresh_home_tab_for_workspace(workspace_record, logger, context=context, user_id=user_id)
    _close_modal_done(client, body, ":door: This Workspace is a member now. You can close this now.")


def _disband_denial_message(reason: str, group_name: str, group_id: int, workspace_id: int) -> str:
    """Explain a disband rejection in terms of what the operator has to do next."""
    if reason == "co_owner_exists":
        return (
            f':lock: *"{group_name}" has more than one Owner, so it cannot be disbanded.*\n\n'
            "Disbanding destroys the group for everyone, so it takes the agreement of every Owner. "
            "Ask the other Owners to give up ownership first — each Owner can demote itself while "
            "another Owner remains — and then you will be the only Owner and able to disband.\n\n"
            "_You can always Leave Sync on your own Channels and leave the group instead._"
        )
    if reason == "other_publishers":
        publisher_ids = helpers.get_other_publisher_workspace_ids(group_id, workspace_id)
        names = []
        for publisher_id in publisher_ids:
            publisher_ws = helpers.get_workspace_by_id(publisher_id)
            names.append(helpers.resolve_workspace_name(publisher_ws) if publisher_ws else f"Workspace {publisher_id}")
        named = ", ".join(f"`{name}`" for name in names) or "another Workspace"
        return (
            f':lock: *"{group_name}" still has Channels published by other Workspaces.*\n\n'
            f"{named} would lose work you do not own, so disbanding is blocked. "
            "Ask them to Leave Sync on their Channels or leave the group first.\n\n"
            "_You can always Leave Sync on your own Channels and leave the group instead._"
        )
    return (
        f':lock: *You are not an Owner of "{group_name}", so you cannot disband it.*\n\n'
        "Only the group's sole Owner can disband a group."
    )


def _disband_confirm_blocks(
    group_name: str,
    group_id: int,
    *,
    member_count: int,
    sync_count: int,
    channel_count: int,
    days: int,
    delete_history: bool,
) -> list:
    """The Disband confirm. The checkbox is an actions element, not an input.

    An input would force Slack's submit button, which cannot be red.
    """
    checkbox = orm.CheckboxesElement(
        options=[orm.SelectorOption(name="Also delete synced message history now", value="delete")],
        action_id=actions.CONFIG_DISBAND_DELETE_HISTORY_NOW,
        initial_values=["delete"] if delete_history else None,
    )
    return [
        orm.SectionBlock(
            label=(
                f":warning: *Disband `{group_name}`?*\n\n"
                "This cannot be undone. It will:\n"
                f"\u2022 Remove {_counted(member_count, 'Workspace', 'Workspaces')} from the Group\n"
                f"\u2022 End {_counted(sync_count, 'Channel Sync', 'Channel Syncs')} "
                f"covering {_counted(channel_count, 'Channel', 'Channels')}\n"
                f"\u2022 Keep synced message history for `{days}` days, then delete it. "
                "It does not come back\n"
                "\u2022 Delete every user mapping for this Group now\n\n"
                "_Ticking the box deletes synced message history now for every Workspace in this Group, "
                "including one on another instance. User mappings are deleted now either way. "
                "Mappings can be built again. Message history cannot._"
            ),
        ),
        orm.ActionsBlock(elements=[checkbox]),
        orm.ActionsBlock(
            elements=[
                orm.ButtonElement(
                    label=":wastebasket: Disband Group",
                    action=actions.CONFIG_DISBAND_GROUP_CONFIRM,
                    value=str(group_id),
                    style="danger",
                ),
            ]
        ),
    ]


def _disband_scope(group_id: int) -> tuple[int, int, int]:
    members = helpers.get_active_members(group_id)
    syncs = DbManager.find_records(schemas.Sync, [schemas.Sync.group_id == group_id])
    channel_count = 0
    if syncs:
        channel_count = len(
            DbManager.find_records(
                schemas.SyncChannel,
                [
                    schemas.SyncChannel.sync_id.in_([sync.id for sync in syncs]),
                    schemas.SyncChannel.deleted_at.is_(None),
                ],
            )
        )
    return len(members), len(syncs), channel_count


def handle_disband_group(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Show a confirmation modal before disbanding a group.

    Gated on two conditions that must both hold: the acting workspace is the
    group's only Owner, and it is the group's only publisher. See
    ``helpers.can_disband``.
    """
    from handlers._common import _get_authorized_workspace

    auth_result = _get_authorized_workspace(body, client, context, "disband_group")
    if not auth_result:
        return
    _, workspace_record = auth_result

    action_data = helpers.safe_get(body, "actions", 0) or {}
    raw = action_data.get("value") or (action_data.get("action_id", "") or "").replace(
        f"{actions.CONFIG_DISBAND_GROUP}_", ""
    )
    try:
        group_id = int(raw)
    except (TypeError, ValueError):
        log_warning("disband_group_invalid_id")
        return

    groups = DbManager.find_records(schemas.WorkspaceGroup, [schemas.WorkspaceGroup.id == group_id])
    if not groups:
        return
    group = groups[0]

    trigger_id = helpers.safe_get(body, "trigger_id")
    if not trigger_id:
        return

    allowed, reason = helpers.can_disband(group_id, workspace_record.id)
    if not allowed:
        log_info("disband_group_blocked", group_id=group_id, workspace_id=workspace_record.id, reason=reason)
        orm.BlockView(
            blocks=[
                orm.SectionBlock(
                    label=_disband_denial_message(reason, group.name, group_id, workspace_record.id),
                )
            ]
        ).post_modal(
            client=client,
            trigger_id=trigger_id,
            callback_id=actions.CONFIG_DISBAND_GROUP_CONFIRM,
            title_text="Disband Group",
            submit_button_text=None,
            close_button_text="Close",
            parent_metadata={"group_id": group_id, "blocked": True},
            body=body,
        )
        return

    member_count, sync_count, channel_count = _disband_scope(group_id)
    days = helpers.soft_delete_retention_days()
    orm.BlockView(
        blocks=_disband_confirm_blocks(
            group.name,
            group_id,
            member_count=member_count,
            sync_count=sync_count,
            channel_count=channel_count,
            days=days,
            delete_history=False,
        )
    ).post_modal(
        client=client,
        trigger_id=trigger_id,
        callback_id=actions.CONFIG_DISBAND_GROUP_CONFIRM,
        title_text="Disband Group",
        submit_button_text=None,
        close_button_text="Cancel",
        parent_metadata={"group_id": group_id, "delete_history": False},
        body=body,
    )


def handle_disband_delete_history(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Remember the history checkbox on the open Disband confirm.

    The checkbox is an actions element, so Slack does not put it in ``view.state``.
    """
    from handlers._common import _get_authorized_workspace, _parse_private_metadata

    if not _get_authorized_workspace(body, client, context, "disband_delete_history"):
        return
    view_id = helpers.safe_get(body, "view", "id")
    if not view_id:
        return
    meta = _parse_private_metadata(body)
    group_id = meta.get("group_id")
    if not group_id:
        return
    groups = DbManager.find_records(schemas.WorkspaceGroup, [schemas.WorkspaceGroup.id == group_id])
    if not groups:
        return
    selected = helpers.safe_get(body, "actions", 0, "selected_options") or []
    delete_history = any(isinstance(option, dict) and option.get("value") == "delete" for option in selected)
    meta["delete_history"] = delete_history
    member_count, sync_count, channel_count = _disband_scope(group_id)
    try:
        orm.BlockView(
            blocks=_disband_confirm_blocks(
                groups[0].name,
                int(group_id),
                member_count=member_count,
                sync_count=sync_count,
                channel_count=channel_count,
                days=helpers.soft_delete_retention_days(),
                delete_history=delete_history,
            )
        ).update_modal(
            client=client,
            view_id=view_id,
            title_text="Disband Group",
            callback_id=actions.CONFIG_DISBAND_GROUP_CONFIRM,
            submit_button_text=None,
            close_button_text="Cancel",
            parent_metadata=meta,
        )
    except Exception as exc:
        log_warning("disband_history_toggle_failed", group_id=group_id, error=str(exc))


def handle_disband_group_confirm(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Disband a group after confirmation: soft-delete its Channel Syncs, tell peers, end memberships and mappings."""
    from handlers._common import _close_modal_done, _parse_private_metadata

    user_id = helpers.get_user_id_from_body(body)
    team_id = helpers.get_team_id_from_body(body)
    if not user_id or not team_id or not helpers.is_workspace_manager(client, user_id, team_id):
        log_warning("authorization_denied", user_id=user_id, action="disband_group_confirm")
        return

    meta = _parse_private_metadata(body)
    group_id = meta.get("group_id")
    if not group_id or meta.get("blocked"):
        return

    workspace_record = helpers.get_workspace_record(team_id, body, context, client)
    if not workspace_record:
        return

    # Re-checked on submit: main_response has no authorization gate, so the
    # modal-time check alone is bypassable with a forged view submission.
    allowed, reason = helpers.can_disband(group_id, workspace_record.id)
    if not allowed:
        log_warning("disband_group_denied", group_id=group_id, workspace_id=workspace_record.id, reason=reason)
        return

    groups = DbManager.find_records(schemas.WorkspaceGroup, [schemas.WorkspaceGroup.id == group_id])
    if not groups:
        return
    group = groups[0]

    acting_user_id = user_id
    person = helpers.format_admin_label(client, acting_user_id, workspace_record)
    acting_workspace = helpers.resolve_workspace_name(workspace_record)
    syncs = DbManager.find_records(schemas.Sync, [schemas.Sync.group_id == group_id])
    notices = _sync_manager_notices(
        syncs,
        action="Disband",
        workspace_name=acting_workspace,
        group_name=group.name,
        manager=person,
        history_deleted=bool(meta.get("delete_history")),
    )
    _tell_other_member_workspaces(group_id, notices, exclude_workspace_id=workspace_record.id, logger=logger)

    from federation.replicate import replicate_sync_ended
    from helpers.workspace_kind import is_local_workspace

    delete_history = bool(meta.get("delete_history"))
    sync_ids = {sync.id for sync in syncs}
    planned: list[tuple[schemas.SyncChannel, schemas.Workspace]] = []
    stubs_by_sync: dict[int, list[tuple[schemas.SyncChannel, schemas.Workspace]]] = {}
    for sync in syncs:
        channels = DbManager.find_records(
            schemas.SyncChannel,
            [schemas.SyncChannel.sync_id == sync.id, schemas.SyncChannel.deleted_at.is_(None)],
        )
        for channel in channels:
            channel_ws = helpers.get_workspace_by_id(channel.workspace_id)
            if not channel_ws or not channel.channel_id:
                continue
            planned.append((channel, channel_ws))
            if not is_local_workspace(channel_ws):
                stubs_by_sync.setdefault(sync.id, []).append((channel, channel_ws))
    announced: set[tuple[int, str]] = set()
    hold = {(channel_ws.id, channel.channel_id) for channel, channel_ws in planned}
    for channel, channel_ws in planned:
        key = (channel_ws.id, channel.channel_id)
        if key in announced:
            continue
        announced.add(key)
        soft_delete_sync_channels(
            channel_ws,
            channel.channel_id,
            "group_disbanded",
            sync_ids=sync_ids,
            person=person,
            acting_workspace=acting_workspace,
            purge=delete_history,
            dm=channel_ws.id == workspace_record.id,
            hold_channels=hold,
        )
    if stubs_by_sync:
        from helpers.sync_participation import format_action_notice

        sync_by_id = {sync.id: sync for sync in syncs}
        for sync_id, stub_rows in stubs_by_sync.items():
            replicate_sync_ended(
                sync_by_id[sync_id],
                group,
                stub_rows,
                reason="group_disbanded",
                notice=format_action_notice(
                    action="Disband",
                    workspace_name=acting_workspace,
                    group_name=group.name,
                    manager=person,
                    sync_id=sync_id,
                ),
                purge=delete_history,
            )
    if delete_history:
        from helpers.sync_cleanup import purge_sync_channels

        doomed: list[schemas.SyncChannel] = []
        for sync in DbManager.find_records(schemas.Sync, [schemas.Sync.group_id == group_id]):
            doomed.extend(
                DbManager.find_records(
                    schemas.SyncChannel,
                    [schemas.SyncChannel.sync_id == sync.id],
                )
            )
        if doomed:
            purge_sync_channels(doomed)
    DbManager.update_records(
        schemas.WorkspaceGroup,
        [schemas.WorkspaceGroup.id == group_id],
        {schemas.WorkspaceGroup.status: "disbanded"},
    )
    # Peers are found from active stub memberships, so this runs before those rows go inactive.
    group.status = "disbanded"
    try:
        from federation.replicate import replicate_group_upsert

        replicate_group_upsert(group, notice="\n\n".join(notices))
    except Exception:
        log_warning("federation_replicate_group_upsert_failed", group_id=group_id)

    DbManager.delete_records(schemas.UserMapping, [schemas.UserMapping.group_id == group_id])
    DbManager.update_records(
        schemas.WorkspaceGroupMember,
        [schemas.WorkspaceGroupMember.group_id == group_id],
        {schemas.WorkspaceGroupMember.status: "inactive", schemas.WorkspaceGroupMember.deleted_at: datetime.now(UTC)},
    )

    log_info("group_disbanded", group_id=group_id, group_name=group.name, workspace_id=workspace_record.id)

    builders.refresh_home_tab_for_workspace(workspace_record, logger, context=context, user_id=user_id)
    _close_modal_done(client, body, f":wastebasket: *{group.name}* has been disbanded. You can close this now.")


def handle_remove_group_member(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Confirm removing another workspace from a group this workspace owns."""
    user_id = helpers.get_user_id_from_body(body)
    team_id = helpers.get_team_id_from_body(body)
    if not user_id or not team_id or not helpers.is_workspace_manager(client, user_id, team_id):
        log_warning("authorization_denied", user_id=user_id, action="remove_group_member")
        return
    member_id = _member_id_from_action(body, actions.CONFIG_REMOVE_GROUP_MEMBER)
    if member_id is None:
        return
    trigger_id = helpers.safe_get(body, "trigger_id")
    if not trigger_id:
        return
    member = DbManager.get_record(schemas.WorkspaceGroupMember, id=member_id)
    if not member or member.deleted_at is not None or member.status != "active":
        return
    group = DbManager.get_record(schemas.WorkspaceGroup, id=member.group_id)
    target = helpers.get_workspace_by_id(member.workspace_id) if member.workspace_id else None
    target_name = helpers.resolve_workspace_name(target) if target else "Workspace"
    group_name = group.name if group else "this Group"
    form = orm.BlockView(
        blocks=[
            orm.SectionBlock(
                label=(
                    f":warning: *Remove `{target_name}` from `{group_name}`?*\n\n"
                    "Channel Syncs in this Group stop and SyncBot leaves those Channels. "
                    f"Synced message history is kept for {helpers.soft_delete_retention_days()} days and comes back "
                    "if the Workspace is invited again and joins the same Channels. "
                    "User mappings for this Group are deleted now. "
                    "The other Channels stay, and the notice lists who is still in the Sync."
                ),
            ),
            orm.ActionsBlock(
                elements=[
                    orm.ButtonElement(
                        label=":eject: Remove from Group",
                        action=actions.CONFIG_REMOVE_GROUP_MEMBER_CONFIRM,
                        value=str(member_id),
                        style="danger",
                    ),
                ]
            ),
        ]
    )
    form.post_modal(
        client=client,
        trigger_id=trigger_id,
        callback_id=actions.CONFIG_REMOVE_GROUP_MEMBER_CONFIRM,
        title_text="Remove Workspace",
        submit_button_text=None,
        close_button_text="Cancel",
        parent_metadata={"member_id": member_id},
        body=body,
    )


def handle_remove_group_member_confirm(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Soft-delete the member's Channel Syncs and hard-delete the membership."""
    from handlers._common import _close_modal_done, _parse_private_metadata
    from handlers.group import _generate_invite_code
    from helpers.leave_sync import _replicate_one_remove, soft_delete_sync_channels
    from helpers.workspace_kind import is_stub_workspace

    user_id = helpers.get_user_id_from_body(body)
    team_id = helpers.get_team_id_from_body(body)
    if not user_id or not team_id or not helpers.is_workspace_manager(client, user_id, team_id):
        log_warning("authorization_denied", user_id=user_id, action="remove_group_member_confirm")
        return
    meta = _parse_private_metadata(body)
    member_id = meta.get("member_id")
    if not member_id:
        return
    actor = helpers.get_workspace_record(team_id, body, context, client)
    member = DbManager.get_record(schemas.WorkspaceGroupMember, id=int(member_id))
    if not actor or not member or member.deleted_at is not None or member.status != "active":
        return
    if member.role == "owner" or member.workspace_id == actor.id:
        log_warning("remove_group_member_denied", member_id=member_id, reason="not_a_member_row")
        return
    if not helpers.is_workspace_owner(member.group_id, actor.id):
        log_warning("remove_group_member_denied", member_id=member_id, reason="not_owner")
        return
    target = helpers.get_workspace_by_id(member.workspace_id) if member.workspace_id else None
    group = DbManager.get_record(schemas.WorkspaceGroup, id=member.group_id)
    if not target or not group:
        return
    syncs = DbManager.find_records(schemas.Sync, [schemas.Sync.group_id == group.id])
    sync_ids = {sync.id for sync in syncs}
    person = helpers.format_admin_label(client, user_id, actor)
    owner_name = helpers.resolve_workspace_name(actor)
    target_name = helpers.resolve_workspace_name(target)
    notices = _sync_manager_notices(
        syncs,
        action="Remove",
        workspace_name=target_name,
        group_name=group.name,
        manager=person,
    )
    removing: list[str] = []
    stub_rows: list[tuple[schemas.Sync, schemas.SyncChannel]] = []
    for sync in syncs:
        rows = DbManager.find_records(
            schemas.SyncChannel,
            [
                schemas.SyncChannel.sync_id == sync.id,
                schemas.SyncChannel.workspace_id == target.id,
                schemas.SyncChannel.deleted_at.is_(None),
            ],
        )
        for row in rows:
            if is_stub_workspace(target):
                stub_rows.append((sync, row))
            if row.channel_id and row.channel_id not in removing:
                removing.append(row.channel_id)
    hold = {(target.id, channel_id) for channel_id in removing}
    helpers.replace_group_invite_code(group.id, _generate_invite_code)
    for channel_id in removing:
        soft_delete_sync_channels(
            target,
            channel_id,
            "removed_from_group",
            sync_ids=sync_ids,
            person=person,
            acting_workspace=owner_name,
            hold_channels=hold,
        )
    for sync, row in stub_rows:
        _replicate_one_remove(sync, group.id, target, row, reason="removed_from_group")
    DbManager.delete_records(
        schemas.UserMapping,
        [
            schemas.UserMapping.group_id == group.id,
            (
                (schemas.UserMapping.source_workspace_id == target.id)
                | (schemas.UserMapping.target_workspace_id == target.id)
            ),
        ],
    )
    DbManager.delete_records(
        schemas.WorkspaceGroupMember,
        [schemas.WorkspaceGroupMember.id == member.id],
    )
    try:
        from federation.replicate import replicate_group_member_remove

        replicate_group_member_remove(group, target, notice="\n\n".join(notices))
    except Exception:
        log_warning("federation_replicate_group_member_remove_failed", group_id=group.id)
    log_info("group_member_removed", group_id=group.id, workspace_id=target.id, by_workspace_id=actor.id)
    # The removed Workspace already got the per-Channel detail. With no Channel, it gets this copy.
    _tell_other_member_workspaces(
        group.id,
        notices,
        exclude_workspace_id=target.id if removing else None,
        logger=logger,
    )
    builders.refresh_home_tab_for_workspace(actor, logger, context=context, user_id=user_id)
    _close_modal_done(client, body, f":eject: `{target_name}` has been removed from *{group.name}*.")
