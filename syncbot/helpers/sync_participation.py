"""Channel publish/subscribe participation and fan-out discovery."""

from __future__ import annotations

from db import DbManager, schemas
from helpers._cache import _cache_delete_prefix, _cache_get, _cache_set


def invalidate_channel_memberships(channel_id: str | None) -> None:
    """Drop cached membership and publish-target entries for *channel_id*."""
    if channel_id:
        _cache_delete_prefix(f"channel_memberships:{channel_id}:")
        _cache_delete_prefix(f"publish_targets:{channel_id}")


def invalidate_sync_fanout_for_syncs(sync_ids) -> None:
    """Drop fan-out caches for every Channel that belongs to *sync_ids*.

    Includes soft-deleted rows so Pause, Leave, and uninstall still clear the
    origin ``publish_targets:`` entry that pointed at this Channel.
    """
    ids = [sid for sid in sync_ids if sid]
    if not ids:
        return
    channels = DbManager.find_records(
        schemas.SyncChannel,
        [schemas.SyncChannel.sync_id.in_(ids)],
    )
    for channel in channels:
        invalidate_channel_memberships(channel.channel_id)


def get_active_sync_channel(sync_channel: schemas.SyncChannel | None) -> schemas.SyncChannel | None:
    """Re-read *sync_channel* so pause or leave wins over a stale fan-out cache."""
    if sync_channel is None:
        return None
    row_id = getattr(sync_channel, "id", None)
    if not row_id:
        return None
    row = DbManager.get_record(schemas.SyncChannel, id=row_id)
    if row is None or row.deleted_at is not None:
        return None
    if (row.status or "active") != "active":
        return None
    return row


def parse_participation_flags(value: str | None) -> tuple[bool, bool]:
    """Map a participation radio value to ``(publishes, subscribes)``.

    Leftover ``subscribe_and_publish`` is the same as Publish and Subscribe.
    """
    raw = (value or "").strip()
    if raw == "publish_only":
        return True, False
    if raw == "subscribe_only":
        return False, True
    return True, True


def participation_label(sync_channel: schemas.SyncChannel | None) -> str:
    """Radio wording for this membership: Publish only, Subscribe only, or Publish and Subscribe."""
    publishes = channel_publishes(sync_channel)
    subscribes = channel_subscribes(sync_channel)
    if publishes and not subscribes:
        return "Publish only"
    if subscribes and not publishes:
        return "Subscribe only"
    return "Publish and Subscribe"


def sync_state_label(sync_channel) -> str:
    """The Home Status line and the bracket on a member tick.

    Active is the participation. Paused is ``Paused`` and not the participation.
    """
    status = getattr(sync_channel, "status", None)
    if status == "paused":
        return "Paused"
    if status == "uninstalled":
        return "Uninstalled"
    if status == "pending":
        return "Waiting for migration"
    return participation_label(sync_channel)


def sync_member_text(channel, workspace=None) -> str:
    """``#general (Workspace A) [Publish and Subscribe]`` without the code ticks."""
    from helpers.workspace import get_workspace_by_id, lookup_channel_name, resolve_workspace_name

    if workspace is None and getattr(channel, "workspace_id", None):
        workspace = get_workspace_by_id(channel.workspace_id)
    channel_id = getattr(channel, "channel_id", None) or "channel"
    stored = (getattr(channel, "channel_name", None) or "").strip()
    raw = stored or (lookup_channel_name(channel_id, workspace) if workspace else "") or channel_id
    name = f"#{str(raw).removeprefix('#')}"
    ws_name = resolve_workspace_name(workspace) if workspace else "Workspace"
    return f"{name} ({ws_name}) [{sync_state_label(channel)}]"


def format_sync_member(channel, workspace=None) -> str:
    """One code-ticked member of a Sync."""
    return f"`{sync_member_text(channel, workspace)}`"


def format_workspaces_in_sync(sync_id: int | None, *, label_for: dict[int, str] | None = None) -> str:
    """Workspaces still in *sync_id* after the write, one each, with the Status label."""
    from sqlalchemy import or_

    from helpers.workspace import get_workspace_by_id, resolve_workspace_name

    if not sync_id:
        return "`none`"
    rows = DbManager.find_records(
        schemas.SyncChannel,
        [
            schemas.SyncChannel.sync_id == int(sync_id),
            or_(schemas.SyncChannel.deleted_at.is_(None), schemas.SyncChannel.status == "uninstalled"),
        ],
    )
    chosen: dict[int, tuple[str, str]] = {}
    for row in rows:
        workspace = get_workspace_by_id(row.workspace_id) if row.workspace_id else None
        name = resolve_workspace_name(workspace) if workspace else "Workspace"
        label = (label_for or {}).get(row.workspace_id) or sync_state_label(row)
        current = chosen.get(row.workspace_id)
        if current is None or (current[1] in {"Paused", "Uninstalled"} and label not in {"Paused", "Uninstalled"}):
            chosen[row.workspace_id] = (name, label)
    if not chosen:
        return "`none`"
    parts = [f"{name} [{label}]" for name, label in sorted(chosen.values(), key=lambda item: item[0].lower())]
    return ", ".join(parts)


_ACTION_ICONS = {
    "Create": ":outbox_tray:",
    "Join": ":inbox_tray:",
    "Edit": ":pencil2:",
    "Pause": ":double_vertical_bar:",
    "Resume": ":arrow_forward:",
    "Leave": ":wave:",
    "Archive": ":double_vertical_bar:",
    "Unarchive": ":arrow_forward:",
    "Removed": ":double_vertical_bar:",
    "Channel deleted": ":wave:",
    "Privacy": ":wave:",
    "Disband": ":wastebasket:",
    "Remove": ":eject:",
    "Uninstall": ":double_vertical_bar:",
    "Install": ":inbox_tray:",
    "Migration": ":package:",
    "Leave Connection": ":wave:",
    "Join Group": ":punch:",
    "Invite": ":punch:",
    "Promote": ":key:",
    "Give Up Ownership": ":door:",
    "Decline": ":eject:",
    "Cancel Invite": ":eject:",
    "Retention": ":wastebasket:",
}


def _plain_label(value: str | None) -> str:
    text = (value or "").strip()
    if text.startswith("`") and text.endswith("`") and len(text) >= 2:
        return text[1:-1]
    return text


def format_action_notice(
    *,
    action: str,
    workspace_name: str,
    group_name: str | None = None,
    manager: str | None = None,
    sync_id: int | None = None,
    channel_name: str | None = None,
    retention_days: int | None = None,
    history_deleted: bool = False,
    for_managers: bool = False,
    note: str | None = None,
    attach_workspace: bool = True,
    label_for: dict[int, str] | None = None,
    workspace_line: bool = False,
    app_name: str | None = None,
    details: list[str] | None = None,
) -> str:
    """The Channel post, or the acting Workspace's manager DM when *for_managers* is set.

    Call this after the row is saved. ``Workspaces in Sync`` is read from the database.
    *app_name* is this Slack app's display name. It is kept whole, including parentheses.
    A person's ``Name (Workspace)`` label still drops the workspace on a manager DM.
    """
    icon = _ACTION_ICONS.get(action, ":wave:")
    lines: list[str] = []
    group = (group_name or "").strip()
    if group:
        lines.append(f"Workspace Group: {group}")
    app = (app_name or "").strip()
    bare = _plain_label(manager) or "SyncBot"
    if app:
        shown = app
    elif manager is None or bare == "SyncBot":
        shown = f"SyncBot ({workspace_name})" if workspace_name and not for_managers else "SyncBot"
    elif " (" in bare and bare.endswith(")"):
        shown = bare if not for_managers else bare.split(" (", 1)[0]
    elif for_managers or not attach_workspace:
        shown = bare
    else:
        shown = f"{bare} ({workspace_name})" if workspace_name else bare
    lines.append(f"Acting Manager: {shown}")
    lines.append(f"Action: {action}")
    if for_managers and channel_name:
        name = str(channel_name).strip()
        if not name.startswith("#"):
            name = f"#{name.removeprefix('#')}"
        lines.append(f"Affected Channel: {name}")
    if workspace_line:
        lines.append(f"Workspace: {workspace_name or 'Workspace'}")
    else:
        roster = format_workspaces_in_sync(sync_id, label_for=label_for).replace("`", "")
        lines.append(f"Workspaces in Sync: {roster}")
    for detail in details or []:
        lines.append(str(detail).replace("`", "'"))
    if for_managers and history_deleted:
        lines.append("History: Deleted")
    elif for_managers and retention_days is not None:
        lines.append(f"Data Retention Days: {retention_days}")
    if for_managers and note:
        lines.append(f"Note: {note}")
    body = "\n".join(lines)
    return f"{icon} *SyncBot Action Notice*\n```\n{body}\n```"


def channel_publishes(sync_channel: schemas.SyncChannel | None) -> bool:
    """True when this membership originates messages and reactions."""
    if sync_channel is None:
        return False
    return bool(getattr(sync_channel, "publishes", True))


def channel_subscribes(sync_channel: schemas.SyncChannel | None) -> bool:
    """True when this membership receives messages and reactions."""
    if sync_channel is None:
        return False
    return bool(getattr(sync_channel, "subscribes", True))


def get_channel_memberships(
    channel_id: str,
    *,
    active_only: bool = True,
) -> list[tuple[schemas.SyncChannel, schemas.Workspace]]:
    """Return the ``(SyncChannel, Workspace)`` rows for *channel_id* with ``deleted_at`` unset.

    ``active_only`` adds ``status == "active"``. Paused and pending rows stay in the
    default-off answer so leave, Home, and pause see every relationship.
    """
    if not channel_id:
        return []

    cache_key = f"channel_memberships:{channel_id}:{'active' if active_only else 'all'}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    filters = [
        schemas.SyncChannel.channel_id == channel_id,
        schemas.SyncChannel.deleted_at.is_(None),
    ]
    if active_only:
        filters.append(schemas.SyncChannel.status == "active")

    rows = DbManager.find_join_records2(
        left_cls=schemas.SyncChannel,
        right_cls=schemas.Workspace,
        filters=filters,
    )

    seen: set[tuple[int, int]] = set()
    deduped: list[tuple[schemas.SyncChannel, schemas.Workspace]] = []
    for sc, ws in rows:
        key = (sc.sync_id, ws.id)
        if key not in seen:
            seen.add(key)
            deduped.append((sc, ws))

    _cache_set(cache_key, deduped)
    return deduped


def has_channel_sync(channel_id: str) -> bool:
    """True when any SyncChannel for *channel_id* has ``deleted_at`` unset, including a paused row."""
    if not channel_id:
        return False
    return bool(get_channel_memberships(channel_id, active_only=False))


def origin_publishes_anywhere(channel_id: str) -> bool:
    """True when *channel_id* has at least one active membership with publishes=True."""
    return any(
        channel_publishes(sync_channel)
        for sync_channel, _workspace in get_channel_memberships(channel_id, active_only=True)
    )


def get_origin_sync_channel(channel_id: str) -> schemas.SyncChannel | None:
    """Return one publishing SyncChannel for *channel_id*, or None if none publish."""
    memberships = get_channel_memberships(channel_id, active_only=True)
    for sc, _ws in memberships:
        if channel_publishes(sc):
            return sc
    return None


def iter_publish_targets(
    channel_id: str,
) -> list[tuple[schemas.SyncChannel, schemas.Workspace]]:
    """Subscribers of syncs where *channel_id* publishes; dedupe (workspace, channel).

    Does not walk the target Channel's other groups (no hop).
    """
    if not channel_id:
        return []

    cache_key = f"publish_targets:{channel_id}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    publish_sync_ids: set[int] = set()
    origin_keys: set[tuple[int, str]] = set()
    for sc, ws in get_channel_memberships(channel_id, active_only=True):
        if not channel_publishes(sc):
            continue
        publish_sync_ids.add(sc.sync_id)
        origin_keys.add((ws.id, sc.channel_id))

    if not publish_sync_ids:
        _cache_set(cache_key, [])
        return []

    sync_id_list = list(publish_sync_ids)

    peers = DbManager.find_join_records2(
        left_cls=schemas.SyncChannel,
        right_cls=schemas.Workspace,
        filters=[
            schemas.SyncChannel.sync_id.in_(sync_id_list),
            schemas.SyncChannel.deleted_at.is_(None),
            schemas.SyncChannel.status == "active",
        ],
    )

    targets: list[tuple[schemas.SyncChannel, schemas.Workspace]] = []
    seen: set[tuple[int, str]] = set()
    for sc, ws in peers:
        key = (ws.id, sc.channel_id)
        if key in origin_keys or key in seen:
            continue
        if not channel_subscribes(sc):
            continue
        seen.add(key)
        targets.append((sc, ws))

    _cache_set(cache_key, targets)
    return targets


def already_subscribed_to_source(
    *,
    workspace_id: int,
    source_workspace_id: int,
    source_channel_id: str,
    exclude_sync_id: int | None = None,
) -> bool:
    """True if *workspace_id* already subscribes to this published source in another sync."""
    rows = DbManager.find_records(
        schemas.SyncChannel,
        [
            schemas.SyncChannel.workspace_id == workspace_id,
            schemas.SyncChannel.deleted_at.is_(None),
        ],
    )
    candidate_sync_ids = [
        sc.sync_id
        for sc in rows
        if channel_subscribes(sc) and (exclude_sync_id is None or sc.sync_id != exclude_sync_id)
    ]
    if not candidate_sync_ids:
        return False

    publishers = DbManager.find_records(
        schemas.SyncChannel,
        [
            schemas.SyncChannel.sync_id.in_(candidate_sync_ids),
            schemas.SyncChannel.workspace_id == source_workspace_id,
            schemas.SyncChannel.channel_id == source_channel_id,
            schemas.SyncChannel.deleted_at.is_(None),
        ],
    )
    return any(channel_publishes(p) for p in publishers)
