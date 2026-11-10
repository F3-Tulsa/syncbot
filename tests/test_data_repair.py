"""Repair removes the same leftovers as migration 022, and merges duplicate Channels."""

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest

os.environ.setdefault("DATABASE_HOST", "localhost")
os.environ.setdefault("DATABASE_USER", "root")
os.environ.setdefault("DATABASE_PASSWORD", "test")
os.environ.setdefault("DATABASE_SCHEMA", "syncbot")
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-0-0")


@pytest.fixture
def real_db(tmp_path):
    import db as db_mod
    from db import initialize_database

    url = f"sqlite:///{tmp_path / 'repair.db'}"
    old_engine = db_mod.GLOBAL_ENGINE
    old_schema = db_mod.GLOBAL_SCHEMA
    old_fk = db_mod._SQLITE_FOREIGN_KEYS_ENFORCED
    with patch.dict(os.environ, {"DATABASE_BACKEND": "sqlite", "DATABASE_URL": url}, clear=False):
        try:
            db_mod.GLOBAL_ENGINE = None
            db_mod.GLOBAL_SCHEMA = None
            db_mod._SQLITE_FOREIGN_KEYS_ENFORCED = True
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
            db_mod._SQLITE_FOREIGN_KEYS_ENFORCED = old_fk


def _now():
    return datetime.now(UTC).replace(tzinfo=None)


def _fk_off(sql, params=None):
    from db import get_engine

    raw = get_engine().raw_connection()
    try:
        raw.execute("PRAGMA foreign_keys=OFF")
        if params:
            raw.execute(sql, params)
        else:
            raw.execute(sql)
        raw.commit()
    finally:
        raw.close()


def _workspace(team_id, name):
    from db import DbManager, schemas
    from federation.core import get_instance_id

    return DbManager.create_record(
        schemas.Workspace(team_id=team_id, workspace_name=name, instance_id=get_instance_id())
    )


def _group(code):
    from db import DbManager, schemas

    return DbManager.create_record(
        schemas.WorkspaceGroup(name="Group", invite_code=code, status="active", created_at=_now(), uid=code)
    )


def _member(group, workspace):
    from db import DbManager, schemas

    return DbManager.create_record(
        schemas.WorkspaceGroupMember(
            group_id=group.id,
            workspace_id=workspace.id,
            status="active",
            role="member",
            joined_at=_now(),
        )
    )


def _sync(group, uid):
    from db import DbManager, schemas

    return DbManager.create_record(schemas.Sync(title="general", sync_mode="group", group_id=group.id, uid=uid))


def _channel(sync, workspace, channel_id, *, created_at=None, channel_name=None, status="active"):
    from db import DbManager, schemas

    return DbManager.create_record(
        schemas.SyncChannel(
            sync_id=sync.id,
            workspace_id=workspace.id,
            channel_id=channel_id,
            channel_name=channel_name,
            status=status,
            publishes=True,
            subscribes=True,
            created_at=created_at or _now(),
        )
    )


def _post(channel, post_id, ts="1700000000.000100"):
    from db import DbManager, schemas

    return DbManager.create_record(
        schemas.PostMeta(post_id=post_id, sync_channel_id=channel.id, ts=Decimal(ts), kind="message")
    )


def _count(key):
    from helpers.data_repair import run_repair

    return run_repair(fix=False).count(key)


def test_sync_missing_group_is_removed_only_on_fix(real_db):
    from db import DbManager, schemas

    _fk_off(
        "INSERT INTO syncs (title, sync_mode, group_id, uid) VALUES (?, ?, ?, ?)",
        ("gone", "group", 999999, "s-missing-group"),
    )
    assert _count("sync_missing_group") == 1
    assert DbManager.find_records(schemas.Sync, [schemas.Sync.uid == "s-missing-group"])
    from helpers.data_repair import run_repair

    assert run_repair(fix=True).count("sync_missing_group") == 1
    assert DbManager.find_records(schemas.Sync, [schemas.Sync.uid == "s-missing-group"]) == []
    assert run_repair(fix=True).count("sync_missing_group") == 0


def test_channel_missing_sync_is_removed_only_on_fix(real_db):
    workspace = _workspace("T0000000A", "Workspace A")
    _fk_off(
        """
        INSERT INTO sync_channels
            (sync_id, workspace_id, channel_id, status, publishes, subscribes, created_at)
        VALUES (999999, ?, 'CORPHAN', 'active', 1, 1, ?)
        """,
        (workspace.id, _now()),
    )
    from db import DbManager, schemas
    from helpers.data_repair import run_repair

    assert _count("channel_missing_sync") == 1
    assert run_repair(fix=True).count("channel_missing_sync") == 1
    assert DbManager.find_records(schemas.SyncChannel, [schemas.SyncChannel.channel_id == "CORPHAN"]) == []
    assert run_repair(fix=True).count("channel_missing_sync") == 0


def test_channel_missing_workspace_is_removed_only_on_fix(real_db):
    group = _group("GRP-MISS-WS")
    sync = _sync(group, "s-miss-ws")
    _fk_off(
        """
        INSERT INTO sync_channels
            (sync_id, workspace_id, channel_id, status, publishes, subscribes, created_at)
        VALUES (?, 999999, 'CGONE', 'active', 1, 1, ?)
        """,
        (sync.id, _now()),
    )
    from db import DbManager, schemas
    from helpers.data_repair import run_repair

    assert _count("channel_missing_workspace") == 1
    assert run_repair(fix=True).count("channel_missing_workspace") == 1
    assert DbManager.find_records(schemas.SyncChannel, [schemas.SyncChannel.channel_id == "CGONE"]) == []
    assert run_repair(fix=True).count("channel_missing_workspace") == 0


def test_channel_outside_group_is_removed_only_on_fix(real_db):
    group = _group("GRP-OUT")
    workspace_a = _workspace("T0000000A", "Workspace A")
    workspace_c = _workspace("T0000000C", "Workspace C")
    _member(group, workspace_a)
    sync = _sync(group, "s-out")
    kept = _channel(sync, workspace_a, "CA")
    outside = _channel(sync, workspace_c, "CC")
    _post(outside, "POUT")
    from db import DbManager, schemas
    from helpers.data_repair import run_repair

    assert _count("channel_outside_group") == 1
    assert DbManager.get_record(schemas.SyncChannel, id=outside.id) is not None
    assert run_repair(fix=True).count("channel_outside_group") == 1
    assert DbManager.get_record(schemas.SyncChannel, id=outside.id) is None
    assert DbManager.find_records(schemas.PostMeta, [schemas.PostMeta.post_id == "POUT"]) == []
    assert DbManager.get_record(schemas.SyncChannel, id=kept.id) is not None
    assert run_repair(fix=True).count("channel_outside_group") == 0


def test_duplicate_channel_is_removed_only_on_fix(real_db):
    group = _group("GRP-DUP")
    workspace = _workspace("T0000000A", "Workspace A")
    _member(group, workspace)
    sync = _sync(group, "s-dup")
    _channel(sync, workspace, "CA", created_at=_now() - timedelta(days=2))
    _channel(sync, workspace, "CA", created_at=_now())
    from db import DbManager, schemas
    from helpers.data_repair import run_repair

    assert _count("duplicate_channel") == 1
    assert len(DbManager.find_records(schemas.SyncChannel, [schemas.SyncChannel.channel_id == "CA"])) == 2
    assert run_repair(fix=True).count("duplicate_channel") == 1
    assert len(DbManager.find_records(schemas.SyncChannel, [schemas.SyncChannel.channel_id == "CA"])) == 1
    assert run_repair(fix=True).count("duplicate_channel") == 0


def test_duplicate_channel_keeps_the_active_row(real_db):
    group = _group("GRP-RANK")
    workspace = _workspace("T0000000A", "Workspace A")
    _member(group, workspace)
    sync = _sync(group, "s-rank")
    paused = _channel(sync, workspace, "CA", created_at=_now() - timedelta(days=2), status="paused")
    active = _channel(sync, workspace, "CA", created_at=_now(), status="active")
    _post(paused, "P1", "1700000000.000100")
    _post(active, "P2", "1700000000.000200")
    from db import DbManager, schemas
    from helpers.data_repair import run_repair

    run_repair(fix=True)
    kept = DbManager.find_records(schemas.PostMeta, [schemas.PostMeta.sync_channel_id == active.id])
    assert sorted(row.post_id for row in kept) == ["P1", "P2"]
    assert DbManager.get_record(schemas.SyncChannel, id=paused.id) is None


def test_failed_repair_leaves_the_database_unchanged(real_db):
    group = _group("GRP-ROLL")
    workspace = _workspace("T0000000A", "Workspace A")
    other = _workspace("T0000000C", "Workspace C")
    _member(group, workspace)
    sync = _sync(group, "s-roll")
    channel = _channel(sync, other, "CC")
    _post(channel, "PC")
    _fk_off(
        "INSERT INTO workspace_settings (workspace_id, key, value) VALUES (424242, 'allow_private_channels', 'no')",
        (),
    )
    from db import DbManager, schemas
    from helpers.data_repair import run_repair

    with (
        patch("helpers.data_repair._fix_workspace_rows", side_effect=RuntimeError("boom")),
        patch("helpers.data_repair.log_error") as log_error,
        pytest.raises(RuntimeError, match="boom"),
    ):
        run_repair(fix=True)
    assert log_error.call_args.args[0] == "data_repair_rolled_back"
    assert log_error.call_args.kwargs["step"] == "rows_missing_workspace"
    assert DbManager.get_record(schemas.SyncChannel, id=channel.id) is not None
    assert DbManager.find_records(schemas.PostMeta, [schemas.PostMeta.post_id == "PC"])
    settings = DbManager.find_records(schemas.WorkspaceSetting, [schemas.WorkspaceSetting.workspace_id == 424242])
    assert len(settings) == 1


def test_orphan_message_records_are_removed_in_batches(real_db):
    for index in range(5):
        _fk_off(
            "INSERT INTO post_meta (post_id, sync_channel_id, ts, kind) VALUES (:post_id, 999999, :ts, 'message')",
            {"post_id": f"P{index}", "ts": index + 1},
        )
    from db import DbManager, schemas
    from helpers.data_repair import run_repair

    with patch("helpers.data_repair._ID_BATCH", 2):
        assert run_repair(fix=True).count("post_meta_missing_channel") == 5
    assert DbManager.find_records(schemas.PostMeta, [schemas.PostMeta.sync_channel_id == 999999]) == []


def test_duplicate_channel_merge_keeps_post_meta(real_db):
    group = _group("GRP-MERGE")
    workspace = _workspace("T0000000A", "Workspace A")
    _member(group, workspace)
    sync = _sync(group, "s-merge")
    older = _channel(sync, workspace, "CA", created_at=_now() - timedelta(days=2))
    newer = _channel(sync, workspace, "CA", created_at=_now())
    _post(older, "P1", "1700000000.000100")
    _post(newer, "P1", "1700000000.000100")
    _post(newer, "P2", "1700000000.000200")
    from db import DbManager, schemas
    from helpers.data_repair import run_repair

    run_repair(fix=True)
    kept = DbManager.find_records(schemas.PostMeta, [schemas.PostMeta.sync_channel_id == older.id])
    assert sorted(row.post_id for row in kept) == ["P1", "P2"]
    assert DbManager.get_record(schemas.SyncChannel, id=newer.id) is None


def test_post_meta_missing_channel_is_removed_only_on_fix(real_db):
    _fk_off(
        "INSERT INTO post_meta (post_id, sync_channel_id, ts, kind) VALUES ('PORPHAN', 999999, 1, 'message')",
        (),
    )
    from db import DbManager, schemas
    from helpers.data_repair import run_repair

    assert _count("post_meta_missing_channel") == 1
    assert run_repair(fix=True).count("post_meta_missing_channel") == 1
    assert DbManager.find_records(schemas.PostMeta, [schemas.PostMeta.post_id == "PORPHAN"]) == []
    assert run_repair(fix=True).count("post_meta_missing_channel") == 0


def test_rows_missing_workspace_are_removed_only_on_fix(real_db):
    _fk_off(
        "INSERT INTO workspace_settings (workspace_id, key, value) VALUES (424242, 'allow_private_channels', 'no')",
        (),
    )
    from db import DbManager, schemas
    from helpers.data_repair import run_repair

    assert _count("rows_missing_workspace") == 1
    assert run_repair(fix=True).count("rows_missing_workspace") == 1
    assert DbManager.find_records(schemas.WorkspaceSetting, [schemas.WorkspaceSetting.workspace_id == 424242]) == []
    assert run_repair(fix=True).count("rows_missing_workspace") == 0


def test_repair_never_writes_federation_tables(real_db):
    from db import get_engine
    from federation.core import get_instance_id

    _fk_off(
        "INSERT INTO federation_workspace_allowlist (instance_id, workspace_id) VALUES (?, 424242)",
        (get_instance_id(),),
    )
    before = (
        get_engine()
        .connect()
        .execute(__import__("sqlalchemy").text("SELECT workspace_id FROM federation_workspace_allowlist"))
        .fetchall()
    )
    from helpers.data_repair import run_repair

    run_repair(fix=False)
    run_repair(fix=True)
    after = (
        get_engine()
        .connect()
        .execute(__import__("sqlalchemy").text("SELECT workspace_id FROM federation_workspace_allowlist"))
        .fetchall()
    )
    assert before == after == [(424242,)]


def test_available_sync_uses_a_channel_inside_the_group(real_db):
    from builders.channel_sync import _build_inline_channel_sync
    from helpers.data_repair import run_repair
    from helpers.synced_by import Viewer

    group = _group("GRP-CARD")
    workspace_a = _workspace("T0000000A", "Workspace A")
    workspace_b = _workspace("T0000000B", "Workspace B")
    workspace_c = _workspace("T0000000C", "Workspace C")
    _member(group, workspace_a)
    _member(group, workspace_b)
    sync = _sync(group, "s-card")
    _channel(sync, workspace_c, "CC", channel_name="outside")
    _channel(sync, workspace_b, "CB", channel_name="inside")
    viewer = Viewer(
        team_id=workspace_a.team_id,
        user_id="U1",
        workspace_id=workspace_a.id,
        is_manager=True,
        allow_private=False,
        has_user_token=False,
        private_channel_ids=None,
    )

    def card_text():
        blocks = []
        _build_inline_channel_sync(blocks, group, workspace_a, [], viewer=viewer)
        rendered = [block.as_form_field() if hasattr(block, "as_form_field") else block for block in blocks]
        return "\n".join(block.get("text", {}).get("text", "") for block in rendered if isinstance(block, dict))

    assert "From: `Workspace C`" in card_text()
    run_repair(fix=True)
    text = card_text()
    assert "From: `Workspace B`" in text
    assert "Workspace C" not in text


def test_repair_and_migration_agree(tmp_path):
    def run(path, mode):
        from alembic import command
        from sqlalchemy import text

        import db as db_mod
        from db import DbManager, _alembic_config, initialize_database, schemas
        from federation import core as federation_core

        url = f"sqlite:///{path}"
        with patch.dict(os.environ, {"DATABASE_BACKEND": "sqlite", "DATABASE_URL": url}, clear=False):
            db_mod.GLOBAL_ENGINE = None
            db_mod.GLOBAL_SCHEMA = None
            db_mod._SQLITE_FOREIGN_KEYS_ENFORCED = True
            initialize_database()
            federation_core._INSTANCE_ID = None
            federation_core._cached_private_key = None
            federation_core._cached_public_pem = None
            federation_core.get_or_create_instance_keypair()
            group = _group("GRP-AGREE")
            workspace_a = _workspace("T0000000A", "Workspace A")
            workspace_b = _workspace("T0000000B", "Workspace B")
            workspace_c = _workspace("T0000000C", "Workspace C")
            _member(group, workspace_a)
            _member(group, workspace_b)
            sync = _sync(group, "s-agree")
            _channel(sync, workspace_a, "CA")
            _channel(sync, workspace_b, "CB")
            outside = _channel(sync, workspace_c, "CC")
            _post(outside, "PC")
            _fk_off(
                "INSERT INTO workspace_settings (workspace_id, key, value) VALUES (424242, 'allow_private_channels', 'no')",
                (),
            )
            # Blank references stay. A gone Workspace removes a Channel even with a blank Sync.
            now = _now()
            DbManager.create_record(
                schemas.SyncChannel(
                    sync_id=None, workspace_id=workspace_a.id, channel_id="CNULLSYNC", status="active", created_at=now
                )
            )
            _fk_off(
                "INSERT INTO sync_channels (sync_id, workspace_id, channel_id, status, publishes, subscribes, created_at)"
                " VALUES (NULL, 515151, 'CGONEWS', 'active', 1, 1, ?)",
                (now,),
            )
            DbManager.create_record(schemas.PostMeta(post_id="PNULL", sync_channel_id=None, ts=1700000000.0003))
            DbManager.create_record(schemas.UserDirectory(workspace_id=None, slack_user_id="UNULL", updated_at=now))
            DbManager.create_record(
                schemas.UserMapping(
                    source_workspace_id=None,
                    source_user_id="UNULLMAP",
                    target_workspace_id=workspace_a.id,
                    mapped_at=now,
                )
            )
            DbManager.create_record(
                schemas.WorkspaceGroupMember(group_id=group.id, workspace_id=None, status="active", joined_at=now)
            )
            if mode == "migrate":
                with db_mod.get_engine().begin() as conn:
                    conn.execute(text("UPDATE alembic_version SET version_num = '021_sync_channel_privacy'"))
                command.upgrade(_alembic_config(), "022_data_consistency")
            else:
                from helpers.data_repair import run_repair

                run_repair(fix=True)
            channels = sorted(
                row.channel_id
                for row in DbManager.find_records(schemas.SyncChannel, [schemas.SyncChannel.id.isnot(None)])
            )
            posts = sorted(
                row.post_id for row in DbManager.find_records(schemas.PostMeta, [schemas.PostMeta.id.isnot(None)])
            )
            settings = DbManager.find_records(
                schemas.WorkspaceSetting, [schemas.WorkspaceSetting.workspace_id == 424242]
            )
            directory = sorted(
                row.slack_user_id
                for row in DbManager.find_records(schemas.UserDirectory, [schemas.UserDirectory.id.isnot(None)])
            )
            mappings = sorted(
                row.source_user_id
                for row in DbManager.find_records(schemas.UserMapping, [schemas.UserMapping.id.isnot(None)])
            )
            blank_members = DbManager.find_records(
                schemas.WorkspaceGroupMember, [schemas.WorkspaceGroupMember.workspace_id.is_(None)]
            )
            if db_mod.GLOBAL_ENGINE:
                db_mod.GLOBAL_ENGINE.dispose()
            db_mod.GLOBAL_ENGINE = None
            return channels, posts, len(settings), directory, mappings, len(blank_members)

    repaired = run(tmp_path / "repair.db", "repair")
    assert repaired == run(tmp_path / "migrate.db", "migrate")
    channels, posts, _settings, directory, mappings, blank_members = repaired
    assert "CNULLSYNC" in channels
    assert "CGONEWS" not in channels
    assert "PNULL" in posts
    assert directory == ["UNULL"]
    assert mappings == ["UNULLMAP"]
    assert blank_members == 1


def test_repair_logs_what_it_committed(real_db):
    from helpers.data_repair import run_repair

    with patch("helpers.data_repair.log_info") as log_info:
        run_repair(fix=False)
        run_repair(fix=True)
    events = [call.args[0] for call in log_info.call_args_list]
    assert events == ["data_repair_verified", "data_repair_committed"]
    assert log_info.call_args_list[1].kwargs["fixed"] == 0


def test_every_db_call_joins_an_open_transaction(real_db):
    from db import DbManager, schemas, transaction

    workspace = _workspace("T0000000A", "Workspace A")
    with pytest.raises(RuntimeError, match="boom"), transaction():
        DbManager.create_record(schemas.InstanceSetting(key="joined", value="1"))
        DbManager.update_record(schemas.Workspace, workspace.team_id, {schemas.Workspace.workspace_name: "Changed"})
        assert DbManager.get_record(schemas.Workspace, workspace.team_id).workspace_name == "Changed"
        assert DbManager.count_records(schemas.Workspace, [schemas.Workspace.id == workspace.id]) == 1
        DbManager.delete_record(schemas.InstanceSetting, "joined")
        DbManager.merge_record(schemas.InstanceSetting(key="merged", value="1"))
        raise RuntimeError("boom")
    assert DbManager.get_record(schemas.Workspace, workspace.team_id).workspace_name == "Workspace A"
    assert DbManager.get_record(schemas.InstanceSetting, "merged") is None


def test_repair_rollback_log_leaves_out_the_statement(real_db):
    from helpers.data_repair import run_repair

    with (
        patch("helpers.data_repair._fix_workspace_rows", side_effect=RuntimeError("SELECT secret")),
        patch("helpers.data_repair.log_error") as log_error,
        pytest.raises(RuntimeError),
    ):
        run_repair(fix=True)
    assert "error" not in log_error.call_args.kwargs
    assert log_error.call_args.kwargs["error_type"] == "RuntimeError"
