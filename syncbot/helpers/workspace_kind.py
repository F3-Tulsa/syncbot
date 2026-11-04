"""Workspace kind helpers: local install vs federated stub.

Import this submodule from handlers/helpers. Do not ``import helpers`` here.
Live vs stub is ``workspaces.instance_id`` vs this install's fingerprint — not
token presence.
"""

from __future__ import annotations

from typing import Any, Literal

from db import DbManager, schemas
from helpers._cache import _cache_get, _cache_set
from logger import log_debug

ConvertResult = Literal["converted", "refused_live", "missing", "already_stub", "already_local", "blocked"]


def _self_instance_id() -> str:
    from federation.core import get_instance_id

    return get_instance_id()


def workspace_allowed_for_peer(workspace: schemas.Workspace | None, peer: schemas.Instance | None) -> bool:
    """True when *workspace* may be replicated or delivered to *peer*.

    When this peer has an allowlist, only those local Workspaces use the
    connection. With no allowlist rows, any local Workspace may. A stub is
    only that peer's own Workspace.
    """
    if workspace is None or peer is None or not getattr(peer, "instance_id", None):
        return False
    if is_stub_workspace(workspace):
        return getattr(workspace, "instance_id", None) == peer.instance_id
    if not is_local_workspace(workspace):
        return False
    workspace_id = getattr(workspace, "id", None)
    if not workspace_id:
        return False
    allowlist = DbManager.find_records(
        schemas.FederationWorkspaceAllowlist,
        [schemas.FederationWorkspaceAllowlist.instance_id == peer.instance_id],
    )
    if not allowlist:
        return True
    return workspace_id in {row.workspace_id for row in allowlist if row.workspace_id}


def is_local_workspace(workspace: schemas.Workspace | None) -> bool:
    """True when *workspace* is a live install on this instance."""
    if workspace is None or getattr(workspace, "deleted_at", None) is not None:
        return False
    instance_id = getattr(workspace, "instance_id", None)
    if not instance_id:
        return False
    try:
        return instance_id == _self_instance_id()
    except Exception:
        return False


def is_stub_workspace(workspace: schemas.Workspace | None) -> bool:
    """True when *workspace* is a remote peer stub (peer instance_id, not deleted)."""
    if workspace is None or getattr(workspace, "deleted_at", None) is not None:
        return False
    instance_id = getattr(workspace, "instance_id", None)
    if not instance_id:
        return False
    try:
        return instance_id != _self_instance_id()
    except Exception:
        return bool(instance_id)


def peer_for_workspace(workspace: schemas.Workspace | None) -> schemas.Instance | None:
    """Return the peer Instance for a stub, or None."""
    if not is_stub_workspace(workspace):
        return None
    peer_id = workspace.instance_id
    if not peer_id:
        return None
    peer = DbManager.get_record(schemas.Instance, id=peer_id)
    if not peer or peer.status != "active":
        return None
    return peer


def peer_is_trusted(peer: schemas.Instance | None) -> bool:
    if peer is None:
        return False
    return (getattr(peer, "trust_status", None) or "trusted") == "trusted"


def is_self_instance(row: object | None) -> bool:
    """True when *row* is this install's ``instances`` row.

    A mock or any other object is not self. Only a stored private key marks
    the row.
    """
    key = getattr(row, "private_key_encrypted", None)
    return isinstance(key, str) and bool(key)


def is_deliverable_workspace(workspace: schemas.Workspace | None) -> bool:
    """Local with this instance, or stub whose peer is active and trusted."""
    if is_local_workspace(workspace):
        return True
    if not is_stub_workspace(workspace):
        return False
    peer = peer_for_workspace(workspace)
    return bool(peer and peer_is_trusted(peer))


def can_select_channels(workspace: schemas.Workspace | None) -> bool:
    return is_local_workspace(workspace)


def can_publish_home(workspace: schemas.Workspace | None) -> bool:
    return is_local_workspace(workspace)


def can_dm(workspace: schemas.Workspace | None) -> bool:
    return is_local_workspace(workspace)


def can_promote(workspace: schemas.Workspace | None) -> bool:
    """True for an active workspace on this instance or another one.

    ``deleted_at`` makes both checks false. A pending invite is not an active
    membership, so it never reaches a promote button.
    """
    return is_local_workspace(workspace) or is_stub_workspace(workspace)


def _stub_channel_rows(stub_ids: list[int], *, status: str | None = None) -> list:
    if not stub_ids:
        return []
    filters = [
        schemas.SyncChannel.workspace_id.in_(stub_ids),
        schemas.SyncChannel.deleted_at.is_(None),
    ]
    if status:
        filters.append(schemas.SyncChannel.status == status)
    return DbManager.find_records(schemas.SyncChannel, filters)


def _announce_stub_rows(rows: list, *, state: str) -> None:
    """One action notice per Sync, carried to the other Channels and to the peer."""
    from helpers.leave_sync import group_name_for_sync_id
    from helpers.sync_lifecycle import announce_sync_channels, replicate_sync_channel_status
    from helpers.sync_participation import format_action_notice
    from helpers.workspace import get_workspace_by_id, resolve_workspace_name

    told: set[tuple[int, int]] = set()
    action = "Resume" if state == "resumed" else "Pause"
    for row in rows:
        workspace = get_workspace_by_id(row.workspace_id)
        if not workspace or not row.sync_id:
            continue
        key = (row.sync_id, row.workspace_id)
        sentence = format_action_notice(
            action=action,
            workspace_name=resolve_workspace_name(workspace),
            group_name=group_name_for_sync_id(row.sync_id),
            sync_id=row.sync_id,
        )
        if key in told:
            replicate_sync_channel_status(row, workspace)
        else:
            announce_sync_channels(workspace, row, sentence)
            told.add(key)


def mark_peer_trusted(peer: schemas.Instance) -> None:
    """Mark *peer* trusted and unpause SyncChannels on its stub workspaces."""
    if peer is None or not getattr(peer, "instance_id", None) or is_self_instance(peer):
        return
    DbManager.update_records(
        schemas.Instance,
        [schemas.Instance.instance_id == peer.instance_id],
        {schemas.Instance.trust_status: "trusted"},
    )
    stubs = DbManager.find_records(
        schemas.Workspace,
        [
            schemas.Workspace.instance_id == peer.instance_id,
            schemas.Workspace.deleted_at.is_(None),
        ],
    )
    stub_ids = [ws.id for ws in stubs if ws.id]
    paused = _stub_channel_rows(stub_ids, status="paused")
    if stub_ids:
        DbManager.update_records(
            schemas.SyncChannel,
            [
                schemas.SyncChannel.workspace_id.in_(stub_ids),
                schemas.SyncChannel.deleted_at.is_(None),
                schemas.SyncChannel.status == "paused",
            ],
            {schemas.SyncChannel.status: "active"},
        )
    for row in paused:
        row.status = "active"
    _announce_stub_rows(paused, state="resumed")


def mark_peer_untrusted(peer: schemas.Instance) -> None:
    """Mark *peer* untrusted and pause SyncChannels on its stub workspaces."""
    if peer is None or not getattr(peer, "instance_id", None) or is_self_instance(peer):
        return
    DbManager.update_records(
        schemas.Instance,
        [schemas.Instance.instance_id == peer.instance_id],
        {schemas.Instance.trust_status: "untrusted"},
    )
    stubs = DbManager.find_records(
        schemas.Workspace,
        [
            schemas.Workspace.instance_id == peer.instance_id,
            schemas.Workspace.deleted_at.is_(None),
        ],
    )
    stub_ids = [ws.id for ws in stubs if ws.id]
    live = [row for row in _stub_channel_rows(stub_ids) if getattr(row, "status", None) != "paused"]
    if stub_ids:
        DbManager.update_records(
            schemas.SyncChannel,
            [
                schemas.SyncChannel.workspace_id.in_(stub_ids),
                schemas.SyncChannel.deleted_at.is_(None),
            ],
            {schemas.SyncChannel.status: "paused"},
        )
    for row in live:
        row.status = "paused"
    _announce_stub_rows(live, state="on_hold")


def _remember_pending_stub(workspace_id: int, peer_instance_id: str) -> None:
    existing = DbManager.find_records(
        schemas.FederationPendingStub,
        [
            schemas.FederationPendingStub.workspace_id == workspace_id,
            schemas.FederationPendingStub.instance_id == peer_instance_id,
        ],
    )
    if existing:
        return
    DbManager.create_record(
        schemas.FederationPendingStub(
            workspace_id=workspace_id,
            instance_id=peer_instance_id,
        )
    )


def _clear_pending_stub(workspace_id: int, peer_instance_id: str | None = None) -> None:
    filters = [schemas.FederationPendingStub.workspace_id == workspace_id]
    if peer_instance_id:
        filters.append(schemas.FederationPendingStub.instance_id == peer_instance_id)
    DbManager.delete_records(schemas.FederationPendingStub, filters)


def _restore_memberships_and_channels(workspace_id: int) -> None:
    """Clear ``deleted_at`` on active group memberships and on ``uninstalled`` Channel rows.

    A ``deleted`` Channel row stays deleted. The caller then pauses and rejoins.
    """
    soft_memberships = DbManager.find_records(
        schemas.WorkspaceGroupMember,
        [
            schemas.WorkspaceGroupMember.workspace_id == workspace_id,
            schemas.WorkspaceGroupMember.deleted_at.isnot(None),
            schemas.WorkspaceGroupMember.status == "active",
        ],
    )
    restored_group_ids: set[int] = set()
    for membership in soft_memberships:
        group = DbManager.get_record(schemas.WorkspaceGroup, id=membership.group_id)
        if not group or group.status != "active":
            continue
        DbManager.update_records(
            schemas.WorkspaceGroupMember,
            [schemas.WorkspaceGroupMember.id == membership.id],
            {schemas.WorkspaceGroupMember.deleted_at: None},
        )
        restored_group_ids.add(membership.group_id)

    if not restored_group_ids:
        return

    soft_channels = DbManager.find_records(
        schemas.SyncChannel,
        [
            schemas.SyncChannel.workspace_id == workspace_id,
            schemas.SyncChannel.status == "uninstalled",
            schemas.SyncChannel.deleted_at.isnot(None),
        ],
    )
    for sync_channel in soft_channels:
        sync = DbManager.get_record(schemas.Sync, id=sync_channel.sync_id)
        if sync and sync.group_id in restored_group_ids:
            DbManager.update_records(
                schemas.SyncChannel,
                [schemas.SyncChannel.id == sync_channel.id],
                {schemas.SyncChannel.deleted_at: None, schemas.SyncChannel.status: "active"},
            )


def _log_convert(
    result: str,
    team_id: str,
    peer_instance_id: str,
    *,
    workspace_id: int | None = None,
    source: str | None = None,
) -> None:
    fields: dict[str, Any] = {
        "result": result,
        "team_id": team_id,
        "peer_instance_id": peer_instance_id,
    }
    if workspace_id is not None:
        fields["workspace_id"] = workspace_id
    if source:
        fields["source"] = source
    log_debug("workspace_convert", **fields)


def notify_live_workspace_conflict(
    team_id: str,
    peer_instance_id: str,
    *,
    app_name: str | None = None,
) -> None:
    """DM group-mate managers once when a peer claims a still-live team.

    *app_name* is the Slack app that listed the team. The direct message is sent
    by this install, and the Acting Manager is that other app.
    """
    cache_key = f"fed_live_conflict:{team_id}:{peer_instance_id}"
    if _cache_get(cache_key):
        return
    _cache_set(cache_key, True, ttl=86400)
    from helpers.notifications import notify_install_while_live

    notify_install_while_live(team_id, peer_instance_id, app_name=app_name)
    log_debug(
        "live_conflict",
        team_id=team_id,
        peer_instance_id=peer_instance_id,
        notified=1,
    )


def convert_workspace_to_local(team_id: str, *, source: str | None = None) -> ConvertResult:
    """Convert *team_id* to a live install on this instance. PostMeta stays.

    Reverse of :func:`convert_workspace_to_stub`: reinstall, import, or reclaim
    after Leave Connection.
    """
    team_id = str(team_id).strip()
    if not team_id:
        _log_convert("missing", team_id, "", source=source)
        return "missing"

    from helpers.settings import team_id_is_blocked

    if team_id_is_blocked(team_id):
        _log_convert("blocked", team_id, "", source=source)
        return "blocked"

    workspace = DbManager.get_record(schemas.Workspace, id=team_id)
    if not workspace:
        _log_convert("missing", team_id, "", source=source)
        return "missing"

    self_id = _self_instance_id()
    if is_local_workspace(workspace):
        _clear_pending_stub(workspace.id)
        _log_convert(
            "already_local",
            team_id,
            self_id,
            workspace_id=workspace.id,
            source=source,
        )
        return "already_local"

    DbManager.update_records(
        schemas.Workspace,
        [schemas.Workspace.id == workspace.id],
        {
            schemas.Workspace.instance_id: self_id,
            schemas.Workspace.deleted_at: None,
        },
    )
    _restore_memberships_and_channels(workspace.id)
    _clear_pending_stub(workspace.id)
    _log_convert(
        "converted",
        team_id,
        self_id,
        workspace_id=workspace.id,
        source=source,
    )
    from helpers.notifications import forget_migration_notices, forget_once

    forget_migration_notices(team_id)
    forget_once(f"uninstall_notice:{team_id}")
    return "converted"


def convert_workspace_to_stub(
    team_id: str,
    peer_instance_id: str,
    *,
    source: str | None = None,
    migration_notice: bool = True,
    app_name: str | None = None,
) -> ConvertResult:
    """Convert *team_id* to a stub for *peer_instance_id*, or refuse if still live.

    Returns:
      * ``converted`` — soft-deleted (or already convertible) row became a stub
      * ``refused_live`` — still installed here; pending stub remembered
      * ``already_stub`` — already a stub for this peer
      * ``missing`` — no workspace row for *team_id*
    """
    team_id = str(team_id).strip()
    peer_instance_id = str(peer_instance_id).strip()
    if not team_id or not peer_instance_id:
        _log_convert("missing", team_id, peer_instance_id, source=source)
        return "missing"

    workspace = DbManager.get_record(schemas.Workspace, id=team_id)
    if not workspace:
        _log_convert("missing", team_id, peer_instance_id, source=source)
        return "missing"

    if (
        getattr(workspace, "deleted_at", None) is None
        and workspace.instance_id == peer_instance_id
        and peer_instance_id != _self_instance_id()
    ):
        _clear_pending_stub(workspace.id, peer_instance_id)
        _log_convert(
            "already_stub",
            team_id,
            peer_instance_id,
            workspace_id=workspace.id,
            source=source,
        )
        return "already_stub"

    if is_local_workspace(workspace):
        _remember_pending_stub(workspace.id, peer_instance_id)
        notify_live_workspace_conflict(team_id, peer_instance_id, app_name=app_name)
        _log_convert(
            "refused_live",
            team_id,
            peer_instance_id,
            workspace_id=workspace.id,
            source=source,
        )
        return "refused_live"

    # Soft-deleted on this instance, or already pointing elsewhere: become stub.
    DbManager.update_records(
        schemas.Workspace,
        [schemas.Workspace.id == workspace.id],
        {
            schemas.Workspace.instance_id: peer_instance_id,
            schemas.Workspace.deleted_at: None,
        },
    )
    _restore_memberships_and_channels(workspace.id)
    _clear_pending_stub(workspace.id, peer_instance_id)
    _log_convert(
        "converted",
        team_id,
        peer_instance_id,
        workspace_id=workspace.id,
        source=source,
    )
    if migration_notice:
        from helpers.notifications import notify_migration

        notify_migration(team_id, peer_instance_id)
    return "converted"


def ensure_stub_workspace(
    *,
    team_id: str,
    workspace_name: str | None,
    instance_id: str,
) -> schemas.Workspace | None:
    """Upsert a stub Workspace for *team_id* on peer *instance_id*.

    Does **not** strip a live install. If the team is still live here, records a
    pending stub and returns the live row unchanged (caller should treat as refuse).
    """
    team_id = str(team_id).strip()
    peer_instance_id = str(instance_id).strip()
    existing = DbManager.get_record(schemas.Workspace, id=team_id)
    name = (workspace_name or team_id)[:100]

    if existing and is_local_workspace(existing):
        convert_workspace_to_stub(team_id, peer_instance_id, source="ensure_stub", migration_notice=False)
        return existing

    if existing:
        result = convert_workspace_to_stub(team_id, peer_instance_id, source="ensure_stub", migration_notice=False)
        if result in ("converted", "already_stub"):
            updates: dict[Any, Any] = {}
            if workspace_name and existing.workspace_name != name:
                updates[schemas.Workspace.workspace_name] = name
            if updates:
                DbManager.update_record(schemas.Workspace, team_id, updates)
            return DbManager.get_record(schemas.Workspace, id=team_id)
        return existing

    stub = schemas.Workspace(
        team_id=team_id,
        workspace_name=name,
        instance_id=peer_instance_id,
        deleted_at=None,
    )
    created = DbManager.create_record(stub)
    _log_convert(
        "created",
        team_id,
        peer_instance_id,
        workspace_id=getattr(created, "id", None),
        source="ensure_stub",
    )
    return DbManager.get_record(schemas.Workspace, id=team_id)


def restore_peer_stubs(peer_instance_id: str, *, source: str = "pair") -> dict[str, str]:
    """Unpause soft-deleted stubs for *peer_instance_id* (reconnect within retention)."""
    peer_instance_id = str(peer_instance_id or "").strip()
    if not peer_instance_id:
        return {}
    rows = DbManager.find_records(
        schemas.Workspace,
        [
            schemas.Workspace.instance_id == peer_instance_id,
            schemas.Workspace.deleted_at.isnot(None),
        ],
    )
    results: dict[str, str] = {}
    for workspace in rows:
        team_id = str(getattr(workspace, "team_id", None) or "").strip()
        if not team_id:
            continue
        results[team_id] = convert_workspace_to_stub(
            team_id,
            peer_instance_id,
            source=source,
            migration_notice=False,
        )
        if results[team_id] not in {"converted", "already_stub"}:
            continue
        from helpers.leave_sync import group_name_for_sync_id
        from helpers.sync_lifecycle import deliver_action_notice

        restored = DbManager.get_record(schemas.Workspace, id=team_id)
        channels = DbManager.find_records(
            schemas.SyncChannel,
            [
                schemas.SyncChannel.workspace_id == workspace.id,
                schemas.SyncChannel.deleted_at.is_(None),
            ],
        )
        seen: set[int] = set()
        for row in channels:
            sync_id = getattr(row, "sync_id", None)
            if not sync_id or sync_id in seen:
                continue
            seen.add(sync_id)
            deliver_action_notice(
                restored,
                row,
                action="Resume",
                group_name=group_name_for_sync_id(sync_id),
                post_in_channel=False,
                dm=False,
            )
    return results
