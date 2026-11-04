"""Admin DM notifications and channel notifications."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from slack_sdk import WebClient
from sqlalchemy.exc import ProgrammingError

from db import DbManager, schemas
from helpers._cache import _cache_get, _cache_set
from helpers.core import safe_get
from helpers.workspace import get_bot_token
from logger import log_debug, log_error, log_info, log_warning
from slack import actions


def get_admin_ids(
    client: WebClient,
    *,
    team_id: str | None = None,
    context: dict | None = None,
) -> list[str]:
    """Return a list of admin/owner user IDs for the workspace behind *client*.

    If *context* and *team_id* are provided, uses request-scoped cache to avoid
    repeated users.list for the same workspace within one request. A 60s process
    cache keyed by *team_id* covers subsequent requests on a warm container.
    """
    if context is not None and team_id:
        cache = context.setdefault("_admin_ids", {})
        if team_id in cache:
            return cache[team_id]

    cache_key = f"admin_ids:{team_id}" if team_id else None
    if cache_key:
        cached = _cache_get(cache_key)
        if cached is not None:
            if context is not None and team_id:
                context.setdefault("_admin_ids", {})[team_id] = cached
            return cached

    from helpers.user_map import _users_list_page

    cursor = ""
    admin_ids: list[str] = []

    while True:
        try:
            res = _users_list_page(client, cursor=cursor)
        except Exception as e:
            log_warning("get_admin_ids", error=str(e))
            break

        members = safe_get(res, "members") or []
        for member in members:
            if member.get("is_bot") or member.get("id") == "USLACKBOT":
                continue
            if member.get("deleted"):
                continue
            if member.get("is_admin") or member.get("is_owner"):
                admin_ids.append(member["id"])

        next_cursor = safe_get(res, "response_metadata", "next_cursor")
        if not next_cursor:
            break
        cursor = next_cursor

    if context is not None and team_id:
        context.setdefault("_admin_ids", {})[team_id] = admin_ids
    if cache_key:
        _cache_set(cache_key, admin_ids)
    return admin_ids


def get_manager_ids(
    client: WebClient,
    *,
    team_id: str | None = None,
    context: dict | None = None,
) -> list[str]:
    """Return Slack admin/owner IDs plus extra managers for *team_id*."""
    from helpers.workspace_settings import extra_manager_user_ids

    admin_ids = get_admin_ids(client, team_id=team_id, context=context)
    if not team_id:
        return admin_ids
    merged = sorted(set(admin_ids) | set(extra_manager_user_ids(team_id)))
    return merged


def notify_admins_dm(
    client: WebClient,
    message: str,
    exclude_user_ids: set[str] | None = None,
    blocks: list[dict] | None = None,
    *,
    include_managers: bool = True,
    team_id: str | None = None,
    context: dict | None = None,
) -> int:
    """Send a DM to workspace admins/owners and managers.

    Managers are included because these DMs are operational (sync failures,
    group changes). Settings / Backup / Reset do not use this helper.
    """
    notified = 0
    kwargs: dict = {"text": message}
    if blocks:
        kwargs["blocks"] = blocks
    user_ids = (
        get_manager_ids(client, team_id=team_id, context=context)
        if include_managers
        else get_admin_ids(client, team_id=team_id, context=context)
    )
    for user_id in user_ids:
        if exclude_user_ids and user_id in exclude_user_ids:
            continue
        try:
            dm = client.conversations_open(users=[user_id])
            channel_id = safe_get(dm, "channel", "id")
            if channel_id:
                client.chat_postMessage(channel=channel_id, **kwargs)
                notified += 1
        except Exception as e:
            log_warning("notify_admins_dm", user_id=user_id, error=str(e))

    return notified


def notify_user_dm(client: WebClient, user_id: str | None, message: str) -> bool:
    """Send *message* to *user_id* as a DM. Same work-phase path as handler ``_dm_user``."""
    if not user_id or not client:
        return False
    try:
        client.chat_postMessage(channel=user_id, text=message)
        return True
    except Exception as e:
        log_warning("notify_user_dm_failed", user_id=user_id, error=str(e))
        return False


def notify_source_user_error(
    *,
    source_client: WebClient | None,
    source_user_id: str | None,
    summary: str,
    details: dict[str, Any] | None = None,
) -> bool:
    """DM the source author with ``format_error_dm`` (work-phase sync failures)."""
    from helpers.core import format_error_dm

    if not source_client or not source_user_id:
        return False
    return notify_user_dm(source_client, source_user_id, format_error_dm(summary, details))


def notify_admins_dm_blocks(
    client: WebClient,
    text: str,
    blocks: list[dict],
    *,
    team_id: str | None = None,
    context: dict | None = None,
) -> list[dict]:
    """Send a Block Kit DM to workspace admins/owners and extra managers."""
    sent: list[dict] = []
    for user_id in get_manager_ids(client, team_id=team_id, context=context):
        try:
            dm = client.conversations_open(users=[user_id])
            channel_id = safe_get(dm, "channel", "id")
            if channel_id:
                res = client.chat_postMessage(channel=channel_id, text=text, blocks=blocks)
                msg_ts = safe_get(res, "ts")
                if msg_ts:
                    sent.append({"channel": channel_id, "ts": msg_ts})
        except Exception as e:
            log_warning("notify_admins_dm_blocks", user_id=user_id, error=str(e))

    return sent


def send_group_invite_dm(member: schemas.WorkspaceGroupMember, workspace: schemas.Workspace, text: str) -> None:
    """DM *workspace*'s managers the invite *text* with Accept and Decline, and keep the DMs on *member*."""
    token = get_bot_token(workspace)
    if not token:
        return
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": text,
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": ":white_check_mark: Accept"},
                    "style": "primary",
                    "action_id": f"{actions.CONFIG_ACCEPT_GROUP_INVITE}_{member.id}",
                    "value": str(member.id),
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": ":eject: Decline"},
                    "style": "danger",
                    "action_id": f"{actions.CONFIG_DECLINE_GROUP_INVITE}_{member.id}",
                    "value": str(member.id),
                },
            ],
        },
    ]
    entries = notify_admins_dm_blocks(WebClient(token=token), text, blocks, team_id=workspace.team_id)
    save_dm_messages_to_group_member(member.id, entries)


def update_group_invite_dms(
    member: schemas.WorkspaceGroupMember,
    workspace: schemas.Workspace | None,
    new_text: str,
) -> None:
    """Replace the original invite DM content with an updated message so the invite
    is removed and replaced by the success message (e.g. workspace joined the group).
    """
    if not member.dm_messages:
        log_debug("invite_dm_update_skipped", member_id=member.id)
        return
    if not workspace or not get_bot_token(workspace):
        return

    try:
        entries = json.loads(member.dm_messages)
    except (ValueError, TypeError):
        log_warning("invite_dm_messages_invalid", member_id=member.id)
        return

    if not entries:
        return

    ws_client = WebClient(token=get_bot_token(workspace))
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": new_text}}]
    for entry in entries:
        channel_id = entry.get("channel")
        message_ts = entry.get("ts")
        if not channel_id or message_ts is None:
            continue
        message_ts_str = str(message_ts).strip()
        if not message_ts_str:
            continue
        try:
            ws_client.chat_update(
                channel=channel_id,
                ts=message_ts_str,
                text=new_text,
                blocks=blocks,
            )
        except Exception as e:
            log_warning(
                "invite_dm_update_failed",
                channel_id=channel_id,
                ts=message_ts_str,
                error=str(e),
            )


def save_dm_messages_to_group_member(member_id: int, dm_entries: list[dict]) -> None:
    """Persist DM channel/ts metadata on a group member record for later updates."""
    import json as _json

    if not dm_entries:
        return
    existing = DbManager.get_record(schemas.WorkspaceGroupMember, id=member_id)
    if not existing:
        return
    try:
        prev = _json.loads(existing.dm_messages) if existing.dm_messages else []
    except (ValueError, TypeError):
        prev = []
    prev.extend(dm_entries)
    DbManager.update_records(
        schemas.WorkspaceGroupMember,
        [schemas.WorkspaceGroupMember.id == member_id],
        {schemas.WorkspaceGroupMember.dm_messages: _json.dumps(prev)},
    )


def notify_managers(
    client: WebClient,
    message: str,
    *,
    team_id: str | None = None,
    channel_id: str | None = None,
    private: bool = False,
    context: dict | None = None,
    exclude_user_ids: set[str] | None = None,
) -> int:
    """DM managers. A private Channel only reaches managers still in that Channel."""
    from helpers.conversations import user_in_private_channel

    notified = 0
    for user_id in get_manager_ids(client, team_id=team_id, context=context):
        if exclude_user_ids is not None and user_id in exclude_user_ids:
            continue
        if private and not user_in_private_channel(team_id, user_id, channel_id):
            continue
        try:
            dm = client.conversations_open(users=[user_id])
            opened = safe_get(dm, "channel", "id")
            if opened:
                client.chat_postMessage(channel=opened, text=message)
                notified += 1
                if exclude_user_ids is not None:
                    exclude_user_ids.add(user_id)
        except Exception as exc:
            log_warning("notify_managers", user_id=user_id, error=str(exc))
    return notified


def notify_channel_or_managers(
    workspace,
    channel_id: str | None,
    text: str,
    *,
    fallback_dm: bool = True,
    private: bool = False,
) -> bool:
    """Post *text* in the Channel. Direct-message managers when that post does not land."""
    token = get_bot_token(workspace) if workspace else None
    if not token or not channel_id:
        log_warning("channel_notice_failed", channel_id=channel_id, reason="no_token")
        return False
    client = WebClient(token=token)
    posted = notify_synced_channels(client, [channel_id], text)
    if posted:
        return True
    if not fallback_dm:
        log_warning(
            "channel_notice_failed",
            channel_id=channel_id,
            reason="not_posted",
            team_id=getattr(workspace, "team_id", None),
        )
        return False
    notify_managers(
        client,
        text,
        team_id=getattr(workspace, "team_id", None),
        channel_id=channel_id,
        private=private,
    )
    log_info("notice_fell_back_to_dm", channel_id=channel_id, team_id=getattr(workspace, "team_id", None))
    return False


def _app_display_name(token: str | None) -> str:
    """Display name of the Slack app that sends with *token*."""
    if not token:
        return "SyncBot"
    from helpers.slack_api import get_app_display_name

    return get_app_display_name(WebClient(token=token))


def notify_primary_action(workspace, action: str) -> None:
    """DM this install's primary Workspace managers. Install and Uninstall only."""
    import os

    from constants import PRIMARY_WORKSPACE
    from helpers.sync_participation import format_action_notice

    primary = (os.environ.get(PRIMARY_WORKSPACE) or "").strip()
    if not primary or workspace is None:
        return
    primary_ws = DbManager.get_record(schemas.Workspace, id=primary)
    token = get_bot_token(primary_ws) if primary_ws and primary_ws.deleted_at is None else None
    if not token:
        return
    name = getattr(workspace, "workspace_name", None) or "Workspace"
    text = format_action_notice(
        action=action,
        workspace_name=name,
        for_managers=True,
        workspace_line=True,
        app_name=_app_display_name(token),
    )
    notify_managers(WebClient(token=token), text, team_id=primary)


def _migration_notice_map() -> dict[str, list[str]]:
    rows = DbManager.find_records(schemas.InstanceSetting, [schemas.InstanceSetting.key == "migration_notices"])
    if not rows or not rows[0].value:
        return {}
    try:
        data = json.loads(rows[0].value)
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(team): [str(peer) for peer in peers] for team, peers in data.items() if isinstance(peers, list)}


def _write_migration_notice_map(data: dict[str, list[str]]) -> None:
    payload = json.dumps(data)
    now = datetime.now(UTC)
    rows = DbManager.find_records(schemas.InstanceSetting, [schemas.InstanceSetting.key == "migration_notices"])
    if rows:
        DbManager.update_records(
            schemas.InstanceSetting,
            [schemas.InstanceSetting.key == "migration_notices"],
            {schemas.InstanceSetting.value: payload, schemas.InstanceSetting.updated_at: now},
        )
        return
    DbManager.create_record(schemas.InstanceSetting(key="migration_notices", value=payload, updated_at=now))


def claim_migration_notice(team_id: str, peer_id: str) -> bool:
    """Record that this install has announced *team_id* moving with *peer_id*.

    Returns False when that pair was already announced, including from another process.
    """
    team_id = (team_id or "").strip()
    peer_id = (peer_id or "").strip()
    if not team_id or not peer_id:
        return False
    data = _migration_notice_map()
    peers = data.get(team_id, [])
    if peer_id in peers:
        return False
    data[team_id] = [*peers, peer_id]
    _write_migration_notice_map(data)
    return True


def claim_once(key: str) -> bool:
    """Insert *key* once. A second caller, including one that raced the first, gets False."""
    from sqlalchemy.exc import IntegrityError

    text = (key or "").strip()[:64]
    if not text:
        return False
    try:
        DbManager.create_record(schemas.InstanceSetting(key=text, value="1", updated_at=datetime.now(UTC)))
        return True
    except IntegrityError:
        return False


def forget_once(key: str) -> None:
    """Drop a :func:`claim_once` mark so a later event can announce."""
    text = (key or "").strip()[:64]
    if not text:
        return
    DbManager.delete_records(schemas.InstanceSetting, [schemas.InstanceSetting.key == text])


def forget_migration_notices(team_id: str) -> None:
    """Drop remembered Migration sends for *team_id* so a later move can announce."""
    team_id = (team_id or "").strip()
    if not team_id:
        return
    data = _migration_notice_map()
    if team_id not in data:
        return
    del data[team_id]
    _write_migration_notice_map(data)


def _local_group_mates(team_id: str) -> list:
    """Local workspaces that share a group with *team_id*, each once."""
    from helpers.workspace import get_workspace_by_id
    from helpers.workspace_kind import is_local_workspace

    workspace = DbManager.get_record(schemas.Workspace, id=team_id)
    if not workspace:
        return []
    memberships = DbManager.find_records(
        schemas.WorkspaceGroupMember,
        [
            schemas.WorkspaceGroupMember.workspace_id == workspace.id,
            schemas.WorkspaceGroupMember.deleted_at.is_(None),
        ],
    )
    group_ids = {row.group_id for row in memberships}
    if not group_ids:
        return []
    mates = DbManager.find_records(
        schemas.WorkspaceGroupMember,
        [
            schemas.WorkspaceGroupMember.group_id.in_(list(group_ids)),
            schemas.WorkspaceGroupMember.deleted_at.is_(None),
        ],
    )
    found: list = []
    seen: set[int] = set()
    for mate in mates:
        other = get_workspace_by_id(mate.workspace_id)
        if not other or other.id in seen or not is_local_workspace(other):
            continue
        seen.add(other.id)
        found.append(other)
    return found


def _dm_local_group_mates(team_id: str, text: str) -> None:
    told_users: set[str] = set()
    for other in _local_group_mates(team_id):
        token = get_bot_token(other)
        if not token:
            continue
        notify_managers(WebClient(token=token), text, team_id=other.team_id, exclude_user_ids=told_users)


def _group_mate_app_name(team_id: str) -> str:
    for other in _local_group_mates(team_id):
        token = get_bot_token(other)
        if token:
            return _app_display_name(token)
    return "SyncBot"


def notify_migration(team_id: str, peer_id: str) -> None:
    """DM each manager once. One send per team and peer, remembered in the database."""
    from helpers.sync_participation import format_action_notice

    team_id = (team_id or "").strip()
    peer_id = (peer_id or "").strip()
    if not team_id or not peer_id or not claim_migration_notice(team_id, peer_id):
        return
    workspace = DbManager.get_record(schemas.Workspace, id=team_id)
    if not workspace or not _local_group_mates(team_id):
        return
    text = format_action_notice(
        action="Migration",
        workspace_name=workspace.workspace_name or "Workspace",
        for_managers=True,
        workspace_line=True,
        app_name=_group_mate_app_name(team_id),
    )
    _dm_local_group_mates(team_id, text)


def notify_install_while_live(team_id: str, peer_id: str, *, app_name: str | None = None) -> None:
    """DM group-mate managers once. *peer_id* listed *team_id* while it is still installed here.

    *app_name* is the Slack app that listed the team. This install sends the
    direct message, and the Acting Manager is that app, not this one. The claim
    is ``install_from:{team}:{peer}``, not the Migration claim, so a later move
    can still announce.
    """
    from helpers.sync_participation import format_action_notice

    team_id = (team_id or "").strip()
    peer_id = (peer_id or "").strip()
    if not team_id or not peer_id or not claim_once(f"install_from:{team_id}:{peer_id}"):
        return
    workspace = DbManager.get_record(schemas.Workspace, id=team_id)
    if not workspace or not _local_group_mates(team_id):
        return
    shown = (app_name or "").strip() or "SyncBot"
    text = format_action_notice(
        action="Install",
        workspace_name=workspace.workspace_name or "Workspace",
        for_managers=True,
        workspace_line=True,
        app_name=shown,
    )
    _dm_local_group_mates(team_id, text)


def notify_group_managers(group_id: int, text: str, *, exclude_workspace_id: int | None = None) -> list:
    """DM managers of each local Workspace in *group_id*. Returns the Workspaces told."""
    from helpers.group_roles import get_active_members
    from helpers.workspace import get_workspace_by_id
    from helpers.workspace_kind import is_local_workspace

    told: list = []
    if not group_id or not (text or "").strip():
        return told
    for member in get_active_members(group_id):
        if not member.workspace_id or member.workspace_id == exclude_workspace_id:
            continue
        workspace = get_workspace_by_id(member.workspace_id)
        if not workspace or workspace.deleted_at is not None or not is_local_workspace(workspace):
            continue
        token = get_bot_token(workspace)
        if not token:
            continue
        try:
            notify_admins_dm(WebClient(token=token), text, team_id=workspace.team_id)
            told.append(workspace)
        except Exception:
            log_warning("failed_to_notify_group_member", workspace_id=member.workspace_id)
    return told


def notify_synced_channels(client: WebClient, channel_ids: list[str], message: str) -> int:
    """Post a message to a list of channels.  Best-effort."""
    notified = 0
    for channel_id in channel_ids:
        try:
            client.chat_postMessage(channel=channel_id, text=message)
            notified += 1
        except Exception as e:
            log_warning("notify_synced_channels", channel_id=channel_id, error=str(e))
    return notified


def purge_stale_soft_deletes() -> int:
    """Hard-delete ``deleted`` Channel rows past retention, then an empty Sync, then a disbanded Group with no Syncs.

    Workspaces soft-deleted past the same window are purged after that. Returns how many workspaces were removed.
    Returns 0 without raising if the schema is missing.
    """
    from helpers.workspace import get_workspace_by_id

    cache_key = "purge_check"
    if _cache_get(cache_key):
        return 0
    _cache_set(cache_key, True, ttl=86400)

    from helpers.settings import soft_delete_retention_days

    retention_days = soft_delete_retention_days()
    # Workspace.deleted_at is timezone-aware. SyncChannel.deleted_at is stored naive.
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    cutoff_naive = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=retention_days)

    try:
        channels_purged = _purge_expired_deleted_channels(cutoff_naive)
    except ProgrammingError as e:
        log_debug("purge_stale_soft_deletes", value=e.orig if hasattr(e, "orig") else e)
        return 0

    try:
        stale_workspaces = DbManager.find_records(
            schemas.Workspace,
            [
                schemas.Workspace.deleted_at.isnot(None),
                schemas.Workspace.deleted_at < cutoff,
            ],
        )
    except ProgrammingError as e:
        log_debug("purge_stale_soft_deletes", value=e.orig if hasattr(e, "orig") else e)
        return 0

    if not stale_workspaces:
        if channels_purged:
            log_info("purge_stale_soft_deletes_complete", purged=0, sync_channels=channels_purged)
        return 0

    purged = 0
    for ws in stale_workspaces:
        ws_name = ws.workspace_name or ws.team_id or f"Workspace {ws.id}"
        manager_notices = _tell_other_channels_workspace_removed(ws, ws_name, retention_days)

        group_memberships = DbManager.find_records(
            schemas.WorkspaceGroupMember,
            [schemas.WorkspaceGroupMember.workspace_id == ws.id],
        )

        notified_ws: set[int] = set()
        for membership in group_memberships:
            other_members = DbManager.find_records(
                schemas.WorkspaceGroupMember,
                [
                    schemas.WorkspaceGroupMember.group_id == membership.group_id,
                    schemas.WorkspaceGroupMember.workspace_id != ws.id,
                    schemas.WorkspaceGroupMember.status == "active",
                    schemas.WorkspaceGroupMember.deleted_at.is_(None),
                ],
            )
            for member in other_members:
                if not member.workspace_id or member.workspace_id in notified_ws:
                    continue
                member_ws = get_workspace_by_id(member.workspace_id)
                if not member_ws or member_ws.deleted_at is not None or not get_bot_token(member_ws):
                    continue
                notified_ws.add(member.workspace_id)
                member_client = WebClient(token=get_bot_token(member_ws))
                for text in manager_notices:
                    try:
                        notify_admins_dm(member_client, text, team_id=member_ws.team_id)
                    except Exception as e:
                        log_warning("purge_notify_failed", workspace_id=member.workspace_id, error=str(e))

        # purge_workspace, not a bare Workspace delete: seven foreign keys across
        # six tables still reference this row, so a parent-first delete fails on
        # MySQL with error 1451 and the retention purge silently stops working.
        from helpers.sync_cleanup import purge_workspace

        try:
            purge_workspace(ws.id)
        except Exception as e:
            log_error("purge_workspace_failed", workspace_id=ws.id, error=str(e))
            continue

        purged += 1

    if purged or channels_purged:
        log_info("purge_stale_soft_deletes_complete", purged=purged, sync_channels=channels_purged)

    return purged


def _purge_expired_deleted_channels(cutoff_naive: datetime) -> int:
    """Hard-delete ``deleted`` rows past retention. ``uninstalled`` rows stay with their workspace."""
    expired = DbManager.find_records(
        schemas.SyncChannel,
        [
            schemas.SyncChannel.status == "deleted",
            schemas.SyncChannel.deleted_at.isnot(None),
            schemas.SyncChannel.deleted_at < cutoff_naive,
        ],
    )
    from helpers.sync_cleanup import purge_sync, purge_sync_channels

    emptied: set[int] = set()
    for row in expired:
        if not getattr(row, "id", None):
            continue
        purge_sync_channels([row])
        sync_id = getattr(row, "sync_id", None)
        if not sync_id or sync_id in emptied:
            continue
        left = DbManager.count_records(schemas.SyncChannel, [schemas.SyncChannel.sync_id == sync_id])
        if left:
            continue
        emptied.add(sync_id)
        sync = DbManager.get_record(schemas.Sync, id=sync_id)
        group_id = getattr(sync, "group_id", None) if sync else None
        purge_sync(sync_id)
        if not group_id:
            continue
        group = DbManager.get_record(schemas.WorkspaceGroup, id=group_id)
        if not group or getattr(group, "status", None) != "disbanded":
            continue
        if DbManager.count_records(schemas.Sync, [schemas.Sync.group_id == group_id]):
            continue
        from helpers.group_roles import _disband_group

        _disband_group(group_id)
    return len(expired)


def _retention_notice(workspace_name: str, retention_days: int, sync, *, for_managers: bool) -> str:
    """Action Notice for a Workspace that aged out. The day count is only on the manager copy."""
    from helpers.sync_participation import format_action_notice

    group_name = None
    if sync is not None and getattr(sync, "group_id", None):
        group = DbManager.get_record(schemas.WorkspaceGroup, id=sync.group_id)
        group_name = group.name if group else None
    return format_action_notice(
        action="Retention",
        workspace_name=workspace_name,
        group_name=group_name,
        manager=None,
        sync_id=getattr(sync, "id", None),
        for_managers=for_managers,
        retention_days=retention_days if for_managers else None,
        workspace_line=sync is None,
    )


def _tell_other_channels_workspace_removed(workspace, workspace_name: str, retention_days: int) -> list[str]:
    """One Channel sentence per Sync, and a remove so a peer's stub ages out on its own clock.

    Returns the manager copy, one per Sync. With no Sync, one notice about the Workspace.
    """
    from helpers.leave_sync import _replicate_one_remove
    from helpers.sync_lifecycle import post_to_other_sync_channels

    rows = DbManager.find_records(
        schemas.SyncChannel,
        [schemas.SyncChannel.workspace_id == workspace.id],
    )
    manager_notices: list[str] = []
    told: set[int] = set()
    for row in rows:
        sync_id = getattr(row, "sync_id", None)
        sync = DbManager.get_record(schemas.Sync, id=sync_id) if sync_id else None
        notice = None
        if sync_id and sync_id not in told and sync is not None:
            told.add(sync_id)
            notice = _retention_notice(workspace_name, retention_days, sync, for_managers=False)
            post_to_other_sync_channels(workspace, sync_id, notice)
            manager_notices.append(_retention_notice(workspace_name, retention_days, sync, for_managers=True))
        _replicate_one_remove(sync, getattr(sync, "group_id", None) if sync else None, workspace, row, notice=notice)
    if not manager_notices:
        manager_notices.append(_retention_notice(workspace_name, retention_days, None, for_managers=True))
    return manager_notices
