"""Remove leftover rows and stop SQLite from reusing deleted ids.

Revision ID: 022_data_consistency
Revises: 021_sync_channel_privacy
Create Date: 2026-11-07

A channel belongs to its Sync's Group when an active membership exists for
that workspace. A live channel also needs that membership's ``deleted_at``
unset. A soft-deleted channel may keep a membership whose ``deleted_at`` is
set. Syncs with no group are left alone. Federation tables are not written.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "022_data_consistency"
down_revision: str | None = "021_sync_channel_privacy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BELONGS = """
EXISTS (
    SELECT 1 FROM workspace_group_members m
    JOIN syncs s ON s.id = sc.sync_id
    WHERE m.group_id = s.group_id
      AND m.workspace_id = sc.workspace_id
      AND m.status = 'active'
      AND (sc.deleted_at IS NOT NULL OR m.deleted_at IS NULL)
)
"""


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "syncs" in tables and "workspace_groups" in tables:
        sync_ids = [
            row[0]
            for row in bind.execute(
                sa.text(
                    """
                    SELECT id FROM syncs
                    WHERE group_id IS NOT NULL
                      AND group_id NOT IN (SELECT id FROM workspace_groups)
                    """
                )
            )
        ]
        if sync_ids and "sync_channels" in tables:
            _delete_channels_for_syncs(bind, tables, sync_ids)
            bind.execute(
                sa.text("DELETE FROM syncs WHERE id IN :ids").bindparams(sa.bindparam("ids", expanding=True)),
                {"ids": sync_ids},
            )

    if "sync_channels" in tables:
        channel_ids = [
            row[0]
            for row in bind.execute(
                sa.text(
                    f"""
                    SELECT sc.id FROM sync_channels sc
                    LEFT JOIN syncs s ON s.id = sc.sync_id
                    WHERE sc.sync_id NOT IN (SELECT id FROM syncs)
                       OR sc.workspace_id NOT IN (SELECT id FROM workspaces)
                       OR (
                            s.group_id IS NOT NULL
                            AND NOT ({_BELONGS})
                          )
                    """
                )
            )
        ]
        if channel_ids:
            if "post_meta" in tables:
                bind.execute(
                    sa.text("DELETE FROM post_meta WHERE sync_channel_id IN :ids").bindparams(
                        sa.bindparam("ids", expanding=True)
                    ),
                    {"ids": channel_ids},
                )
            bind.execute(
                sa.text("DELETE FROM sync_channels WHERE id IN :ids").bindparams(sa.bindparam("ids", expanding=True)),
                {"ids": channel_ids},
            )

    if "post_meta" in tables and "sync_channels" in tables:
        bind.execute(
            sa.text(
                """
                DELETE FROM post_meta
                WHERE sync_channel_id IS NOT NULL
                  AND sync_channel_id NOT IN (SELECT id FROM sync_channels)
                """
            )
        )

    if "workspaces" in tables:
        for table, column in (
            ("workspace_settings", "workspace_id"),
            ("user_directory", "workspace_id"),
            ("workspace_group_members", "workspace_id"),
        ):
            if table not in tables:
                continue
            bind.execute(
                sa.text(
                    f"""
                    DELETE FROM {table}
                    WHERE {column} IS NOT NULL
                      AND {column} NOT IN (SELECT id FROM workspaces)
                    """
                )
            )
        if "user_mappings" in tables:
            bind.execute(
                sa.text(
                    """
                    DELETE FROM user_mappings
                    WHERE (source_workspace_id IS NOT NULL AND source_workspace_id NOT IN (SELECT id FROM workspaces))
                       OR (target_workspace_id IS NOT NULL AND target_workspace_id NOT IN (SELECT id FROM workspaces))
                    """
                )
            )

    if "post_meta" in tables and "workspaces" in tables:
        bind.execute(
            sa.text(
                """
                UPDATE post_meta
                SET source_workspace_id = NULL
                WHERE source_workspace_id IS NOT NULL
                  AND source_workspace_id NOT IN (SELECT id FROM workspaces)
                """
            )
        )
    if "workspace_group_members" in tables and "workspaces" in tables:
        bind.execute(
            sa.text(
                """
                UPDATE workspace_group_members
                SET invited_by_workspace_id = NULL
                WHERE invited_by_workspace_id IS NOT NULL
                  AND invited_by_workspace_id NOT IN (SELECT id FROM workspaces)
                """
            )
        )
    if "user_mappings" in tables and "workspace_groups" in tables:
        bind.execute(
            sa.text(
                """
                UPDATE user_mappings
                SET group_id = NULL
                WHERE group_id IS NOT NULL
                  AND group_id NOT IN (SELECT id FROM workspace_groups)
                """
            )
        )

    if "workspace_group_members" in tables and "workspace_groups" in tables:
        bind.execute(
            sa.text(
                """
                DELETE FROM workspace_group_members
                WHERE group_id NOT IN (SELECT id FROM workspace_groups)
                """
            )
        )

    if bind.dialect.name == "sqlite":
        for name in ("workspaces", "workspace_groups", "syncs", "sync_channels"):
            if name not in tables:
                continue
            with op.batch_alter_table(name, recreate="always", table_kwargs={"sqlite_autoincrement": True}):
                pass


def downgrade() -> None:
    """Deleted rows are not restored."""


def _delete_channels_for_syncs(bind, tables: set[str], sync_ids: list[int]) -> None:
    if "post_meta" in tables:
        bind.execute(
            sa.text(
                """
                DELETE FROM post_meta
                WHERE sync_channel_id IN (
                    SELECT id FROM sync_channels WHERE sync_id IN :ids
                )
                """
            ).bindparams(sa.bindparam("ids", expanding=True)),
            {"ids": sync_ids},
        )
    bind.execute(
        sa.text("DELETE FROM sync_channels WHERE sync_id IN :ids").bindparams(sa.bindparam("ids", expanding=True)),
        {"ids": sync_ids},
    )
