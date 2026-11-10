"""Repair leftover rows the same way migration 022 does, and merge duplicate Channels.

Federation tables are never written. A Sync with no Group is skipped.
"""

import time
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func, or_, select

from db import DbManager, get_session, read_session, schemas, transaction
from helpers.sync_cleanup import purge_sync, purge_sync_channels
from logger import log_debug, log_error, log_info

_ID_BATCH = 500
_STATUS_RANK = {"active": 0, "paused": 1, "pending": 2}

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
    started = time.monotonic()
    syncs = DbManager.find_records(schemas.Sync, [schemas.Sync.id.isnot(None)])
    channels = DbManager.find_records(schemas.SyncChannel, [schemas.SyncChannel.id.isnot(None)])
    groups = DbManager.find_records(schemas.WorkspaceGroup, [schemas.WorkspaceGroup.id.isnot(None)])
    members = DbManager.find_records(schemas.WorkspaceGroupMember, [schemas.WorkspaceGroupMember.id.isnot(None)])
    workspaces = DbManager.find_records(schemas.Workspace, [schemas.Workspace.id.isnot(None)])

    sync_by_id = {row.id: row for row in syncs}
    group_ids = {row.id for row in groups}
    workspace_ids = {row.id for row in workspaces}

    missing_group = [row for row in syncs if row.group_id is not None and row.group_id not in group_ids]
    missing_group_ids = {row.id for row in missing_group}

    missing_sync = [row for row in channels if row.sync_id is not None and row.sync_id not in sync_by_id]
    missing_sync_ids = {row.id for row in missing_sync}
    in_live_sync = [row for row in channels if row.sync_id in sync_by_id and row.sync_id not in missing_group_ids]
    # Like migration 022: a gone Workspace removes the Channel even when its Sync
    # reference is blank. A blank Workspace reference is not a gone Workspace.
    missing_workspace = [
        row
        for row in channels
        if row.id not in missing_sync_ids
        and row.sync_id not in missing_group_ids
        and row.workspace_id is not None
        and row.workspace_id not in workspace_ids
    ]
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

    post_meta_missing_channel = _count_post_meta_missing_channel()
    workspace_rows = _count_workspace_rows()

    counts = {
        "sync_missing_group": len(missing_group),
        "channel_missing_sync": len(missing_sync),
        "channel_missing_workspace": len(missing_workspace),
        "channel_outside_group": len(outside),
        "duplicate_channel": sum(len(group) - 1 for group in duplicates),
        "post_meta_missing_channel": post_meta_missing_channel,
        "rows_missing_workspace": workspace_rows,
    }
    report = RepairReport(counts=counts)
    found = sum(counts.values())
    if not fix:
        log_info("data_repair_verified", found=found, duration_ms=_elapsed_ms(started), **counts)
        return report

    step = "syncs_missing_group"
    try:
        with transaction():
            for sync in missing_group:
                purge_sync(sync.id)
            step = "channels"
            purge_sync_channels(missing_sync + missing_workspace + outside)
            step = "duplicate_channels"
            for group in duplicates:
                _merge_duplicate_channels(group)
            step = "post_meta_missing_channel"
            _delete_matching(schemas.PostMeta, *_post_meta_missing_channel_filters())
            step = "rows_missing_workspace"
            _fix_workspace_rows()
            step = "commit"
    except Exception as exc:
        # No error text: a SQLAlchemy error carries the statement and up to 500
        # ids. The Data Tools handler logs the full trace.
        log_error(
            "data_repair_rolled_back",
            step=step,
            error_type=type(exc).__name__,
            duration_ms=_elapsed_ms(started),
        )
        raise
    log_info("data_repair_committed", fixed=found, duration_ms=_elapsed_ms(started), **counts)
    try:
        _invalidate_after_repair()
    except Exception:
        log_error("data_repair_invalidate_failed", exc_info=True)
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


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _keep_rank(channel) -> tuple:
    """Prefer the row that is in service, then the oldest."""
    if channel.deleted_at is not None:
        status_rank = 4
    else:
        status_rank = _STATUS_RANK.get(channel.status, 3)
    return (status_rank, channel.created_at or datetime.max, channel.id)


def _merge_duplicate_channels(group) -> None:
    """Keep the in-service row and move the others' message records onto it.

    ``active`` wins over ``paused``, then ``pending``, then any other row that
    has not been removed. The same rank keeps the oldest row.
    """
    kept_channel = min(group, key=_keep_rank)
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


def _workspace_ids():
    return select(schemas.Workspace.id)


def _group_ids():
    return select(schemas.WorkspaceGroup.id)


def _missing_workspace(column):
    """A filled-in reference whose Workspace row is gone. A blank value stays."""
    return column.isnot(None), column.notin_(_workspace_ids())


def _mapping_missing_workspace():
    source, target = schemas.UserMapping.source_workspace_id, schemas.UserMapping.target_workspace_id
    source_missing = source.isnot(None) & source.notin_(_workspace_ids())
    target_missing = target.isnot(None) & target.notin_(_workspace_ids())
    return (or_(source_missing, target_missing),)


def _member_missing_group():
    column = schemas.WorkspaceGroupMember.group_id
    workspace = schemas.WorkspaceGroupMember.workspace_id
    return (
        column.isnot(None),
        column.notin_(_group_ids()),
        or_(workspace.is_(None), workspace.in_(_workspace_ids())),
    )


def _mapping_missing_group():
    column = schemas.UserMapping.group_id
    return (column.isnot(None), column.notin_(_group_ids()), ~_mapping_missing_workspace()[0])


def _inviter_missing_filters():
    """A bad inviter on a member this repair is not already deleting."""
    workspace = schemas.WorkspaceGroupMember.workspace_id
    return (
        *_missing_workspace(schemas.WorkspaceGroupMember.invited_by_workspace_id),
        or_(workspace.is_(None), workspace.in_(_workspace_ids())),
    )


def _post_meta_missing_channel_filters():
    column = schemas.PostMeta.sync_channel_id
    return (column.isnot(None), column.notin_(select(schemas.SyncChannel.id)))


def _count_where(model, *filters) -> int:
    with read_session() as session:
        return session.query(func.count()).select_from(model).filter(*filters).scalar() or 0


def _count_post_meta_missing_channel() -> int:
    return _count_where(schemas.PostMeta, *_post_meta_missing_channel_filters())


def _count_workspace_rows() -> int:
    return (
        _count_where(schemas.WorkspaceSetting, *_missing_workspace(schemas.WorkspaceSetting.workspace_id))
        + _count_where(schemas.UserDirectory, *_missing_workspace(schemas.UserDirectory.workspace_id))
        + _count_where(schemas.UserMapping, *_mapping_missing_workspace())
        + _count_where(schemas.WorkspaceGroupMember, *_missing_workspace(schemas.WorkspaceGroupMember.workspace_id))
        + _count_where(schemas.PostMeta, *_missing_workspace(schemas.PostMeta.source_workspace_id))
        + _count_where(schemas.WorkspaceGroupMember, *_inviter_missing_filters())
        + _count_where(schemas.UserMapping, *_mapping_missing_group())
        + _count_where(schemas.WorkspaceGroupMember, *_member_missing_group())
    )


def _id_page(model, *filters) -> list[int]:
    session = get_session()
    rows = session.query(model.id).filter(*filters).limit(_ID_BATCH).all()
    return [row[0] for row in rows]


def _delete_matching(model, *filters) -> None:
    """Delete matching rows 500 at a time so one statement stays under the driver limit."""
    session = get_session()
    total = 0
    while True:
        ids = _id_page(model, *filters)
        if not ids:
            break
        session.query(model).filter(model.id.in_(ids)).delete(synchronize_session=False)
        session.flush()
        total += len(ids)
    if total:
        log_debug("data_repair_rows_deleted", table=model.__tablename__, rows=total)


def _null_matching(model, column, *filters) -> None:
    session = get_session()
    total = 0
    while True:
        ids = _id_page(model, *filters)
        if not ids:
            break
        session.query(model).filter(model.id.in_(ids)).update({column: None}, synchronize_session=False)
        session.flush()
        total += len(ids)
    if total:
        log_debug("data_repair_rows_cleared", table=model.__tablename__, column=column.key, rows=total)


def _fix_workspace_rows() -> None:
    session = get_session()
    session.query(schemas.WorkspaceSetting).filter(*_missing_workspace(schemas.WorkspaceSetting.workspace_id)).delete(
        synchronize_session=False
    )
    session.flush()
    _delete_matching(schemas.UserDirectory, *_missing_workspace(schemas.UserDirectory.workspace_id))
    _delete_matching(schemas.UserMapping, *_mapping_missing_workspace())
    _delete_matching(schemas.WorkspaceGroupMember, *_missing_workspace(schemas.WorkspaceGroupMember.workspace_id))
    _null_matching(
        schemas.PostMeta,
        schemas.PostMeta.source_workspace_id,
        *_missing_workspace(schemas.PostMeta.source_workspace_id),
    )
    _null_matching(
        schemas.WorkspaceGroupMember,
        schemas.WorkspaceGroupMember.invited_by_workspace_id,
        *_inviter_missing_filters(),
    )
    _null_matching(schemas.UserMapping, schemas.UserMapping.group_id, *_mapping_missing_group())
    _delete_matching(schemas.WorkspaceGroupMember, *_member_missing_group())


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
