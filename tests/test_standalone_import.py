"""A signed migration file can be imported without an External Connection."""

import base64
import os
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

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

    url = f"sqlite:///{tmp_path / 'import.db'}"
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


def _sign(body, *, name="Secret Install"):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    from federation.core import public_key_fingerprint
    from helpers.export_import import canonical_json_dumps

    private = Ed25519PrivateKey.generate()
    public_pem = private.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode()
    instance_id = public_key_fingerprint(public_pem)
    payload = {
        "version": 1,
        "signed_at": "2026-01-01T00:00:00Z",
        **body,
        "source_instance": {"instance_id": instance_id, "public_key": public_pem, "name": name},
    }
    raw = canonical_json_dumps({key: value for key, value in payload.items() if key != "signature"}).decode()
    payload["signature"] = base64.b64encode(private.sign(raw.encode())).decode()
    return payload, instance_id, public_pem


def _file():
    return {
        "workspace": {"team_id": "T0000000A", "workspace_name": "Workspace A"},
        "groups": [
            {
                "uid": "g-move",
                "name": "Group",
                "role": "owner",
                "member_workspaces": [{"team_id": "T0000000B", "role": "member"}],
            }
        ],
        "syncs": [{"uid": "s-move", "group_uid": "g-move", "title": "general"}],
        "sync_channels": [
            {"sync_uid": "s-move", "team_id": "T0000000A", "channel_id": "CA"},
            {"sync_uid": "s-move", "team_id": "T0000000B", "channel_id": "CB"},
        ],
        "post_meta": {
            "s-move:CA": [{"post_id": "P1", "ts": "1700000000.000100"}],
            "s-move:CB": [{"post_id": "P1", "ts": "1700000000.000100"}],
        },
        "user_mappings": [
            {
                "source_team_id": "T0000000A",
                "target_team_id": "T0000000B",
                "source_user_id": "UA",
                "target_user_id": "UB",
                "group_uid": "g-move",
            }
        ],
    }


def _workspace(team_id, name, instance_id):
    from db import DbManager, schemas

    return DbManager.create_record(schemas.Workspace(team_id=team_id, workspace_name=name, instance_id=instance_id))


def _resume(workspace, client=None, context=None, source=None, rows=None):
    from db import DbManager, schemas

    for row in rows or []:
        DbManager.update_records(
            schemas.SyncChannel,
            [schemas.SyncChannel.id == row.id],
            {schemas.SyncChannel.status: "active"},
        )
    return {"resumed": len(rows or [])}


def _run(workspace, payload):
    from handlers.export_import import handle_data_migration_review

    meta = {
        "data": payload,
        "group_id": 0,
        "workspace_id": workspace.id,
        "team_id_to_workspace_id": {workspace.team_id: workspace.id, "T0000000B": 999999},
        "source_name": "Connection",
        "source_kind": "unknown",
    }
    body = {"user": {"id": "U1"}, "view": {"id": "V1"}}
    pushed = MagicMock(side_effect=AssertionError("push_teams"))
    with (
        patch("handlers.export_import._is_admin", return_value=True),
        patch("helpers._cache._cache_get", return_value=meta),
        patch("helpers.workspace.get_bot_token", return_value="xoxb-test"),
        patch("helpers.notifications.notify_migration"),
        patch("helpers.workspace.resume_restored_sync_channels", side_effect=_resume),
        patch("federation.core.push_teams", pushed),
        patch("handlers.export_import.builders.refresh_home_tab_for_workspace"),
        patch("handlers.export_import.helpers.notify_user_dm") as dm,
        patch("handlers._common._close_modal_done"),
    ):
        handle_data_migration_review(body, MagicMock(), MagicMock(), {})
    return dm.call_args.args[2], pushed


def test_import_from_unknown_install_brings_this_workspace_only(real_db):
    from db import DbManager, schemas
    from federation.core import get_instance_id

    workspace_a = _workspace("T0000000A", "Workspace A", get_instance_id())
    peer = "b" * 64
    DbManager.create_record(
        schemas.Instance(
            instance_id=peer,
            public_key="pem",
            status="active",
            trust_status="trusted",
            name="Partner Org",
            created_at=_now(),
        )
    )
    stub = _workspace("T0000000B", "Workspace B", peer)
    payload, _instance_id, _pem = _sign(_file())
    before_instances = DbManager.find_records(schemas.Instance, [schemas.Instance.instance_id.isnot(None)])
    text, pushed = _run(workspace_a, payload)
    channels = DbManager.find_records(schemas.SyncChannel, [schemas.SyncChannel.id.isnot(None)])
    assert [row.channel_id for row in channels] == ["CA"]
    assert channels[0].status == "active"
    posts = DbManager.find_records(schemas.PostMeta, [schemas.PostMeta.post_id == "P1"])
    assert len(posts) == 1
    assert posts[0].sync_channel_id == channels[0].id
    assert DbManager.find_records(schemas.UserMapping, [schemas.UserMapping.id.isnot(None)]) == []
    members = DbManager.find_records(
        schemas.WorkspaceGroupMember, [schemas.WorkspaceGroupMember.workspace_id == stub.id]
    )
    assert members == []
    assert DbManager.get_record(schemas.Workspace, id=stub.team_id).instance_id == peer
    after = DbManager.find_records(schemas.Instance, [schemas.Instance.instance_id.isnot(None)])
    assert {row.instance_id for row in after} == {row.instance_id for row in before_instances}
    pushed.assert_not_called()
    assert "were left out" in text
    assert "Secret Install" not in text


def test_import_from_unknown_install_cannot_join_an_existing_group(real_db):
    from db import DbManager, schemas
    from federation.core import get_instance_id

    workspace_a = _workspace("T0000000A", "Workspace A", get_instance_id())
    group = DbManager.create_record(
        schemas.WorkspaceGroup(name="Group", invite_code="GRP-STAY", status="active", created_at=_now(), uid="g-move")
    )
    payload, _instance_id, _pem = _sign(_file())
    text, _pushed = _run(workspace_a, payload)
    members = DbManager.find_records(schemas.WorkspaceGroupMember, [schemas.WorkspaceGroupMember.group_id == group.id])
    assert members == []
    assert "left out" in text


def test_unsigned_and_tampered_files_are_still_refused(real_db):
    from federation.core import get_instance_id
    from handlers.export_import import handle_data_migration_submit_ack

    workspace_a = _workspace("T0000000A", "Workspace A", get_instance_id())
    payload, _instance_id, _pem = _sign(_file())
    unsigned = dict(payload)
    unsigned.pop("signature")
    changed = dict(payload)
    changed["workspace"] = {"team_id": "T0000000A", "workspace_name": "Edited"}

    def ack(data):
        body = {
            "user": {"id": "U1"},
            "team": {"id": "T0000000A"},
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
            patch(
                "handlers.export_import._download_uploaded_file", return_value=(__import__("json").dumps(data), None)
            ),
            patch("handlers.export_import.helpers.get_workspace_record", return_value=workspace_a),
        ):
            return handle_data_migration_submit_ack(body, MagicMock(token="xoxb"), {})

    unsigned_resp = ack(unsigned)
    assert "cannot check this file" in unsigned_resp["errors"]["data_migration_json_input"]
    tampered = ack(changed)
    assert "changed after it was exported" in tampered["errors"]["data_migration_json_input"]


def test_second_workspace_imports_after_joining(real_db):
    from db import DbManager, schemas
    from federation.core import get_instance_id

    workspace_a = _workspace("T0000000A", "Workspace A", get_instance_id())
    payload, _instance_id, _pem = _sign(_file())
    _run(workspace_a, payload)
    workspace_b = _workspace("T0000000B", "Workspace B", get_instance_id())
    group = DbManager.find_records(schemas.WorkspaceGroup, [schemas.WorkspaceGroup.uid == "g-move"])[0]
    DbManager.create_record(
        schemas.WorkspaceGroupMember(
            group_id=group.id,
            workspace_id=workspace_b.id,
            status="active",
            role="member",
            joined_at=_now(),
        )
    )
    _run(workspace_b, payload)
    channels = DbManager.find_records(schemas.SyncChannel, [schemas.SyncChannel.deleted_at.is_(None)])
    by_channel = {row.channel_id: row for row in channels}
    assert set(by_channel) == {"CA", "CB"}
    assert by_channel["CA"].sync_id == by_channel["CB"].sync_id
    posts = DbManager.find_records(schemas.PostMeta, [schemas.PostMeta.post_id == "P1"])
    assert {row.sync_channel_id for row in posts} == {by_channel["CA"].id, by_channel["CB"].id}


def test_review_names_the_signer_by_fingerprint(real_db):
    from federation.core import get_instance_id
    from handlers.export_import import handle_data_migration_submit_ack

    workspace_a = _workspace("T0000000A", "Workspace A", get_instance_id())
    payload, instance_id, _pem = _sign(_file(), name="Secret Install")
    body = {
        "user": {"id": "U1"},
        "team": {"id": "T0000000A"},
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
        patch("handlers.export_import._download_uploaded_file", return_value=(__import__("json").dumps(payload), None)),
        patch("handlers.export_import.helpers.get_workspace_record", return_value=workspace_a),
    ):
        resp = handle_data_migration_submit_ack(body, MagicMock(token="xoxb"), {})
    text = __import__("json").dumps(resp["view"])
    assert f"Connection {instance_id[:8]}" in text
    assert "Secret Install" not in text
