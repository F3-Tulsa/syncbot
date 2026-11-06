"""Federation bridge: file offer, URL heal, envelope JSON cap, stub routing."""

from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

os.environ.setdefault("LOCAL_DEVELOPMENT", "true")
os.environ.setdefault("DATABASE_BACKEND", "sqlite")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")


def test_build_remote_envelope_keeps_target_ts_and_drops_origin_pks():
    from federation.deliver import build_remote_envelope

    remote = build_remote_envelope(
        {
            "kind": "message",
            "action": "create",
            "post_id": "REPLY",
            "thread_post_id": "PARENT",
            "target_ts": "20.000001",
            "event_ts": "30.000001",
            "sync_id": 99,
            "source_workspace_id": 7,
            "source_sync_channel_id": 12,
            "mapped_user_id": "ULOCAL",
        },
        "CREMOTE",
    )
    assert remote["channel_id"] == "CREMOTE"
    assert remote["target_ts"] == "20.000001"
    assert remote["event_ts"] == "30.000001"
    assert "sync_id" not in remote
    assert "source_workspace_id" not in remote
    assert "source_sync_channel_id" not in remote
    assert "mapped_user_id" not in remote


@pytest.fixture
def real_db(tmp_path):
    import db as db_mod
    from db import initialize_database
    from helpers._cache import clear_all_caches

    url = f"sqlite:///{tmp_path / 'federation_bridge.db'}"
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
            db_mod.GLOBAL_ENGINE = old_engine
            db_mod.GLOBAL_SESSION = old_session
            db_mod.GLOBAL_SCHEMA = old_schema
            clear_all_caches()


def _seed_peer():
    from db import DbManager, schemas
    from federation.core import get_instance_id, public_key_fingerprint

    now = datetime.now(UTC).replace(tzinfo=None)
    peer_private = Ed25519PrivateKey.generate()
    peer_public = (
        peer_private.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    fed = DbManager.create_record(
        schemas.Instance(
            instance_id=public_key_fingerprint(peer_public),
            webhook_url="https://peer.example/api/federation",
            public_key=peer_public,
            private_key_encrypted=None,
            status="active",
            trust_status="trusted",
            name="Peer",
            created_at=now,
        )
    )
    local = DbManager.create_record(
        schemas.Workspace(
            team_id="TLOCAL",
            workspace_name="Local",
            instance_id=get_instance_id(),
        )
    )
    stub = DbManager.create_record(
        schemas.Workspace(
            team_id="TSTUB",
            workspace_name="Stub",
            instance_id=fed.instance_id,
        )
    )
    group = DbManager.create_record(
        schemas.WorkspaceGroup(
            name="Shared",
            invite_code="GRP-TEST01",
            status="active",
            created_at=now,
            uid="11111111-1111-1111-1111-111111111111",
        )
    )
    for ws, role in ((local, "owner"), (stub, "member")):
        DbManager.create_record(
            schemas.WorkspaceGroupMember(
                group_id=group.id,
                workspace_id=ws.id,
                status="active",
                role=role,
                joined_at=now,
            )
        )
    return {"fed": fed, "local": local, "stub": stub, "group": group}


def _peer_public_key() -> tuple[str, str]:
    from federation.core import public_key_fingerprint

    private = Ed25519PrivateKey.generate()
    public = (
        private.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return public, public_key_fingerprint(public)


def test_workspace_kind_local_vs_stub(real_db):
    from helpers.workspace_kind import can_promote, is_local_workspace, is_stub_workspace

    peer = _seed_peer()
    assert is_local_workspace(peer["local"])
    assert is_stub_workspace(peer["stub"])
    assert not is_stub_workspace(peer["local"])
    assert not is_local_workspace(peer["stub"])
    assert can_promote(peer["local"])
    assert can_promote(peer["stub"])
    peer["local"].deleted_at = datetime.now(UTC)
    assert not can_promote(peer["local"])


def test_workspace_convert_logs_refused_live(real_db, caplog):
    import logging

    from helpers.workspace_kind import convert_workspace_to_stub

    peer = _seed_peer()
    with caplog.at_level(logging.DEBUG, logger="syncbot"):
        result = convert_workspace_to_stub(peer["local"].team_id, peer["fed"].instance_id)
    assert result == "refused_live"
    assert any(record.message == "workspace_convert" for record in caplog.records)


def test_inbound_skip_logs_channel_not_found(real_db, caplog):
    import logging

    from federation.api import handle_message

    peer = _seed_peer()
    with caplog.at_level(logging.DEBUG, logger="syncbot"):
        status, _resp = handle_message(
            {"kind": "message", "action": "create", "post_id": "p1", "channel_id": "CMISSING"},
            peer["fed"],
        )
    assert status == 404
    assert any(
        record.message == "inbound_skip" and record.__dict__.get("reason") == "channel_not_found"
        for record in caplog.records
    )


def test_source_stub_id_requires_source_team_id(real_db):
    from federation.api import _source_stub_id

    peer = _seed_peer()
    assert _source_stub_id(peer["fed"], {}) is None
    assert _source_stub_id(peer["fed"], {"source_workspace_id": peer["stub"].id}) is None
    assert _source_stub_id(peer["fed"], {"source_team_id": peer["stub"].team_id}) == peer["stub"].id


def test_inbound_rejects_unknown_source_team(real_db):
    from federation.api import handle_message

    peer = _seed_peer()
    _sync, sc = _seed_inbound_channel(peer)
    status, resp = handle_message(
        {
            "kind": "message",
            "action": "create",
            "post_id": "p-blocked",
            "channel_id": sc.channel_id,
            "source_team_id": "TUNKNOWN",
            "text": "hi",
        },
        peer["fed"],
    )
    assert status == 404
    assert resp.get("error") == "workspace_not_found"


def test_inbound_follow_ups_wait_for_parent_post_meta(real_db):
    from federation.api import handle_message, handle_message_edit, handle_message_react

    peer = _seed_peer()
    _sync, sc = _seed_inbound_channel(peer)
    reply = handle_message(
        {
            "kind": "message",
            "action": "create",
            "post_id": "reply-1",
            "channel_id": sc.channel_id,
            "thread_post_id": "PARENT",
            "text": "reply",
        },
        peer["fed"],
    )
    assert reply[0] == 409
    assert reply[1]["error"] == "parent_missing"
    edit = handle_message_edit(
        {
            "kind": "message",
            "action": "edit",
            "post_id": "PARENT",
            "channel_id": sc.channel_id,
            "target_ts": "10.000001",
            "text": "edited",
        },
        peer["fed"],
    )
    assert edit[0] == 409
    assert edit[1]["error"] == "parent_missing"
    react = handle_message_react(
        {
            "kind": "reaction",
            "action": "add",
            "post_id": "PARENT",
            "channel_id": sc.channel_id,
            "target_ts": "10.000001",
            "reaction": "eyes",
        },
        peer["fed"],
    )
    assert react[0] == 409
    assert react[1]["error"] == "parent_missing"


def test_inbound_follow_ups_apply_when_parent_post_meta_exists(real_db):
    from db import DbManager, schemas
    from federation.api import handle_message, handle_message_edit, handle_message_react
    from helpers.sync_apply import ApplyOutcome
    from helpers.user_action_echo import post_meta_ts

    peer = _seed_peer()
    _sync, sc = _seed_inbound_channel(peer)
    DbManager.create_record(schemas.PostMeta(post_id="PARENT", sync_channel_id=sc.id, ts=post_meta_ts("10.000001")))
    created = SimpleNamespace(ts=post_meta_ts("11.000001"), posted_as_user_id=None)
    with patch("federation.api.apply_target", return_value=ApplyOutcome(created=[created])) as apply:
        reply = handle_message(
            {
                "kind": "message",
                "action": "create",
                "post_id": "reply-1",
                "channel_id": sc.channel_id,
                "thread_post_id": "PARENT",
                "text": "reply",
            },
            peer["fed"],
        )
        assert reply[0] == 200
        assert apply.call_args.kwargs["thread_ts"] == "10.000001"

        apply.reset_mock()
        edit = handle_message_edit(
            {
                "kind": "message",
                "action": "edit",
                "post_id": "PARENT",
                "channel_id": sc.channel_id,
                "text": "edited",
            },
            peer["fed"],
        )
        assert edit[0] == 200
        assert apply.call_args.kwargs["target_post_meta"].post_id == "PARENT"

        apply.reset_mock()
        react = handle_message_react(
            {
                "kind": "reaction",
                "action": "add",
                "post_id": "PARENT",
                "channel_id": sc.channel_id,
                "reaction": "eyes",
            },
            peer["fed"],
        )
        assert react[0] == 200
        apply.assert_called_once()


def test_inbound_reply_uses_envelope_target_ts_without_parent_post_meta(real_db):
    from federation.api import handle_message
    from helpers.sync_apply import ApplyOutcome
    from helpers.user_action_echo import post_meta_ts

    peer = _seed_peer()
    _sync, sc = _seed_inbound_channel(peer)
    created = SimpleNamespace(ts=post_meta_ts("11.000001"), posted_as_user_id=None)
    with patch("federation.api.apply_target", return_value=ApplyOutcome(created=[created])) as apply:
        reply = handle_message(
            {
                "kind": "message",
                "action": "create",
                "post_id": "reply-1",
                "channel_id": sc.channel_id,
                "thread_post_id": "PARENT",
                "target_ts": "10.000001",
                "text": "reply",
            },
            peer["fed"],
        )
    assert reply[0] == 200
    assert apply.call_args.kwargs["thread_ts"] == "10.000001"


def test_pair_logs_heal_results(real_db, monkeypatch, caplog):
    import logging

    monkeypatch.setattr("helpers.federation_enabled", lambda: True)
    from db import DbManager, schemas
    from federation.api import handle_pair

    peer = _seed_peer()
    now = datetime.now(UTC).replace(tzinfo=None)
    DbManager.create_record(
        schemas.FederationPairingCode(
            code="FED-AABB0011",
            created_at=now,
            subject_team_id=peer["local"].team_id,
            label="Heal",
        )
    )
    body = {
        "code": "FED-AABB0011",
        "webhook_url": "https://new-peer.example/api/federation",
        "instance_id": peer["fed"].instance_id,
        "public_key": peer["fed"].public_key,
        "team_id": peer["local"].team_id,
    }
    with _pair_ok(), caplog.at_level(logging.DEBUG, logger="syncbot"):
        status, resp = handle_pair(body, json.dumps(body), _pair_headers(body["instance_id"]))
    assert status == 200
    assert resp.get("url_updated") is True
    assert "json_chunk_mb" in resp
    pair_recs = [record for record in caplog.records if record.message == "federation_pair"]
    assert pair_recs
    assert pair_recs[0].__dict__.get("direction") == "inbound"
    assert (pair_recs[0].__dict__.get("convert") or {}).get(peer["local"].team_id) == "refused_live"
    assert any(
        record.message == "workspace_convert"
        and record.__dict__.get("source") == "pair"
        and record.__dict__.get("result") == "refused_live"
        for record in caplog.records
    )


def test_teams_convert_logs_source(real_db, caplog):
    import logging

    from federation.api import handle_teams

    peer = _seed_peer()
    with caplog.at_level(logging.DEBUG, logger="syncbot"):
        status, resp = handle_teams(
            {"workspaces": [{"team_id": peer["local"].team_id, "name": "Local"}]},
            peer["fed"],
        )
    assert status == 200
    assert resp["results"][peer["local"].team_id] == "refused_live"
    assert any(
        record.message == "workspace_convert" and record.__dict__.get("source") == "teams" for record in caplog.records
    )
    assert any(record.message == "teams_convert" for record in caplog.records)


def test_teams_refreshes_stub_name_and_primary_workspace(real_db):
    from db import DbManager, schemas
    from federation.api import handle_teams

    peer = _seed_peer()
    status, resp = handle_teams(
        {
            "workspaces": [{"team_id": "TSTUB", "name": "Renamed Stub"}],
            "primary_team_id": "TPEERPRI",
            "primary_workspace_name": "Peer HQ",
        },
        peer["fed"],
    )
    assert status == 200
    assert resp["ok"] is True
    stub = DbManager.get_record(schemas.Workspace, id="TSTUB")
    assert stub.workspace_name == "Renamed Stub"
    fed = DbManager.get_record(schemas.Instance, id=peer["fed"].instance_id)
    assert fed.primary_team_id == "TPEERPRI"
    assert fed.primary_workspace_name == "Peer HQ"


def test_ensure_stub_logs_created(real_db, caplog):
    import logging

    from helpers.workspace_kind import ensure_stub_workspace

    peer = _seed_peer()
    with caplog.at_level(logging.DEBUG, logger="syncbot"):
        workspace = ensure_stub_workspace(
            team_id="TNEWSTUB",
            workspace_name="New",
            instance_id=peer["fed"].instance_id,
        )
    assert workspace is not None
    assert workspace.team_id == "TNEWSTUB"
    assert any(
        record.message == "workspace_convert" and record.__dict__.get("result") == "created"
        for record in caplog.records
    )


def test_dispatch_json_oversize_413(monkeypatch):
    monkeypatch.setattr("helpers.federation_enabled", lambda: True)
    monkeypatch.setattr("federation.api.constants.federation_json_max_bytes", lambda: 64)
    from federation.api import dispatch_federation_request

    status, body = dispatch_federation_request(
        "POST",
        "/api/federation/message",
        "x" * 65,
        _known_peer_headers("a" * 64),
    )
    assert status == 413
    assert body.get("error") == "payload_too_large"


def test_dispatch_not_ready_returns_503():
    from sqlalchemy.exc import OperationalError

    from federation.api import dispatch_federation_request

    with patch(
        "federation.api.helpers.federation_enabled",
        side_effect=OperationalError("SELECT 1", {}, Exception("schema")),
    ):
        status, body = dispatch_federation_request(
            "POST",
            "/api/federation/message",
            "{}",
            {"User-Agent": "SyncBot-Federation/1.0"},
        )
    assert status == 503
    assert body.get("error") == "not_ready"


def test_dispatch_file_skips_json_parse(real_db, monkeypatch):
    monkeypatch.setattr("helpers.federation_enabled", lambda: True)
    from federation.api import dispatch_federation_request

    peer = _seed_peer()
    fed = peer["fed"]
    data = b"\xff\xfe binary not json"
    sha = hashlib.sha256(data).hexdigest()
    with patch("federation.api.federation.federation_verify", return_value=True):
        status, body = dispatch_federation_request(
            "POST",
            "/api/federation/file",
            "",
            {
                "User-Agent": "SyncBot-Federation/1.0",
                "X-Federation-Instance": fed.instance_id,
                "X-Federation-Signature": "sig",
                "X-Federation-Timestamp": "1",
                "X-Federation-File-Sha256": sha,
                "X-Federation-File-Index": "0",
                "X-Federation-File-Total": "1",
                "X-Federation-File-Size": str(len(data)),
            },
            raw_body=data,
        )
    assert status == 200
    assert body.get("ok") is True


def test_file_offer_have_false(real_db, monkeypatch):
    monkeypatch.setattr("helpers.federation_enabled", lambda: True)
    from federation.api import dispatch_federation_request

    peer = _seed_peer()
    body = json.dumps({"sha256": "b" * 64, "size": 10})
    with patch("federation.api.federation.federation_verify", return_value=True):
        status, resp = dispatch_federation_request(
            "POST",
            "/api/federation/file/offer",
            body,
            _known_peer_headers(peer["fed"].instance_id),
        )
    assert status == 200
    assert resp["have"] is False
    assert "file_chunk_mb" in resp
    assert "json_chunk_mb" in resp


def test_url_heal_same_key_updates_webhook(real_db, monkeypatch):
    monkeypatch.setattr("helpers.federation_enabled", lambda: True)
    from db import DbManager, schemas
    from federation.api import handle_pair

    peer = _seed_peer()
    fed = peer["fed"]
    now = datetime.now(UTC).replace(tzinfo=None)
    DbManager.create_record(
        schemas.FederationPairingCode(
            code="FED-AABBCCDD",
            created_at=now,
            subject_team_id=peer["local"].team_id,
            label="Heal",
        )
    )
    new_url = "https://new-peer.example/api/federation"
    body = {
        "code": "FED-AABBCCDD",
        "webhook_url": new_url,
        "instance_id": fed.instance_id,
        "public_key": fed.public_key,
    }
    body_str = json.dumps(body)
    with (
        patch("federation.api.federation.validate_webhook_url", return_value=True),
        patch("federation.api.federation.federation_verify", return_value=True),
        patch("federation.api.federation.instance_id_matches_public_key", return_value=True),
        patch("federation.api.federation.get_or_create_instance_keypair", return_value=(None, "our-pem")),
        patch("federation.api.federation.get_instance_id", return_value="local-id"),
        patch("federation.api.federation.push_allowed_workspaces", return_value={"ok": True}),
        patch("federation.replicate.replicate_peer_snapshot"),
    ):
        status, resp = handle_pair(
            body,
            body_str,
            {
                "User-Agent": "SyncBot-Federation/1.0",
                "X-Federation-Signature": "s",
                "X-Federation-Timestamp": "1",
                "X-Federation-Instance": body["instance_id"],
            },
        )
    assert status == 200
    assert resp.get("url_updated") is True
    updated = DbManager.get_record(schemas.Instance, id=fed.instance_id)
    assert updated.webhook_url == new_url
    assert updated.trust_status == "trusted"
    leftover = DbManager.find_records(
        schemas.FederationPairingCode,
        [schemas.FederationPairingCode.code == "FED-AABBCCDD"],
    )
    assert leftover == []


def test_pipeline_stub_uses_deliver_remote_not_local_apply(real_db):
    from db import DbManager, schemas
    from helpers.sync_pipeline import run_sync_pipeline

    peer = _seed_peer()
    now = datetime.now(UTC).replace(tzinfo=None)
    sync = DbManager.create_record(
        schemas.Sync(
            title="S",
            group_id=peer["group"].id,
            sync_mode="group",
            uid="22222222-2222-2222-2222-222222222222",
        )
    )
    stub_sc = DbManager.create_record(
        schemas.SyncChannel(
            sync_id=sync.id,
            workspace_id=peer["stub"].id,
            channel_id="CSTUB",
            status="active",
            publishes=False,
            subscribes=True,
            created_at=now,
        )
    )
    envelope = {
        "kind": "message",
        "action": "create",
        "post_id": "pid-1",
        "source_channel_id": "CLOCAL",
        "source_workspace_id": peer["local"].id,
        "text": "hi",
    }
    with (
        patch("helpers.sync_pipeline.iter_publish_targets", return_value=[(stub_sc, peer["stub"])]),
        patch("federation.deliver.deliver_remote", return_value=[]) as deliver,
        patch("helpers.sync_pipeline.apply_target") as apply,
    ):
        run_sync_pipeline(envelope, source_channel_id="CLOCAL")
        deliver.assert_called_once()
        apply.assert_not_called()


def test_pipeline_mixed_group_local_not_diverted(real_db):
    from db import DbManager, schemas
    from helpers.sync_apply import ApplyOutcome
    from helpers.sync_pipeline import run_sync_pipeline

    peer = _seed_peer()
    now = datetime.now(UTC).replace(tzinfo=None)
    other = DbManager.create_record(
        schemas.Workspace(
            team_id="TOTHER",
            workspace_name="Other",
            instance_id=peer["local"].instance_id,
        )
    )
    sync = DbManager.create_record(
        schemas.Sync(
            title="S2",
            group_id=peer["group"].id,
            sync_mode="group",
            uid="22222222-2222-2222-2222-222222222223",
        )
    )
    other_sc = DbManager.create_record(
        schemas.SyncChannel(
            sync_id=sync.id,
            workspace_id=other.id,
            channel_id="COTHER",
            status="active",
            publishes=False,
            subscribes=True,
            created_at=now,
        )
    )
    envelope = {
        "kind": "message",
        "action": "create",
        "post_id": "pid-2",
        "source_channel_id": "CLOCAL",
        "source_workspace_id": peer["local"].id,
        "text": "hi",
    }
    with (
        patch("helpers.sync_pipeline.iter_publish_targets", return_value=[(other_sc, other)]),
        patch("federation.deliver.deliver_remote") as deliver,
        patch("helpers.sync_pipeline.apply_target", return_value=ApplyOutcome(ts="1.000000")) as apply,
    ):
        run_sync_pipeline(envelope, source_channel_id="CLOCAL")
        apply.assert_called_once()
        deliver.assert_not_called()


def test_replicate_group_invite_pushes_peer(real_db):
    from federation.replicate import replicate_group_invite

    peer = _seed_peer()
    with (
        patch("federation.core.push_group_upsert") as upsert,
        patch("federation.core.push_group_invite") as invite,
    ):
        replicate_group_invite(peer["group"], peer["stub"])
    upsert.assert_called()
    invite.assert_called()
    payload = invite.call_args.args[1]
    assert payload["team_id"] == peer["stub"].team_id
    assert payload["uid"]
    assert payload["role"] == "member"
    assert "set_role" not in payload
    assert "notice" not in payload
    assert "invited_by_team_id" not in payload


def test_a_new_invite_names_the_inviting_workspace(real_db):
    from federation.replicate import replicate_group_invite

    peer = _seed_peer()
    with (
        patch("federation.core.push_group_upsert"),
        patch("federation.core.push_group_invite") as invite,
    ):
        replicate_group_invite(peer["group"], peer["stub"], peer=peer["fed"], invited_by=peer["local"])
    payload = invite.call_args.args[1]
    assert payload["invited_by_team_id"] == "TLOCAL"


def test_replicate_peer_snapshot_pushes_group_and_channels(real_db):
    from db import DbManager, schemas
    from federation.replicate import replicate_peer_snapshot

    peer = _seed_peer()
    now = datetime.now(UTC).replace(tzinfo=None)
    sync = DbManager.create_record(
        schemas.Sync(
            title="Announcements",
            group_id=peer["group"].id,
            sync_mode="group",
            uid="55555555-5555-5555-5555-555555555555",
        )
    )
    DbManager.create_record(
        schemas.SyncChannel(
            sync_id=sync.id,
            workspace_id=peer["local"].id,
            channel_id="CLOCAL",
            status="active",
            publishes=True,
            subscribes=True,
            created_at=now,
        )
    )
    with (
        patch("federation.core.push_group_upsert") as upsert,
        patch("federation.core.push_group_invite") as invite,
        patch("federation.core.push_sync_upsert") as sync_upsert,
        patch("federation.core.push_sync_channel_upsert") as channel_upsert,
    ):
        replicate_peer_snapshot(peer["fed"])
    upsert.assert_called()
    invited_teams = {call.args[1]["team_id"] for call in invite.call_args_list}
    assert invited_teams == {"TLOCAL", "TSTUB"}
    roles = {call.args[1]["team_id"]: call.args[1]["role"] for call in invite.call_args_list}
    assert roles["TLOCAL"] == "owner"
    assert roles["TSTUB"] == "member"
    assert all(
        "set_role" not in call.args[1] and "notice" not in call.args[1] and "invited_by_team_id" not in call.args[1]
        for call in invite.call_args_list
    )
    sync_upsert.assert_called()
    channel_upsert.assert_called()
    channel_payload = channel_upsert.call_args.args[1]
    assert channel_payload["team_id"] == "TLOCAL"
    assert channel_payload["channel_id"] == "CLOCAL"
    assert "notice" not in channel_payload


def test_replicate_peer_snapshot_skips_local_not_on_allowlist(real_db):
    from db import DbManager, schemas
    from federation.replicate import replicate_peer_snapshot

    peer = _seed_peer()
    extra = DbManager.create_record(
        schemas.Workspace(
            team_id="TEXTRA",
            workspace_name="Extra",
            instance_id=peer["local"].instance_id,
        )
    )
    DbManager.create_record(
        schemas.WorkspaceGroupMember(
            group_id=peer["group"].id,
            workspace_id=extra.id,
            status="active",
            role="member",
            joined_at=datetime.now(UTC).replace(tzinfo=None),
        )
    )
    DbManager.create_record(
        schemas.FederationWorkspaceAllowlist(
            instance_id=peer["fed"].instance_id,
            workspace_id=peer["local"].id,
        )
    )
    with (
        patch("federation.core.push_group_upsert"),
        patch("federation.core.push_group_invite") as invite,
        patch("federation.core.push_sync_upsert"),
        patch("federation.core.push_sync_channel_upsert"),
    ):
        replicate_peer_snapshot(peer["fed"])
    invited_teams = {call.args[1]["team_id"] for call in invite.call_args_list}
    assert invited_teams == {"TLOCAL", "TSTUB"}
    assert "TEXTRA" not in invited_teams


def test_handle_group_invite_does_not_create_unknown_stub(real_db):
    from db import DbManager, schemas
    from federation.api import handle_group_invite

    peer = _seed_peer()
    status, resp = handle_group_invite(
        {
            "uid": peer["group"].uid,
            "team_id": "TNEW",
            "workspace_name": "Workspace B",
            "role": "owner",
        },
        peer["fed"],
    )
    assert status == 404
    assert resp.get("error") == "workspace_not_found"
    assert DbManager.get_record(schemas.Workspace, id="TNEW") is None


def test_handle_group_invite_honors_owner_role(real_db):
    from db import DbManager, schemas
    from federation.api import handle_group_invite
    from helpers.workspace_kind import ensure_stub_workspace

    peer = _seed_peer()
    ensure_stub_workspace(team_id="TNEW", workspace_name="Workspace B", instance_id=peer["fed"].instance_id)
    status, resp = handle_group_invite(
        {
            "uid": peer["group"].uid,
            "team_id": "TNEW",
            "workspace_name": "Workspace B",
            "role": "owner",
        },
        peer["fed"],
    )
    assert status == 200
    assert resp["ok"] is True
    stub = DbManager.get_record(schemas.Workspace, id="TNEW")
    members = DbManager.find_records(
        schemas.WorkspaceGroupMember,
        [
            schemas.WorkspaceGroupMember.group_id == peer["group"].id,
            schemas.WorkspaceGroupMember.workspace_id == stub.id,
        ],
    )
    assert members[0].role == "member"


def test_handle_group_invite_set_role_stores_owner_and_notifies(real_db):
    from db import DbManager, schemas
    from federation.api import handle_group_invite

    peer = _seed_peer()
    with (
        patch("helpers.notifications.get_bot_token", return_value="xoxb-test"),
        patch("helpers.notifications.notify_admins_dm") as dm,
    ):
        status, resp = handle_group_invite(
            {
                "uid": peer["group"].uid,
                "team_id": "TSTUB",
                "role": "creator",
                "set_role": True,
                "notice": ":key: `Stub` is now an Owner of `Shared`.",
            },
            peer["fed"],
        )
    assert status == 200
    assert resp["ok"] is True
    members = DbManager.find_records(
        schemas.WorkspaceGroupMember,
        [
            schemas.WorkspaceGroupMember.group_id == peer["group"].id,
            schemas.WorkspaceGroupMember.workspace_id == peer["stub"].id,
        ],
    )
    assert members[0].role == "owner"
    dm.assert_called_once()
    assert dm.call_args.args[1] == ":key: `Stub` is now an Owner of `Shared`."


def test_handle_group_invite_without_set_role_keeps_an_owner(real_db):
    from db import DbManager, schemas
    from federation.api import handle_group_invite

    peer = _seed_peer()
    with (
        patch("helpers.notifications.get_bot_token", return_value="xoxb-test"),
        patch("helpers.notifications.notify_admins_dm") as dm,
    ):
        status, _resp = handle_group_invite(
            {
                "uid": peer["group"].uid,
                "team_id": "TLOCAL",
                "role": "member",
                "notice": "do not send",
            },
            peer["fed"],
        )
    assert status == 200
    members = DbManager.find_records(
        schemas.WorkspaceGroupMember,
        [
            schemas.WorkspaceGroupMember.group_id == peer["group"].id,
            schemas.WorkspaceGroupMember.workspace_id == peer["local"].id,
        ],
    )
    assert members[0].role == "owner"
    dm.assert_not_called()


def _add_workspace_b(peer):
    from db import DbManager, schemas
    from federation.core import get_instance_id

    workspace_b = DbManager.create_record(
        schemas.Workspace(
            team_id="TLOCALB",
            workspace_name="Workspace B",
            instance_id=get_instance_id(),
        )
    )
    DbManager.create_record(
        schemas.FederationWorkspaceAllowlist(
            instance_id=peer["fed"].instance_id,
            workspace_id=workspace_b.id,
        )
    )
    return workspace_b


def _memberships(group_id, workspace_id):
    from db import DbManager, schemas

    return DbManager.find_records(
        schemas.WorkspaceGroupMember,
        [
            schemas.WorkspaceGroupMember.group_id == group_id,
            schemas.WorkspaceGroupMember.workspace_id == workspace_id,
        ],
    )


def test_group_invite_for_a_local_workspace_waits_for_accept(real_db):
    from federation.api import handle_group_invite

    peer = _seed_peer()
    workspace_b = _add_workspace_b(peer)
    with patch("helpers.send_group_invite_dm") as dm:
        status, resp = handle_group_invite({"uid": peer["group"].uid, "team_id": "TLOCALB"}, peer["fed"])
    assert status == 200
    assert resp["ok"] is True
    rows = _memberships(peer["group"].id, workspace_b.id)
    assert len(rows) == 1
    assert rows[0].status == "pending"
    assert rows[0].deleted_at is None
    assert rows[0].invited_by_workspace_id is None
    dm.assert_called_once()
    assert "Action: Invite" in dm.call_args.args[2]


def test_new_group_invite_names_the_inviting_workspace(real_db):
    from federation.api import handle_group_invite

    peer = _seed_peer()
    workspace_b = _add_workspace_b(peer)
    notice = "Workspace A invited Workspace B."
    with patch("helpers.send_group_invite_dm") as dm:
        handle_group_invite(
            {
                "uid": peer["group"].uid,
                "team_id": "TLOCALB",
                "invited_by_team_id": "TSTUB",
                "notice": notice,
            },
            peer["fed"],
        )
    rows = [row for row in _memberships(peer["group"].id, workspace_b.id) if row.deleted_at is None]
    assert len(rows) == 1
    assert rows[0].status == "pending"
    assert rows[0].invited_by_workspace_id == peer["stub"].id
    assert dm.call_args.args[2] == notice


def test_group_invite_does_not_bring_back_a_local_workspace_that_left(real_db):
    from db import DbManager, schemas
    from federation.api import handle_group_invite

    peer = _seed_peer()
    workspace_b = _add_workspace_b(peer)
    now = datetime.now(UTC).replace(tzinfo=None)
    DbManager.create_record(
        schemas.WorkspaceGroupMember(
            group_id=peer["group"].id,
            workspace_id=workspace_b.id,
            status="inactive",
            role="member",
            joined_at=now,
            deleted_at=now,
        )
    )
    with patch("helpers.send_group_invite_dm") as dm:
        handle_group_invite({"uid": peer["group"].uid, "team_id": "TLOCALB"}, peer["fed"])
    live = [row for row in _memberships(peer["group"].id, workspace_b.id) if row.deleted_at is None]
    assert live == []
    dm.assert_not_called()


def test_new_group_invite_asks_a_local_workspace_that_left(real_db):
    from db import DbManager, schemas
    from federation.api import handle_group_invite

    peer = _seed_peer()
    workspace_b = _add_workspace_b(peer)
    now = datetime.now(UTC).replace(tzinfo=None)
    ended = DbManager.create_record(
        schemas.WorkspaceGroupMember(
            group_id=peer["group"].id,
            workspace_id=workspace_b.id,
            status="inactive",
            role="member",
            joined_at=now,
            deleted_at=now,
        )
    )
    with patch("helpers.send_group_invite_dm") as dm:
        handle_group_invite(
            {"uid": peer["group"].uid, "team_id": "TLOCALB", "invited_by_team_id": "TSTUB"},
            peer["fed"],
        )
    rows = _memberships(peer["group"].id, workspace_b.id)
    pending = [row for row in rows if row.deleted_at is None]
    still_ended = [row for row in rows if row.id == ended.id]
    assert len(pending) == 1
    assert pending[0].status == "pending"
    assert still_ended[0].deleted_at is not None
    assert still_ended[0].status == "inactive"
    dm.assert_called_once()


def test_a_pending_local_invite_gets_one_dm(real_db):
    from federation.api import handle_group_invite

    peer = _seed_peer()
    workspace_b = _add_workspace_b(peer)
    body = {"uid": peer["group"].uid, "team_id": "TLOCALB"}
    with patch("helpers.send_group_invite_dm") as dm:
        handle_group_invite(body, peer["fed"])
        handle_group_invite(body, peer["fed"])
    pending = [row for row in _memberships(peer["group"].id, workspace_b.id) if row.deleted_at is None]
    assert len(pending) == 1
    assert pending[0].status == "pending"
    dm.assert_called_once()


def test_a_role_change_does_not_add_a_local_workspace(real_db):
    from federation.api import handle_group_invite

    peer = _seed_peer()
    workspace_b = _add_workspace_b(peer)
    with patch("helpers.send_group_invite_dm") as dm:
        handle_group_invite(
            {"uid": peer["group"].uid, "team_id": "TLOCALB", "set_role": True, "role": "owner"},
            peer["fed"],
        )
    assert _memberships(peer["group"].id, workspace_b.id) == []
    dm.assert_not_called()


def test_a_peer_report_turns_its_pending_invite_active(real_db):
    from db import DbManager, schemas
    from federation.api import handle_group_invite

    peer = _seed_peer()
    now = datetime.now(UTC).replace(tzinfo=None)
    ended_rows = _memberships(peer["group"].id, peer["stub"].id)
    DbManager.update_records(
        schemas.WorkspaceGroupMember,
        [schemas.WorkspaceGroupMember.id == ended_rows[0].id],
        {
            schemas.WorkspaceGroupMember.joined_at: now,
            schemas.WorkspaceGroupMember.deleted_at: now,
            schemas.WorkspaceGroupMember.status: "inactive",
        },
    )
    pending = DbManager.create_record(
        schemas.WorkspaceGroupMember(
            group_id=peer["group"].id,
            workspace_id=peer["stub"].id,
            status="pending",
            role="member",
            joined_at=None,
        )
    )
    handle_group_invite({"uid": peer["group"].uid, "team_id": "TSTUB"}, peer["fed"])
    rows = _memberships(peer["group"].id, peer["stub"].id)
    by_id = {row.id: row for row in rows}
    assert by_id[pending.id].status == "active"
    assert by_id[pending.id].joined_at is not None
    assert by_id[pending.id].deleted_at is None
    assert by_id[ended_rows[0].id].deleted_at is not None


def test_group_upsert_disbanded_ends_the_group_and_a_later_upsert_leaves_it(real_db):
    from db import DbManager, schemas
    from federation.api import handle_group_upsert

    peer = _seed_peer()
    with (
        patch("helpers.notifications.get_bot_token", return_value="xoxb-test"),
        patch("helpers.notifications.notify_admins_dm") as dm,
    ):
        status, resp = handle_group_upsert(
            {
                "uid": peer["group"].uid,
                "name": "Shared",
                "status": "disbanded",
                "notice": ":wastebasket: disbanded `Shared`.",
            },
            peer["fed"],
        )
    assert status == 200
    assert resp["ok"] is True
    group = DbManager.find_records(schemas.WorkspaceGroup, [schemas.WorkspaceGroup.uid == peer["group"].uid])[0]
    assert group.status == "disbanded"
    members = DbManager.find_records(
        schemas.WorkspaceGroupMember,
        [schemas.WorkspaceGroupMember.group_id == group.id],
    )
    assert members
    assert all(member.status == "inactive" and member.deleted_at is not None for member in members)
    dm.assert_called()
    assert dm.call_args.args[1] == ":wastebasket: disbanded `Shared`."

    status, _resp = handle_group_upsert({"uid": peer["group"].uid, "name": "Renamed"}, peer["fed"])
    assert status == 200
    group = DbManager.find_records(schemas.WorkspaceGroup, [schemas.WorkspaceGroup.uid == peer["group"].uid])[0]
    assert group.status == "disbanded"
    assert group.name == "Shared"


def test_group_upsert_disbanded_does_not_create_a_missing_group(real_db):
    from db import DbManager, schemas
    from federation.api import handle_group_upsert

    peer = _seed_peer()
    status, resp = handle_group_upsert(
        {"uid": "missing-group", "name": "Nope", "status": "disbanded"},
        peer["fed"],
    )
    assert status == 200
    assert resp == {"ok": True}
    assert DbManager.find_records(schemas.WorkspaceGroup, [schemas.WorkspaceGroup.uid == "missing-group"]) == []


def test_group_leave_and_remove_notify_local_managers(real_db):
    from db import DbManager, schemas
    from federation.api import handle_group_leave, handle_group_member_remove

    peer = _seed_peer()
    with (
        patch("helpers.notifications.get_bot_token", return_value="xoxb-test"),
        patch("helpers.notifications.notify_admins_dm") as dm,
    ):
        status, _resp = handle_group_leave(
            {"uid": peer["group"].uid, "team_id": "TSTUB", "notice": ":wave: `Stub` left `Shared`."},
            peer["fed"],
        )
    assert status == 200
    dm.assert_called_once()
    assert dm.call_args.args[1] == ":wave: `Stub` left `Shared`."
    left = DbManager.find_records(schemas.WorkspaceGroup, [schemas.WorkspaceGroup.id == peer["group"].id])[0]
    assert left.invite_code == "GRP-TEST01"

    with (
        patch("helpers.notifications.get_bot_token", return_value="xoxb-test"),
        patch("helpers.notifications.notify_admins_dm") as dm,
    ):
        status, _resp = handle_group_member_remove(
            {
                "uid": peer["group"].uid,
                "team_id": "TSTUB",
                "notice": ":wave: removed `Stub` from `Shared`.",
            },
            peer["fed"],
        )
    assert status == 200
    dm.assert_called_once()
    assert dm.call_args.args[1] == ":wave: removed `Stub` from `Shared`."
    assert (
        DbManager.find_records(
            schemas.WorkspaceGroupMember,
            [
                schemas.WorkspaceGroupMember.group_id == peer["group"].id,
                schemas.WorkspaceGroupMember.workspace_id == peer["stub"].id,
            ],
        )
        == []
    )
    removed = DbManager.find_records(schemas.WorkspaceGroup, [schemas.WorkspaceGroup.id == peer["group"].id])[0]
    assert removed.invite_code != "GRP-TEST01"
    assert removed.invite_code.startswith("FED-")
    assert (
        DbManager.find_records(
            schemas.WorkspaceGroup,
            [schemas.WorkspaceGroup.invite_code == "GRP-TEST01"],
        )
        == []
    )


def test_member_remove_changes_the_invite_code_first(real_db):
    from db import DbManager, schemas
    from federation.api import handle_group_member_remove

    peer = _seed_peer()
    with (
        patch("federation.api.helpers.replace_group_invite_code", side_effect=RuntimeError),
        patch("helpers.notifications.notify_admins_dm") as dm,
        pytest.raises(RuntimeError),
    ):
        handle_group_member_remove(
            {"uid": peer["group"].uid, "team_id": "TSTUB", "notice": ":wave: removed `Stub` from `Shared`."},
            peer["fed"],
        )
    kept = DbManager.find_records(
        schemas.WorkspaceGroupMember,
        [
            schemas.WorkspaceGroupMember.group_id == peer["group"].id,
            schemas.WorkspaceGroupMember.workspace_id == peer["stub"].id,
        ],
    )
    assert kept
    dm.assert_not_called()


def test_replicate_role_and_disband_payloads(real_db):
    from db import DbManager, schemas
    from federation.replicate import replicate_group_invite, replicate_group_leave, replicate_group_upsert

    peer = _seed_peer()
    DbManager.update_records(
        schemas.WorkspaceGroupMember,
        [schemas.WorkspaceGroupMember.workspace_id == peer["stub"].id],
        {schemas.WorkspaceGroupMember.role: "owner"},
    )
    with (
        patch("federation.core.push_group_upsert"),
        patch("federation.core.push_group_invite") as invite,
    ):
        replicate_group_invite(peer["group"], peer["stub"], set_role=True, notice=":key: `Stub` is now an Owner.")
    payload = invite.call_args.args[1]
    assert payload["set_role"] is True
    assert payload["role"] == "owner"
    assert payload["notice"] == ":key: `Stub` is now an Owner."

    DbManager.update_records(
        schemas.WorkspaceGroupMember,
        [schemas.WorkspaceGroupMember.workspace_id == peer["local"].id],
        {schemas.WorkspaceGroupMember.role: "member"},
    )
    with (
        patch("federation.core.push_group_upsert"),
        patch("federation.core.push_group_invite") as invite,
    ):
        replicate_group_invite(peer["group"], peer["local"], set_role=True, notice=":door: `Local` stepped down.")
    payload = invite.call_args.args[1]
    assert payload["set_role"] is True
    assert payload["role"] == "member"

    with patch("federation.core.push_group_leave") as leave:
        replicate_group_leave(peer["group"], peer["local"], notice=":wave: left")
    assert leave.call_args.args[1]["notice"] == ":wave: left"

    peer["group"].status = "disbanded"
    with patch("federation.core.push_group_upsert") as upsert:
        replicate_group_upsert(peer["group"], notice=":wastebasket: gone")
    body = upsert.call_args.args[1]
    assert body["status"] == "disbanded"
    assert body["notice"] == ":wastebasket: gone"


def _pair_headers(instance_id: str):
    return {
        "User-Agent": "SyncBot-Federation/1.0",
        "X-Federation-Signature": "s",
        "X-Federation-Timestamp": "1",
        "X-Federation-Instance": instance_id,
    }


def _known_peer_headers(instance_id: str):
    return {
        "User-Agent": "SyncBot-Federation/1.0",
        "X-Federation-Signature": "sig",
        "X-Federation-Timestamp": "1",
        "X-Federation-Instance": instance_id,
    }


@contextmanager
def _pair_ok():
    from federation.core import get_instance_id

    self_instance_id = get_instance_id()
    with (
        patch("federation.api.federation.validate_webhook_url", return_value=True),
        patch("federation.api.federation.federation_verify", return_value=True),
        patch("federation.api.federation.instance_id_matches_public_key", return_value=True),
        patch("federation.api.federation.get_or_create_instance_keypair", return_value=(None, "our-pem")),
        patch("federation.api.federation.get_instance_id", return_value=self_instance_id),
        patch("federation.api.federation.push_allowed_workspaces", return_value={"ok": True}),
        patch("federation.replicate.replicate_peer_snapshot"),
    ):
        yield


def _seed_inbound_channel(peer, channel_id="CLOCAL"):
    from db import DbManager, schemas

    now = datetime.now(UTC).replace(tzinfo=None)
    sync = DbManager.create_record(
        schemas.Sync(
            title="Inbound",
            group_id=peer["group"].id,
            sync_mode="group",
            uid="33333333-3333-3333-3333-333333333333",
        )
    )
    sc = DbManager.create_record(
        schemas.SyncChannel(
            sync_id=sync.id,
            workspace_id=peer["local"].id,
            channel_id=channel_id,
            status="active",
            publishes=True,
            subscribes=True,
            created_at=now,
        )
    )
    return sync, sc


def test_url_heal_does_not_retrust_untrusted_peer(real_db, monkeypatch):
    monkeypatch.setattr("helpers.federation_enabled", lambda: True)
    from db import DbManager, schemas
    from federation.api import handle_pair

    peer = _seed_peer()
    fed = peer["fed"]
    DbManager.update_records(
        schemas.Instance,
        [schemas.Instance.instance_id == fed.instance_id],
        {schemas.Instance.trust_status: "untrusted"},
    )
    now = datetime.now(UTC).replace(tzinfo=None)
    DbManager.create_record(
        schemas.FederationPairingCode(
            code="FED-CCDDEEFF",
            created_at=now,
            subject_team_id=peer["local"].team_id,
            label="Heal",
        )
    )
    body = {
        "code": "FED-CCDDEEFF",
        "webhook_url": "https://healed.example/api/federation",
        "instance_id": fed.instance_id,
        "public_key": fed.public_key,
    }
    with _pair_ok():
        status, resp = handle_pair(body, json.dumps(body), _pair_headers(body["instance_id"]))
    assert status == 200
    assert resp.get("url_updated") is True
    updated = DbManager.get_record(schemas.Instance, id=fed.instance_id)
    assert updated.trust_status == "untrusted"
    assert updated.webhook_url == "https://healed.example/api/federation"
    leftover = DbManager.find_records(
        schemas.FederationPairingCode,
        [schemas.FederationPairingCode.code == "FED-CCDDEEFF"],
    )
    assert leftover == []


def test_pair_same_instance_id_different_key_untrusts_and_pauses(real_db):
    from db import DbManager, schemas
    from federation.api import handle_pair

    peer = _seed_peer()
    now = datetime.now(UTC).replace(tzinfo=None)
    sync = DbManager.create_record(
        schemas.Sync(
            title="S",
            group_id=peer["group"].id,
            sync_mode="group",
            uid="55555555-5555-5555-5555-555555555555",
        )
    )
    stub_sc = DbManager.create_record(
        schemas.SyncChannel(
            sync_id=sync.id,
            workspace_id=peer["stub"].id,
            channel_id="CSTUB",
            status="active",
            publishes=True,
            subscribes=True,
            created_at=now,
        )
    )
    DbManager.create_record(
        schemas.FederationPairingCode(
            code="FED-99887766",
            created_at=now,
            subject_team_id=peer["local"].team_id,
        )
    )
    body = {
        "code": "FED-99887766",
        "webhook_url": "https://newkey.example/api/federation",
        "instance_id": peer["fed"].instance_id,
        "public_key": "-----BEGIN PUBLIC KEY-----\nNEWdifferentkey\n-----END PUBLIC KEY-----\n",
    }
    with _pair_ok():
        status, resp = handle_pair(body, json.dumps(body), _pair_headers(body["instance_id"]))
    assert status == 409
    assert resp.get("error") == "already_connected"
    updated = DbManager.get_record(schemas.Instance, id=peer["fed"].instance_id)
    assert updated.trust_status == "untrusted"
    paused = DbManager.get_record(schemas.SyncChannel, id=stub_sc.id)
    assert paused.status == "paused"
    leftover = DbManager.find_records(
        schemas.FederationPairingCode,
        [schemas.FederationPairingCode.code == "FED-99887766"],
    )
    assert leftover == []


def test_mark_peer_trusted_unpauses_stub_channels(real_db):
    from db import DbManager, schemas
    from helpers.workspace_kind import mark_peer_trusted, mark_peer_untrusted

    peer = _seed_peer()
    now = datetime.now(UTC).replace(tzinfo=None)
    sync = DbManager.create_record(
        schemas.Sync(
            title="S",
            group_id=peer["group"].id,
            sync_mode="group",
            uid="66666666-6666-6666-6666-666666666666",
        )
    )
    stub_sc = DbManager.create_record(
        schemas.SyncChannel(
            sync_id=sync.id,
            workspace_id=peer["stub"].id,
            channel_id="CSTUB",
            status="active",
            publishes=True,
            subscribes=True,
            created_at=now,
        )
    )
    mark_peer_untrusted(peer["fed"])
    assert DbManager.get_record(schemas.Instance, id=peer["fed"].instance_id).trust_status == "untrusted"
    assert DbManager.get_record(schemas.SyncChannel, id=stub_sc.id).status == "paused"
    mark_peer_trusted(peer["fed"])
    assert DbManager.get_record(schemas.Instance, id=peer["fed"].instance_id).trust_status == "trusted"
    assert DbManager.get_record(schemas.SyncChannel, id=stub_sc.id).status == "active"


def test_pair_new_fingerprint_same_primary_untrusts_old(real_db):
    from db import DbManager, schemas
    from federation.api import handle_pair

    peer = _seed_peer()
    now = datetime.now(UTC).replace(tzinfo=None)
    DbManager.update_records(
        schemas.Instance,
        [schemas.Instance.instance_id == peer["fed"].instance_id],
        {schemas.Instance.primary_team_id: "TPEER"},
    )
    sync = DbManager.create_record(
        schemas.Sync(
            title="S",
            group_id=peer["group"].id,
            sync_mode="group",
            uid="66666666-6666-6666-6666-666666666666",
        )
    )
    stub_sc = DbManager.create_record(
        schemas.SyncChannel(
            sync_id=sync.id,
            workspace_id=peer["stub"].id,
            channel_id="CSTUB",
            status="active",
            publishes=True,
            subscribes=True,
            created_at=now,
        )
    )
    DbManager.create_record(
        schemas.FederationPairingCode(
            code="FED-55667788",
            created_at=now,
            subject_team_id=peer["local"].team_id,
        )
    )
    new_public_key, new_instance_id = _peer_public_key()
    body = {
        "code": "FED-55667788",
        "webhook_url": "https://newfp.example/api/federation",
        "instance_id": new_instance_id,
        "public_key": new_public_key,
        "team_id": "TPEER",
        "workspace_name": "Peer",
    }
    with _pair_ok():
        status, resp = handle_pair(body, json.dumps(body), _pair_headers(body["instance_id"]))
    assert status == 200
    assert resp.get("ok") is True
    old = DbManager.get_record(schemas.Instance, id=peer["fed"].instance_id)
    assert old.trust_status == "untrusted"
    paused = DbManager.get_record(schemas.SyncChannel, id=stub_sc.id)
    assert paused.status == "paused"


def test_pair_keeps_matching_live_install_and_records_pending_stub(real_db):
    from db import DbManager, schemas
    from federation.api import handle_pair

    peer = _seed_peer()
    now = datetime.now(UTC).replace(tzinfo=None)
    former = DbManager.create_record(
        schemas.Workspace(
            team_id="TFORMER",
            workspace_name="Former",
            instance_id=peer["local"].instance_id,
        )
    )
    DbManager.create_record(
        schemas.FederationPairingCode(
            code="FED-AABB1122",
            created_at=now,
            subject_team_id=peer["local"].team_id,
        )
    )
    new_public_key, new_instance_id = _peer_public_key()
    body = {
        "code": "FED-AABB1122",
        "webhook_url": "https://b.example/api/federation",
        "instance_id": new_instance_id,
        "public_key": new_public_key,
        "team_id": "TFORMER",
        "workspace_name": "Former",
    }
    with _pair_ok():
        status, resp = handle_pair(body, json.dumps(body), _pair_headers(body["instance_id"]))
    assert status == 200
    converted = DbManager.get_record(schemas.Workspace, id="TFORMER")
    assert converted.instance_id == peer["local"].instance_id
    assert converted.deleted_at is None
    assert converted.id == former.id
    pending = DbManager.find_records(
        schemas.FederationPendingStub,
        [schemas.FederationPendingStub.workspace_id == former.id],
    )
    assert len(pending) == 1


def test_untrusted_peer_json_and_file_401(real_db, monkeypatch):
    monkeypatch.setattr("helpers.federation_enabled", lambda: True)
    from db import DbManager, schemas
    from federation.api import dispatch_federation_request

    peer = _seed_peer()
    DbManager.update_records(
        schemas.Instance,
        [schemas.Instance.instance_id == peer["fed"].instance_id],
        {schemas.Instance.trust_status: "untrusted"},
    )
    body = json.dumps({"channel_id": "C1", "text": "hi", "post_id": "p1"})
    status, resp = dispatch_federation_request(
        "POST",
        "/api/federation/message",
        body,
        _known_peer_headers(peer["fed"].instance_id),
    )
    assert status == 401
    assert resp.get("error") == "unauthorized"

    data = b"file-bytes"
    sha = hashlib.sha256(data).hexdigest()
    status, _resp = dispatch_federation_request(
        "POST",
        "/api/federation/file",
        "",
        {
            "User-Agent": "SyncBot-Federation/1.0",
            "X-Federation-Instance": peer["fed"].instance_id,
            "X-Federation-Signature": "sig",
            "X-Federation-Timestamp": "1",
            "X-Federation-File-Sha256": sha,
            "X-Federation-File-Index": "0",
            "X-Federation-File-Total": "1",
            "X-Federation-File-Size": str(len(data)),
        },
        raw_body=data,
    )
    assert status == 401


def test_dispatch_clears_file_pins(real_db, monkeypatch):
    monkeypatch.setattr("helpers.federation_enabled", lambda: True)
    import helpers.files as files_mod
    from federation.api import dispatch_federation_request
    from helpers.files import pin_hashed_file

    peer = _seed_peer()
    files_mod._pinned.clear()

    def _pin_then_ok(*_args, **_kwargs):
        pin_hashed_file("deadbeef")
        assert "deadbeef" in files_mod._pinned
        return 200, {"ok": True, "have": False}

    with (
        patch("federation.api.federation.federation_verify", return_value=True),
        patch("federation.api.handle_file_offer", side_effect=_pin_then_ok),
    ):
        status, _resp = dispatch_federation_request(
            "POST",
            "/api/federation/file/offer",
            json.dumps({"sha256": "b" * 64, "size": 1}),
            _known_peer_headers(peer["fed"].instance_id),
        )
    assert status == 200
    assert "deadbeef" not in files_mod._pinned


def test_inbound_edit_and_delete_call_apply_target():
    from federation.api import handle_message_delete, handle_message_edit
    from helpers.envelope import ACTION_DELETE, ACTION_EDIT
    from helpers.sync_apply import ApplyOutcome

    sc = SimpleNamespace(id=9, channel_id="C1", subscribes=True)
    ws = SimpleNamespace(id=2)
    fed = SimpleNamespace(id=7, instance_id="a" * 64, primary_workspace_name="Peer", name="Peer")
    post_meta = SimpleNamespace(ts=1.0)
    with (
        patch("federation.api._accept_inbound_workspace", return_value=True),
        patch("federation.api._resolve_channel_for_federated", return_value=(sc, ws)),
        patch("federation.api._get_post_records", return_value=[post_meta]),
        patch("federation.api._source_stub_id", return_value=99),
        patch("federation.api.apply_target", return_value=ApplyOutcome()) as apply,
    ):
        status, resp = handle_message_edit(
            {"kind": "message", "action": "edit", "post_id": "p1", "channel_id": "C1", "text": "edited"},
            fed,
        )
        assert status == 200
        assert resp["updated"] == 1
        assert apply.call_args.args[0]["action"] == ACTION_EDIT
        assert apply.call_args.kwargs["target_post_meta"] is post_meta

        apply.reset_mock()
        status, resp = handle_message_delete(
            {"kind": "message", "action": "delete", "post_id": "p1", "channel_id": "C1"},
            fed,
        )
        assert status == 200
        assert resp["deleted"] == 1
        apply.assert_called_once()
        assert apply.call_args.args[0]["action"] == ACTION_DELETE


def test_handle_message_idempotent_create_returns_existing_ts(real_db):
    from db import DbManager, schemas
    from federation.api import handle_message
    from helpers.user_action_echo import post_meta_ts

    peer = _seed_peer()
    _sync, sc = _seed_inbound_channel(peer)
    DbManager.create_record(schemas.PostMeta(post_id="pid-1", sync_channel_id=sc.id, ts=post_meta_ts("10.000001")))
    DbManager.create_record(schemas.PostMeta(post_id="pid-1", sync_channel_id=sc.id, ts=post_meta_ts("10.000002")))
    with patch("federation.api.apply_target") as apply:
        status, resp = handle_message(
            {
                "kind": "message",
                "action": "create",
                "post_id": "pid-1",
                "channel_id": "CLOCAL",
            },
            peer["fed"],
        )
        apply.assert_not_called()
        assert status == 200
        assert resp == {"ok": True, "ts": "10.000001", "posted_as_user_id": None}

        status, resp = handle_message(
            {"post_id": "pid-1", "channel_id": "CLOCAL", "text": "hi"},
            peer["fed"],
        )
        assert status == 400
        assert resp["error"] == "missing_kind"


def test_replicated_invite_skips_non_allowlisted_live_workspace(real_db):
    from db import DbManager, schemas
    from federation.api import handle_group_invite, handle_sync_channel_upsert

    peer = _seed_peer()
    DbManager.create_record(
        schemas.FederationWorkspaceAllowlist(
            instance_id=peer["fed"].instance_id,
            workspace_id=peer["local"].id,
        )
    )
    stranger = DbManager.create_record(
        schemas.Workspace(
            team_id="TSTRANGER",
            workspace_name="Stranger",
            instance_id=peer["local"].instance_id,
        )
    )
    status, resp = handle_group_invite(
        {
            "uid": peer["group"].uid,
            "team_id": "TSTRANGER",
            "workspace_name": "Stranger",
        },
        peer["fed"],
    )
    assert status == 404
    assert resp.get("error") == "workspace_not_found"
    members = DbManager.find_records(
        schemas.WorkspaceGroupMember,
        [schemas.WorkspaceGroupMember.workspace_id == stranger.id],
    )
    assert members == []

    sync = DbManager.create_record(
        schemas.Sync(
            title="S",
            group_id=peer["group"].id,
            sync_mode="group",
            uid="44444444-4444-4444-4444-444444444444",
        )
    )
    status, resp = handle_sync_channel_upsert(
        {"sync_uid": sync.uid, "team_id": "TSTRANGER", "channel_id": "CSTRANGE"},
        peer["fed"],
    )
    assert status == 404
    assert resp.get("error") == "workspace_not_found"


def test_inbound_channel_create_skips_resume_notice(real_db):
    from db import DbManager, schemas
    from federation.api import handle_sync_channel_upsert

    peer = _seed_peer()
    now = datetime.now(UTC).replace(tzinfo=None)
    sync = DbManager.create_record(
        schemas.Sync(
            title="Shared",
            group_id=peer["group"].id,
            sync_mode="group",
            uid="55555555-5555-5555-5555-555555555555",
        )
    )
    DbManager.create_record(
        schemas.SyncChannel(
            sync_id=sync.id,
            workspace_id=peer["local"].id,
            channel_id="CLOCAL",
            status="active",
            publishes=True,
            subscribes=True,
            created_at=now,
        )
    )
    with patch("helpers.sync_lifecycle.post_to_other_sync_channels") as notify:
        status, resp = handle_sync_channel_upsert(
            {
                "sync_uid": sync.uid,
                "team_id": "TSTUB",
                "channel_id": "CSTUB",
                "status": "active",
            },
            peer["fed"],
        )
    assert status == 200
    assert resp["ok"] is True
    notify.assert_not_called()


def test_inbound_channel_create_posts_the_origin_sentence(real_db):
    from db import DbManager, schemas
    from federation.api import handle_sync_channel_upsert

    peer = _seed_peer()
    now = datetime.now(UTC).replace(tzinfo=None)
    sync = DbManager.create_record(
        schemas.Sync(
            title="Shared",
            group_id=peer["group"].id,
            sync_mode="group",
            uid="55555555-5555-5555-5555-555555555556",
        )
    )
    DbManager.create_record(
        schemas.SyncChannel(
            sync_id=sync.id,
            workspace_id=peer["local"].id,
            channel_id="CLOCAL",
            status="active",
            publishes=True,
            subscribes=True,
            created_at=now,
        )
    )
    sentence = ":arrows_counterclockwise: *Ada Lovelace (Workspace B)* joined `#general (Workspace B)` to this Sync."
    with patch("helpers.sync_lifecycle.post_to_other_sync_channels") as notify:
        status, resp = handle_sync_channel_upsert(
            {
                "sync_uid": sync.uid,
                "team_id": "TSTUB",
                "channel_id": "CSTUB",
                "status": "active",
                "notice": sentence,
            },
            peer["fed"],
        )
    assert status == 200
    _assert_posted_notice(notify, peer["stub"], sync.id, sentence)


def test_inbound_resume_with_notice_does_not_add_a_second_sentence(real_db):
    from db import DbManager, schemas
    from federation.api import handle_sync_channel_upsert

    peer = _seed_peer()
    now = datetime.now(UTC).replace(tzinfo=None)
    sync = DbManager.create_record(
        schemas.Sync(
            title="Shared",
            group_id=peer["group"].id,
            sync_mode="group",
            uid="55555555-5555-5555-5555-555555555557",
        )
    )
    DbManager.create_record(
        schemas.SyncChannel(
            sync_id=sync.id,
            workspace_id=peer["stub"].id,
            channel_id="CSTUB",
            status="paused",
            publishes=True,
            subscribes=True,
            created_at=now,
        )
    )
    sentence = ":arrow_forward: Syncing with `Workspace B` has been resumed."
    with patch("helpers.sync_lifecycle.post_to_other_sync_channels") as notify:
        status, _resp = handle_sync_channel_upsert(
            {
                "sync_uid": sync.uid,
                "team_id": "TSTUB",
                "channel_id": "CSTUB",
                "status": "active",
                "notice": sentence,
            },
            peer["fed"],
        )
    assert status == 200
    _assert_posted_notice(notify, peer["stub"], sync.id, sentence)


def test_inbound_remove_with_notice_posts_that_sentence_only(real_db):
    from db import DbManager, schemas
    from federation.api import handle_sync_channel_remove

    peer = _seed_peer()
    now = datetime.now(UTC).replace(tzinfo=None)
    sync = DbManager.create_record(
        schemas.Sync(
            title="Shared",
            group_id=peer["group"].id,
            sync_mode="group",
            uid="55555555-5555-5555-5555-555555555558",
        )
    )
    DbManager.create_record(
        schemas.SyncChannel(
            sync_id=sync.id,
            workspace_id=peer["stub"].id,
            channel_id="CSTUB",
            status="active",
            publishes=True,
            subscribes=True,
            created_at=now,
        )
    )
    sentence = ":wave: `#general (Workspace B)` left this Sync."
    with (
        patch("helpers.leave_sync.post_to_other_sync_channels") as notify,
        patch("helpers.leave_sync.notify_channel_or_managers", return_value=True),
        patch("helpers.leave_sync._leave_channel_id"),
    ):
        status, _resp = handle_sync_channel_remove(
            {
                "sync_uid": sync.uid,
                "team_id": "TSTUB",
                "channel_id": "CSTUB",
                "notice": sentence,
            },
            peer["fed"],
        )
    assert status == 200
    _assert_posted_notice(notify, peer["stub"], sync.id, sentence)
    removed = DbManager.find_records(
        schemas.SyncChannel,
        [schemas.SyncChannel.sync_id == sync.id, schemas.SyncChannel.workspace_id == peer["stub"].id],
    )
    assert removed and removed[0].status == "deleted" and removed[0].deleted_at is not None


def test_create_rejoins_onto_the_sync_a_peer_stub_already_revived(real_db):
    from decimal import Decimal

    from db import DbManager, schemas
    from federation.api import handle_sync_channel_upsert
    from handlers.channel_sync import _revive_deleted_create
    from helpers.post_meta import get_post_records
    from helpers.sync_lifecycle import rejoin_sync_channel

    peer = _seed_peer()
    now = datetime.now(UTC).replace(tzinfo=None)
    sync = DbManager.create_record(
        schemas.Sync(
            title="Shared",
            group_id=peer["group"].id,
            sync_mode="group",
            uid="66666666-6666-6666-6666-666666666666",
        )
    )
    local = DbManager.create_record(
        schemas.SyncChannel(
            sync_id=sync.id,
            workspace_id=peer["local"].id,
            channel_id="CLOCAL",
            status="deleted",
            publishes=True,
            subscribes=True,
            deleted_at=now,
            created_at=now,
        )
    )
    DbManager.create_record(
        schemas.SyncChannel(
            sync_id=sync.id,
            workspace_id=peer["stub"].id,
            channel_id="CSTUB",
            status="deleted",
            publishes=True,
            subscribes=True,
            deleted_at=now,
            created_at=now,
        )
    )
    DbManager.create_record(
        schemas.PostMeta(post_id="P-bridge", sync_channel_id=local.id, ts=Decimal("1700000000.000100"))
    )
    status, _resp = handle_sync_channel_upsert(
        {
            "sync_uid": sync.uid,
            "team_id": "TSTUB",
            "channel_id": "CSTUB",
            "channel_name": "general",
            "status": "active",
            "publishes": True,
            "subscribes": True,
        },
        peer["fed"],
    )
    assert status == 200
    assert len(DbManager.find_records(schemas.Sync, [schemas.Sync.group_id == peer["group"].id])) == 1
    stub = DbManager.find_records(
        schemas.SyncChannel,
        [schemas.SyncChannel.sync_id == sync.id, schemas.SyncChannel.workspace_id == peer["stub"].id],
    )[0]
    assert stub.status == "active" and stub.deleted_at is None
    chosen = rejoin_sync_channel(peer["group"].id, peer["local"].id, "CLOCAL")
    assert chosen.id == local.id
    with patch("helpers.sync_lifecycle.ensure_bot_in_conversation", return_value=False):
        revived = _revive_deleted_create(
            MagicMock(), peer["local"], "UADA", peer["group"].id, "CLOCAL", True, True, None, None
        )
    assert revived.sync_id == sync.id
    assert len(DbManager.find_records(schemas.Sync, [schemas.Sync.group_id == peer["group"].id])) == 1
    found = get_post_records("1700000000.000100")
    assert found and found[0][1].id == local.id


def test_inbound_local_row_keeps_its_status_and_posts_the_carried_notice(real_db):
    from db import DbManager, schemas
    from federation.api import handle_sync_channel_upsert

    peer = _seed_peer()
    now = datetime.now(UTC).replace(tzinfo=None)
    sync = DbManager.create_record(
        schemas.Sync(
            title="Shared",
            group_id=peer["group"].id,
            sync_mode="group",
            uid="55555555-5555-5555-5555-555555555559",
        )
    )
    row = DbManager.create_record(
        schemas.SyncChannel(
            sync_id=sync.id,
            workspace_id=peer["local"].id,
            channel_id="CLOCAL",
            status="active",
            publishes=True,
            subscribes=True,
            created_at=now,
        )
    )
    sentence = ":double_vertical_bar: Syncing with `Workspace A` is on hold while the External Connection is untrusted."
    with patch("helpers.sync_lifecycle.post_to_other_sync_channels") as notify:
        status, _resp = handle_sync_channel_upsert(
            {
                "sync_uid": sync.uid,
                "team_id": peer["local"].team_id,
                "channel_id": "CLOCAL",
                "status": "paused",
                "notice": sentence,
            },
            peer["fed"],
        )
    assert status == 200
    assert DbManager.get_record(schemas.SyncChannel, id=row.id).status == "active"
    notify.assert_called_once()
    assert notify.call_args.args[2] == sentence


def _assert_posted_notice(notify, workspace, sync_id: int, sentence: str) -> None:
    notify.assert_called_once()
    posted, sid, text = notify.call_args.args
    assert posted.id == workspace.id
    assert sid == sync_id
    assert text == sentence


def test_post_parts_retries_413_once(tmp_path):
    from federation.deliver import _post_parts

    path = tmp_path / "part.bin"
    path.write_bytes(b"abcdefghij")
    calls = []

    def _push(_fed, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return {"_http_status": 413, "file_chunk_mb": 1}
        return {"_http_status": 200}

    fed = SimpleNamespace(id=1)
    with patch("federation.deliver.push_file_part", side_effect=_push):
        assert _post_parts(fed, "a" * 64, 10, str(path), 8) is True
    assert len(calls) == 2


def test_post_parts_second_413_does_not_recurse(tmp_path):
    from federation.deliver import _post_parts

    path = tmp_path / "part.bin"
    path.write_bytes(b"abcdefghij")
    calls = []

    def _push(_fed, **kwargs):
        calls.append(kwargs)
        return {"_http_status": 413, "file_chunk_mb": 1}

    fed = SimpleNamespace(id=1)
    with patch("federation.deliver.push_file_part", side_effect=_push):
        assert _post_parts(fed, "a" * 64, 10, str(path), 8) is False
    assert len(calls) == 2


def test_post_parts_skips_retry_when_peer_chunk_not_smaller(tmp_path):
    from federation.deliver import _post_parts

    path = tmp_path / "part.bin"
    path.write_bytes(b"abcdefghij")
    calls = []

    def _push(_fed, **kwargs):
        calls.append(kwargs)
        return {"_http_status": 413, "file_chunk_mb": 16}

    fed = SimpleNamespace(id=1)
    with patch("federation.deliver.push_file_part", side_effect=_push):
        assert _post_parts(fed, "a" * 64, 10, str(path), 8) is False
    assert len(calls) == 1


def test_peer_part_bytes_zero_is_unlimited():
    from federation.deliver import _peer_part_bytes

    assert _peer_part_bytes(0) is None
    assert _peer_part_bytes(-1) is None
    assert _peer_part_bytes(4) == 4 * 1024 * 1024
    assert _peer_part_bytes(32) == 32 * 1024 * 1024


def test_post_parts_splits_to_given_part_bytes(tmp_path, monkeypatch):
    from federation.deliver import _post_parts

    monkeypatch.setattr("federation.deliver._peer_part_bytes", lambda _mb: 4)
    path = tmp_path / "part.bin"
    path.write_bytes(b"abcdefghij")
    calls = []

    def _push(_fed, **kwargs):
        calls.append(len(kwargs["payload"]))
        return {"_http_status": 200}

    fed = SimpleNamespace(id=1)
    with patch("federation.deliver.push_file_part", side_effect=_push):
        assert _post_parts(fed, "a" * 64, 10, str(path), 32) is True
    assert calls == [4, 4, 2]


def test_post_parts_zero_cap_sends_whole_file(tmp_path):
    from federation.deliver import _post_parts

    path = tmp_path / "part.bin"
    path.write_bytes(b"abcdefghij")
    calls = []

    def _push(_fed, **kwargs):
        calls.append(len(kwargs["payload"]))
        return {"_http_status": 200}

    fed = SimpleNamespace(id=1)
    with patch("federation.deliver.push_file_part", side_effect=_push):
        assert _post_parts(fed, "a" * 64, 10, str(path), 0) is True
    assert calls == [10]


def test_post_parts_413_without_advertised_cap_does_not_guess(tmp_path):
    from federation.deliver import _post_parts

    path = tmp_path / "part.bin"
    path.write_bytes(b"abcdefghij")
    calls = []

    def _push(_fed, **kwargs):
        calls.append(kwargs)
        return {"_http_status": 413}

    fed = SimpleNamespace(id=1)
    with patch("federation.deliver.push_file_part", side_effect=_push):
        assert _post_parts(fed, "a" * 64, 10, str(path), 0) is False
    assert len(calls) == 1


def test_post_parts_413_after_unlimited_retries_advertised_cap(tmp_path):
    from federation.deliver import _post_parts

    path = tmp_path / "part.bin"
    path.write_bytes(b"abcdefghij")
    calls = []

    def _push(_fed, **kwargs):
        calls.append(len(kwargs["payload"]))
        if len(calls) == 1:
            return {"_http_status": 413, "file_chunk_mb": 1}
        return {"_http_status": 200}

    fed = SimpleNamespace(id=1)
    with patch("federation.deliver.push_file_part", side_effect=_push):
        assert _post_parts(fed, "a" * 64, 10, str(path), 0) is True
    assert calls[0] == 10
    assert len(calls) == 2


def _seed_shared_sync(peer):
    from db import DbManager, schemas
    from helpers.user_action_echo import post_meta_ts

    now = datetime.now(UTC).replace(tzinfo=None)
    sync = DbManager.create_record(
        schemas.Sync(
            title="Shared",
            group_id=peer["group"].id,
            sync_mode="group",
            uid="44444444-4444-4444-4444-444444444444",
        )
    )
    local_sc = DbManager.create_record(
        schemas.SyncChannel(
            sync_id=sync.id,
            workspace_id=peer["local"].id,
            channel_id="CLOCAL",
            channel_name="general",
            status="active",
            publishes=True,
            subscribes=True,
            created_at=now,
        )
    )
    stub_sc = DbManager.create_record(
        schemas.SyncChannel(
            sync_id=sync.id,
            workspace_id=peer["stub"].id,
            channel_id="CSTUB",
            channel_name="announcements",
            status="active",
            publishes=True,
            subscribes=True,
            created_at=now,
        )
    )
    DbManager.create_record(
        schemas.PostMeta(post_id="PARENT", sync_channel_id=local_sc.id, ts=post_meta_ts("10.000001"))
    )
    DbManager.create_record(
        schemas.PostMeta(post_id="PARENT", sync_channel_id=stub_sc.id, ts=post_meta_ts("20.000001"))
    )
    return sync, local_sc, stub_sc


def test_leave_connection_keeps_post_meta_for_retention(real_db):
    from db import DbManager, schemas
    from handlers.federation_cmds import handle_leave_external_connection_confirm

    peer = _seed_peer()
    _sync, _local_sc, stub_sc = _seed_shared_sync(peer)
    body = {
        "user": {"id": "U_ADMIN"},
        "team": {"id": peer["local"].team_id},
        "view": {"private_metadata": json.dumps({"instance_id": peer["fed"].instance_id}), "id": "V1"},
    }
    with (
        patch("handlers.federation_cmds._require_primary_admin", return_value=peer["local"]),
        patch("handlers.federation_cmds._close_modal_done"),
        patch("handlers.federation_cmds.builders.refresh_home_tab_for_workspace"),
    ):
        handle_leave_external_connection_confirm(body, MagicMock(), MagicMock(), {})

    stub = DbManager.get_record(schemas.Workspace, id=peer["stub"].team_id)
    assert stub.deleted_at is not None
    assert stub.instance_id == peer["fed"].instance_id
    paused = DbManager.get_record(schemas.SyncChannel, id=stub_sc.id)
    assert paused.deleted_at is not None
    assert paused.status == "uninstalled"
    assert DbManager.find_records(schemas.PostMeta, [schemas.PostMeta.sync_channel_id == stub_sc.id])
    fed = DbManager.get_record(schemas.Instance, id=peer["fed"].instance_id)
    assert fed.status == "inactive"


def test_reconnect_restores_paused_peer_stubs(real_db):
    from db import DbManager, schemas
    from federation.core import get_or_create_instance
    from handlers.federation_cmds import handle_leave_external_connection_confirm
    from helpers.workspace_kind import is_stub_workspace

    peer = _seed_peer()
    _sync, _local_sc, stub_sc = _seed_shared_sync(peer)
    body = {
        "user": {"id": "U_ADMIN"},
        "team": {"id": peer["local"].team_id},
        "view": {"private_metadata": json.dumps({"instance_id": peer["fed"].instance_id}), "id": "V1"},
    }
    with (
        patch("handlers.federation_cmds._require_primary_admin", return_value=peer["local"]),
        patch("handlers.federation_cmds._close_modal_done"),
        patch("handlers.federation_cmds.builders.refresh_home_tab_for_workspace"),
    ):
        handle_leave_external_connection_confirm(body, MagicMock(), MagicMock(), {})

    get_or_create_instance(
        instance_id=peer["fed"].instance_id,
        webhook_url=peer["fed"].webhook_url,
        public_key=peer["fed"].public_key,
        name="Peer",
    )
    stub = DbManager.get_record(schemas.Workspace, id=peer["stub"].team_id)
    assert is_stub_workspace(stub)
    restored = DbManager.get_record(schemas.SyncChannel, id=stub_sc.id)
    assert restored.deleted_at is None
    assert restored.status == "active"
    assert DbManager.find_records(schemas.PostMeta, [schemas.PostMeta.sync_channel_id == stub_sc.id])
    fed = DbManager.get_record(schemas.Instance, id=peer["fed"].instance_id)
    assert fed.status == "active"


def test_reinstall_reclaims_stub_to_local(real_db):
    from db import DbManager, schemas
    from helpers.workspace import get_workspace_record
    from helpers.workspace_kind import is_local_workspace

    peer = _seed_peer()
    _sync, _local_sc, stub_sc = _seed_shared_sync(peer)
    client = MagicMock()
    with patch("helpers.workspace.resume_restored_sync_channels", return_value={}):
        restored = get_workspace_record(
            peer["stub"].team_id,
            {"team": {"id": peer["stub"].team_id}},
            {"team_id": peer["stub"].team_id},
            client,
        )
    assert is_local_workspace(restored)
    assert restored.deleted_at is None
    channel = DbManager.get_record(schemas.SyncChannel, id=stub_sc.id)
    assert channel.deleted_at is None
    assert channel.status == "active"
    assert DbManager.find_records(schemas.PostMeta, [schemas.PostMeta.sync_channel_id == stub_sc.id])


def test_export_includes_paused_remote_post_meta(real_db):
    from helpers.export_import import build_migration_export
    from helpers.workspace import soft_delete_workspace

    peer = _seed_peer()
    _sync, _local_sc, _stub_sc = _seed_shared_sync(peer)
    soft_delete_workspace(peer["stub"])
    payload = build_migration_export(peer["local"].id)
    channels = {(row["team_id"], row["channel_id"]) for row in payload["sync_channels"]}
    assert ("TSTUB", "CSTUB") not in channels
    assert "44444444-4444-4444-4444-444444444444:CSTUB" not in payload["post_meta"]
    members = payload["groups"][0]["member_team_ids"]
    assert "TSTUB" in members


def test_import_heals_importing_workspace_and_maps_paused_stub(real_db):
    from db import DbManager, schemas
    from helpers.export_import import import_migration_data
    from helpers.workspace import soft_delete_workspace
    from helpers.workspace_kind import is_local_workspace

    peer = _seed_peer()
    _sync, _local_sc, stub_sc = _seed_shared_sync(peer)
    soft_delete_workspace(peer["stub"])
    data = {
        "source_instance": {"instance_id": peer["fed"].instance_id},
        "workspace": {"team_id": peer["stub"].team_id},
        "groups": [
            {
                "uid": peer["group"].uid,
                "name": "Shared",
                "role": "member",
                "member_team_ids": [peer["local"].team_id],
            }
        ],
        "syncs": [{"uid": "44444444-4444-4444-4444-444444444444", "group_uid": peer["group"].uid, "title": "Shared"}],
        "sync_channels": [
            {
                "sync_uid": "44444444-4444-4444-4444-444444444444",
                "team_id": peer["stub"].team_id,
                "channel_id": "CSTUB",
            }
        ],
        "post_meta": {
            "44444444-4444-4444-4444-444444444444:CSTUB": [
                {"post_id": "PARENT", "ts": "20.000001"},
                {"post_id": "EXTRA", "ts": "21.000001"},
            ]
        },
    }
    import_migration_data(
        data,
        peer["stub"].id,
        peer["group"].id,
        team_id_to_workspace_id={peer["stub"].team_id: peer["stub"].id},
    )
    stub = DbManager.get_record(schemas.Workspace, id=peer["stub"].team_id)
    assert is_local_workspace(stub)
    posts = DbManager.find_records(schemas.PostMeta, [schemas.PostMeta.sync_channel_id == stub_sc.id])
    assert {row.post_id for row in posts} == {"PARENT", "EXTRA"}


def test_teams_pauses_stub_not_in_payload_and_unpauses_later(real_db):
    from db import DbManager, schemas
    from federation.api import handle_teams

    peer = _seed_peer()
    status, resp = handle_teams({"workspaces": [{"team_id": "TOTHER", "name": "Other"}]}, peer["fed"])
    assert status == 200
    stub = DbManager.get_record(schemas.Workspace, id="TSTUB")
    assert stub.deleted_at is not None
    local = DbManager.get_record(schemas.Workspace, id="TLOCAL")
    assert local.deleted_at is None

    status, resp = handle_teams({"workspaces": [{"team_id": "TSTUB", "name": "Stub"}]}, peer["fed"])
    assert status == 200
    stub = DbManager.get_record(schemas.Workspace, id="TSTUB")
    assert stub.deleted_at is None


def test_teams_409_when_omitted_stub_owns_mixed_group(real_db):
    from db import DbManager, schemas
    from federation.api import handle_teams
    from helpers.group_roles import MEMBER, OWNER

    peer = _seed_peer()
    now = datetime.now(UTC).replace(tzinfo=None)
    mixed = DbManager.create_record(
        schemas.WorkspaceGroup(
            name="Mixed",
            invite_code="GRP-OWN01",
            status="active",
            created_at=now,
            uid="22222222-2222-2222-2222-222222222222",
        )
    )
    DbManager.create_record(
        schemas.WorkspaceGroupMember(
            group_id=mixed.id,
            workspace_id=peer["stub"].id,
            status="active",
            role=OWNER,
            joined_at=now,
        )
    )
    DbManager.create_record(
        schemas.WorkspaceGroupMember(
            group_id=mixed.id,
            workspace_id=peer["local"].id,
            status="active",
            role=MEMBER,
            joined_at=now,
        )
    )
    status, resp = handle_teams({"workspaces": [{"team_id": "TOTHER", "name": "Other"}]}, peer["fed"])
    assert status == 409
    assert resp["error"] == "owner_on_connection"
    assert "TSTUB" in resp["team_ids"]
    stub = DbManager.get_record(schemas.Workspace, id="TSTUB")
    assert stub.deleted_at is None


def test_teams_clears_local_home_hash_on_drop(real_db):
    from federation.api import handle_teams

    peer = _seed_peer()
    with patch("federation.api.invalidate_home_tab_caches_for_team") as inv:
        handle_teams({"workspaces": [{"team_id": "TOTHER", "name": "Other"}]}, peer["fed"])
    assert any(call.args[0] == peer["local"].team_id for call in inv.call_args_list)


def test_handle_group_leave_invalidates_local_homes(real_db):
    from federation.api import handle_group_leave

    peer = _seed_peer()
    # Replicated group changes drop member Homes through helpers.export_import.invalidate_group_member_homes.
    with patch("helpers.export_import.invalidate_home_tab_caches_for_team") as inv:
        status, _resp = handle_group_leave(
            {"uid": peer["group"].uid, "team_id": peer["stub"].team_id},
            peer["fed"],
        )
    assert status == 200
    assert any(call.args[0] == peer["local"].team_id for call in inv.call_args_list)


def test_a_leave_ends_a_pending_invite_for_a_local_workspace(real_db):
    from db import DbManager, schemas
    from federation.api import handle_group_leave

    peer = _seed_peer()
    workspace_b = _add_workspace_b(peer)
    pending = DbManager.create_record(
        schemas.WorkspaceGroupMember(
            group_id=peer["group"].id,
            workspace_id=workspace_b.id,
            status="pending",
            role="member",
            dm_messages='[{"channel": "D1", "ts": "1.0"}]',
        )
    )
    notice = "Invite cancelled."
    with patch("helpers.update_group_invite_dms") as update:
        handle_group_leave(
            {"uid": peer["group"].uid, "team_id": "TLOCALB", "notice": notice},
            peer["fed"],
        )
    remaining = [row for row in _memberships(peer["group"].id, workspace_b.id) if row.id == pending.id]
    assert remaining == []
    update.assert_called_once()
    assert update.call_args.args[0].id == pending.id
    assert update.call_args.args[1].id == workspace_b.id
    assert update.call_args.args[2] == notice


def test_a_leave_ends_a_pending_invite_for_a_peer_workspace(real_db):
    from db import DbManager, schemas
    from federation.api import handle_group_leave

    peer = _seed_peer()
    rows = _memberships(peer["group"].id, peer["stub"].id)
    DbManager.update_records(
        schemas.WorkspaceGroupMember,
        [schemas.WorkspaceGroupMember.id == rows[0].id],
        {schemas.WorkspaceGroupMember.status: "pending"},
    )
    handle_group_leave({"uid": peer["group"].uid, "team_id": "TSTUB"}, peer["fed"])
    assert _memberships(peer["group"].id, peer["stub"].id) == []


def test_handle_group_upsert_invalidates_on_rename(real_db):
    from federation.api import handle_group_upsert

    peer = _seed_peer()
    with patch("helpers.export_import.invalidate_home_tab_caches_for_team") as inv:
        status, _resp = handle_group_upsert({"uid": peer["group"].uid, "name": "Renamed"}, peer["fed"])
    assert status == 200
    assert any(call.args[0] == peer["local"].team_id for call in inv.call_args_list)


def test_handle_message_returns_apply_outcome_strings_not_expired_post_meta(real_db):
    from federation.api import handle_message
    from helpers.sync_apply import ApplyOutcome

    peer = _seed_peer()
    _sync, sc = _seed_inbound_channel(peer)

    class _Expired:
        @property
        def ts(self):
            raise AssertionError("expired PostMeta.ts")

        @property
        def posted_as_user_id(self):
            raise AssertionError("expired PostMeta.posted_as_user_id")

    with patch(
        "federation.api.apply_target",
        return_value=ApplyOutcome(
            created=[_Expired()],
            ts="11.000001",
            posted_as_user_id="UADA",
        ),
    ):
        status, resp = handle_message(
            {
                "kind": "message",
                "action": "create",
                "post_id": "p-new",
                "channel_id": sc.channel_id,
                "text": "hi",
            },
            peer["fed"],
        )
    assert status == 200
    assert resp == {"ok": True, "ts": "11.000001", "posted_as_user_id": "UADA"}


def test_home_hash_changes_when_remote_stub_paused(real_db):
    from builders.home import _home_tab_content_hash
    from federation.api import handle_group_upsert, handle_sync_upsert, handle_teams
    from helpers.workspace import replace_federation_allowlist

    peer = _seed_peer()
    sync, _sc = _seed_inbound_channel(peer)
    with (
        patch("builders.home.helpers.user_permission_lists", return_value=((), ())),
        patch("builders.home.helpers.is_db_reset_visible_for_workspace", return_value=False),
    ):
        before = _home_tab_content_hash(peer["local"], "U1", is_manager=True, is_admin=True)
        replace_federation_allowlist(peer["fed"].instance_id, [peer["local"].id])
        after_allow = _home_tab_content_hash(peer["local"], "U1", is_manager=True, is_admin=True)
        handle_teams({"workspaces": [{"team_id": "TOTHER", "name": "Other"}]}, peer["fed"])
        after_pause = _home_tab_content_hash(peer["local"], "U1", is_manager=True, is_admin=True)
        handle_group_upsert({"uid": peer["group"].uid, "name": "Renamed Group"}, peer["fed"])
        after_name = _home_tab_content_hash(peer["local"], "U1", is_manager=True, is_admin=True)
        handle_sync_upsert(
            {"uid": sync.uid, "group_uid": peer["group"].uid, "title": "Renamed Sync"},
            peer["fed"],
        )
        after_title = _home_tab_content_hash(peer["local"], "U1", is_manager=True, is_admin=True)
    assert before != after_allow
    assert after_allow != after_pause
    assert after_pause != after_name
    assert after_name != after_title


def test_edit_ack_blocks_dropping_owner_of_mixed_group(real_db):
    import json

    from db import DbManager, schemas
    from federation.core import get_instance_id
    from handlers.federation_cmds import handle_edit_external_connection_submit_ack
    from helpers.workspace import replace_federation_allowlist
    from slack import actions

    peer = _seed_peer()
    other = DbManager.create_record(
        schemas.Workspace(team_id="T3", workspace_name="Workspace B", instance_id=get_instance_id())
    )
    replace_federation_allowlist(peer["fed"].instance_id, [peer["local"].id, other.id])
    now = datetime.now(UTC).replace(tzinfo=None)
    mixed = DbManager.create_record(
        schemas.WorkspaceGroup(
            name="Mixed",
            invite_code="GRP-ACK01",
            status="active",
            created_at=now,
            uid="33333333-3333-3333-3333-333333333333",
        )
    )
    DbManager.create_record(
        schemas.WorkspaceGroupMember(
            group_id=mixed.id,
            workspace_id=other.id,
            status="active",
            role="owner",
            joined_at=now,
        )
    )
    DbManager.create_record(
        schemas.WorkspaceGroupMember(
            group_id=mixed.id,
            workspace_id=peer["stub"].id,
            status="active",
            role="member",
            joined_at=now,
        )
    )
    body = {
        "view": {
            "private_metadata": json.dumps({"instance_id": peer["fed"].instance_id}),
            "state": {
                "values": {
                    "name": {actions.CONFIG_EDIT_EXTERNAL_CONNECTION_NAME: {"value": "Partner Org"}},
                    "ws": {
                        actions.CONFIG_EDIT_EXTERNAL_WORKSPACES: {
                            "selected_options": [{"value": str(peer["local"].id)}],
                        }
                    },
                }
            },
        }
    }
    with (
        patch("handlers.federation_cmds.helpers.federation_enabled", return_value=True),
        patch("helpers.workspace.WebClient") as slack,
    ):
        result = handle_edit_external_connection_submit_ack(body, MagicMock(), {})
    slack.assert_not_called()
    assert result is not None
    assert result["response_action"] == "errors"
    assert "Give Up Ownership" in result["errors"][actions.CONFIG_EDIT_EXTERNAL_WORKSPACES]
    assert "Workspace B" in result["errors"][actions.CONFIG_EDIT_EXTERNAL_WORKSPACES]


def test_edit_ack_allows_same_instance_only_owner(real_db):
    import json

    from db import DbManager, schemas
    from federation.core import get_instance_id
    from handlers.federation_cmds import handle_edit_external_connection_submit_ack
    from helpers.workspace import replace_federation_allowlist
    from slack import actions

    peer = _seed_peer()
    other = DbManager.create_record(
        schemas.Workspace(team_id="T3", workspace_name="Workspace B", instance_id=get_instance_id())
    )
    replace_federation_allowlist(peer["fed"].instance_id, [peer["local"].id, other.id])
    now = datetime.now(UTC).replace(tzinfo=None)
    local_only = DbManager.create_record(
        schemas.WorkspaceGroup(
            name="Local Only",
            invite_code="GRP-ACK02",
            status="active",
            created_at=now,
            uid="44444444-4444-4444-4444-444444444444",
        )
    )
    DbManager.create_record(
        schemas.WorkspaceGroupMember(
            group_id=local_only.id,
            workspace_id=other.id,
            status="active",
            role="owner",
            joined_at=now,
        )
    )
    DbManager.create_record(
        schemas.WorkspaceGroupMember(
            group_id=local_only.id,
            workspace_id=peer["local"].id,
            status="active",
            role="member",
            joined_at=now,
        )
    )
    body = {
        "view": {
            "private_metadata": json.dumps({"instance_id": peer["fed"].instance_id}),
            "state": {
                "values": {
                    "name": {actions.CONFIG_EDIT_EXTERNAL_CONNECTION_NAME: {"value": "Partner Org"}},
                    "ws": {
                        actions.CONFIG_EDIT_EXTERNAL_WORKSPACES: {
                            "selected_options": [{"value": str(peer["local"].id)}],
                        }
                    },
                }
            },
        }
    }
    with patch("handlers.federation_cmds.helpers.federation_enabled", return_value=True):
        result = handle_edit_external_connection_submit_ack(body, MagicMock(), {})
    assert result is None


def test_edit_ack_allows_drop_after_demote(real_db):
    import json

    from db import DbManager, schemas
    from federation.core import get_instance_id
    from handlers.federation_cmds import handle_edit_external_connection_submit_ack
    from helpers.group_roles import MEMBER
    from helpers.workspace import replace_federation_allowlist
    from slack import actions

    peer = _seed_peer()
    other = DbManager.create_record(
        schemas.Workspace(team_id="T3", workspace_name="Workspace B", instance_id=get_instance_id())
    )
    replace_federation_allowlist(peer["fed"].instance_id, [peer["local"].id, other.id])
    now = datetime.now(UTC).replace(tzinfo=None)
    mixed = DbManager.create_record(
        schemas.WorkspaceGroup(
            name="Mixed",
            invite_code="GRP-ACK03",
            status="active",
            created_at=now,
            uid="55555555-5555-5555-5555-555555555555",
        )
    )
    owner_row = DbManager.create_record(
        schemas.WorkspaceGroupMember(
            group_id=mixed.id,
            workspace_id=other.id,
            status="active",
            role="owner",
            joined_at=now,
        )
    )
    DbManager.create_record(
        schemas.WorkspaceGroupMember(
            group_id=mixed.id,
            workspace_id=peer["stub"].id,
            status="active",
            role="member",
            joined_at=now,
        )
    )
    DbManager.update_records(
        schemas.WorkspaceGroupMember,
        [schemas.WorkspaceGroupMember.id == owner_row.id],
        {schemas.WorkspaceGroupMember.role: MEMBER},
    )
    body = {
        "view": {
            "private_metadata": json.dumps({"instance_id": peer["fed"].instance_id}),
            "state": {
                "values": {
                    "name": {actions.CONFIG_EDIT_EXTERNAL_CONNECTION_NAME: {"value": "Partner Org"}},
                    "ws": {
                        actions.CONFIG_EDIT_EXTERNAL_WORKSPACES: {
                            "selected_options": [{"value": str(peer["local"].id)}],
                        }
                    },
                }
            },
        }
    }
    with patch("handlers.federation_cmds.helpers.federation_enabled", return_value=True):
        result = handle_edit_external_connection_submit_ack(body, MagicMock(), {})
    assert result is None


def test_handle_sync_channel_remove_purge_deletes_the_local_row(real_db):
    from db import DbManager, schemas
    from federation.api import handle_sync_channel_remove

    peer = _seed_peer()
    sync, channel = _seed_inbound_channel(peer)
    status, _resp = handle_sync_channel_remove(
        {
            "sync_uid": sync.uid,
            "team_id": "TLOCAL",
            "channel_id": "CLOCAL",
            "purge": True,
            "notice": ":wastebasket: history was deleted.",
        },
        peer["fed"],
    )
    assert status == 200
    assert DbManager.get_record(schemas.SyncChannel, id=channel.id) is None


def test_handle_sync_channel_remove_invalidates_local_homes(real_db):
    from db import DbManager, schemas
    from federation.api import handle_sync_channel_remove

    peer = _seed_peer()
    sync, _sc = _seed_inbound_channel(peer)
    now = datetime.now(UTC).replace(tzinfo=None)
    DbManager.create_record(
        schemas.SyncChannel(
            sync_id=sync.id,
            workspace_id=peer["stub"].id,
            channel_id="CSTUB",
            status="active",
            publishes=True,
            subscribes=True,
            created_at=now,
        )
    )
    with patch("helpers.export_import.invalidate_home_tab_caches_for_team") as inv:
        status, _resp = handle_sync_channel_remove(
            {"sync_uid": sync.uid, "team_id": peer["stub"].team_id, "channel_id": "CSTUB"},
            peer["fed"],
        )
    assert status == 200
    assert any(call.args[0] == peer["local"].team_id for call in inv.call_args_list)


def test_dispatch_missing_header_400_skips_json(monkeypatch):
    monkeypatch.setattr("helpers.federation_enabled", lambda: True)
    from federation.api import dispatch_federation_request

    with patch("federation.api.json.loads") as loads:
        status, resp = dispatch_federation_request(
            "POST",
            "/api/federation/message",
            '{"channel_id":"C1"}',
            {"User-Agent": "SyncBot-Federation/1.0"},
        )
    assert status == 400
    assert resp.get("error") == "invalid_header"
    loads.assert_not_called()


def test_dispatch_bad_signature_401(real_db, monkeypatch):
    monkeypatch.setattr("helpers.federation_enabled", lambda: True)
    from federation.api import dispatch_federation_request

    peer = _seed_peer()
    status, resp = dispatch_federation_request(
        "POST",
        "/api/federation/message",
        '{"channel_id":"C1"}',
        _known_peer_headers(peer["fed"].instance_id),
    )
    assert status == 401
    assert resp.get("error") == "unauthorized"


def test_ping_unsigned_401(real_db, monkeypatch):
    monkeypatch.setattr("helpers.federation_enabled", lambda: True)
    from federation.api import dispatch_federation_request

    peer = _seed_peer()
    status, resp = dispatch_federation_request(
        "GET",
        "/api/federation/ping",
        "",
        _known_peer_headers(peer["fed"].instance_id),
    )
    assert status == 401
    assert resp.get("error") == "unauthorized"


def test_pair_unknown_code_does_not_dns(real_db, monkeypatch):
    monkeypatch.setattr("helpers.federation_enabled", lambda: True)
    from federation.api import handle_pair

    public_key, instance_id = _peer_public_key()
    body = {
        "code": "FED-DEADBEEF",
        "webhook_url": "https://peer.example/api/federation",
        "instance_id": instance_id,
        "public_key": public_key,
    }
    with (
        patch("federation.api.federation.federation_verify", return_value=True),
        patch("federation.api.federation.instance_id_matches_public_key", return_value=True),
        patch("federation.api.federation.validate_webhook_url") as dns,
    ):
        status, resp = handle_pair(body, json.dumps(body), _pair_headers(instance_id))
    assert status == 404
    assert resp.get("message") == "Not Found"
    dns.assert_not_called()


def test_channel_id_length_21_is_valid():
    from federation.api import _validate_fields

    channel_id = "C" + ("0" * 20)
    assert len(channel_id) == 21
    err = _validate_fields(
        {"channel_id": channel_id, "post_id": "p1", "kind": "message", "action": "create"},
        ["channel_id", "post_id", "kind", "action"],
    )
    assert err is None
    too_long = "C" + ("0" * 100)
    assert _validate_fields({"channel_id": too_long}, ["channel_id"]) == "channel_id_too_long"


def test_page_users_splits_when_over_cap(monkeypatch):
    from federation import api as fed_api

    real_dumps = json.dumps

    def fake_dumps(obj):
        if len(obj.get("users") or []) >= 2:
            return "x" * (2 * 1024 * 1024)
        return real_dumps(obj)

    monkeypatch.setattr(fed_api.json, "dumps", fake_dumps)
    page, nxt = fed_api._page_users_for_json_cap(
        [{"user_id": "U1"}, {"user_id": "U2"}, {"user_id": "U3"}],
        0,
        1,
    )
    assert page == [{"user_id": "U1"}]
    assert nxt == 1


def test_handle_users_returns_json_chunk_mb(real_db, monkeypatch):
    monkeypatch.setattr("helpers.federation_enabled", lambda: True)
    from federation.api import handle_users

    peer = _seed_peer()
    status, resp = handle_users({"users": [], "offset": 0}, peer["fed"])
    assert status == 200
    assert resp["ok"] is True
    assert "json_chunk_mb" in resp
    assert "next_offset" not in resp


def test_group_invite_reuses_soft_deleted_membership(real_db):
    from db import DbManager, schemas
    from federation.api import handle_group_invite

    peer = _seed_peer()
    now = datetime.now(UTC).replace(tzinfo=None)
    rows = DbManager.find_records(
        schemas.WorkspaceGroupMember,
        [
            schemas.WorkspaceGroupMember.group_id == peer["group"].id,
            schemas.WorkspaceGroupMember.workspace_id == peer["stub"].id,
        ],
    )
    DbManager.update_records(
        schemas.WorkspaceGroupMember,
        [schemas.WorkspaceGroupMember.id == rows[0].id],
        {schemas.WorkspaceGroupMember.deleted_at: now, schemas.WorkspaceGroupMember.status: "active"},
    )
    status, _resp = handle_group_invite(
        {"uid": peer["group"].uid, "team_id": "TSTUB", "role": "owner"},
        peer["fed"],
    )
    assert status == 200
    again = DbManager.find_records(
        schemas.WorkspaceGroupMember,
        [
            schemas.WorkspaceGroupMember.group_id == peer["group"].id,
            schemas.WorkspaceGroupMember.workspace_id == peer["stub"].id,
            schemas.WorkspaceGroupMember.deleted_at.is_(None),
        ],
    )
    assert len(again) == 1
    assert again[0].id == rows[0].id
    assert again[0].role == "member"


def test_inbound_pending_row_is_an_empty_success(real_db):
    from db import DbManager, schemas
    from federation.api import handle_message

    peer = _seed_peer()
    channel = DbManager.create_record(
        schemas.SyncChannel(
            sync_id=DbManager.create_record(
                schemas.Sync(
                    title="Move",
                    group_id=peer["group"].id,
                    sync_mode="group",
                    uid="33333333-3333-3333-3333-333333333333",
                )
            ).id,
            workspace_id=peer["local"].id,
            channel_id="CWAIT",
            status="pending",
            publishes=True,
            subscribes=True,
            created_at=datetime.now(UTC).replace(tzinfo=None),
        )
    )
    status, resp = handle_message(
        {
            "channel_id": "CWAIT",
            "post_id": "P1",
            "kind": "message",
            "action": "create",
            "text": "hello",
            "sync_uid": "33333333-3333-3333-3333-333333333333",
        },
        peer["fed"],
    )
    assert status == 200
    assert resp.get("ts") is None
    stored = DbManager.find_records(schemas.PostMeta, [schemas.PostMeta.sync_channel_id == channel.id])
    assert stored == []


def test_active_delivery_retries_then_stops(monkeypatch):
    from helpers.sync_pipeline import _while_active

    sleeps: list[float] = []
    monkeypatch.setattr("helpers.sync_pipeline._sleep_between_attempts", sleeps.append)
    calls = {"n": 0}

    class _row:
        channel_id = "C1"
        status = "active"
        subscribes = True
        deleted_at = None

    monkeypatch.setattr("helpers.sync_pipeline.get_active_sync_channel", lambda _row_in: _row())
    monkeypatch.setattr("helpers.sync_pipeline.channel_subscribes", lambda _row_in: True)

    def work(_live):
        calls["n"] += 1
        return False

    assert _while_active(_row(), work) is False
    assert calls["n"] == 3
    assert sleeps == [1, 2]


def test_inbound_convert_does_not_announce_and_each_peer_announces_once(real_db):
    from datetime import UTC, datetime

    from db import DbManager, schemas
    from federation.api import _convert_pending_target_to_stub
    from helpers.notifications import notify_migration

    peer = _seed_peer()
    now = datetime.now(UTC)
    DbManager.update_records(
        schemas.Workspace,
        [schemas.Workspace.id == peer["local"].id],
        {schemas.Workspace.deleted_at: now},
    )
    sync = DbManager.create_record(
        schemas.Sync(
            title="Move",
            group_id=peer["group"].id,
            sync_mode="group",
            uid="77777777-7777-7777-7777-777777777777",
        )
    )
    DbManager.create_record(
        schemas.SyncChannel(
            sync_id=sync.id,
            workspace_id=peer["local"].id,
            channel_id="CMOVE",
            status="uninstalled",
            publishes=True,
            subscribes=True,
            created_at=now.replace(tzinfo=None),
            deleted_at=now.replace(tzinfo=None),
        )
    )
    DbManager.create_record(
        schemas.FederationPendingStub(
            workspace_id=peer["local"].id,
            instance_id=peer["fed"].instance_id,
        )
    )
    other = DbManager.create_record(
        schemas.Workspace(
            team_id="TOTHER",
            workspace_name="Workspace A",
            instance_id=peer["local"].instance_id,
        )
    )
    DbManager.create_record(
        schemas.WorkspaceGroupMember(
            group_id=peer["group"].id,
            workspace_id=other.id,
            status="active",
            role="member",
            joined_at=now.replace(tzinfo=None),
        )
    )
    with patch("helpers.notifications.notify_migration") as announced:
        _convert_pending_target_to_stub("CMOVE", peer["fed"])
    announced.assert_not_called()
    moved = DbManager.get_record(schemas.Workspace, id="TLOCAL")
    assert moved.instance_id == peer["fed"].instance_id
    assert moved.deleted_at is None

    client = MagicMock()
    client.conversations_open.return_value = {"channel": {"id": "D1"}}
    with (
        patch("helpers.notifications.get_bot_token", return_value="xoxb-test"),
        patch("helpers.notifications.get_manager_ids", return_value=["U1"]),
        patch("helpers.notifications.WebClient", return_value=client),
    ):
        notify_migration("TLOCAL", peer["fed"].instance_id)
        notify_migration("TLOCAL", peer["fed"].instance_id)
        assert client.chat_postMessage.call_count == 1
        text = client.chat_postMessage.call_args.kwargs["text"]
        assert "Workspace: Local" in text
        assert "Workspaces in Sync" not in text
        notify_migration("TLOCAL", "c" * 64)
        assert client.chat_postMessage.call_count == 2


def _slack_app_client():
    client = MagicMock()
    client.auth_test.return_value = {"ok": True, "user_id": "UBOT", "bot_id": "BBOT"}
    client.users_info.return_value = {
        "ok": True,
        "user": {
            "id": "UBOT",
            "real_name": "SyncBot (Test)",
            "profile": {"display_name": "SyncBot (Test)", "real_name": "SyncBot (Test)"},
        },
    }
    client.conversations_open.return_value = {"channel": {"id": "D1"}}
    return client


def test_install_and_uninstall_name_the_slack_app(real_db, monkeypatch):
    from db import DbManager, schemas
    from helpers.notifications import notify_primary_action
    from helpers.workspace import get_workspace_record

    peer = _seed_peer()
    DbManager.update_records(
        schemas.Workspace,
        [schemas.Workspace.id == peer["stub"].id],
        {schemas.Workspace.workspace_name: "Workspace A"},
    )
    monkeypatch.setenv("PRIMARY_WORKSPACE", peer["local"].team_id)
    client = _slack_app_client()
    with (
        patch("helpers.notifications.get_bot_token", return_value="xoxb-app"),
        patch("helpers.notifications.WebClient", return_value=client),
        patch("helpers.notifications.get_manager_ids", return_value=["U1"]),
        patch("helpers.workspace.resume_restored_sync_channels", return_value={}),
        patch("helpers.format_admin_label", return_value="`Ada Lovelace (Workspace A)`") as person,
    ):
        get_workspace_record(
            peer["stub"].team_id,
            {"user": {"id": "UADA"}, "team": {"id": peer["stub"].team_id}},
            {"team_id": peer["stub"].team_id},
            client,
        )
        install = client.chat_postMessage.call_args.kwargs["text"]
        assert "Acting Manager: SyncBot (Test)" in install
        assert "Action: Install" in install
        assert "Workspace: Workspace A" in install
        assert "Ada Lovelace" not in install
        person.assert_not_called()

        peer["local"].workspace_name = "Workspace A"
        notify_primary_action(peer["local"], "Uninstall")
        uninstall = client.chat_postMessage.call_args.kwargs["text"]
        assert "Acting Manager: SyncBot (Test)" in uninstall
        assert "Action: Uninstall" in uninstall
        assert "Ada Lovelace" not in uninstall


def test_live_list_posts_install_and_a_later_move_still_posts_migration(real_db):
    from db import DbManager, schemas
    from helpers._cache import clear_all_caches
    from helpers.workspace_kind import convert_workspace_to_stub

    peer = _seed_peer()
    DbManager.update_records(
        schemas.Workspace,
        [schemas.Workspace.id == peer["local"].id],
        {schemas.Workspace.workspace_name: "Workspace A"},
    )
    now = datetime.now(UTC).replace(tzinfo=None)
    other = DbManager.create_record(
        schemas.Workspace(
            team_id="TOTHER",
            workspace_name="Workspace B",
            instance_id=peer["local"].instance_id,
        )
    )
    DbManager.create_record(
        schemas.WorkspaceGroupMember(
            group_id=peer["group"].id,
            workspace_id=other.id,
            status="active",
            role="member",
            joined_at=now,
        )
    )
    client = _slack_app_client()
    with (
        patch("helpers.notifications.get_bot_token", return_value="xoxb-app"),
        patch("helpers.notifications.WebClient", return_value=client),
        patch("helpers.notifications.get_manager_ids", return_value=["U1"]),
    ):
        client.users_info.return_value["user"]["real_name"] = "SyncBot (GCP Test)"
        client.users_info.return_value["user"]["profile"]["display_name"] = "SyncBot (GCP Test)"
        client.users_info.return_value["user"]["profile"]["real_name"] = "SyncBot (GCP Test)"
        assert (
            convert_workspace_to_stub(
                peer["local"].team_id,
                peer["fed"].instance_id,
                app_name="SyncBot (Test)",
            )
            == "refused_live"
        )
        install_count = client.chat_postMessage.call_count
        assert install_count >= 1
        for call in client.chat_postMessage.call_args_list:
            text = call.kwargs["text"]
            assert "Action: Install" in text
            assert "Acting Manager: SyncBot (Test)" in text
            assert "SyncBot (GCP Test)" not in text
            assert "Workspace: Workspace A" in text
        assert not DbManager.find_records(
            schemas.InstanceSetting,
            [schemas.InstanceSetting.key == "migration_notices"],
        )
        claim_key = f"install_from:{peer['local'].team_id}:{peer['fed'].instance_id}"[:64]
        assert DbManager.get_record(schemas.InstanceSetting, claim_key) is not None

        clear_all_caches()
        assert (
            convert_workspace_to_stub(
                peer["local"].team_id,
                peer["fed"].instance_id,
                app_name="SyncBot (Test)",
            )
            == "refused_live"
        )
        assert client.chat_postMessage.call_count == install_count

        DbManager.update_records(
            schemas.Workspace,
            [schemas.Workspace.id == peer["local"].id],
            {schemas.Workspace.deleted_at: datetime.now(UTC)},
        )
        assert (
            convert_workspace_to_stub(peer["local"].team_id, peer["fed"].instance_id, source="uninstall") == "converted"
        )
        assert client.chat_postMessage.call_count == install_count + 1
        moved = client.chat_postMessage.call_args.kwargs["text"]
        assert "Action: Migration" in moved
        assert "Acting Manager: SyncBot (GCP Test)" in moved
        assert "Workspace: Workspace A" in moved


def test_teams_push_names_the_app_that_listed_the_workspace(real_db):
    from federation.api import handle_teams

    peer = _seed_peer()
    with patch("helpers.workspace_kind.notify_live_workspace_conflict") as notified:
        status, resp = handle_teams(
            {
                "workspaces": [{"team_id": peer["local"].team_id, "name": "Workspace A"}],
                "app_name": "SyncBot (Test)",
            },
            peer["fed"],
        )
    assert status == 200
    assert resp["results"][peer["local"].team_id] == "refused_live"
    notified.assert_called_once_with(
        peer["local"].team_id,
        peer["fed"].instance_id,
        app_name="SyncBot (Test)",
    )


def test_teams_payload_carries_this_app_name(real_db, monkeypatch):
    from federation.core import teams_allowlist_payload

    peer = _seed_peer()
    monkeypatch.setenv("PRIMARY_WORKSPACE", peer["local"].team_id)
    with (
        patch("helpers.workspace.get_bot_token", return_value="xoxb-aws"),
        patch("helpers.slack_api.get_app_display_name", return_value="SyncBot (Test)"),
    ):
        payload = teams_allowlist_payload(peer["fed"].instance_id)
    assert payload["app_name"] == "SyncBot (Test)"
