"""A Workspace is created, restored, or refreshed only from its own install."""

import os
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("DATABASE_HOST", "localhost")
os.environ.setdefault("DATABASE_USER", "root")
os.environ.setdefault("DATABASE_PASSWORD", "test")
os.environ.setdefault("DATABASE_SCHEMA", "syncbot")
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-0-0")

from db import DbManager, schemas  # noqa: E402


@pytest.fixture
def real_db(tmp_path):
    import db as db_mod
    from db import initialize_database

    url = f"sqlite:///{tmp_path / 'own-install.db'}"
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


def _live(team_id: str, name: str):
    from federation.core import get_instance_id

    return DbManager.create_record(
        schemas.Workspace(team_id=team_id, workspace_name=name, instance_id=get_instance_id())
    )


def _stub(team_id: str, name: str):
    peer_id = "b" * 64
    DbManager.create_record(
        schemas.Instance(
            instance_id=peer_id,
            public_key="peer-public",
            webhook_url="https://peer.example/federation",
            status="active",
            trust_status="trusted",
            created_at=_now(),
        )
    )
    return DbManager.create_record(schemas.Workspace(team_id=team_id, workspace_name=name, instance_id=peer_id))


def test_live_workspace_keeps_its_name_and_token_for_another_install(real_db):
    from helpers.workspace import get_workspace_record

    workspace = _live("T0000000B", "Workspace B")
    client = MagicMock()
    client.team_info.side_effect = AssertionError("team_info")
    with patch("helpers.encryption_installation_store.EncryptedSQLAlchemyInstallationStore") as store_cls:
        store_cls.return_value.save_bot = MagicMock()
        got = get_workspace_record(
            "T0000000B",
            {"team": {"id": "T0000000B"}},
            {"team_id": "T0000000A", "bot_token": "xoxb-a"},
            client,
        )
    assert got is not None
    assert got.id == workspace.id
    reloaded = DbManager.get_record(schemas.Workspace, id="T0000000B")
    assert reloaded.workspace_name == "Workspace B"
    client.team_info.assert_not_called()
    store_cls.return_value.save_bot.assert_not_called()


def test_missing_workspace_is_not_created_for_another_install(real_db):
    from helpers.workspace import get_workspace_record

    client = MagicMock()
    client.team_info.side_effect = AssertionError("team_info")
    with (
        patch("helpers.workspace._push_live_team_to_trusted_peers") as push,
        patch("helpers.notifications.notify_primary_action") as notify,
    ):
        got = get_workspace_record(
            "T0000000B",
            {},
            {"team_id": "T0000000A", "bot_token": "xoxb-a"},
            client,
        )
    assert got is None
    assert DbManager.get_record(schemas.Workspace, id="T0000000B") is None
    client.team_info.assert_not_called()
    push.assert_not_called()
    notify.assert_not_called()


def test_stub_workspace_is_not_restored_for_another_install(real_db):
    from helpers.workspace import get_workspace_record

    _stub("T0000000B", "Workspace B")
    client = MagicMock()
    with (
        patch("helpers.workspace_kind.convert_workspace_to_local") as convert,
        patch("helpers.workspace.resume_restored_sync_channels") as resume,
    ):
        got = get_workspace_record(
            "T0000000B",
            {"team": {"id": "T0000000B"}},
            {"team_id": "T0000000A"},
            client,
        )
    assert got is None
    convert.assert_not_called()
    resume.assert_not_called()


def test_own_install_refreshes_name_and_token(real_db, monkeypatch):
    from slack_sdk.oauth.installation_store import Bot

    from db import get_engine
    from helpers._cache import _cache_delete
    from helpers.encryption_installation_store import EncryptedSQLAlchemyInstallationStore
    from helpers.workspace import get_bot_token, get_workspace_record

    monkeypatch.setenv("SLACK_CLIENT_ID", "111.222")
    monkeypatch.setenv("DATA_ENCRYPTION_KEY", "own-install-key-16b")
    workspace = _live("T0000000B", "Workspace B")
    _cache_delete(f"ws_name_refresh:{workspace.id}")
    store = EncryptedSQLAlchemyInstallationStore(client_id="111.222", engine=get_engine())
    store.save_bot(
        Bot(
            app_id="A0APP",
            enterprise_id=None,
            team_id="T0000000B",
            bot_token="xoxb-old",
            bot_id="B0BOT",
            bot_user_id="U0BOT",
            installed_at=datetime.now(UTC),
        )
    )
    client = MagicMock()
    client.team_info.return_value = {"team": {"name": "Workspace B Prime"}}
    got = get_workspace_record(
        "T0000000B",
        {"team": {"id": "T0000000B"}},
        {"team_id": "T0000000B", "bot_token": "xoxb-b"},
        client,
    )
    assert got.workspace_name == "Workspace B Prime"
    reloaded = DbManager.get_record(schemas.Workspace, id="T0000000B")
    assert reloaded.workspace_name == "Workspace B Prime"
    assert get_bot_token(reloaded) == "xoxb-b"
    client.team_info.assert_called_once()


def test_message_deleted_through_another_install_leaves_workspace_unchanged(real_db):
    from handlers.message import respond_to_message_event

    _live("T0000000B", "Workspace B")
    body = {
        "team_id": "T0000000B",
        "event": {
            "type": "message",
            "subtype": "message_deleted",
            "channel": "C1",
            "previous_message": {"ts": "1.000000"},
        },
    }
    client = MagicMock()
    client.team_info.side_effect = AssertionError("team_info")
    with patch("helpers.encryption_installation_store.EncryptedSQLAlchemyInstallationStore") as store_cls:
        store_cls.return_value.save_bot = MagicMock()
        respond_to_message_event(
            body,
            client,
            MagicMock(),
            {"team_id": "T0000000A", "bot_token": "xoxb-a"},
        )
    reloaded = DbManager.get_record(schemas.Workspace, id="T0000000B")
    assert reloaded.workspace_name == "Workspace B"
    client.team_info.assert_not_called()
    store_cls.return_value.save_bot.assert_not_called()
