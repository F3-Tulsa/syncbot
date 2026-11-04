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
        import_migration_data(data, 42, 3, team_id_to_workspace_id={"T1": 42, "T-PEER": 99})
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


def test_review_refuses_a_file_with_no_signature():
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
        patch("helpers._cache._cache_set") as cache_set,
    ):
        resp = handle_data_migration_submit_ack(body, MagicMock(token="xoxb"), {})
    assert resp["response_action"] == "errors"
    assert "can't check this file" in resp["errors"]["data_migration_json_input"]
    assert "1.7.3" in resp["errors"]["data_migration_json_input"]
    cache_set.assert_not_called()


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


def _two_local_workspaces():
    from db import DbManager, schemas
    from federation.core import get_instance_id

    now = datetime.now(UTC).replace(tzinfo=None)
    instance_id = get_instance_id()
    workspace_a = DbManager.create_record(
        schemas.Workspace(team_id="TA", workspace_name="Workspace A", instance_id=instance_id)
    )
    workspace_b = DbManager.create_record(
        schemas.Workspace(team_id="TB", workspace_name="Workspace B", instance_id=instance_id)
    )
    group = DbManager.create_record(
        schemas.WorkspaceGroup(
            name="B Group",
            invite_code="BGRP-0001",
            status="active",
            created_at=now,
            uid="b-group",
        )
    )
    DbManager.create_record(
        schemas.WorkspaceGroupMember(
            group_id=group.id,
            workspace_id=workspace_b.id,
            status="active",
            role="owner",
            joined_at=now,
        )
    )
    sync = DbManager.create_record(schemas.Sync(uid="b-sync", title="B Sync", group_id=group.id, sync_mode="group"))
    DbManager.create_record(
        schemas.SyncChannel(
            sync_id=sync.id,
            workspace_id=workspace_b.id,
            channel_id="CGEN",
            channel_name="general",
            status="active",
            publishes=True,
            subscribes=True,
            created_at=now,
        )
    )
    return workspace_a, workspace_b, group, sync


def test_import_leaves_another_local_workspace_unchanged(real_db):
    from db import DbManager, schemas
    from helpers.export_import import import_migration_data

    workspace_a, workspace_b, b_sync_group, b_sync = _two_local_workspaces()
    import_migration_data(
        {
            "workspace": {"team_id": "TA"},
            "groups": [
                {
                    "uid": "a-group",
                    "name": "A Group",
                    "role": "owner",
                    "member_workspaces": [{"team_id": "TB", "role": "member", "workspace_name": "Workspace B"}],
                }
            ],
            "syncs": [{"uid": "a-sync", "group_uid": "a-group", "title": "A Sync"}],
            "sync_channels": [{"sync_uid": "a-sync", "team_id": "TB", "channel_id": "CGEN", "channel_name": "general"}],
            "user_mappings": [],
        },
        workspace_a.id,
        team_id_to_workspace_id={"TA": workspace_a.id},
    )
    a_group = DbManager.find_records(schemas.WorkspaceGroup, [schemas.WorkspaceGroup.uid == "a-group"])[0]
    members = DbManager.find_records(
        schemas.WorkspaceGroupMember,
        [
            schemas.WorkspaceGroupMember.group_id == a_group.id,
            schemas.WorkspaceGroupMember.deleted_at.is_(None),
        ],
    )
    assert {row.workspace_id for row in members} == {workspace_a.id}
    a_sync = DbManager.find_records(schemas.Sync, [schemas.Sync.uid == "a-sync"])[0]
    assert (
        DbManager.find_records(
            schemas.SyncChannel,
            [
                schemas.SyncChannel.sync_id == a_sync.id,
                schemas.SyncChannel.workspace_id == workspace_b.id,
            ],
        )
        == []
    )
    original = DbManager.find_records(schemas.SyncChannel, [schemas.SyncChannel.channel_id == "CGEN"])[0]
    assert original.sync_id == b_sync.id
    assert original.workspace_id == workspace_b.id
    assert b_sync_group.id == b_sync.group_id


def test_import_skips_a_group_this_workspace_is_not_in(real_db):
    from db import DbManager, schemas
    from helpers.export_import import import_migration_data

    workspace_a, workspace_b, group, _sync = _two_local_workspaces()
    import_migration_data(
        {
            "workspace": {"team_id": "TA"},
            "groups": [{"uid": "b-group", "name": "B Group", "role": "owner"}],
            "syncs": [{"uid": "b-sync", "group_uid": "b-group", "title": "Taken"}],
            "sync_channels": [],
        },
        workspace_a.id,
        team_id_to_workspace_id={"TA": workspace_a.id},
    )
    members = DbManager.find_records(
        schemas.WorkspaceGroupMember,
        [schemas.WorkspaceGroupMember.group_id == group.id],
    )
    assert [row.workspace_id for row in members] == [workspace_b.id]
    assert members[0].role == "owner"
    kept = DbManager.find_records(schemas.Sync, [schemas.Sync.uid == "b-sync"])[0]
    assert kept.title == "B Sync"
    assert kept.group_id == group.id


def test_import_keeps_an_existing_sync_in_its_group(real_db):
    from db import DbManager, schemas
    from helpers.export_import import import_migration_data

    workspace_a, _workspace_b, group, _sync = _two_local_workspaces()
    import_migration_data(
        {
            "workspace": {"team_id": "TA"},
            "groups": [{"uid": "a-group", "name": "A Group", "role": "owner"}],
            "syncs": [{"uid": "b-sync", "group_uid": "a-group", "title": "Moved"}],
            "sync_channels": [{"sync_uid": "b-sync", "team_id": "TB", "channel_id": "CGEN"}],
        },
        workspace_a.id,
        team_id_to_workspace_id={"TA": workspace_a.id},
    )
    kept = DbManager.find_records(schemas.Sync, [schemas.Sync.uid == "b-sync"])[0]
    assert kept.group_id == group.id
    assert kept.title == "B Sync"
    a_sync_channels = DbManager.find_records(schemas.Sync, [schemas.Sync.uid == "a-group"])
    assert a_sync_channels == []


def test_import_skips_a_mapping_onto_another_local_workspace(real_db):
    from db import DbManager, schemas
    from helpers.export_import import import_migration_data

    workspace_a, workspace_b, _group, _sync = _two_local_workspaces()
    import_migration_data(
        {
            "workspace": {"team_id": "TA"},
            "groups": [{"uid": "a-group", "name": "A Group", "role": "member"}],
            "syncs": [],
            "sync_channels": [],
            "user_mappings": [
                {
                    "source_team_id": "TA",
                    "target_team_id": "TB",
                    "source_user_id": "UA",
                    "target_user_id": "UB",
                    "map_method": "manual",
                    "group_uid": "a-group",
                }
            ],
        },
        workspace_a.id,
        team_id_to_workspace_id={"TA": workspace_a.id},
    )
    assert (
        DbManager.find_records(
            schemas.UserMapping,
            [schemas.UserMapping.target_workspace_id == workspace_b.id],
        )
        == []
    )


def test_import_writes_a_stub_already_in_the_group(real_db):
    from db import DbManager, schemas
    from federation.core import get_instance_id
    from helpers.export_import import import_migration_data

    now = datetime.now(UTC).replace(tzinfo=None)
    workspace_a = DbManager.create_record(
        schemas.Workspace(team_id="TA", workspace_name="Workspace A", instance_id=get_instance_id())
    )
    peer = DbManager.create_record(
        schemas.Instance(
            instance_id="b" * 64,
            public_key="pem",
            status="active",
            trust_status="trusted",
            name="Partner Org",
            created_at=now,
        )
    )
    stub = DbManager.create_record(
        schemas.Workspace(team_id="TC", workspace_name="Workspace C", instance_id=peer.instance_id)
    )
    group = DbManager.create_record(
        schemas.WorkspaceGroup(
            name="A Group",
            invite_code="AGRP-0001",
            status="active",
            created_at=now,
            uid="a-group",
        )
    )
    DbManager.create_record(
        schemas.WorkspaceGroupMember(
            group_id=group.id,
            workspace_id=workspace_a.id,
            status="active",
            role="owner",
            joined_at=now,
        )
    )
    import_migration_data(
        {
            "workspace": {"team_id": "TA"},
            "groups": [
                {
                    "uid": "a-group",
                    "name": "A Group",
                    "role": "owner",
                    "member_workspaces": [{"team_id": "TC", "role": "member", "workspace_name": "Workspace C"}],
                }
            ],
            "syncs": [{"uid": "a-sync", "group_uid": "a-group", "title": "A Sync"}],
            "sync_channels": [
                {"sync_uid": "a-sync", "team_id": "TC", "channel_id": "CSTUB", "channel_name": "general"}
            ],
            "post_meta": {"a-sync:CSTUB": [{"post_id": "P1", "ts": "10.000001"}]},
        },
        workspace_a.id,
        team_id_to_workspace_id={"TA": workspace_a.id, "TC": stub.id},
    )
    members = DbManager.find_records(
        schemas.WorkspaceGroupMember,
        [
            schemas.WorkspaceGroupMember.group_id == group.id,
            schemas.WorkspaceGroupMember.deleted_at.is_(None),
        ],
    )
    assert {row.workspace_id for row in members} == {workspace_a.id, stub.id}
    channel = DbManager.find_records(schemas.SyncChannel, [schemas.SyncChannel.channel_id == "CSTUB"])[0]
    assert channel.workspace_id == stub.id
    assert DbManager.find_records(schemas.PostMeta, [schemas.PostMeta.sync_channel_id == channel.id])


def test_prepare_maps_only_this_workspace_and_live_stubs(real_db):
    from db import DbManager, schemas
    from federation.core import get_instance_id
    from handlers.export_import import _data_migration_prepare

    now = datetime.now(UTC).replace(tzinfo=None)
    workspace_a, workspace_b, _group, _sync = _two_local_workspaces()
    peer = DbManager.create_record(
        schemas.Instance(
            instance_id="c" * 64,
            public_key="pem",
            status="active",
            trust_status="trusted",
            name="Partner Org",
            created_at=now,
        )
    )
    stub = DbManager.create_record(
        schemas.Workspace(team_id="TC", workspace_name="Workspace C", instance_id=peer.instance_id)
    )
    DbManager.create_record(
        schemas.Workspace(
            team_id="TD",
            workspace_name="Workspace D",
            instance_id=get_instance_id(),
            deleted_at=now,
        )
    )
    DbManager.create_record(
        schemas.Workspace(
            team_id="TE",
            workspace_name="Workspace E",
            instance_id=peer.instance_id,
            deleted_at=now,
        )
    )
    body = {
        "user": {"id": "U1"},
        "team": {"id": "TA"},
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
    payload = {"version": 1, "workspace": {"team_id": "TA"}, "groups": []}
    with (
        patch("handlers.export_import._is_admin", return_value=True),
        patch("handlers.export_import._download_uploaded_file", return_value=(json.dumps(payload), None)),
        patch("handlers.export_import.helpers.get_workspace_record", return_value=workspace_a),
    ):
        err, _data, group_id, team_map, _workspace = _data_migration_prepare(body, MagicMock(token="xoxb"), {})
    assert err is None
    assert group_id == 0
    assert set(team_map) == {"TA", "TC"}
    assert team_map["TA"] == workspace_a.id
    assert team_map["TC"] == stub.id
    assert workspace_b.id not in team_map.values()


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
    assert "• `Partner Org`: 1 Sync (`Announcements`), 1 Channel, 0 messages" in text
    assert "Deleted Status" not in text
    assert "Disbanded" not in text
    assert "\nStatus:" not in text
    assert "people are mapped" not in text


def test_overview_uses_plain_counts():
    from handlers.export_import import _migration_overview_blocks

    channels = [{"sync_uid": "daily" if index < 3 else "news", "channel_id": f"C{index}"} for index in range(6)]
    blocks = _migration_overview_blocks(
        {
            "groups": [
                {"uid": "live", "name": "Live Group"},
                {"uid": "empty", "name": "Empty Group"},
            ],
            "syncs": [
                {"uid": "daily", "group_uid": "live", "title": "Daily"},
                {"uid": "news", "group_uid": "live", "title": "Announcements"},
            ],
            "sync_channels": channels,
            "user_mappings": [{"source_user_id": f"U{index}"} for index in range(12)],
            "post_meta": {"daily:C0": [{"kind": "message", "post_id": str(index)} for index in range(1204)]},
        }
    )
    text = "\n".join(getattr(block, "label", "") or "" for block in blocks)
    assert "*What's in it*" in text
    assert "• `Live Group`: 2 Syncs (`Daily`, `Announcements`), 6 Channels, 1,204 messages" in text
    assert "• `Empty Group`: no Syncs" in text
    assert text.count("12 people are mapped between Workspaces.") == 1


def test_overview_uses_singular_words_and_says_when_there_are_no_groups():
    from handlers.export_import import _migration_overview_blocks

    one = _migration_overview_blocks(
        {
            "groups": [{"uid": "live", "name": "Live Group"}],
            "syncs": [{"uid": "daily", "group_uid": "live", "title": "Daily"}],
            "sync_channels": [{"sync_uid": "daily", "channel_id": "C1"}],
            "user_mappings": [{"source_user_id": "U1"}],
            "post_meta": {"daily:C1": [{"kind": "message", "post_id": "p1"}]},
        }
    )
    text = "\n".join(getattr(block, "label", "") or "" for block in one)
    assert "• `Live Group`: 1 Sync (`Daily`), 1 Channel, 1 message" in text
    assert "1 person is mapped between Workspaces." in text

    empty = _migration_overview_blocks({"groups": [], "syncs": [], "sync_channels": [], "user_mappings": []})
    empty_text = "\n".join(getattr(block, "label", "") or "" for block in empty)
    assert "No Groups." in empty_text
    assert "•" not in empty_text
    assert "mapped" not in empty_text


def _upload_body(payload):
    return {
        "user": {"id": "U1"},
        "team": {"id": "TA"},
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
    }, json.dumps(payload)


def _peer_signed_file(*, name="Nope", groups=None):
    import base64

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    from federation.core import public_key_fingerprint
    from helpers.export_import import canonical_json_dumps

    private = Ed25519PrivateKey.generate()
    public_pem = private.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode()
    instance_id = public_key_fingerprint(public_pem)
    payload = {
        "version": 1,
        "workspace": {"team_id": "TA", "workspace_name": "Workspace A"},
        "groups": groups if groups is not None else [{"uid": "g1", "name": "Live Group"}],
        "syncs": [],
        "sync_channels": [],
        "source_instance": {"instance_id": instance_id, "public_key": public_pem, "name": name},
        "signed_at": "2026-01-01T00:00:00Z",
    }
    raw = canonical_json_dumps({key: value for key, value in payload.items() if key != "signature"}).decode()
    payload["signature"] = base64.b64encode(private.sign(raw.encode())).decode()
    return payload, instance_id, public_pem


def _save_peer(
    instance_id, public_pem, *, name="Partner Org", primary_workspace_name=None, status="active", trust="trusted"
):
    from datetime import UTC, datetime

    from db import DbManager, schemas

    return DbManager.create_record(
        schemas.Instance(
            instance_id=instance_id,
            public_key=public_pem,
            private_key_encrypted=None,
            status=status,
            trust_status=trust,
            name=name,
            primary_workspace_name=primary_workspace_name,
            created_at=datetime.now(UTC).replace(tzinfo=None),
        )
    )


def test_import_of_this_install_opens_the_review(real_db):
    from handlers.export_import import handle_data_migration_submit_ack
    from helpers.export_import import build_migration_export, migration_file_status

    local = _export_rows()
    payload = build_migration_export(local.id)
    assert migration_file_status(payload)[0] == "this_install"
    body, text = _upload_body(payload)
    with (
        patch("handlers.export_import._is_admin", return_value=True),
        patch("handlers.export_import._download_uploaded_file", return_value=(text, None)),
        patch("handlers.export_import.helpers.get_workspace_record", return_value=local),
    ):
        resp = handle_data_migration_submit_ack(body, MagicMock(token="xoxb"), {})
    assert resp["response_action"] == "update"
    assert resp["view"]["title"]["text"] == "Confirm Import"


def test_import_refuses_a_changed_file(real_db):
    import copy

    from handlers.export_import import handle_data_migration_submit_ack
    from helpers.export_import import build_migration_export

    local = _export_rows()
    payload = build_migration_export(local.id)
    changed = copy.deepcopy(payload)
    changed["groups"][0]["name"] = "Renamed"
    body, text = _upload_body(changed)
    with (
        patch("handlers.export_import._is_admin", return_value=True),
        patch("handlers.export_import._download_uploaded_file", return_value=(text, None)),
        patch("handlers.export_import.helpers.get_workspace_record", return_value=local),
        patch("helpers._cache._cache_set") as cache_set,
    ):
        resp = handle_data_migration_submit_ack(body, MagicMock(token="xoxb"), {})
    assert resp["response_action"] == "errors"
    assert "was changed after it was exported" in resp["errors"]["data_migration_json_input"]
    cache_set.assert_not_called()


def test_import_names_an_unknown_signer_by_id_prefix(real_db):
    from handlers.export_import import handle_data_migration_submit_ack
    from helpers.export_import import migration_file_status

    payload, instance_id, _public = _peer_signed_file(name="Workspace A")
    assert migration_file_status(payload)[0] == "unknown"
    local = _export_rows()
    body, text = _upload_body(payload)
    with (
        patch("handlers.export_import._is_admin", return_value=True),
        patch("handlers.export_import._download_uploaded_file", return_value=(text, None)),
        patch("handlers.export_import.helpers.get_workspace_record", return_value=local),
    ):
        resp = handle_data_migration_submit_ack(body, MagicMock(token="xoxb"), {})
    message = resp["errors"]["data_migration_json_input"]
    assert f"`Connection {instance_id[:8]}`" in message
    assert "Workspace A" not in message


def test_connected_file_is_named_from_the_saved_connection(real_db):
    from helpers.export_import import migration_file_status, migration_source_name

    payload, instance_id, public_pem = _peer_signed_file(name="Nope")
    peer = _save_peer(instance_id, public_pem, name="Partner Org")
    with patch("helpers.settings.federation_enabled", return_value=True):
        kind, row = migration_file_status(payload)
    assert kind == "connected"
    assert row.instance_id == peer.instance_id
    assert migration_source_name(instance_id, row) == "Partner Org"

    from db import DbManager, schemas

    DbManager.update_records(
        schemas.Instance,
        [schemas.Instance.instance_id == instance_id],
        {schemas.Instance.name: None, schemas.Instance.primary_workspace_name: "Workspace A"},
    )
    blank = DbManager.get_record(schemas.Instance, id=instance_id)
    assert migration_source_name(instance_id, blank) == "Workspace A"


def test_import_refuses_a_peer_that_is_not_an_active_connection(real_db):
    from handlers.export_import import handle_data_migration_submit_ack

    payload, instance_id, public_pem = _peer_signed_file()
    peer = _save_peer(instance_id, public_pem, name="Partner Org", trust="untrusted")
    local = _export_rows()
    body, text = _upload_body(payload)
    with (
        patch("handlers.export_import._is_admin", return_value=True),
        patch("handlers.export_import._download_uploaded_file", return_value=(text, None)),
        patch("handlers.export_import.helpers.get_workspace_record", return_value=local),
        patch("helpers.settings.federation_enabled", return_value=True),
    ):
        refused = handle_data_migration_submit_ack(body, MagicMock(token="xoxb"), {})
    assert "not active" in refused["errors"]["data_migration_json_input"]
    assert "`Partner Org`" in refused["errors"]["data_migration_json_input"]

    from db import DbManager, schemas

    DbManager.update_records(
        schemas.Instance,
        [schemas.Instance.instance_id == peer.instance_id],
        {schemas.Instance.trust_status: "trusted", schemas.Instance.status: "inactive"},
    )
    with (
        patch("handlers.export_import._is_admin", return_value=True),
        patch("handlers.export_import._download_uploaded_file", return_value=(text, None)),
        patch("handlers.export_import.helpers.get_workspace_record", return_value=local),
        patch("helpers.settings.federation_enabled", return_value=True),
    ):
        inactive = handle_data_migration_submit_ack(body, MagicMock(token="xoxb"), {})
    assert "not active" in inactive["errors"]["data_migration_json_input"]

    DbManager.update_records(
        schemas.Instance,
        [schemas.Instance.instance_id == peer.instance_id],
        {schemas.Instance.status: "active"},
    )
    with (
        patch("handlers.export_import._is_admin", return_value=True),
        patch("handlers.export_import._download_uploaded_file", return_value=(text, None)),
        patch("handlers.export_import.helpers.get_workspace_record", return_value=local),
        patch("helpers.settings.federation_enabled", return_value=False),
    ):
        off = handle_data_migration_submit_ack(body, MagicMock(token="xoxb"), {})
    assert "Federation is off" in off["errors"]["data_migration_json_input"]
    assert "`Partner Org`" in off["errors"]["data_migration_json_input"]


def test_finish_imports_nothing_when_the_signer_is_no_longer_trusted(real_db):
    from handlers.export_import import handle_data_migration_review

    payload, instance_id, public_pem = _peer_signed_file()
    _save_peer(instance_id, public_pem, name="Partner Org", trust="untrusted")
    local = _export_rows()
    body = {"user": {"id": "U1"}, "view": {"id": "V1", "state": {"values": {}}}}
    meta = {
        "data": payload,
        "group_id": 0,
        "workspace_id": local.id,
        "team_id_to_workspace_id": {"TA": local.id},
        "source_name": "Partner Org",
        "source_kind": "connected",
        "source_instance_id": instance_id,
    }
    with (
        patch("handlers.export_import._is_admin", return_value=True),
        patch("helpers._cache._cache_get", return_value=meta),
        patch("handlers.export_import.helpers.get_workspace_by_id", return_value=local),
        patch("helpers.settings.federation_enabled", return_value=True),
        patch("handlers.export_import._run_migration_import") as run_import,
        patch("handlers.export_import.helpers.notify_user_dm") as dm,
        patch("handlers._common._close_modal_done"),
    ):
        handle_data_migration_review(body, MagicMock(), MagicMock(), {})
    run_import.assert_not_called()
    assert "no longer connected" in dm.call_args.args[2]
    assert "`Partner Org`" in dm.call_args.args[2]


def test_connected_import_writes_only_that_installs_stubs(real_db):
    from datetime import UTC, datetime

    from db import DbManager, schemas
    from federation.core import get_instance_id
    from handlers.export_import import _narrow_import_map
    from helpers.export_import import import_migration_data

    now = datetime.now(UTC).replace(tzinfo=None)
    workspace_a = DbManager.create_record(
        schemas.Workspace(team_id="TA", workspace_name="Workspace A", instance_id=get_instance_id())
    )
    partner = DbManager.create_record(
        schemas.Instance(
            instance_id="b" * 64,
            public_key="pem",
            status="active",
            trust_status="trusted",
            name="Partner Org",
            created_at=now,
        )
    )
    other = DbManager.create_record(
        schemas.Instance(
            instance_id="c" * 64,
            public_key="pem-other",
            status="active",
            trust_status="trusted",
            name="Other Org",
            created_at=now,
        )
    )
    partner_stub = DbManager.create_record(
        schemas.Workspace(team_id="TC", workspace_name="Workspace C", instance_id=partner.instance_id)
    )
    other_stub = DbManager.create_record(
        schemas.Workspace(team_id="TD", workspace_name="Workspace D", instance_id=other.instance_id)
    )
    group = DbManager.create_record(
        schemas.WorkspaceGroup(
            name="A Group",
            invite_code="AGRP-0002",
            status="active",
            created_at=now,
            uid="a-group",
        )
    )
    DbManager.create_record(
        schemas.WorkspaceGroupMember(
            group_id=group.id,
            workspace_id=workspace_a.id,
            status="active",
            role="owner",
            joined_at=now,
        )
    )
    narrowed = _narrow_import_map(
        {"TA": workspace_a.id, "TC": partner_stub.id, "TD": other_stub.id},
        workspace_a,
        partner,
    )
    assert set(narrowed) == {"TA", "TC"}
    import_migration_data(
        {
            "workspace": {"team_id": "TA"},
            "groups": [
                {
                    "uid": "a-group",
                    "name": "A Group",
                    "role": "owner",
                    "member_workspaces": [
                        {"team_id": "TC", "role": "member"},
                        {"team_id": "TD", "role": "member"},
                    ],
                }
            ],
            "syncs": [{"uid": "a-sync", "group_uid": "a-group", "title": "A Sync"}],
            "sync_channels": [
                {"sync_uid": "a-sync", "team_id": "TC", "channel_id": "CPART"},
                {"sync_uid": "a-sync", "team_id": "TD", "channel_id": "COTHER"},
            ],
        },
        workspace_a.id,
        team_id_to_workspace_id=narrowed,
    )
    members = DbManager.find_records(
        schemas.WorkspaceGroupMember,
        [schemas.WorkspaceGroupMember.group_id == group.id, schemas.WorkspaceGroupMember.deleted_at.is_(None)],
    )
    assert {row.workspace_id for row in members} == {workspace_a.id, partner_stub.id}
    assert DbManager.find_records(schemas.SyncChannel, [schemas.SyncChannel.channel_id == "CPART"])
    assert DbManager.find_records(schemas.SyncChannel, [schemas.SyncChannel.channel_id == "COTHER"]) == []
