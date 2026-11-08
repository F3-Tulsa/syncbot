"""Alembic 022 removes leftover rows and stops SQLite from reusing ids."""

import os
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

os.environ.setdefault("DATABASE_HOST", "localhost")
os.environ.setdefault("DATABASE_USER", "root")
os.environ.setdefault("DATABASE_PASSWORD", "test")
os.environ.setdefault("DATABASE_SCHEMA", "syncbot")
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-0-0")

from alembic import command  # noqa: E402
from sqlalchemy import text  # noqa: E402


@pytest.fixture
def real_db(tmp_path):
    import db as db_mod
    from db import initialize_database

    url = f"sqlite:///{tmp_path / 'alembic022.db'}"
    old_engine = db_mod.GLOBAL_ENGINE
    old_schema = db_mod.GLOBAL_SCHEMA
    with patch.dict(os.environ, {"DATABASE_BACKEND": "sqlite", "DATABASE_URL": url}, clear=False):
        try:
            db_mod.GLOBAL_ENGINE = None
            db_mod.GLOBAL_SCHEMA = None
            initialize_database()
            from federation import core as federation_core

            federation_core._INSTANCE_ID = None
            federation_core._cached_private_key = None
            federation_core._cached_public_pem = None
            federation_core.get_or_create_instance_keypair()
            yield
        finally:
            if db_mod.GLOBAL_ENGINE:
                db_mod.GLOBAL_ENGINE.dispose()
            db_mod.GLOBAL_ENGINE = old_engine
            db_mod.GLOBAL_SCHEMA = old_schema


def _now():
    return datetime.now(UTC).replace(tzinfo=None)


def _stamp_021():
    from db import get_engine

    with get_engine().begin() as conn:
        conn.execute(text("UPDATE alembic_version SET version_num = '021_sync_channel_privacy'"))


def _upgrade():
    from db import _alembic_config

    command.upgrade(_alembic_config(), "022_data_consistency")


def _workspace(team_id, name):
    from db import DbManager, schemas
    from federation.core import get_instance_id

    return DbManager.create_record(
        schemas.Workspace(team_id=team_id, workspace_name=name, instance_id=get_instance_id())
    )


def _member(group, workspace, *, deleted_at=None):
    from db import DbManager, schemas

    return DbManager.create_record(
        schemas.WorkspaceGroupMember(
            group_id=group.id,
            workspace_id=workspace.id,
            status="active",
            role="member",
            joined_at=_now(),
            deleted_at=deleted_at,
        )
    )


def _channel(sync, workspace, channel_id, *, deleted_at=None, status="active"):
    from db import DbManager, schemas

    return DbManager.create_record(
        schemas.SyncChannel(
            sync_id=sync.id,
            workspace_id=workspace.id,
            channel_id=channel_id,
            status=status,
            publishes=True,
            subscribes=True,
            created_at=_now(),
            deleted_at=deleted_at,
        )
    )


def _post(channel, post_id):
    from decimal import Decimal

    from db import DbManager, schemas

    return DbManager.create_record(
        schemas.PostMeta(post_id=post_id, sync_channel_id=channel.id, ts=Decimal("1700000000.000100"))
    )


def test_leftover_channel_from_outside_the_group_is_removed(real_db):
    from db import DbManager, schemas

    group = DbManager.create_record(
        schemas.WorkspaceGroup(name="Group", invite_code="GRP-022A", status="active", created_at=_now(), uid="g-022-a")
    )
    workspace_a = _workspace("T0000000A", "Workspace A")
    workspace_b = _workspace("T0000000B", "Workspace B")
    workspace_c = _workspace("T0000000C", "Workspace C")
    _member(group, workspace_a)
    _member(group, workspace_b)
    sync = DbManager.create_record(schemas.Sync(title="general", sync_mode="group", group_id=group.id, uid="s-022-a"))
    channel_a = _channel(sync, workspace_a, "CA")
    channel_b = _channel(sync, workspace_b, "CB")
    channel_c = _channel(sync, workspace_c, "CC")
    _post(channel_a, "PA")
    _post(channel_b, "PB")
    _post(channel_c, "PC")

    _stamp_021()
    _upgrade()

    assert DbManager.get_record(schemas.SyncChannel, id=channel_c.id) is None
    assert DbManager.find_records(schemas.PostMeta, [schemas.PostMeta.post_id == "PC"]) == []
    assert DbManager.get_record(schemas.SyncChannel, id=channel_a.id) is not None
    assert DbManager.get_record(schemas.SyncChannel, id=channel_b.id) is not None
    assert DbManager.find_records(schemas.PostMeta, [schemas.PostMeta.post_id == "PA"])
    assert DbManager.find_records(schemas.PostMeta, [schemas.PostMeta.post_id == "PB"])


def test_uninstalled_channel_with_soft_deleted_membership_is_kept(real_db):
    from db import DbManager, schemas

    group = DbManager.create_record(
        schemas.WorkspaceGroup(name="Group", invite_code="GRP-022B", status="active", created_at=_now(), uid="g-022-b")
    )
    workspace = _workspace("T0000000A", "Workspace A")
    _member(group, workspace, deleted_at=_now())
    sync = DbManager.create_record(schemas.Sync(title="general", sync_mode="group", group_id=group.id, uid="s-022-b"))
    channel = _channel(sync, workspace, "CA", deleted_at=_now(), status="uninstalled")
    _post(channel, "PA")

    _stamp_021()
    _upgrade()

    assert DbManager.get_record(schemas.SyncChannel, id=channel.id) is not None
    assert DbManager.find_records(schemas.PostMeta, [schemas.PostMeta.post_id == "PA"])


def test_channel_whose_sync_was_deleted_is_removed(real_db):
    from db import get_engine

    workspace = _workspace("T0000000A", "Workspace A")
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("PRAGMA foreign_keys=OFF"))
        conn.execute(
            text(
                """
                INSERT INTO sync_channels
                    (sync_id, workspace_id, channel_id, status, publishes, subscribes, created_at)
                VALUES (999999, :ws, 'CORPHAN', 'active', 1, 1, :now)
                """
            ),
            {"ws": workspace.id, "now": _now()},
        )
        channel_id = conn.execute(text("SELECT id FROM sync_channels WHERE channel_id = 'CORPHAN'")).scalar()
        conn.execute(
            text("INSERT INTO post_meta (post_id, sync_channel_id, ts, kind) VALUES ('PORPHAN', :cid, 1, 'message')"),
            {"cid": channel_id},
        )
    _stamp_021()
    _upgrade()
    with get_engine().connect() as conn:
        assert conn.execute(text("SELECT id FROM sync_channels WHERE channel_id = 'CORPHAN'")).first() is None
        assert conn.execute(text("SELECT id FROM post_meta WHERE post_id = 'PORPHAN'")).first() is None


def test_federation_tables_are_untouched(real_db):
    from db import get_engine
    from federation.core import get_instance_id

    peer = "c" * 64
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("PRAGMA foreign_keys=OFF"))
        conn.execute(
            text(
                """
                INSERT INTO instances
                    (instance_id, public_key, webhook_url, status, trust_status, created_at)
                VALUES (:iid, 'peer-public', 'https://peer.example/federation', 'active', 'trusted', :now)
                """
            ),
            {"iid": peer, "now": _now()},
        )
        conn.execute(
            text(
                """
                INSERT INTO federation_workspace_allowlist (instance_id, workspace_id)
                VALUES (:iid, 424242)
                """
            ),
            {"iid": get_instance_id()},
        )
        conn.execute(
            text(
                """
                INSERT INTO federation_pending_stubs (instance_id, workspace_id)
                VALUES (:iid, 424242)
                """
            ),
            {"iid": peer},
        )
    _stamp_021()
    _upgrade()
    with get_engine().connect() as conn:
        assert conn.execute(text("SELECT workspace_id FROM federation_workspace_allowlist")).scalar() == 424242
        assert conn.execute(text("SELECT workspace_id FROM federation_pending_stubs")).scalar() == 424242


def test_every_foreign_key_is_clean_after_upgrade(real_db):
    from db import DbManager, get_engine, schemas
    from federation.core import get_instance_id

    workspace = _workspace("T0000000A", "Workspace A")
    group = DbManager.create_record(
        schemas.WorkspaceGroup(name="Group", invite_code="GRP-FK", status="active", created_at=_now(), uid="g-fk")
    )
    sync = DbManager.create_record(schemas.Sync(title="general", sync_mode="group", group_id=group.id, uid="s-fk"))
    channel = _channel(sync, workspace, "CA")
    now = _now()
    with get_engine().begin() as conn:
        conn.execute(text("PRAGMA foreign_keys=OFF"))
        conn.execute(
            text(
                "INSERT INTO workspaces (team_id, workspace_name, instance_id) VALUES ('T_DANGLING', 'Dangle', 'missing-instance')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO workspace_group_members (group_id, workspace_id, status, role) VALUES (999991, :ws, 'active', 'member')"
            ),
            {"ws": workspace.id},
        )
        conn.execute(
            text(
                "INSERT INTO workspace_group_members (group_id, workspace_id, status, role) VALUES (:gid, 424242, 'active', 'member')"
            ),
            {"gid": group.id},
        )
        conn.execute(
            text(
                """
                INSERT INTO workspace_group_members
                    (group_id, workspace_id, status, role, invited_by_workspace_id)
                VALUES (:gid, :ws, 'active', 'member', 424242)
                """
            ),
            {"gid": group.id, "ws": workspace.id},
        )
        conn.execute(
            text("INSERT INTO syncs (title, sync_mode, group_id, uid) VALUES ('gone', 'group', 999992, 's-gone')")
        )
        conn.execute(
            text(
                """
                INSERT INTO sync_channels
                    (sync_id, workspace_id, channel_id, status, publishes, subscribes, created_at)
                VALUES (999993, :ws, 'CORPHAN', 'active', 1, 1, :now)
                """
            ),
            {"ws": workspace.id, "now": now},
        )
        conn.execute(
            text(
                """
                INSERT INTO sync_channels
                    (sync_id, workspace_id, channel_id, status, publishes, subscribes, created_at)
                VALUES (:sid, 424242, 'CGONE', 'active', 1, 1, :now)
                """
            ),
            {"sid": sync.id, "now": now},
        )
        conn.execute(
            text("INSERT INTO post_meta (post_id, sync_channel_id, ts, kind) VALUES ('PORPHAN', 999994, 1, 'message')")
        )
        conn.execute(
            text(
                "INSERT INTO post_meta (post_id, sync_channel_id, ts, kind, source_workspace_id) VALUES ('PSOURCE', :cid, 1, 'message', 424242)"
            ),
            {"cid": channel.id},
        )
        conn.execute(
            text("INSERT INTO user_directory (workspace_id, slack_user_id, updated_at) VALUES (424242, 'UDIR', :now)"),
            {"now": now},
        )
        conn.execute(
            text(
                """
                INSERT INTO user_mappings
                    (source_workspace_id, source_user_id, target_workspace_id, map_method, mapped_at)
                VALUES (424242, 'USRC', :ws, 'none', :now)
                """
            ),
            {"ws": workspace.id, "now": now},
        )
        conn.execute(
            text(
                """
                INSERT INTO user_mappings
                    (source_workspace_id, source_user_id, target_workspace_id, map_method, mapped_at)
                VALUES (:ws, 'UTGT', 424243, 'none', :now)
                """
            ),
            {"ws": workspace.id, "now": now},
        )
        conn.execute(
            text(
                """
                INSERT INTO user_mappings
                    (source_workspace_id, source_user_id, target_workspace_id, map_method, mapped_at, group_id)
                VALUES (:ws, 'UGRP', :ws, 'none', :now, 999995)
                """
            ),
            {"ws": workspace.id, "now": now},
        )
        conn.execute(
            text(
                "INSERT INTO workspace_settings (workspace_id, key, value) VALUES (424242, 'allow_private_channels', 'no')"
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO federation_pairing_requests
                    (subject_team_id, requested_by_user_id, requested_at, status, pairing_code_id)
                VALUES ('T0000000A', 'U1', :now, 'pending', 999996)
                """
            ),
            {"now": now},
        )
        conn.execute(
            text("INSERT INTO federation_pending_stubs (instance_id, workspace_id) VALUES (:iid, 424242)"),
            {"iid": get_instance_id()},
        )
        conn.execute(
            text("INSERT INTO federation_pending_stubs (instance_id, workspace_id) VALUES ('missing-peer', :ws)"),
            {"ws": workspace.id},
        )
        conn.execute(
            text("INSERT INTO federation_workspace_allowlist (instance_id, workspace_id) VALUES (:iid, 424242)"),
            {"iid": get_instance_id()},
        )
        conn.execute(
            text(
                "INSERT INTO federation_workspace_allowlist (instance_id, workspace_id) VALUES ('missing-peer-allow', :ws)"
            ),
            {"ws": workspace.id},
        )
    _stamp_021()
    _upgrade()
    with get_engine().connect() as conn:
        rows = conn.execute(text("PRAGMA foreign_key_check")).fetchall()
    for row in rows:
        table = row[0]
        parent = row[2]
        assert (
            table.startswith("federation_") or table == "instances" or (table == "workspaces" and parent == "instances")
        )
    assert any(row[0] == "workspaces" and row[2] == "instances" for row in rows)


def test_deleted_sync_id_is_not_reused(real_db):
    from db import DbManager, schemas
    from federation.core import get_instance_id

    first_sync = DbManager.create_record(schemas.Sync(title="one", sync_mode="group", uid="sync-reuse-1"))
    first_sync_id = first_sync.id
    DbManager.delete_records(schemas.Sync, [schemas.Sync.id == first_sync_id])
    second_sync = DbManager.create_record(schemas.Sync(title="two", sync_mode="group", uid="sync-reuse-2"))
    assert second_sync.id > first_sync_id

    first_group = DbManager.create_record(
        schemas.WorkspaceGroup(
            name="One", invite_code="GRP-REUSE1", status="active", created_at=_now(), uid="g-reuse-1"
        )
    )
    first_group_id = first_group.id
    DbManager.delete_records(schemas.WorkspaceGroup, [schemas.WorkspaceGroup.id == first_group_id])
    second_group = DbManager.create_record(
        schemas.WorkspaceGroup(
            name="Two", invite_code="GRP-REUSE2", status="active", created_at=_now(), uid="g-reuse-2"
        )
    )
    assert second_group.id > first_group_id

    first_ws = DbManager.create_record(
        schemas.Workspace(team_id="T0000000R", workspace_name="Workspace R", instance_id=get_instance_id())
    )
    first_ws_id = first_ws.id
    DbManager.delete_records(schemas.Workspace, [schemas.Workspace.id == first_ws_id])
    second_ws = DbManager.create_record(
        schemas.Workspace(team_id="T0000000S", workspace_name="Workspace S", instance_id=get_instance_id())
    )
    assert second_ws.id > first_ws_id


def test_deleted_channel_id_is_not_reused(real_db):
    from db import DbManager, schemas
    from federation.core import get_instance_id

    workspace = DbManager.create_record(
        schemas.Workspace(team_id="T0000000A", workspace_name="Workspace A", instance_id=get_instance_id())
    )
    sync = DbManager.create_record(schemas.Sync(title="general", sync_mode="group", uid="s-reuse"))
    first = _channel(sync, workspace, "C1")
    first_id = first.id
    DbManager.delete_records(schemas.SyncChannel, [schemas.SyncChannel.id == first_id])
    second = _channel(sync, workspace, "C2")
    assert second.id > first_id
