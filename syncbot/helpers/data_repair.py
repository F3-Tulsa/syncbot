"""Repair leftover rows the same way migration 022 does, and merge duplicate Channels.

Federation tables are never written. A Sync with no Group is skipped.
"""

from dataclasses import dataclass, field
from datetime import datetime

from db import DbManager, schemas
from helpers.sync_cleanup import purge_sync, purge_sync_channels
from logger import log_info

REPAIR_CHECKS: tuple[tuple[str, str], ...] = (
    ("sync_missing_group", "Syncs whose Group is gone"),
    ("channel_missing_sync", "Channels in a Sync that is gone"),
    ("channel_missing_workspace", "Channels from a Workspace that is gone"),
    ("channel_outside_group", "Channels from a Workspace outside the Group"),
    ("duplicate_channel", "Duplicate Channels in one Sync"),
    ("post_meta_missing_channel", "Message records with no Channel"),
    ("rows_missing_workspace", "Rows for a Workspace that is gone"),
)


@dataclass
class RepairReport:
    """Ordered counts from one repair pass. Zero means that check found nothing."""

    counts: dict[str, int] = field(default_factory=dict)

    def count(self, key: str) -> int:
        return self.counts.get(key, 0)


def run_repair(*, fix: bool) -> RepairReport:
    """Count leftover rows, and delete them when *fix* is set."""
    syncs = DbManager.find_records(schemas.Sync, [schemas.Sync.id.isnot(None)])
    channels = DbManager.find_records(schemas.SyncChannel, [schemas.SyncChannel.id.isnot(None)])
    workspaces = DbManager.find_records(schemas.Workspace, [schemas.Workspace.id.isnot(None)])
    groups = DbManager.find_records(schemas.WorkspaceGroup, [schemas.WorkspaceGroup.id.isnot(None)])
    members = DbManager.find_records(schemas.WorkspaceGroupMember, [schemas.WorkspaceGroupMember.id.isnot(None)])
    post_meta_rows = DbManager.find_records(schemas.PostMeta, [schemas.PostMeta.id.isnot(None)])

    sync_by_id = {row.id: row for row in syncs}
    workspace_ids = {row.id for row in workspaces}
    group_ids = {row.id for row in groups}
    channel_ids = {row.id for row in channels}

    missing_group = [row for row in syncs if row.group_id is not None and row.group_id not in group_ids]
    missing_group_ids = {row.id for row in missing_group}

    missing_sync = [row for row in channels if row.sync_id not in sync_by_id]
    in_live_sync = [row for row in channels if row.sync_id in sync_by_id and row.sync_id not in missing_group_ids]
    missing_workspace = [row for row in in_live_sync if row.workspace_id not in workspace_ids]
    missing_workspace_channel_ids = {row.id for row in missing_workspace}
    outside = [
        row
        for row in in_live_sync
        if row.id not in missing_workspace_channel_ids
        and _is_channel_outside_group(row, sync_by_id.get(row.sync_id), members)
    ]
    removed_ids = {row.id for row in missing_sync} | missing_workspace_channel_ids | {row.id for row in outside}
    remaining = [row for row in channels if row.id not in removed_ids and row.sync_id not in missing_group_ids]
    duplicates = _get_duplicate_channels(remaining)

    post_meta_missing_channel = [row for row in post_meta_rows if row.sync_channel_id not in channel_ids]
    workspace_rows = _count_workspace_rows(workspace_ids, group_ids)

    counts = {
        "sync_missing_group": len(missing_group),
        "channel_missing_sync": len(missing_sync),
        "channel_missing_workspace": len(missing_workspace),
        "channel_outside_group": len(outside),
        "duplicate_channel": sum(len(group) - 1 for group in duplicates),
        "post_meta_missing_channel": len(post_meta_missing_channel),
        "rows_missing_workspace": workspace_rows,
    }
    report = RepairReport(counts=counts)
    log_info("data_repair", fix=fix, **counts)
    if not fix:
        return report

    for sync in missing_group:
        purge_sync(sync.id)
    purge_sync_channels(missing_sync + missing_workspace + outside)
    for group in duplicates:
        _merge_duplicate_channels(group)
    if post_meta_missing_channel:
        DbManager.delete_records(
            schemas.PostMeta, [schemas.PostMeta.id.in_([row.id for row in post_meta_missing_channel])]
        )
    _fix_workspace_rows(workspace_ids, group_ids)
    _invalidate_after_repair()
    return report


def _is_channel_outside_group(channel, sync, members) -> bool:
    """True when the channel's Sync has a Group and this Workspace is not an active member."""
    if sync is None or getattr(sync, "group_id", None) is None:
        return False
    matches = [
        member
        for member in members
        if member.group_id == sync.group_id
        and member.workspace_id == channel.workspace_id
        and member.status == "active"
    ]
    if not matches:
        return True
    return channel.deleted_at is None and all(member.deleted_at is not None for member in matches)


def _get_duplicate_channels(channels) -> list[list]:
    """One list per set of Channels that share a Sync, Workspace, and Channel id."""
    grouped: dict[tuple, list] = {}
    for channel in channels:
        key = (channel.sync_id, channel.workspace_id, channel.channel_id)
        grouped.setdefault(key, []).append(channel)
    return [rows for rows in grouped.values() if len(rows) > 1]


def _merge_duplicate_channels(group) -> None:
    """Keep the oldest live row and move the others' message records onto it."""
    live = [channel for channel in group if channel.deleted_at is None]
    candidates = live or list(group)
    kept_channel = min(candidates, key=lambda channel: (channel.created_at or datetime.max, channel.id))
    others = [channel for channel in group if channel.id != kept_channel.id]
    kept = {
        (row.post_id, row.ts)
        for row in DbManager.find_records(schemas.PostMeta, [schemas.PostMeta.sync_channel_id == kept_channel.id])
    }
    for other in others:
        metas = DbManager.find_records(schemas.PostMeta, [schemas.PostMeta.sync_channel_id == other.id])
        for meta in metas:
            key = (meta.post_id, meta.ts)
            if key in kept:
                continue
            DbManager.update_records(
                schemas.PostMeta,
                [schemas.PostMeta.id == meta.id],
                {schemas.PostMeta.sync_channel_id: kept_channel.id},
            )
            kept.add(key)
    purge_sync_channels(others)


def _count_workspace_rows(workspace_ids: set[int], group_ids: set[int]) -> int:
    return (
        len(_get_settings_missing_workspace(workspace_ids))
        + len(_get_directory_missing_workspace(workspace_ids))
        + len(_get_mappings_missing_workspace(workspace_ids))
        + len(_get_members_missing_workspace(workspace_ids))
        + len(_get_post_meta_missing_source(workspace_ids))
        + len(_get_members_missing_inviter(workspace_ids))
        + len(_get_mappings_missing_group(group_ids, workspace_ids))
        + len(_get_members_missing_group(group_ids, workspace_ids))
    )


def _get_settings_missing_workspace(workspace_ids: set[int]):
    rows = DbManager.find_records(schemas.WorkspaceSetting, [schemas.WorkspaceSetting.workspace_id.isnot(None)])
    return [row for row in rows if row.workspace_id not in workspace_ids]


def _get_directory_missing_workspace(workspace_ids: set[int]):
    rows = DbManager.find_records(schemas.UserDirectory, [schemas.UserDirectory.id.isnot(None)])
    return [row for row in rows if row.workspace_id not in workspace_ids]


def _get_mappings_missing_workspace(workspace_ids: set[int]):
    rows = DbManager.find_records(schemas.UserMapping, [schemas.UserMapping.id.isnot(None)])
    return [
        row
        for row in rows
        if row.source_workspace_id not in workspace_ids or row.target_workspace_id not in workspace_ids
    ]


def _get_members_missing_workspace(workspace_ids: set[int]):
    rows = DbManager.find_records(schemas.WorkspaceGroupMember, [schemas.WorkspaceGroupMember.id.isnot(None)])
    return [row for row in rows if row.workspace_id not in workspace_ids]


def _get_post_meta_missing_source(workspace_ids: set[int]):
    rows = DbManager.find_records(schemas.PostMeta, [schemas.PostMeta.source_workspace_id.isnot(None)])
    return [row for row in rows if row.source_workspace_id not in workspace_ids]


def _get_members_missing_inviter(workspace_ids: set[int]):
    rows = DbManager.find_records(
        schemas.WorkspaceGroupMember, [schemas.WorkspaceGroupMember.invited_by_workspace_id.isnot(None)]
    )
    removed_ids = {row.id for row in _get_members_missing_workspace(workspace_ids)}
    return [row for row in rows if row.id not in removed_ids and row.invited_by_workspace_id not in workspace_ids]


def _get_mappings_missing_group(group_ids: set[int], workspace_ids: set[int]):
    rows = DbManager.find_records(schemas.UserMapping, [schemas.UserMapping.group_id.isnot(None)])
    removed_ids = {row.id for row in _get_mappings_missing_workspace(workspace_ids)}
    return [row for row in rows if row.id not in removed_ids and row.group_id not in group_ids]


def _get_members_missing_group(group_ids: set[int], workspace_ids: set[int]):
    rows = DbManager.find_records(schemas.WorkspaceGroupMember, [schemas.WorkspaceGroupMember.id.isnot(None)])
    removed_ids = {row.id for row in _get_members_missing_workspace(workspace_ids)}
    return [row for row in rows if row.id not in removed_ids and row.group_id not in group_ids]


def _fix_workspace_rows(workspace_ids: set[int], group_ids: set[int]) -> None:
    setting_ids = sorted({row.workspace_id for row in _get_settings_missing_workspace(workspace_ids)})
    if setting_ids:
        DbManager.delete_records(schemas.WorkspaceSetting, [schemas.WorkspaceSetting.workspace_id.in_(setting_ids)])
    directory_ids = [row.id for row in _get_directory_missing_workspace(workspace_ids)]
    if directory_ids:
        DbManager.delete_records(schemas.UserDirectory, [schemas.UserDirectory.id.in_(directory_ids)])
    mapping_ids = [row.id for row in _get_mappings_missing_workspace(workspace_ids)]
    if mapping_ids:
        DbManager.delete_records(schemas.UserMapping, [schemas.UserMapping.id.in_(mapping_ids)])
    member_ids = [row.id for row in _get_members_missing_workspace(workspace_ids)]
    if member_ids:
        DbManager.delete_records(schemas.WorkspaceGroupMember, [schemas.WorkspaceGroupMember.id.in_(member_ids)])
    post_ids = [row.id for row in _get_post_meta_missing_source(workspace_ids)]
    if post_ids:
        DbManager.update_records(
            schemas.PostMeta,
            [schemas.PostMeta.id.in_(post_ids)],
            {schemas.PostMeta.source_workspace_id: None},
        )
    inviter_ids = [row.id for row in _get_members_missing_inviter(workspace_ids)]
    if inviter_ids:
        DbManager.update_records(
            schemas.WorkspaceGroupMember,
            [schemas.WorkspaceGroupMember.id.in_(inviter_ids)],
            {schemas.WorkspaceGroupMember.invited_by_workspace_id: None},
        )
    group_mapping_ids = [row.id for row in _get_mappings_missing_group(group_ids, workspace_ids)]
    if group_mapping_ids:
        DbManager.update_records(
            schemas.UserMapping,
            [schemas.UserMapping.id.in_(group_mapping_ids)],
            {schemas.UserMapping.group_id: None},
        )
    group_member_ids = [row.id for row in _get_members_missing_group(group_ids, workspace_ids)]
    if group_member_ids:
        DbManager.delete_records(schemas.WorkspaceGroupMember, [schemas.WorkspaceGroupMember.id.in_(group_member_ids)])


def _invalidate_after_repair() -> None:
    from helpers.export_import import invalidate_home_tab_caches_for_team
    from helpers.workspace_kind import is_local_workspace

    workspaces = DbManager.find_records(
        schemas.Workspace,
        [schemas.Workspace.deleted_at.is_(None)],
    )
    for workspace in workspaces:
        if workspace.team_id and is_local_workspace(workspace):
            invalidate_home_tab_caches_for_team(workspace.team_id)
