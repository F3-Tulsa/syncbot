"""Outbound federation membership / sync replication.

Pushes group and sync channel state to peer instances so stubs can receive
Subscribe/Create Sync without sharing integer PKs. Keys are ``uid``,
Slack ``team_id``, and Slack ``channel_id``.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import Callable, Iterable

from db import DbManager, schemas
from federation import core as federation
from helpers.workspace import get_workspace_by_id
from helpers.workspace_kind import is_stub_workspace, peer_for_workspace, workspace_allowed_for_peer
from logger import log_debug, log_error, log_warning


def _normalize_group_role(role: str | None) -> str:
    if role == "creator":
        return "owner"
    if role == "owner":
        return "owner"
    return "member"


def mint_uid() -> str:
    return str(uuid.uuid4())


def ensure_group_uid(group: schemas.WorkspaceGroup) -> str:
    """Return a durable uid for *group*, minting if needed."""
    uid = getattr(group, "uid", None)
    if uid:
        return str(uid)
    uid = mint_uid()
    DbManager.update_records(
        schemas.WorkspaceGroup,
        [schemas.WorkspaceGroup.id == group.id],
        {schemas.WorkspaceGroup.uid: uid},
    )
    group.uid = uid
    return uid


def _sync_channel_payload(
    sync_uid: str,
    workspace: schemas.Workspace,
    sync_channel: schemas.SyncChannel,
    *,
    notice: str | None = None,
) -> dict:
    """Fields for a SyncChannel, including a display name when known."""
    from helpers.workspace import lookup_channel_name, remember_channel_name

    name = (getattr(sync_channel, "channel_name", None) or "").strip()
    if not name or name == sync_channel.channel_id:
        looked = lookup_channel_name(sync_channel.channel_id, workspace)
        if looked and looked != sync_channel.channel_id:
            remember_channel_name(sync_channel.channel_id, getattr(workspace, "id", None), looked)
            name = looked
        else:
            name = ""
    payload = {
        "sync_uid": sync_uid,
        "team_id": workspace.team_id,
        "channel_id": sync_channel.channel_id,
        "status": sync_channel.status or "active",
        "publishes": bool(getattr(sync_channel, "publishes", True)),
        "subscribes": bool(getattr(sync_channel, "subscribes", True)),
        "reaction_style": getattr(sync_channel, "reaction_style", None),
    }
    if getattr(sync_channel, "is_private", None) is not None:
        payload["is_private"] = bool(sync_channel.is_private)
    if name:
        payload["channel_name"] = name[:100]
    text = (notice or "").strip()
    if text:
        payload["notice"] = text
    return payload


def ensure_sync_uid(sync: schemas.Sync) -> str:
    uid = getattr(sync, "uid", None)
    if uid:
        return str(uid)
    uid = mint_uid()
    DbManager.update_records(
        schemas.Sync,
        [schemas.Sync.id == sync.id],
        {schemas.Sync.uid: uid},
    )
    sync.uid = uid
    return uid


def peers_for_group(group_id: int) -> list[schemas.Instance]:
    """Instance rows reachable via stub members of *group_id*."""
    members = DbManager.find_records(
        schemas.WorkspaceGroupMember,
        [
            schemas.WorkspaceGroupMember.group_id == group_id,
            schemas.WorkspaceGroupMember.status == "active",
            schemas.WorkspaceGroupMember.deleted_at.is_(None),
            schemas.WorkspaceGroupMember.workspace_id.isnot(None),
        ],
    )
    peers: dict[str, schemas.Instance] = {}
    for member in members:
        ws = get_workspace_by_id(member.workspace_id) if member.workspace_id else None
        if not ws or not is_stub_workspace(ws):
            continue
        fed = peer_for_workspace(ws)
        if fed and fed.instance_id not in peers:
            peers[fed.instance_id] = fed
    return list(peers.values())


def _push_all(peers: Iterable[schemas.Instance], push_fn, payload: dict, *, label: str) -> None:
    for fed_ws in peers:
        try:
            push_fn(fed_ws, payload)
        except Exception:
            log_warning(
                "federation_replicate_failed",
                label=label,
                peer=getattr(fed_ws, "instance_id", None),
            )


def _with_notice(payload: dict, notice: str | None) -> dict:
    text = (notice or "").strip()
    if text:
        payload["notice"] = text
    return payload


def replicate_group_upsert(group: schemas.WorkspaceGroup, *, notice: str | None = None) -> None:
    peers = peers_for_group(group.id)
    if not peers:
        return
    uid = ensure_group_uid(group)
    _push_all(
        peers,
        federation.push_group_upsert,
        _with_notice(
            {"uid": uid, "name": group.name, "status": getattr(group, "status", None) or "active"},
            notice,
        ),
        label="group_upsert",
    )


def replicate_group_invite(
    group: schemas.WorkspaceGroup,
    workspace: schemas.Workspace,
    *,
    peer: schemas.Instance | None = None,
    set_role: bool = False,
    notice: str | None = None,
    announce: bool = False,
    invited_by: schemas.Workspace | None = None,
) -> None:
    """Tell peers (or one *peer*) that *workspace* joined *group*.

    *invited_by* marks a new invite and names the Workspace that sent it. A snapshot never sets it.
    """
    uid = ensure_group_uid(group)
    role = "member"
    if getattr(workspace, "id", None) and group.id:
        memberships = DbManager.find_records(
            schemas.WorkspaceGroupMember,
            [
                schemas.WorkspaceGroupMember.group_id == group.id,
                schemas.WorkspaceGroupMember.workspace_id == workspace.id,
                schemas.WorkspaceGroupMember.deleted_at.is_(None),
            ],
        )
        if memberships:
            role = _normalize_group_role(memberships[0].role)
    payload = {
        "uid": uid,
        "team_id": workspace.team_id,
        "workspace_name": workspace.workspace_name,
        "role": role,
    }
    if set_role:
        payload["set_role"] = True
    if announce:
        payload["announce"] = True
    if invited_by is not None:
        payload["invited_by_team_id"] = invited_by.team_id
    _with_notice(payload, notice)
    targets = [peer] if peer is not None else peers_for_group(group.id)
    if peer is None and is_stub_workspace(workspace):
        fed = peer_for_workspace(workspace)
        if fed and all(t.instance_id != fed.instance_id for t in targets):
            targets = list(targets) + [fed]
    targets = [fed_ws for fed_ws in targets if workspace_allowed_for_peer(workspace, fed_ws)]
    if not targets:
        return
    # Ensure the peer has the group row before the invite.
    for fed_ws in targets:
        try:
            federation.push_group_upsert(fed_ws, {"uid": uid, "name": group.name})
            federation.push_group_invite(fed_ws, payload)
        except Exception:
            log_warning(
                "federation_replicate_failed",
                label="group_invite",
                peer=getattr(fed_ws, "instance_id", None),
            )


def replicate_group_member_remove(
    group: schemas.WorkspaceGroup,
    workspace: schemas.Workspace,
    *,
    notice: str | None = None,
) -> None:
    """Tell peers to drop this workspace's membership and mappings. Not a leave."""
    peers = peers_for_group(group.id)
    if is_stub_workspace(workspace):
        fed = peer_for_workspace(workspace)
        if fed and all(p.instance_id != fed.instance_id for p in peers):
            peers = list(peers) + [fed]
    if not peers:
        return
    uid = ensure_group_uid(group)
    _push_all(
        peers,
        federation.push_group_member_remove,
        _with_notice({"uid": uid, "team_id": workspace.team_id}, notice),
        label="group_member_remove",
    )
    invalidate_snapshot_hash()


def replicate_group_leave(
    group: schemas.WorkspaceGroup,
    workspace: schemas.Workspace,
    *,
    notice: str | None = None,
) -> None:
    peers = peers_for_group(group.id)
    if is_stub_workspace(workspace):
        fed = peer_for_workspace(workspace)
        if fed and all(p.instance_id != fed.instance_id for p in peers):
            peers = list(peers) + [fed]
    if not peers:
        return
    uid = ensure_group_uid(group)
    _push_all(
        peers,
        federation.push_group_leave,
        _with_notice({"uid": uid, "team_id": workspace.team_id}, notice),
        label="group_leave",
    )


def replicate_sync_upsert(sync: schemas.Sync, group: schemas.WorkspaceGroup) -> None:
    peers = peers_for_group(group.id)
    if not peers:
        return
    group_uid = ensure_group_uid(group)
    sync_uid = ensure_sync_uid(sync)
    for fed_ws in peers:
        try:
            federation.push_group_upsert(fed_ws, {"uid": group_uid, "name": group.name})
            federation.push_sync_upsert(
                fed_ws,
                {
                    "uid": sync_uid,
                    "group_uid": group_uid,
                    "title": sync.title,
                    "description": sync.description,
                    "sync_mode": sync.sync_mode,
                },
            )
        except Exception:
            log_warning(
                "federation_replicate_failed",
                label="sync_upsert",
                peer=getattr(fed_ws, "instance_id", None),
            )


def _notice_reaches_peer(workspace, peer, notice: str | None) -> bool:
    """Whether this Channel upsert may go to *peer*.

    A live row uses the allowlist. A row that was just uninstalled or removed
    from a connection is no longer local or a stub, and a farewell sentence
    still has to reach the peer.
    """
    if workspace_allowed_for_peer(workspace, peer):
        return True
    if not (notice or "").strip() or workspace is None or peer is None:
        return False
    if getattr(workspace, "deleted_at", None) is None:
        return False
    instance_id = getattr(workspace, "instance_id", None)
    peer_id = getattr(peer, "instance_id", None)
    if not instance_id or not peer_id:
        return False
    if instance_id == peer_id:
        return True
    from federation.core import get_instance_id

    if instance_id != get_instance_id():
        return False
    workspace_id = getattr(workspace, "id", None)
    if not workspace_id:
        return False
    allowlist = DbManager.find_records(
        schemas.FederationWorkspaceAllowlist,
        [schemas.FederationWorkspaceAllowlist.instance_id == peer_id],
    )
    if not allowlist:
        return True
    return workspace_id in {row.workspace_id for row in allowlist if row.workspace_id}


def replicate_sync_channel_upsert(
    sync: schemas.Sync,
    group: schemas.WorkspaceGroup,
    sync_channel: schemas.SyncChannel,
    workspace: schemas.Workspace,
    *,
    notice: str | None = None,
) -> None:
    peers = peers_for_group(group.id)
    if not peers:
        return
    group_uid = ensure_group_uid(group)
    sync_uid = ensure_sync_uid(sync)
    payload = _sync_channel_payload(sync_uid, workspace, sync_channel, notice=notice)
    for fed_ws in peers:
        if not _notice_reaches_peer(workspace, fed_ws, notice):
            continue
        try:
            federation.push_group_upsert(fed_ws, {"uid": group_uid, "name": group.name})
            federation.push_sync_upsert(
                fed_ws,
                {
                    "uid": sync_uid,
                    "group_uid": group_uid,
                    "title": sync.title,
                    "description": sync.description,
                    "sync_mode": sync.sync_mode,
                },
            )
            federation.push_sync_channel_upsert(fed_ws, payload)
            invalidate_snapshot_hash(fed_ws.instance_id)
        except Exception:
            log_warning(
                "federation_replicate_failed",
                label="sync_channel_upsert",
                peer=getattr(fed_ws, "instance_id", None),
            )


FORWARDED_REMOVE_REASONS = frozenset({"group_disbanded", "removed_from_group"})
"""The only ``reason`` values a sync_channel_remove carries; a peer ends its own local row for these."""


def replicate_sync_channel_remove(
    sync: schemas.Sync,
    group: schemas.WorkspaceGroup,
    *,
    team_id: str,
    channel_id: str,
    reason: str | None = None,
    notice: str | None = None,
    purge: bool = False,
) -> None:
    peers = peers_for_group(group.id)
    if not peers:
        return
    uid = getattr(sync, "uid", None) or ensure_sync_uid(sync)
    payload = {
        "sync_uid": str(uid),
        "team_id": team_id,
        "channel_id": channel_id,
    }
    if reason in FORWARDED_REMOVE_REASONS:
        payload["reason"] = reason
    if purge:
        payload["purge"] = True
    text = (notice or "").strip()
    if text:
        payload["notice"] = text
    _push_all(peers, federation.push_sync_channel_remove, payload, label="sync_channel_remove")


def replicate_sync_ended(
    sync: schemas.Sync,
    group: schemas.WorkspaceGroup,
    channels: list[tuple[schemas.SyncChannel, schemas.Workspace | None]],
    *,
    reason: str | None = None,
    notice: str | None = None,
    purge: bool = False,
) -> None:
    """Tell peers that these rows of *sync* ended here, with the reason they should apply."""
    if not getattr(sync, "uid", None):
        ensure_sync_uid(sync)
    for sync_channel, workspace in channels:
        team_id = getattr(workspace, "team_id", None) if workspace is not None else None
        if not team_id:
            continue
        replicate_sync_channel_remove(
            sync,
            group,
            team_id=team_id,
            channel_id=sync_channel.channel_id,
            reason=reason,
            notice=notice,
            purge=purge,
        )


def _group_ids_for_workspace(workspace_id: int | None) -> set[int]:
    if not workspace_id:
        return set()
    memberships = DbManager.find_records(
        schemas.WorkspaceGroupMember,
        [
            schemas.WorkspaceGroupMember.workspace_id == workspace_id,
            schemas.WorkspaceGroupMember.status == "active",
            schemas.WorkspaceGroupMember.deleted_at.is_(None),
        ],
    )
    return {row.group_id for row in memberships if getattr(row, "group_id", None)}


_SNAPSHOT_SLICE_SECONDS = 1.0
_snapshot_state: dict[str, dict] = {}


def invalidate_snapshot_hash(peer_instance_id: str | None = None) -> None:
    """Forget a finished snapshot so the next keep-warm sends again."""
    if peer_instance_id:
        state = _snapshot_state.get(peer_instance_id)
        if state is not None:
            state["hash"] = None
            state["cursor"] = 0
        return
    for state in _snapshot_state.values():
        state["hash"] = None
        state["cursor"] = 0


def _snapshot_calls(peer: schemas.Instance) -> list[tuple[str, dict, Callable[[], None]]]:
    """Ordered upserts for *peer*. Each item is ``(label, body, call)``."""
    calls: list[tuple[str, dict, Callable[[], None]]] = []
    group_ids: set[int] = set()
    stubs = DbManager.find_records(
        schemas.Workspace,
        [
            schemas.Workspace.instance_id == peer.instance_id,
            schemas.Workspace.deleted_at.is_(None),
        ],
    )
    for stub in stubs:
        group_ids.update(_group_ids_for_workspace(stub.id))
    allow = DbManager.find_records(
        schemas.FederationWorkspaceAllowlist,
        [schemas.FederationWorkspaceAllowlist.instance_id == peer.instance_id],
    )
    for row in allow:
        group_ids.update(_group_ids_for_workspace(row.workspace_id))
    for group_id in sorted(group_ids):
        group = DbManager.get_record(schemas.WorkspaceGroup, id=group_id)
        if not group or getattr(group, "status", None) not in (None, "active"):
            continue
        uid = ensure_group_uid(group)
        group_body = {"uid": uid, "name": group.name}
        calls.append(("group", group_body, lambda body=group_body: federation.push_group_upsert(peer, body)))
        members = DbManager.find_records(
            schemas.WorkspaceGroupMember,
            [
                schemas.WorkspaceGroupMember.group_id == group.id,
                schemas.WorkspaceGroupMember.status == "active",
                schemas.WorkspaceGroupMember.deleted_at.is_(None),
            ],
        )
        for membership in sorted(members, key=lambda row: row.id or 0):
            workspace = get_workspace_by_id(membership.workspace_id) if membership.workspace_id else None
            if not workspace or not getattr(workspace, "team_id", None):
                continue
            if not workspace_allowed_for_peer(workspace, peer):
                continue
            invite = {
                "uid": uid,
                "team_id": workspace.team_id,
                "workspace_name": workspace.workspace_name,
                "role": _normalize_group_role(membership.role),
            }
            calls.append(("invite", invite, lambda body=invite: federation.push_group_invite(peer, body)))
        syncs = DbManager.find_records(schemas.Sync, [schemas.Sync.group_id == group.id])
        for sync in sorted(syncs, key=lambda row: row.id or 0):
            sync_uid = ensure_sync_uid(sync)
            sync_body = {
                "uid": sync_uid,
                "group_uid": uid,
                "title": sync.title,
                "description": sync.description,
                "sync_mode": sync.sync_mode,
            }
            calls.append(("sync", sync_body, lambda body=sync_body: federation.push_sync_upsert(peer, body)))
            channels = DbManager.find_records(
                schemas.SyncChannel,
                [
                    schemas.SyncChannel.sync_id == sync.id,
                    schemas.SyncChannel.deleted_at.is_(None),
                ],
            )
            for sync_channel in sorted(channels, key=lambda row: row.id or 0):
                workspace = get_workspace_by_id(sync_channel.workspace_id) if sync_channel.workspace_id else None
                if not workspace or not getattr(workspace, "team_id", None):
                    continue
                if not workspace_allowed_for_peer(workspace, peer):
                    continue
                channel_body = _sync_channel_payload(sync_uid, workspace, sync_channel)
                calls.append(
                    (
                        "channel",
                        channel_body,
                        lambda body=channel_body: federation.push_sync_channel_upsert(peer, body),
                    )
                )
    return calls


def _snapshot_hash(calls: list[tuple[str, dict, Callable[[], None]]]) -> str:
    material = [[label, body] for label, body, _call in calls]
    return hashlib.sha256(json.dumps(material, sort_keys=True, default=str).encode()).hexdigest()


def replicate_peer_snapshot(peer: schemas.Instance) -> None:
    """Push one short slice of this instance's groups and Sync Channels to *peer*.

    Only allowlisted local Workspaces and this peer's own stubs are invited.
    A finished pass is skipped until a row changes. Never raises.
    """
    if peer is None or not getattr(peer, "instance_id", None):
        return
    try:
        calls = _snapshot_calls(peer)
        digest = _snapshot_hash(calls)
        state = _snapshot_state.setdefault(peer.instance_id, {"cursor": 0, "hash": None, "digest": None})
        if state.get("digest") != digest:
            state["cursor"] = 0
            state["digest"] = digest
            state["hash"] = None
        if state.get("hash") == digest:
            log_debug("federation_snapshot", peer_instance_id=peer.instance_id, groups=0, skipped=True)
            return
        start = time.monotonic()
        index = int(state.get("cursor") or 0)
        sent = 0
        while index < len(calls) and (time.monotonic() - start) < _SNAPSHOT_SLICE_SECONDS:
            _label, _body, call = calls[index]
            try:
                call()
            except Exception:
                log_warning("federation_snapshot", peer_instance_id=peer.instance_id, ok=False, index=index)
            index += 1
            sent += 1
            if sent == 1 and (time.monotonic() - start) >= _SNAPSHOT_SLICE_SECONDS:
                break
        if index >= len(calls):
            state["cursor"] = 0
            state["hash"] = digest
        else:
            state["cursor"] = index
        log_debug(
            "federation_snapshot",
            peer_instance_id=peer.instance_id,
            sent=sent,
            remaining=max(len(calls) - index, 0),
        )
    except Exception:
        log_error("federation_snapshot", peer_instance_id=peer.instance_id, ok=False)
