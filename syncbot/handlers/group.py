"""Workspace group handlers — create, join, accept, cancel."""

import contextlib
import secrets
import string
from datetime import UTC, datetime
from logging import Logger

from slack_sdk.web import WebClient

import builders
import helpers
from db import DbManager, schemas
from handlers._common import (
    _get_authorized_workspace,
    _get_selected_option_value,
    _get_text_input_value,
    _parse_private_metadata,
)
from logger import log_info, log_warning
from slack import actions, orm
from slack.blocks import context as block_context
from slack.blocks import divider, section

_INVITE_CODE_CHARS = string.ascii_uppercase + string.digits


def _generate_invite_code(length: int = 7) -> str:
    """Generate a random alphanumeric invite code like ``A7X-K9M``."""
    raw = "".join(secrets.choice(_INVITE_CODE_CHARS) for _ in range(length))
    return f"{raw[:3]}-{raw[3:]}" if length >= 6 else raw


def _activate_group_membership(
    client: WebClient,
    workspace_record: "schemas.Workspace",
    group: "schemas.WorkspaceGroup",
) -> None:
    """Seed mapping stubs from existing directories (no crawl, no auto-match).

    Auto Map Now runs from the User Mapping modal. Partners
    keep whatever ``user_directory`` rows they already have.
    """
    members = DbManager.find_records(
        schemas.WorkspaceGroupMember,
        [
            schemas.WorkspaceGroupMember.group_id == group.id,
            schemas.WorkspaceGroupMember.status == "active",
            schemas.WorkspaceGroupMember.deleted_at.is_(None),
            schemas.WorkspaceGroupMember.workspace_id != workspace_record.id,
        ],
    )

    for member in members:
        if not member.workspace_id:
            continue
        member_ws = helpers.get_workspace_by_id(member.workspace_id)
        if not member_ws or member_ws.deleted_at:
            continue

        try:
            helpers.seed_user_mappings(workspace_record.id, member_ws.id, group_id=group.id)
            helpers.seed_user_mappings(member_ws.id, workspace_record.id, group_id=group.id)
        except Exception as e:
            log_warning("failed_to_seed_user_mappings", error=str(e))


def handle_create_group(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Open a modal for naming a new workspace group."""
    user_id = helpers.get_user_id_from_body(body)
    team_id = helpers.get_team_id_from_body(body)
    if not user_id or not team_id or not helpers.is_workspace_manager(client, user_id, team_id):
        log_warning("authorization_denied", user_id=user_id, action="create_group")
        return

    trigger_id = helpers.safe_get(body, "trigger_id")
    if not trigger_id:
        return

    view = orm.BlockView(
        blocks=[
            orm.InputBlock(
                label="Workspace Group Name",
                action=actions.CONFIG_CREATE_GROUP_NAME,
                element=orm.PlainTextInputElement(placeholder="e.g. Slack Syndicate, The Multiverse..."),
                optional=False,
            ),
            orm.ContextBlock(
                element=orm.ContextElement(
                    initial_value="_Give this Workspace Group a friendly and descriptive name._",
                ),
            ),
        ]
    )

    view.post_modal(
        client=client,
        trigger_id=trigger_id,
        callback_id=actions.CONFIG_CREATE_GROUP_SUBMIT,
        title_text="Create Group",
        submit_button_text="Create Group",
        body=body,
    )


def handle_create_group_submit(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Create the workspace group and add this workspace as the creator."""
    auth_result = _get_authorized_workspace(body, client, context, "create_group_submit")
    if not auth_result:
        return
    user_id, workspace_record = auth_result

    group_name = (_get_text_input_value(body, actions.CONFIG_CREATE_GROUP_NAME) or "").strip()

    if not group_name:
        log_warning("create_group_submit", reason="empty group name")
        return

    if len(group_name) > 100:
        group_name = group_name[:100]

    from federation.replicate import mint_uid

    code = _generate_invite_code()
    now = datetime.now(UTC)

    group = schemas.WorkspaceGroup(
        name=group_name,
        invite_code=code,
        status="active",
        created_at=now,
        uid=mint_uid(),
    )
    DbManager.create_record(group)

    member = schemas.WorkspaceGroupMember(
        group_id=group.id,
        workspace_id=workspace_record.id,
        status="active",
        role="owner",
        joined_at=now,
    )
    DbManager.create_record(member)

    log_info("group_created", workspace_id=workspace_record.id, group_id=group.id, group_name=group_name)

    acting_user_id = user_id
    if acting_user_id:
        try:
            dm = client.conversations_open(users=[acting_user_id])
            dm_channel = helpers.safe_get(dm, "channel", "id")
            if dm_channel:
                client.chat_postMessage(
                    channel=dm_channel,
                    text=f":raised_hands: *New Workspace Group Created!*\n\n*Group Name:* `{group_name}`\n\n*Invite Code:* `{code}`\n\n"
                    "You can share the Invite Code with an Admin from another Workspace on this SyncBot instance, "
                    "and they can join the Group.",
                )
        except Exception as e:
            log_warning("failed_to_dm_invite_code", error=str(e))

    builders.refresh_home_tab_for_workspace(workspace_record, logger, context=context, user_id=user_id)


def handle_join_group(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Open a modal for entering a group invite code."""
    user_id = helpers.get_user_id_from_body(body)
    team_id = helpers.get_team_id_from_body(body)
    if not user_id or not team_id or not helpers.is_workspace_manager(client, user_id, team_id):
        log_warning("authorization_denied", user_id=user_id, action="join_group")
        return

    trigger_id = helpers.safe_get(body, "trigger_id")
    if not trigger_id:
        return

    view = orm.BlockView(
        blocks=[
            orm.InputBlock(
                label="Group Invite Code",
                action=actions.CONFIG_JOIN_GROUP_CODE,
                element=orm.PlainTextInputElement(placeholder="Enter the code (e.g. A7X-K9M)"),
                optional=False,
            ),
            orm.ContextBlock(
                element=orm.ContextElement(
                    initial_value=(
                        "Enter the Invite Code from an Admin of a Workspace in the Group. "
                        "A code works only on the SyncBot instance it came from."
                    ),
                ),
            ),
        ]
    )
    view.post_modal(
        client=client,
        trigger_id=trigger_id,
        callback_id=actions.CONFIG_JOIN_GROUP_SUBMIT,
        title_text="Join Group",
        body=body,
    )


def _read_join_code(body: dict) -> str:
    """Return the Join Group code, stripped, uppercased, and dashed."""
    raw_code = (_get_text_input_value(body, actions.CONFIG_JOIN_GROUP_CODE) or "").strip().upper()
    if "-" not in raw_code and len(raw_code) >= 6:
        raw_code = f"{raw_code[:3]}-{raw_code[3:]}"
    return raw_code


def _read_join_attempts(raw) -> dict:
    """Return the stored attempt count and the submission that last counted."""
    if isinstance(raw, dict):
        return {"count": int(raw.get("count") or 0), "trigger_id": raw.get("trigger_id")}
    return {"count": int(raw or 0), "trigger_id": None}


def handle_join_group_submit_ack(body: dict, client: WebClient, context: dict) -> dict | None:
    """Tell the person in the modal when a Join Group code does not match."""
    auth_result = _get_authorized_workspace(body, client, context, "join_group_submit")
    if not auth_result:
        return None
    _user_id, workspace_record = auth_result
    rate_key = f"group_join_attempts:{workspace_record.id}"
    stored = _read_join_attempts(helpers._cache_get(rate_key))
    if stored["count"] >= 5:
        log_warning("group_join_rate_limited", workspace_id=workspace_record.id)
        return {
            "response_action": "errors",
            "errors": {
                actions.CONFIG_JOIN_GROUP_CODE: "Too many codes were tried. Wait 15 minutes, then try again.",
            },
        }
    code = _read_join_code(body)
    groups = DbManager.find_records(
        schemas.WorkspaceGroup,
        [
            schemas.WorkspaceGroup.invite_code == code,
            schemas.WorkspaceGroup.status == "active",
        ],
    )
    if groups:
        return None
    # In production the ack runs twice for one submission. Count one attempt for each trigger_id.
    trigger_id = body.get("trigger_id")
    if trigger_id and stored["trigger_id"] == trigger_id:
        attempt = stored["count"]
    else:
        attempt = stored["count"] + 1
        helpers._cache_set(rate_key, {"count": attempt, "trigger_id": trigger_id}, ttl=900)
    log_warning(
        "group_code_invalid",
        workspace_id=workspace_record.id,
        attempt=attempt,
        code_length=len(code),
    )
    return {
        "response_action": "errors",
        "errors": {
            actions.CONFIG_JOIN_GROUP_CODE: (
                "That code does not match a Group on this SyncBot instance. "
                "A Workspace on another instance joins when a Group member uses Invite Workspace."
            ),
        },
    }


def handle_join_group_submit(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Validate an invite code and join the workspace group."""
    auth_result = _get_authorized_workspace(body, client, context, "join_group_submit")
    if not auth_result:
        return
    user_id, workspace_record = auth_result

    raw_code = _read_join_code(body)

    acting_user_id = user_id

    groups = DbManager.find_records(
        schemas.WorkspaceGroup,
        [
            schemas.WorkspaceGroup.invite_code == raw_code,
            schemas.WorkspaceGroup.status == "active",
        ],
    )

    if not groups:
        return

    group = groups[0]

    # The old self-join guard read created_by_workspace_id here. The membership
    # check below already covers it, since a group's creator always has a
    # membership row — and dropping the guard lets a workspace that left rejoin
    # by code instead of being blocked forever.
    existing = DbManager.find_records(
        schemas.WorkspaceGroupMember,
        [
            schemas.WorkspaceGroupMember.group_id == group.id,
            schemas.WorkspaceGroupMember.workspace_id == workspace_record.id,
            schemas.WorkspaceGroupMember.deleted_at.is_(None),
        ],
    )
    if existing:
        log_info("group_already_member", workspace_id=workspace_record.id, group_id=group.id)
        builders.build_home_tab(body, client, logger, context, user_id=acting_user_id)
        return

    now = datetime.now(UTC)
    member = schemas.WorkspaceGroupMember(
        group_id=group.id,
        workspace_id=workspace_record.id,
        status="active",
        role="member",
        joined_at=now,
    )
    DbManager.create_record(member)

    log_info("group_joined", workspace_id=workspace_record.id, group_id=group.id, group_name=group.name)

    _activate_group_membership(client, workspace_record, group)

    person = helpers.format_admin_label(client, acting_user_id, workspace_record)
    from helpers.sync_participation import format_action_notice

    notice = format_action_notice(
        action="Join Group",
        workspace_name=helpers.resolve_workspace_name(workspace_record),
        group_name=group.name,
        manager=person,
        for_managers=True,
        workspace_line=True,
    )

    try:
        from federation.replicate import replicate_group_invite

        replicate_group_invite(group, workspace_record, notice=notice, announce=True)
    except Exception:
        log_warning("federation_replicate_group_invite_failed", group_id=group.id)

    from helpers.notifications import notify_group_managers

    for member_ws in notify_group_managers(group.id, notice, exclude_workspace_id=workspace_record.id):
        builders.refresh_home_tab_for_workspace(member_ws, logger, context=None)

    builders.refresh_home_tab_for_workspace(workspace_record, logger, context=context, user_id=user_id)


# ---------------------------------------------------------------------------
# Invite workspace to group
# ---------------------------------------------------------------------------


def handle_invite_workspace(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Open a modal for inviting a workspace to a group."""

    auth_result = _get_authorized_workspace(body, client, context, "invite_workspace")
    if not auth_result:
        return
    _, workspace_record = auth_result

    trigger_id = helpers.safe_get(body, "trigger_id")
    raw_group_id = helpers.safe_get(body, "actions", 0, "value")
    try:
        group_id = int(raw_group_id)
    except (TypeError, ValueError):
        log_warning("invite_workspace", raw_group_id=raw_group_id)
        return

    group = DbManager.get_record(schemas.WorkspaceGroup, id=group_id)
    if not group:
        return

    current_workspace_id = workspace_record.id if workspace_record else None

    # Only active members count as "already in the group"; pending invites can be re-invited
    current_members = DbManager.find_records(
        schemas.WorkspaceGroupMember,
        [
            schemas.WorkspaceGroupMember.group_id == group_id,
            schemas.WorkspaceGroupMember.status == "active",
            schemas.WorkspaceGroupMember.deleted_at.is_(None),
        ],
    )
    member_ws_ids = {member.workspace_id for member in current_members if member.workspace_id}

    all_workspaces = DbManager.find_records(
        schemas.Workspace,
        [schemas.Workspace.deleted_at.is_(None)],
    )
    from helpers.workspace_kind import (
        is_deliverable_workspace,
        is_stub_workspace,
        peer_for_workspace,
        workspace_allowed_for_peer,
    )

    def _listed(ws) -> bool:
        if not is_deliverable_workspace(ws):
            return False
        if is_stub_workspace(ws):
            return workspace_allowed_for_peer(workspace_record, peer_for_workspace(ws))
        return True

    eligible = [ws for ws in all_workspaces if ws.id not in member_ws_ids and _listed(ws)]

    # Show Oops only when there are no other deliverable workspaces at all (not when everyone is already in the group)
    other_installed = [ws for ws in all_workspaces if is_deliverable_workspace(ws) and ws.id != current_workspace_id]
    if not other_installed and not helpers.federation_enabled():
        msg_blocks = [
            section(
                "At least one other Slack Workspace needs to install this SyncBot app, or "
                "External Connections need to be allowed, before you can invite another Workspace to this Group."
            ),
        ]
        orm.BlockView(blocks=msg_blocks).post_modal(
            client=client,
            trigger_id=trigger_id,
            callback_id=actions.CONFIG_INVITE_WORKSPACE_SUBMIT,
            title_text="Oops!",
            submit_button_text=None,
            body=body,
        )
        return

    modal_blocks: list = []

    if eligible:
        workspace_options = [
            orm.SelectorOption(
                name=(
                    f"{helpers.resolve_workspace_name(workspace)} (External)"
                    if is_stub_workspace(workspace)
                    else helpers.resolve_workspace_name(workspace)
                ),
                value=str(workspace.id),
            )
            for workspace in eligible
        ]
        modal_blocks.append(
            orm.InputBlock(
                label="Send a SyncBot DM",
                action=actions.CONFIG_INVITE_WORKSPACE_SELECT,
                element=orm.StaticSelectElement(
                    placeholder="Select a Workspace",
                    options=workspace_options,
                ),
                optional=True,
            )
        )
        modal_blocks.append(block_context("Admins of that Workspace get a SyncBot DM to accept or decline the invite."))

    modal_blocks.append(block_context("\u200b"))
    modal_blocks.append(divider())
    modal_blocks.append(section(":memo: *Invite Code*"))
    modal_blocks.append(
        block_context(
            "Alternatively, share this Invite Code with an Admin from another Workspace "
            f"on this SyncBot instance:\n\n`{group.invite_code}`"
        )
    )

    submit_text = "Send Invite" if eligible else None
    view = orm.BlockView(blocks=modal_blocks)
    view.post_modal(
        client=client,
        trigger_id=trigger_id,
        callback_id=actions.CONFIG_INVITE_WORKSPACE_SUBMIT,
        title_text="Invite Workspace",
        submit_button_text=submit_text,
        parent_metadata={"group_id": group_id},
        body=body,
    )


def handle_invite_workspace_submit(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Send a DM invite to admins of the selected workspace."""
    auth_result = _get_authorized_workspace(body, client, context, "invite_workspace_submit")
    if not auth_result:
        return
    user_id, workspace_record = auth_result
    meta = _parse_private_metadata(body)
    group_id = meta.get("group_id")
    if not group_id:
        return

    group = DbManager.get_record(schemas.WorkspaceGroup, id=group_id)
    if not group:
        return

    selected_ws_id = _get_selected_option_value(body, actions.CONFIG_INVITE_WORKSPACE_SELECT)

    if not selected_ws_id:
        return

    try:
        target_ws_id = int(selected_ws_id)
    except (TypeError, ValueError):
        return

    target_ws = helpers.get_workspace_by_id(target_ws_id)
    from helpers.workspace_kind import is_deliverable_workspace, is_local_workspace, is_stub_workspace

    if not target_ws or target_ws.deleted_at or not is_deliverable_workspace(target_ws):
        log_warning("invite_workspace_submit", target_ws_id=target_ws_id)
        return

    existing = DbManager.find_records(
        schemas.WorkspaceGroupMember,
        [
            schemas.WorkspaceGroupMember.group_id == group_id,
            schemas.WorkspaceGroupMember.workspace_id == target_ws_id,
            schemas.WorkspaceGroupMember.deleted_at.is_(None),
        ],
    )
    if existing:
        log_info("invite_workspace_submit", target_ws_id=target_ws_id, group_id=group_id)
        builders.refresh_home_tab_for_workspace(workspace_record, logger, context=context, user_id=user_id)
        return

    acting_user_id = user_id
    from helpers.workspace_kind import peer_for_workspace, workspace_allowed_for_peer

    peer = None
    if is_stub_workspace(target_ws):
        peer = peer_for_workspace(target_ws)
        if peer is None or not workspace_allowed_for_peer(workspace_record, peer):
            log_warning("invite_workspace_submit", target_ws_id=target_ws_id)
            return
    elif not is_local_workspace(target_ws) or not helpers.get_bot_token(target_ws):
        log_warning("invite_workspace_submit", target_ws_id=target_ws_id)
        return

    person = helpers.format_admin_label(client, acting_user_id, workspace_record)
    from helpers.sync_participation import format_action_notice

    invite_text = format_action_notice(
        action="Invite",
        workspace_name=helpers.resolve_workspace_name(target_ws),
        group_name=group.name,
        manager=person,
        for_managers=True,
        workspace_line=True,
    )
    member = schemas.WorkspaceGroupMember(
        group_id=group_id,
        workspace_id=target_ws_id,
        status="pending",
        role="member",
        joined_at=None,
        invited_by_slack_user_id=acting_user_id,
        invited_by_workspace_id=workspace_record.id,
    )
    DbManager.create_record(member)

    if peer:
        from federation.replicate import replicate_group_invite

        replicate_group_invite(group, workspace_record, peer=peer)
        replicate_group_invite(group, target_ws, peer=peer, notice=invite_text, invited_by=workspace_record)
    else:
        helpers.send_group_invite_dm(member, target_ws, invite_text)
        builders.refresh_home_tab_for_workspace(target_ws, logger, context=None)

    log_info("group_invite_sent", group_id=group_id, target_workspace_id=target_ws_id, member_id=member.id)

    builders.refresh_home_tab_for_workspace(workspace_record, logger, context=context, user_id=user_id)


# ---------------------------------------------------------------------------
# Accept / Decline group invite
# ---------------------------------------------------------------------------


def handle_accept_group_invite(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Accept a pending group invite from a DM button.

    Only the invited workspace may accept. ``member_id`` comes from the button
    value, so the acting workspace is resolved from the request and compared
    against ``member.workspace_id`` — without that check any signed interaction
    carrying an arbitrary integer could activate any pending invite.
    """
    raw_member_id = helpers.safe_get(body, "actions", 0, "value")
    try:
        member_id = int(raw_member_id)
    except (TypeError, ValueError):
        log_warning("accept_group_invite", raw_member_id=raw_member_id)
        return

    auth_result = _get_authorized_workspace(body, client, context, "accept_group_invite")
    if not auth_result:
        return
    user_id, acting_workspace = auth_result

    member = DbManager.get_record(schemas.WorkspaceGroupMember, id=member_id)
    if not member or member.status != "pending":
        log_info("accept_group_invite", member_id=member_id)
        return

    if not member.workspace_id or member.workspace_id != acting_workspace.id:
        log_warning(
            "authorization_denied",
            action="accept_group_invite",
            member_id=member_id,
            acting_workspace_id=acting_workspace.id,
        )
        return

    group = DbManager.get_record(schemas.WorkspaceGroup, id=member.group_id)
    if not group:
        return

    workspace_record = helpers.get_workspace_by_id(member.workspace_id)
    if not workspace_record:
        return

    now = datetime.now(UTC)
    DbManager.update_records(
        schemas.WorkspaceGroupMember,
        [schemas.WorkspaceGroupMember.id == member_id],
        {
            schemas.WorkspaceGroupMember.status: "active",
            schemas.WorkspaceGroupMember.joined_at: now,
        },
    )

    _activate_group_membership(client, workspace_record, group)

    person = helpers.format_admin_label(client, user_id, workspace_record)
    ws_name = helpers.resolve_workspace_name(workspace_record)
    from helpers.sync_participation import format_action_notice

    notice = format_action_notice(
        action="Join Group",
        workspace_name=ws_name,
        group_name=group.name,
        manager=person,
        for_managers=True,
        workspace_line=True,
    )

    try:
        from federation.replicate import replicate_group_invite

        replicate_group_invite(group, workspace_record, notice=notice, announce=True)
    except Exception:
        log_warning("federation_replicate_group_invite_failed", group_id=group.id)

    helpers.update_group_invite_dms(member, workspace_record, notice)

    from helpers.notifications import notify_group_managers

    for member_ws in notify_group_managers(group.id, notice, exclude_workspace_id=workspace_record.id):
        builders.refresh_home_tab_for_workspace(member_ws, logger, context=None)

    log_info("group_invite_accepted", member_id=member_id, group_id=group.id, workspace_id=workspace_record.id)

    builders.refresh_home_tab_for_workspace(workspace_record, logger, context=context, user_id=user_id)


def handle_decline_group_invite(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Decline a pending group invite. Only the invited workspace may decline."""
    _end_pending_invite(body, client, logger, context, is_cancel=False)


def handle_cancel_group_invite(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Ask before cancelling an outgoing group invite."""
    auth_result = _get_authorized_workspace(body, client, context, "cancel_group_invite")
    if not auth_result:
        return
    _, acting = auth_result
    raw = helpers.safe_get(body, "actions", 0, "value")
    try:
        member_id = int(raw)
    except (TypeError, ValueError):
        log_warning("cancel_group_invite", raw_member_id=raw)
        return
    trigger_id = helpers.safe_get(body, "trigger_id")
    if not trigger_id:
        return
    member = DbManager.get_record(schemas.WorkspaceGroupMember, id=member_id)
    if not member or member.status != "pending":
        return
    allowed = member.invited_by_workspace_id == acting.id or helpers.is_workspace_owner(member.group_id, acting.id)
    group = DbManager.get_record(schemas.WorkspaceGroup, id=member.group_id)
    group_name = group.name if group else "this Group"
    target_ws = helpers.get_workspace_by_id(member.workspace_id) if member.workspace_id else None
    target_name = helpers.resolve_workspace_name(target_ws) if target_ws else "Workspace"
    if not allowed:
        text = f":lock: *Only the inviter, or an Owner, can cancel the invite to `{target_name}`.*"
        button = None
    else:
        text = (
            f":eject: *Cancel the invite to `{target_name}`?*\n\n"
            f"They will not join `{group_name}` unless someone invites them again."
        )
        button = ":eject: Cancel Invite"
    blocks = [orm.SectionBlock(label=text)]
    if button:
        blocks.append(
            orm.ActionsBlock(
                elements=[
                    orm.ButtonElement(
                        label=button,
                        action=actions.CONFIG_CANCEL_GROUP_INVITE_CONFIRM,
                        value=str(member_id),
                        style="danger",
                    )
                ]
            )
        )
    orm.BlockView(blocks=blocks).post_modal(
        client=client,
        trigger_id=trigger_id,
        callback_id=actions.CONFIG_CANCEL_GROUP_INVITE_CONFIRM,
        title_text="Cancel Invite",
        submit_button_text=None,
        close_button_text="Cancel" if button else "Close",
        parent_metadata={"member_id": member_id},
        body=body,
    )


def handle_cancel_group_invite_confirm(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Cancel a pending invite after confirmation."""
    _end_pending_invite(body, client, logger, context, is_cancel=True)


def _end_pending_invite(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
    *,
    is_cancel: bool,
) -> None:
    """Decline (invitee) or cancel (inviter or owner) a pending group invite."""
    raw_member_id = helpers.safe_get(body, "actions", 0, "value")
    try:
        member_id = int(raw_member_id)
    except (TypeError, ValueError):
        log_warning("decline_group_invite", raw_member_id=raw_member_id)
        return

    action_name = "cancel_group_invite" if is_cancel else "decline_group_invite"

    auth_result = _get_authorized_workspace(body, client, context, action_name)
    if not auth_result:
        return
    user_id, acting_workspace = auth_result

    member = DbManager.get_record(schemas.WorkspaceGroupMember, id=member_id)
    if not member or member.status != "pending":
        log_info("decline_group_invite", member_id=member_id)
        return

    if is_cancel:
        # Cancel is an inviter action: the workspace that sent the invite may
        # cancel it, and so may an owner of the group (co-owners have equal
        # standing over group membership).
        authorized = member.invited_by_workspace_id == acting_workspace.id or helpers.is_workspace_owner(
            member.group_id, acting_workspace.id
        )
    else:
        # Accept and decline are invitee actions: only the invited workspace.
        authorized = member.workspace_id == acting_workspace.id

    if not authorized:
        log_warning(
            "authorization_denied", action=action_name, member_id=member_id, acting_workspace_id=acting_workspace.id
        )
        return

    group = DbManager.get_record(schemas.WorkspaceGroup, id=member.group_id)
    target_ws = helpers.get_workspace_by_id(member.workspace_id) if member.workspace_id else None
    from helpers.sync_participation import format_action_notice

    notice = format_action_notice(
        action="Cancel Invite" if is_cancel else "Decline",
        workspace_name=helpers.resolve_workspace_name(target_ws) if target_ws else "Workspace",
        group_name=group.name if group else None,
        manager=helpers.format_admin_label(client, user_id, acting_workspace),
        for_managers=True,
        workspace_line=True,
    )
    helpers.update_group_invite_dms(member, target_ws, notice)
    from helpers.notifications import notify_group_managers

    notify_group_managers(
        member.group_id,
        notice,
        exclude_workspace_id=target_ws.id if target_ws else None,
    )

    group_id = member.group_id
    invited_by_id = member.invited_by_workspace_id

    DbManager.delete_records(
        schemas.WorkspaceGroupMember,
        [schemas.WorkspaceGroupMember.id == member_id],
    )

    log_info("group_invite_declined", member_id=member_id, group_id=group_id)

    from helpers.workspace_kind import is_local_workspace, is_stub_workspace

    inviter_ws = helpers.get_workspace_by_id(invited_by_id) if invited_by_id else None
    if group and target_ws and (is_stub_workspace(target_ws) or not is_local_workspace(inviter_ws)):
        try:
            from federation.replicate import replicate_group_leave

            replicate_group_leave(group, target_ws, notice=notice)
        except Exception:
            log_warning("federation_replicate_group_leave_failed", group_id=group_id)

    all_members = DbManager.find_records(
        schemas.WorkspaceGroupMember,
        [
            schemas.WorkspaceGroupMember.group_id == group_id,
            schemas.WorkspaceGroupMember.status == "active",
            schemas.WorkspaceGroupMember.deleted_at.is_(None),
        ],
    )
    for member in all_members:
        if not member.workspace_id:
            continue
        member_ws = helpers.get_workspace_by_id(member.workspace_id)
        if not member_ws or member_ws.deleted_at is not None or not helpers.get_bot_token(member_ws):
            continue
        with contextlib.suppress(Exception):
            builders.refresh_home_tab_for_workspace(member_ws, logger, context=None)

    if target_ws and not target_ws.deleted_at and helpers.get_bot_token(target_ws):
        with contextlib.suppress(Exception):
            builders.refresh_home_tab_for_workspace(target_ws, logger, context=None)
