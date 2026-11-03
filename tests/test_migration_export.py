"""Data migration export looks up Workspaces by integer primary key."""

import json
import os
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from helpers.export_import import build_migration_export, import_migration_data


class TestBuildMigrationExportWorkspaceLookup:
    def test_finds_workspace_by_integer_pk(self, caplog):
        import logging

        workspace = SimpleNamespace(
            id=42,
            team_id="T1",
            workspace_name="Workspace A",
            deleted_at=None,
        )
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

        from federation.core import public_key_fingerprint

        key = Ed25519PrivateKey.generate()
        pem = key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode()
        fingerprint = public_key_fingerprint(pem)
        with (
            patch("helpers.export_import.get_workspace_by_id", return_value=workspace) as get_ws,
            patch("helpers.export_import.DbManager.find_records", return_value=[]),
            patch("helpers.export_import.os.environ.get", return_value=""),
            patch("federation.core.get_or_create_instance_keypair", return_value=(key, pem)),
            patch("federation.core.get_instance_id", return_value=fingerprint),
            caplog.at_level(logging.DEBUG, logger="syncbot"),
        ):
            payload = build_migration_export(42)

        get_ws.assert_called_with(42)
        assert payload["workspace"] == {"team_id": "T1", "workspace_name": "Workspace A"}
        assert payload["syncs"] == []
        assert payload["groups"] == []
        assert payload["version"] == 1
        assert payload["syncbot_version"]
        assert "federation_file_parts" not in payload
        assert payload["source_instance"] == {"instance_id": fingerprint, "public_key": pem}
        assert "webhook_url" not in payload["source_instance"]
        assert "connection_code" not in payload["source_instance"]
        assert any(
            record.message == "migration_export" and record.__dict__.get("team_id") == "T1" for record in caplog.records
        )

    def test_raises_when_workspace_missing(self):
        with (
            patch("helpers.export_import.get_workspace_by_id", return_value=None),
            pytest.raises(ValueError, match="Workspace not found"),
        ):
            build_migration_export(99)

    def test_exports_channel_participation_and_posting_identity(self):
        workspace = SimpleNamespace(id=42, team_id="T1", workspace_name="Workspace A", deleted_at=None)
        sync = SimpleNamespace(
            id=9,
            uid="sync-uid",
            group_id=3,
            title="Announcements",
            sync_mode="group",
        )
        group = SimpleNamespace(id=3, uid="group-uid", name="Partner Org", status="active")
        membership = SimpleNamespace(group_id=3, role="owner", workspace_id=42, status="active", deleted_at=None)
        sync_channel = SimpleNamespace(
            id=7,
            sync_id=9,
            workspace_id=42,
            channel_id="C1",
            status="active",
            publishes=True,
            subscribes=False,
            reaction_style="direct_only",
        )
        post_meta = SimpleNamespace(
            post_id="post-1",
            ts=123.0,
            kind="message",
            parent_post_id=None,
            reaction=None,
            source_user_id="U1",
            source_workspace_id=42,
            posted_as_user_id="U2",
        )

        def find_records(model, _filters):
            return {
                "WorkspaceGroupMember": [membership],
                "SyncChannel": [sync_channel],
                "PostMeta": [post_meta],
                "UserDirectory": [],
                "UserMapping": [],
            }.get(model.__name__, [])

        with (
            patch("helpers.export_import.get_workspace_by_id", return_value=workspace),
            patch("helpers.export_import.DbManager.find_records", side_effect=find_records),
            patch(
                "helpers.export_import.DbManager.get_record",
                side_effect=lambda model, *args, **kwargs: group if model.__name__ == "WorkspaceGroup" else sync,
            ),
        ):
            payload = build_migration_export(42)

        assert payload["sync_channels"] == [
            {
                "sync_uid": "sync-uid",
                "team_id": "T1",
                "channel_id": "C1",
                "channel_name": None,
                "status": "active",
                "publishes": True,
                "subscribes": False,
                "reaction_style": "direct_only",
                "synced_by_user_id": None,
                "is_private": None,
            }
        ]
        assert payload["post_meta"]["sync-uid:C1"][0]["posted_as_user_id"] == "U2"

    def test_exports_post_meta_for_peer_stub_channels(self):
        local = SimpleNamespace(id=42, team_id="T1", workspace_name="Workspace A", deleted_at=None)
        peer = SimpleNamespace(id=99, team_id="T-PEER", workspace_name="Workspace B", deleted_at=None)
        sync = SimpleNamespace(id=9, uid="sync-uid", group_id=3, title="Announcements", sync_mode="group")
        group = SimpleNamespace(id=3, uid="group-uid", name="Partner Org", status="active")
        local_sc = SimpleNamespace(
            id=7,
            sync_id=9,
            workspace_id=42,
            channel_id="C1",
            channel_name="announcements",
            status="active",
            publishes=True,
            subscribes=True,
            reaction_style="hybrid",
        )
        peer_sc = SimpleNamespace(
            id=8,
            sync_id=9,
            workspace_id=99,
            channel_id="C-PEER",
            channel_name="general",
            status="active",
            publishes=True,
            subscribes=True,
            reaction_style="hybrid",
        )
        local_pm = SimpleNamespace(
            post_id="post-1",
            ts="10.000001",
            kind="message",
            parent_post_id=None,
            reaction=None,
            source_user_id="U1",
            source_workspace_id=42,
            posted_as_user_id=None,
        )
        peer_pm = SimpleNamespace(
            post_id="post-1",
            ts="20.000002",
            kind="message",
            parent_post_id=None,
            reaction=None,
            source_user_id="U1",
            source_workspace_id=42,
            posted_as_user_id="U-PEER",
        )

        def find_records(model, filters):
            name = model.__name__
            if name == "WorkspaceGroupMember":
                return [SimpleNamespace(group_id=3, role="owner", workspace_id=42, status="active", deleted_at=None)]
            if name == "SyncChannel":
                return [local_sc, peer_sc]
            if name == "PostMeta":
                clause = filters[0]
                sync_channel_id = clause.right.value
                return [local_pm] if sync_channel_id == 7 else [peer_pm]
            return []

        def get_ws(pk):
            return local if pk == 42 else peer

        with (
            patch("helpers.export_import.get_workspace_by_id", side_effect=get_ws),
            patch("helpers.export_import.DbManager.find_records", side_effect=find_records),
            patch(
                "helpers.export_import.DbManager.get_record",
                side_effect=lambda model, *args, **kwargs: group if model.__name__ == "WorkspaceGroup" else sync,
            ),
        ):
            payload = build_migration_export(42)

        channels = {(row["team_id"], row["channel_id"]) for row in payload["sync_channels"]}
        assert channels == {("T1", "C1"), ("T-PEER", "C-PEER")}
        by_channel = {row["channel_id"]: row for row in payload["sync_channels"]}
        assert by_channel["C1"]["channel_name"] == "announcements"
        assert by_channel["C-PEER"]["channel_name"] == "general"
        assert payload["post_meta"]["sync-uid:C1"][0]["ts"] == "10.000001"
        assert payload["post_meta"]["sync-uid:C-PEER"][0]["ts"] == "20.000002"
        assert payload["post_meta"]["sync-uid:C-PEER"][0]["posted_as_user_id"] == "U-PEER"


def test_import_defaults_legacy_channel_participation_and_posting_identity():
    data = {
        "workspace": {"team_id": "T1"},
        "groups": [{"uid": "group-uid", "name": "Group", "role": "owner"}],
        "syncs": [{"uid": "sync-uid", "group_uid": "group-uid", "title": "S1"}],
        "sync_channels": [
            {"sync_uid": "sync-uid", "channel_id": "C1"},
            {
                "sync_uid": "sync-uid",
                "channel_id": "C2",
                "publishes": False,
                "subscribes": True,
                "reaction_style": "direct_only",
                "reaction_direction": "receive",
            },
        ],
        "post_meta": {"sync-uid:C1": [{"post_id": "post-1", "ts": 100.0}]},
    }
    created = []

    def capture(record):
        if type(record).__name__ == "WorkspaceGroup":
            record.id = 3
        elif type(record).__name__ == "Sync":
            record.id = 1
        elif type(record).__name__ == "SyncChannel":
            record.id = 2
        created.append(record)
        return record

    with (
        patch("helpers.export_import.DbManager.find_records", return_value=[]),
        patch("helpers.export_import.DbManager.create_record", side_effect=capture),
        patch("helpers.export_import.DbManager.delete_records"),
        patch("helpers.export_import.get_workspace_by_id", return_value=None),
    ):
        import_migration_data(data, 42, 3, team_id_to_workspace_id={"T1": 42})

    sync_channels = [row for row in created if type(row).__name__ == "SyncChannel"]
    sync_channel = next(row for row in sync_channels if row.channel_id == "C1")
    configured_channel = next(row for row in sync_channels if row.channel_id == "C2")
    post_meta = next(row for row in created if type(row).__name__ == "PostMeta")
    assert sync_channel.publishes is True
    assert sync_channel.subscribes is True
    assert configured_channel.publishes is False
    assert configured_channel.subscribes is True
    assert configured_channel.reaction_style == "direct_only"
    assert post_meta.posted_as_user_id is None


def test_import_skips_unknown_peer_group_members():
    data = {
        "source_instance": {"instance_id": "peer-fp"},
        "workspace": {"team_id": "T1"},
        "groups": [
            {
                "uid": "group-uid",
                "name": "Group",
                "role": "owner",
                "member_team_ids": ["T-PEER"],
                "member_workspaces": [{"team_id": "T-PEER", "workspace_name": "Workspace B"}],
            }
        ],
        "syncs": [{"uid": "sync-uid", "group_uid": "group-uid", "title": "S1"}],
        "sync_channels": [
            {"sync_uid": "sync-uid", "team_id": "T1", "channel_id": "C1"},
            {"sync_uid": "sync-uid", "team_id": "T-PEER", "channel_id": "C-PEER"},
        ],
        "post_meta": {},
    }
    created = []

    def find_records(model, _filters):
        if model.__name__ == "Instance":
            return [SimpleNamespace(instance_id="peer-fp", private_key_encrypted=None)]
        return []

    def capture(record):
        if type(record).__name__ == "WorkspaceGroup":
            record.id = 3
        elif type(record).__name__ == "Sync":
            record.id = 1
        elif type(record).__name__ == "SyncChannel":
            record.id = 2
        elif type(record).__name__ == "WorkspaceGroupMember":
            record.id = 8
        created.append(record)
        return record

    with (
        patch("helpers.export_import.DbManager.find_records", side_effect=find_records),
        patch("helpers.export_import.DbManager.create_record", side_effect=capture),
        patch("helpers.export_import.DbManager.delete_records"),
        patch("helpers.workspace_kind.ensure_stub_workspace") as ensure_stub,
        patch("helpers.export_import.get_workspace_by_id", return_value=None),
    ):
        import_migration_data(data, 42, 3, team_id_to_workspace_id={"T1": 42})

    ensure_stub.assert_not_called()
    members = [row for row in created if type(row).__name__ == "WorkspaceGroupMember"]
    assert {row.workspace_id for row in members} == {42}
    channels = [row for row in created if type(row).__name__ == "SyncChannel"]
    assert {row.workspace_id for row in channels} == {42}


def test_import_without_source_instance_skips_unknown_peer_channels():
    data = {
        "workspace": {"team_id": "T1"},
        "groups": [
            {
                "uid": "group-uid",
                "name": "Group",
                "role": "owner",
                "member_team_ids": ["T-PEER"],
                "member_workspaces": [{"team_id": "T-PEER", "workspace_name": "Workspace B"}],
            }
        ],
        "syncs": [{"uid": "sync-uid", "group_uid": "group-uid", "title": "S1"}],
        "sync_channels": [
            {"sync_uid": "sync-uid", "team_id": "T1", "channel_id": "C1"},
            {"sync_uid": "sync-uid", "team_id": "T-PEER", "channel_id": "C-PEER"},
        ],
        "post_meta": {
            "sync-uid:C-PEER": [{"post_id": "PARENT", "ts": "10.000001"}],
        },
    }
    created = []

    def find_records(model, _filters):
        if model.__name__ == "Instance":
            return [
                SimpleNamespace(
                    instance_id="peer-fp",
                    private_key_encrypted=None,
                    status="active",
                    trust_status="trusted",
                )
            ]
        return []

    def capture(record):
        name = type(record).__name__
        if name == "WorkspaceGroup":
            record.id = 3
        elif name == "Sync":
            record.id = 1
        elif name == "SyncChannel":
            record.id = 2
        elif name == "WorkspaceGroupMember":
            record.id = 8
        created.append(record)
        return record

    with (
        patch("helpers.export_import.DbManager.find_records", side_effect=find_records),
        patch("helpers.export_import.DbManager.create_record", side_effect=capture),
        patch("helpers.export_import.DbManager.delete_records"),
        patch("helpers.workspace_kind.ensure_stub_workspace") as ensure_stub,
        patch("helpers.export_import.get_workspace_by_id", return_value=None),
    ):
        import_migration_data(data, 42, 3, team_id_to_workspace_id={"T1": 42})

    ensure_stub.assert_not_called()
    posts = [row for row in created if type(row).__name__ == "PostMeta"]
    assert posts == []


def test_import_unknown_source_instance_does_not_guess_sole_trusted_peer():
    from helpers.export_import import _peer_instance_id_from_migration

    with (
        patch("helpers.export_import.DbManager.find_records", return_value=[]),
        patch("helpers.export_import._sole_trusted_peer_instance_id") as sole,
    ):
        assert _peer_instance_id_from_migration({"source_instance": {"instance_id": "other-fp"}}) is None
        sole.assert_not_called()


def test_import_post_meta_onto_peer_stub_channels():
    data = {
        "source_instance": {"instance_id": "peer-fp"},
        "workspace": {"team_id": "T1"},
        "groups": [
            {
                "uid": "group-uid",
                "name": "Group",
                "role": "owner",
                "member_team_ids": ["T-PEER"],
                "member_workspaces": [{"team_id": "T-PEER", "workspace_name": "Workspace B"}],
            }
        ],
        "syncs": [{"uid": "sync-uid", "group_uid": "group-uid", "title": "S1"}],
        "sync_channels": [
            {"sync_uid": "sync-uid", "team_id": "T1", "channel_id": "C1", "channel_name": "announcements"},
            {"sync_uid": "sync-uid", "team_id": "T-PEER", "channel_id": "C-PEER", "channel_name": "general"},
        ],
        "post_meta": {
            "sync-uid:C1": [{"post_id": "post-1", "ts": "10.000001"}],
            "sync-uid:C-PEER": [{"post_id": "post-1", "ts": "20.000002", "posted_as_user_id": "U-PEER"}],
        },
    }
    created = []
    stub = SimpleNamespace(id=99, team_id="T-PEER")
    next_id = {"WorkspaceGroup": 3, "Sync": 1, "SyncChannel": 10, "WorkspaceGroupMember": 8}

    def find_records(model, _filters):
        if model.__name__ == "Instance":
            return [SimpleNamespace(instance_id="peer-fp", private_key_encrypted=None)]
        if model.__name__ == "Workspace":
            return [stub]
        return []

    def capture(record):
        name = type(record).__name__
        if name in next_id:
            record.id = next_id[name]
            next_id[name] += 1
        created.append(record)
        return record

    with (
        patch("helpers.export_import.DbManager.find_records", side_effect=find_records),
        patch("helpers.export_import.DbManager.create_record", side_effect=capture),
        patch("helpers.export_import.DbManager.delete_records"),
        patch("helpers.workspace_kind.ensure_stub_workspace") as ensure_stub,
        patch("helpers.export_import.get_workspace_by_id", return_value=None),
    ):
        import_migration_data(data, 42, 3, team_id_to_workspace_id={"T1": 42})
    ensure_stub.assert_not_called()

    channels = {row.channel_id: row for row in created if type(row).__name__ == "SyncChannel"}
    post_metas = [row for row in created if type(row).__name__ == "PostMeta"]
    assert channels["C1"].channel_name == "announcements"
    assert channels["C-PEER"].channel_name == "general"
    assert {row.sync_channel_id for row in post_metas} == {channels["C1"].id, channels["C-PEER"].id}
    remote = next(row for row in post_metas if row.sync_channel_id == channels["C-PEER"].id)
    assert remote.post_id == "post-1"
    assert str(remote.ts) == "20.000002"
    assert remote.posted_as_user_id == "U-PEER"


@pytest.fixture
def real_db(tmp_path):
    import db as db_mod
    from db import initialize_database
    from helpers._cache import clear_all_caches

    url = f"sqlite:///{tmp_path / 'migration_export.db'}"
    old_engine = db_mod.GLOBAL_ENGINE
    old_session = db_mod.GLOBAL_SESSION
    old_schema = db_mod.GLOBAL_SCHEMA
    with patch.dict(os.environ, {"DATABASE_BACKEND": "sqlite", "DATABASE_URL": url}, clear=False):
        try:
            db_mod.GLOBAL_ENGINE = None
            db_mod.GLOBAL_SESSION = None
            db_mod.GLOBAL_SCHEMA = None
            initialize_database()
            from federation import core as federation_core

            federation_core._INSTANCE_ID = None
            federation_core._cached_private_key = None
            federation_core._cached_public_pem = None
            federation_core.get_or_create_instance_keypair()
            clear_all_caches()
            yield
        finally:
            if db_mod.GLOBAL_ENGINE is not None:
                db_mod.GLOBAL_ENGINE.dispose()
            db_mod.GLOBAL_ENGINE = old_engine
            db_mod.GLOBAL_SESSION = old_session
            db_mod.GLOBAL_SCHEMA = old_schema
            clear_all_caches()


def _export_rows():
    from db import DbManager, schemas
    from federation.core import get_instance_id

    now = datetime.now(UTC).replace(tzinfo=None)
    local = DbManager.create_record(
        schemas.Workspace(team_id="TA", workspace_name="Workspace A", instance_id=get_instance_id())
    )
    other = DbManager.create_record(
        schemas.Workspace(team_id="TB", workspace_name="Workspace B", instance_id=get_instance_id())
    )
    live = DbManager.create_record(
        schemas.WorkspaceGroup(
            name="Live Group", invite_code="LIVE-0001", status="active", created_at=now, uid="live-uid"
        )
    )
    disbanded = DbManager.create_record(
        schemas.WorkspaceGroup(
            name="Partner Org", invite_code="DEAD-0001", status="disbanded", created_at=now, uid="dead-uid"
        )
    )
    left = DbManager.create_record(
        schemas.WorkspaceGroup(
            name="Left Group", invite_code="LEFT-0001", status="active", created_at=now, uid="left-uid"
        )
    )
    DbManager.create_record(
        schemas.WorkspaceGroupMember(
            group_id=live.id, workspace_id=local.id, status="active", role="owner", joined_at=now
        )
    )
    DbManager.create_record(
        schemas.WorkspaceGroupMember(
            group_id=live.id,
            workspace_id=other.id,
            status="active",
            role="member",
            joined_at=now,
            deleted_at=now,
        )
    )
    DbManager.create_record(
        schemas.WorkspaceGroupMember(
            group_id=disbanded.id, workspace_id=local.id, status="active", role="owner", joined_at=now
        )
    )
    DbManager.create_record(
        schemas.WorkspaceGroupMember(
            group_id=left.id,
            workspace_id=local.id,
            status="inactive",
            role="member",
            joined_at=now,
            deleted_at=now,
        )
    )

    def add_sync(group, uid, title):
        return DbManager.create_record(schemas.Sync(uid=uid, title=title, group_id=group.id, sync_mode="group"))

    def add_channel(sync, workspace, channel_id, status="active", deleted_at=None):
        return DbManager.create_record(
            schemas.SyncChannel(
                sync_id=sync.id,
                workspace_id=workspace.id,
                channel_id=channel_id,
                channel_name=channel_id.lower(),
                status=status,
                publishes=True,
                subscribes=True,
                created_at=now,
                deleted_at=deleted_at,
            )
        )

    live_sync = add_sync(live, "live-sync", "Announcements")
    add_channel(live_sync, local, "C-LIVE")
    add_channel(live_sync, other, "C-OTHER")
    add_channel(live_sync, local, "C-PAUSE", status="paused")
    add_channel(live_sync, local, "C-GONE", status="deleted")
    dead_sync = add_sync(disbanded, "dead-sync", "Old")
    dead_channel = add_channel(dead_sync, local, "C-DEAD")
    left_sync = add_sync(left, "left-sync", "Gone")
    add_channel(left_sync, local, "C-LEFT")
    DbManager.create_record(
        schemas.PostMeta(post_id="P-LIVE", sync_channel_id=dead_channel.id, ts=Decimal("1700000000.000100"))
    )
    DbManager.create_record(
        schemas.UserMapping(
            source_workspace_id=local.id,
            source_user_id="U1",
            target_workspace_id=other.id,
            target_user_id="U2",
            map_method="manual",
            mapped_at=now,
            group_id=disbanded.id,
        )
    )
    DbManager.create_record(
        schemas.UserMapping(
            source_workspace_id=local.id,
            source_user_id="U3",
            target_workspace_id=other.id,
            target_user_id="U4",
            map_method="manual",
            mapped_at=now,
            group_id=live.id,
        )
    )
    return local


def test_export_signature_names_this_install(real_db):
    import base64
    import copy

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    from federation.core import get_instance_id, public_key_fingerprint
    from helpers.export_import import canonical_json_dumps, verify_migration_signature

    local = _export_rows()
    payload = build_migration_export(local.id)
    source = payload["source_instance"]
    assert source["instance_id"] == get_instance_id()
    assert public_key_fingerprint(source["public_key"]) == source["instance_id"]
    assert "webhook_url" not in source
    assert "connection_code" not in source
    assert verify_migration_signature(payload)

    changed = copy.deepcopy(payload)
    changed["groups"][0]["name"] = "Renamed"
    assert verify_migration_signature(changed) is False

    other = Ed25519PrivateKey.generate()
    other_pem = other.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode()
    resigned = copy.deepcopy(payload)
    resigned["source_instance"]["public_key"] = other_pem
    raw = canonical_json_dumps({key: value for key, value in resigned.items() if key != "signature"}).decode()
    resigned["signature"] = base64.b64encode(other.sign(raw.encode())).decode()
    assert verify_migration_signature(resigned) is False
    assert verify_migration_signature({"version": 1, "groups": []}) is False


def test_review_skips_the_integrity_warning_without_a_source_instance():
    from handlers.export_import import handle_data_migration_submit_ack

    payload = {"version": 1, "workspace": {"team_id": "T1"}, "groups": []}
    workspace = SimpleNamespace(id=1, team_id="T1", workspace_name="Workspace A")
    body = {
        "user": {"id": "U1"},
        "team": {"id": "T1"},
        "view": {
            "state": {
                "values": {
                    "data_migration_json_input": {
                        "data_migration_json_input": {
                            "files": [{"url_private_download": "https://files.slack.com/m.json"}],
                        }
                    }
                }
            }
        },
    }
    with (
        patch("handlers.export_import._is_admin", return_value=True),
        patch("handlers.export_import._download_uploaded_file", return_value=(json.dumps(payload), None)),
        patch("handlers.export_import.helpers.get_workspace_record", return_value=workspace),
        patch("handlers.export_import.DbManager.find_records", return_value=[]),
        patch("handlers.export_import.helpers.federation_enabled", return_value=True),
        patch("helpers._cache._cache_set"),
    ):
        resp = handle_data_migration_submit_ack(body, MagicMock(token="xoxb"), {})
    assert "Integrity check failed" not in str(resp["view"])


def test_export_omits_a_disbanded_group_and_a_group_this_workspace_left(real_db):
    from helpers.export_import import build_migration_export

    local = _export_rows()
    payload = build_migration_export(local.id)
    assert [group["uid"] for group in payload["groups"]] == ["live-uid"]
    assert payload["groups"][0]["member_team_ids"] == ["TB"]
    channels = {row["channel_id"] for row in payload["sync_channels"]}
    assert channels == {"C-LIVE", "C-OTHER", "C-PAUSE"}
    assert not any(key.startswith("dead-sync:") or key.startswith("left-sync:") for key in payload["post_meta"])
    assert [row["group_uid"] for row in payload["user_mappings"]] == ["live-uid"]
    assert {row["uid"] for row in payload["syncs"]} == {"live-sync"}


def test_import_does_not_revive_a_disbanded_group(real_db):
    from db import DbManager, schemas
    from helpers.export_import import import_migration_data

    local = _export_rows()
    disbanded = DbManager.find_records(schemas.WorkspaceGroup, [schemas.WorkspaceGroup.uid == "dead-uid"])[0]
    membership = DbManager.find_records(
        schemas.WorkspaceGroupMember,
        [schemas.WorkspaceGroupMember.group_id == disbanded.id],
    )[0]
    DbManager.update_records(
        schemas.WorkspaceGroupMember,
        [schemas.WorkspaceGroupMember.id == membership.id],
        {
            schemas.WorkspaceGroupMember.status: "inactive",
            schemas.WorkspaceGroupMember.deleted_at: datetime.now(UTC),
        },
    )
    channel = DbManager.find_records(
        schemas.SyncChannel,
        [schemas.SyncChannel.channel_id == "C-DEAD"],
    )[0]
    DbManager.update_records(
        schemas.SyncChannel,
        [schemas.SyncChannel.id == channel.id],
        {schemas.SyncChannel.status: "deleted", schemas.SyncChannel.deleted_at: datetime.now(UTC)},
    )
    data = {
        "workspace": {"team_id": "TA"},
        "groups": [
            {"uid": "dead-uid", "name": "Partner Org", "role": "owner"},
            {"uid": "fresh-uid", "name": "Live Group", "role": "member"},
        ],
        "syncs": [
            {"uid": "dead-sync", "group_uid": "dead-uid", "title": "Old"},
            {"uid": "fresh-sync", "group_uid": "fresh-uid", "title": "New"},
        ],
        "sync_channels": [
            {"sync_uid": "dead-sync", "team_id": "TA", "channel_id": "C-DEAD", "status": "active"},
            {"sync_uid": "fresh-sync", "team_id": "TA", "channel_id": "C-FRESH", "status": "active"},
        ],
        "post_meta": {},
        "user_mappings": [
            {
                "source_team_id": "TA",
                "target_team_id": "TB",
                "source_user_id": "U9",
                "target_user_id": "U8",
                "map_method": "manual",
                "group_uid": "dead-uid",
            }
        ],
    }
    import_migration_data(data, local.id, team_id_to_workspace_id={"TA": local.id, "TB": 2})
    kept = DbManager.find_records(
        schemas.WorkspaceGroupMember,
        [schemas.WorkspaceGroupMember.group_id == disbanded.id],
    )[0]
    assert kept.status == "inactive"
    assert kept.deleted_at is not None
    dead_channel = DbManager.find_records(schemas.SyncChannel, [schemas.SyncChannel.channel_id == "C-DEAD"])[0]
    assert dead_channel.deleted_at is not None
    assert dead_channel.status == "deleted"
    fresh = DbManager.find_records(schemas.WorkspaceGroup, [schemas.WorkspaceGroup.uid == "fresh-uid"])
    assert fresh and fresh[0].status == "active"
    assert DbManager.find_records(schemas.SyncChannel, [schemas.SyncChannel.channel_id == "C-FRESH"])
    revived_maps = DbManager.find_records(
        schemas.UserMapping,
        [schemas.UserMapping.source_user_id == "U9"],
    )
    assert revived_maps == []


def test_confirm_import_does_not_add_a_status_line():
    from handlers.export_import import _migration_overview_blocks

    blocks = _migration_overview_blocks(
        {
            "groups": [{"uid": "live-uid", "name": "Partner Org"}],
            "syncs": [{"uid": "live-sync", "group_uid": "live-uid", "title": "Announcements"}],
            "sync_channels": [{"sync_uid": "live-sync", "channel_id": "C1"}],
            "user_mappings": [],
            "post_meta": {},
        }
    )
    text = "\n".join(getattr(block, "label", "") or "" for block in blocks)
    assert "Partner Org" in text
    assert "Deleted Status" not in text
    assert "Disbanded" not in text
    assert "\nStatus:" not in text
