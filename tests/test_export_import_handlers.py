"""Focused unit tests for backup/restore and migration handler validation."""

import json
import os
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("DATABASE_HOST", "localhost")
os.environ.setdefault("DATABASE_USER", "root")
os.environ.setdefault("DATABASE_PASSWORD", "test")
os.environ.setdefault("DATABASE_SCHEMA", "syncbot")
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-0-0")

from handlers.export_import import (  # noqa: E402
    handle_backup_restore,
    handle_backup_restore_submit_ack,
)
from slack import actions  # noqa: E402


class TestBackupRestoreSubmitValidation:
    def test_returns_error_when_file_missing(self):
        client = MagicMock()
        body = {"user": {"id": "U1"}, "team": {"id": "TTEST"}, "view": {"state": {"values": {}}}}

        with (
            patch.dict(os.environ, {"PRIMARY_WORKSPACE": "TTEST"}),
            patch("handlers.export_import._is_admin", return_value=True),
        ):
            resp = handle_backup_restore_submit_ack(body, client, context={})

        assert resp["response_action"] == "errors"
        assert actions.CONFIG_BACKUP_RESTORE_JSON_INPUT in resp["errors"]

    def test_returns_error_when_uploaded_file_has_no_url(self):
        client = MagicMock()
        body = {
            "user": {"id": "U1"},
            "team": {"id": "TTEST"},
            "view": {
                "state": {
                    "values": {
                        actions.CONFIG_BACKUP_RESTORE_JSON_INPUT: {
                            actions.CONFIG_BACKUP_RESTORE_JSON_INPUT: {
                                "files": [{"id": "F123"}],
                            }
                        }
                    }
                }
            },
        }

        with (
            patch.dict(os.environ, {"PRIMARY_WORKSPACE": "TTEST"}),
            patch("handlers.export_import._is_admin", return_value=True),
        ):
            resp = handle_backup_restore_submit_ack(body, client, context={})

        assert resp["response_action"] == "errors"
        assert "Could not retrieve the uploaded file." in resp["errors"][actions.CONFIG_BACKUP_RESTORE_JSON_INPUT]

    def test_rejects_wrong_dump_format_version(self):
        client = MagicMock()
        body = {
            "user": {"id": "U1"},
            "team": {"id": "TTEST"},
            "view": {
                "state": {
                    "values": {
                        actions.CONFIG_BACKUP_RESTORE_JSON_INPUT: {
                            actions.CONFIG_BACKUP_RESTORE_JSON_INPUT: {
                                "files": [{"url_private_download": "https://files.slack.com/b.json"}],
                            }
                        }
                    }
                }
            },
        }
        with (
            patch.dict(os.environ, {"PRIMARY_WORKSPACE": "TTEST"}),
            patch("handlers.export_import._is_admin", return_value=True),
            patch(
                "handlers.export_import._download_uploaded_file",
                return_value=('{"version": 2, "syncbot_version": "1.7.0"}', None),
            ),
        ):
            resp = handle_backup_restore_submit_ack(body, client, context={})

        assert resp["response_action"] == "errors"
        assert "Unsupported backup version" in resp["errors"][actions.CONFIG_BACKUP_RESTORE_JSON_INPUT]

    def test_syncbot_version_label_is_not_a_gate(self):
        from handlers._common import _STAY_WAIT_TEXT

        client = MagicMock()
        body = {
            "user": {"id": "U1"},
            "team": {"id": "TTEST"},
            "view": {
                "state": {
                    "values": {
                        actions.CONFIG_BACKUP_RESTORE_JSON_INPUT: {
                            actions.CONFIG_BACKUP_RESTORE_JSON_INPUT: {
                                "files": [{"url_private_download": "https://files.slack.com/b.json"}],
                            }
                        }
                    }
                }
            },
        }
        with (
            patch.dict(os.environ, {"PRIMARY_WORKSPACE": "TTEST"}),
            patch("handlers.export_import._is_admin", return_value=True),
            patch(
                "handlers.export_import._download_uploaded_file",
                return_value=('{"version": 1, "syncbot_version": "0.0.0"}', None),
            ),
            patch("handlers.export_import.ei.verify_backup_hmac", return_value=True),
            patch("handlers.export_import.ei.verify_backup_encryption_key", return_value=True),
        ):
            resp = handle_backup_restore_submit_ack(body, client, context={})

        assert resp["response_action"] == "update"
        assert resp["view"]["title"]["text"] == "Backup / Restore"
        assert resp["view"]["close"]["text"] == "Close"
        assert "submit" not in resp["view"]
        assert resp["view"]["blocks"][0]["text"]["text"] == _STAY_WAIT_TEXT


class TestHandleBackupRestorePrimaryWorkspace:
    def test_returns_early_when_primary_mismatch(self):
        client = MagicMock()
        body = {
            "user": {"id": "U1"},
            "team": {"id": "T_WRONG"},
            "trigger_id": "trig",
        }
        with (
            patch.dict(os.environ, {"PRIMARY_WORKSPACE": "T_PRIMARY"}),
            patch("handlers.export_import._is_admin", return_value=True),
        ):
            handle_backup_restore(body, client, MagicMock(), {})

        client.views_update.assert_not_called()


class TestDataMigrationModal:
    @pytest.mark.parametrize("federation_on", [True, False])
    def test_export_buttons_and_copy(self, federation_on):
        from handlers.export_import import handle_data_migration

        client = MagicMock()
        body = {"user": {"id": "U1"}, "team": {"id": "T1"}, "trigger_id": "tr"}
        with (
            patch("handlers.export_import._is_admin", return_value=True),
            patch("handlers.export_import.helpers.federation_enabled", return_value=federation_on),
        ):
            handle_data_migration(body, client, MagicMock(), {})
        view = client.views_update.call_args.kwargs["view"]
        text = str(view)
        assert ":outbox_tray: Export" in text
        assert ":link:" not in text
        assert "sends it to you in a DM" in text
        assert "no tokens or passwords" in text
        assert "from this instance or from a connected instance" in text
        assert "You will see what is in it" in text

    def test_export_confirms_in_modal_after_dm(self):
        from types import SimpleNamespace

        from handlers._common import _DM_WAIT_TEXT
        from handlers.export_import import handle_data_migration_export

        client = MagicMock()
        body = {"user": {"id": "U1"}, "team": {"id": "T1"}, "view": {"id": "V1"}}
        workspace = SimpleNamespace(id=1, team_id="T1", workspace_name="Workspace A", deleted_at=None)
        with (
            patch("handlers.export_import._is_admin", return_value=True),
            patch("handlers.export_import.helpers.get_workspace_record", return_value=workspace),
            patch("handlers.export_import.ei.build_migration_export", return_value={"workspace": {"team_id": "T1"}}),
            patch("handlers.export_import._open_dm_channel", return_value="D1"),
            patch("handlers.export_import.helpers.resolve_workspace_name", return_value="Workspace A"),
        ):
            handle_data_migration_export(body, client, MagicMock(), {})
        assert [name for name, _a, _k in client.method_calls[:2]] == ["views_update", "files_upload_v2"]
        view = client.views_update.call_args.kwargs["view"]
        assert view["close"]["text"] == "Close"
        assert view["blocks"][0]["text"]["text"] == _DM_WAIT_TEXT
        assert view.get("submit") is None

    def test_export_that_fails_sends_no_file(self):
        from types import SimpleNamespace

        from handlers.export_import import handle_data_migration_export

        client = MagicMock()
        body = {"user": {"id": "U1"}, "team": {"id": "T1"}, "view": {"id": "V1"}}
        workspace = SimpleNamespace(id=1, team_id="T1", workspace_name="Workspace A", deleted_at=None)
        with (
            patch("handlers.export_import._is_admin", return_value=True),
            patch("handlers.export_import.helpers.get_workspace_record", return_value=workspace),
            patch("handlers.export_import.ei.build_migration_export", side_effect=RuntimeError),
            patch("handlers.export_import._close_modal_done") as close,
        ):
            handle_data_migration_export(body, client, MagicMock(), {})
        client.files_upload_v2.assert_not_called()
        assert close.call_args.args[2] == (":warning: SyncBot could not send the migration file. Try Export again.")

    def test_wrong_workspace_names_the_file(self):
        from types import SimpleNamespace

        from handlers.export_import import _data_migration_prepare

        payload = {"version": 1, "workspace": {"team_id": "T2", "workspace_name": "Workspace B"}}
        workspace = SimpleNamespace(id=1, team_id="T1", workspace_name="Workspace A")
        body = {
            "user": {"id": "U1"},
            "team": {"id": "T1"},
            "view": {
                "state": {
                    "values": {
                        actions.CONFIG_DATA_MIGRATION_JSON_INPUT: {
                            actions.CONFIG_DATA_MIGRATION_JSON_INPUT: {
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
        ):
            err, *_rest = _data_migration_prepare(body, MagicMock(token="xoxb"), {})
        assert err["errors"][actions.CONFIG_DATA_MIGRATION_JSON_INPUT] == (
            "This file is for `Workspace B`. Open SyncBot in that Workspace to import it."
        )


class TestDataMigrationImportConnect:
    @pytest.fixture(autouse=True)
    def _signed_by_this_install(self):
        with patch(
            "handlers.export_import.ei.get_migration_file_status",
            return_value=("this_install", None),
        ):
            yield

    def test_prepare_does_not_pair(self):
        from types import SimpleNamespace

        from handlers.export_import import _data_migration_prepare

        payload = {
            "version": 1,
            "workspace": {"team_id": "T1"},
            "source_instance": {
                "webhook_url": "https://ws-a.example/api/federation",
                "instance_id": "a" * 64,
                "public_key": "pem",
                "connection_code": "ENCODED_BLOB",
            },
        }
        workspace = SimpleNamespace(id=1, team_id="T1", workspace_name="Workspace A")
        body = {
            "user": {"id": "U1"},
            "team": {"id": "T1"},
            "view": {
                "state": {
                    "values": {
                        actions.CONFIG_DATA_MIGRATION_JSON_INPUT: {
                            actions.CONFIG_DATA_MIGRATION_JSON_INPUT: {
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
                "handlers.export_import._download_uploaded_file",
                return_value=(json.dumps(payload), None),
            ),
            patch("handlers.export_import.helpers.get_workspace_record", return_value=workspace),
            patch("handlers.export_import.DbManager.find_records", return_value=[]),
            patch("federation.core.initiate_federation_connect") as connect,
        ):
            err, data, *_rest = _data_migration_prepare(body, MagicMock(token="xoxb"), {})

        assert err is None
        assert data is not None
        connect.assert_not_called()

    def test_submit_ack_reviews_groups_and_connection(self):
        from types import SimpleNamespace

        from handlers.export_import import handle_data_migration_submit_ack

        payload = {
            "version": 1,
            "workspace": {"team_id": "T1"},
            "groups": [{"uid": "g1", "name": "Live Group", "role": "member"}],
            "syncs": [{"uid": "s1", "group_uid": "g1", "title": "Announcements"}],
            "sync_channels": [{"sync_uid": "s1", "team_id": "T1", "channel_id": "C1", "channel_name": "general"}],
            "user_mappings": [{"source_user_id": "U1", "group_uid": "g1"}],
            "post_meta": {"s1:C1": [{"kind": "message", "post_id": "p1"}]},
            "source_instance": {
                "webhook_url": "https://ws-a.example/api/federation",
                "instance_id": "a" * 64,
                "public_key": "pem",
                "connection_code": "ENCODED_BLOB",
            },
        }
        workspace = SimpleNamespace(id=1, team_id="T1", workspace_name="Workspace A")
        body = {
            "user": {"id": "U1"},
            "team": {"id": "T1"},
            "view": {
                "state": {
                    "values": {
                        actions.CONFIG_DATA_MIGRATION_JSON_INPUT: {
                            actions.CONFIG_DATA_MIGRATION_JSON_INPUT: {
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
                "handlers.export_import._download_uploaded_file",
                return_value=(json.dumps(payload), None),
            ),
            patch("handlers.export_import.helpers.get_workspace_record", return_value=workspace),
            patch("handlers.export_import.DbManager.find_records", return_value=[]),
            patch("handlers.export_import.ei.verify_migration_signature", return_value=True),
            patch("federation.core.initiate_federation_connect") as connect,
            patch("helpers._cache._cache_set") as cache_set,
        ):
            resp = handle_data_migration_submit_ack(body, MagicMock(token="xoxb"), {})

        connect.assert_not_called()
        cache_set.assert_called_once()
        assert resp["response_action"] == "update"
        view = resp["view"]
        assert view["callback_id"] == actions.CONFIG_DATA_MIGRATION_REVIEW
        assert view["submit"]["text"] == "Import"
        text = str(view)
        assert "Partner Org" not in text
        assert "connection code" not in text
        assert "*File contents*" in text
        assert "Group Name: Live Group" in text
        assert "Synced Channels: 1 (#general)" in text
        assert "Mapped Users: 1" in text
        assert "Synced Messages: 1" in text

    def test_review_names_a_workspace_that_is_not_here(self):
        from types import SimpleNamespace

        from handlers.export_import import handle_data_migration_submit_ack

        payload = {
            "version": 1,
            "workspace": {"team_id": "T1", "workspace_name": "Workspace A"},
            "groups": [
                {
                    "uid": "g1",
                    "name": "Live Group",
                    "member_workspaces": [
                        {"team_id": "T1", "workspace_name": "Workspace A"},
                        {"team_id": "T2", "workspace_name": "Workspace B"},
                        {"team_id": "T3"},
                    ],
                }
            ],
        }
        workspace = SimpleNamespace(id=1, team_id="T1", workspace_name="Workspace A")
        body = {
            "user": {"id": "U1"},
            "team": {"id": "T1"},
            "view": {
                "state": {
                    "values": {
                        actions.CONFIG_DATA_MIGRATION_JSON_INPUT: {
                            actions.CONFIG_DATA_MIGRATION_JSON_INPUT: {
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
            patch(
                "handlers.export_import.DbManager.find_records",
                side_effect=[[], [], [SimpleNamespace(team_id="T3")], []],
            ),
            patch("handlers.export_import.ei.verify_migration_signature", return_value=True),
            patch("handlers.export_import.helpers.federation_enabled", return_value=True),
            patch("helpers._cache._cache_set"),
        ):
            resp = handle_data_migration_submit_ack(body, MagicMock(token="xoxb"), {})

        text = str(resp["view"])
        assert resp["view"]["submit"]["text"] == "Import"
        assert "`Workspace B` is not on this instance yet" in text
        assert "will not come over" in text
        assert "T3" not in text
        assert "connection code" not in text
        assert "exported from this Workspace on this SyncBot instance" in text
        assert "start right away" in text
        assert not any(block.get("type") == "input" for block in resp["view"]["blocks"])

    def test_review_names_a_group_this_workspace_is_not_in(self):
        from types import SimpleNamespace

        from handlers.export_import import handle_data_migration_submit_ack

        payload = {
            "version": 1,
            "workspace": {"team_id": "T1"},
            "groups": [{"uid": "b-group", "name": "B Group"}],
        }
        workspace = SimpleNamespace(id=1, team_id="T1", workspace_name="Workspace A")
        group = SimpleNamespace(id=5, name="B Group", status="active", uid="b-group")
        body = {
            "user": {"id": "U1"},
            "team": {"id": "T1"},
            "view": {
                "state": {
                    "values": {
                        actions.CONFIG_DATA_MIGRATION_JSON_INPUT: {
                            actions.CONFIG_DATA_MIGRATION_JSON_INPUT: {
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
            patch("handlers.export_import.DbManager.find_records", side_effect=[[], [group], []]),
            patch("handlers.export_import.ei.verify_migration_signature", return_value=True),
            patch("handlers.export_import.helpers.federation_enabled", return_value=True),
            patch("helpers._cache._cache_set"),
        ):
            resp = handle_data_migration_submit_ack(body, MagicMock(token="xoxb"), {})
        text = str(resp["view"])
        assert "but this Workspace is not in it" in text
        assert "Ask an Owner of `B Group`" in text

    def test_import_does_not_open_a_connection(self):
        from types import SimpleNamespace

        from handlers.export_import import handle_data_migration_review

        payload = {
            "version": 1,
            "source_instance": {
                "webhook_url": "https://ws-a.example/api/federation",
                "instance_id": "a" * 64,
                "public_key": "pem",
                "connection_code": "ENCODED_BLOB",
            },
        }
        workspace = SimpleNamespace(id=1, team_id="T1", workspace_name="Workspace A")
        peer = SimpleNamespace(instance_id="a" * 64)
        body = {"user": {"id": "U1"}, "view": {"id": "V1"}}
        meta = {
            "data": payload,
            "group_id": 7,
            "workspace_id": 1,
            "team_id_to_workspace_id": {},
        }

        for existing in ([], [peer]):
            with (
                patch("handlers.export_import._is_admin", return_value=True),
                patch("helpers._cache._cache_get", return_value=meta),
                patch("handlers.export_import.helpers.get_workspace_by_id", return_value=workspace),
                patch("federation.core.initiate_federation_connect") as connect,
                patch("federation.core.get_or_create_instance") as create,
                patch("handlers.export_import.DbManager.find_records", return_value=existing),
                patch("handlers.export_import.DbManager.create_record") as insert,
                patch("handlers.export_import.ei.import_migration_data"),
                patch("handlers.export_import.helpers.consider_migration_release"),
                patch("handlers.export_import.ei.invalidate_home_tab_caches_for_team"),
                patch("handlers.export_import.builders.refresh_home_tab_for_workspace"),
                patch("handlers.export_import.helpers.notify_user_dm"),
            ):
                handle_data_migration_review(body, MagicMock(), MagicMock(), {})
            connect.assert_not_called()
            create.assert_not_called()
            insert.assert_not_called()

    def test_review_ack_keeps_wait_modal(self):
        from handlers._common import _DM_WAIT_TEXT
        from handlers.export_import import handle_data_migration_review_ack

        result = handle_data_migration_review_ack(
            {
                "view": {
                    "state": {
                        "values": {
                            "data_migration_finish": {"data_migration_finish": {"selected_option": {"value": "wait"}}}
                        }
                    }
                }
            },
            MagicMock(),
            {},
        )
        assert result["response_action"] == "update"
        assert result["view"]["title"]["text"] == "Confirm Import"
        assert result["view"]["close"]["text"] == "Close"
        assert "submit" not in result["view"]
        assert result["view"]["blocks"][0]["text"]["text"] == _DM_WAIT_TEXT

    def test_this_install_review_has_no_choice_and_starts(self):
        from types import SimpleNamespace

        from handlers.export_import import handle_data_migration_review, handle_data_migration_submit_ack

        payload = {"version": 1, "workspace": {"team_id": "T1"}, "groups": []}
        workspace = SimpleNamespace(id=1, team_id="T1", workspace_name="Workspace A")
        body = {
            "user": {"id": "U1"},
            "team": {"id": "T1"},
            "view": {
                "state": {
                    "values": {
                        actions.CONFIG_DATA_MIGRATION_JSON_INPUT: {
                            actions.CONFIG_DATA_MIGRATION_JSON_INPUT: {
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
            patch("helpers._cache._cache_set"),
        ):
            resp = handle_data_migration_submit_ack(body, MagicMock(token="xoxb"), {})
        text = str(resp["view"])
        assert "This file was exported from this Workspace on this SyncBot instance." in text
        assert "Importing adds these Groups and Syncs back to this Workspace" in text
        assert "they start right away" in text
        assert not any(block.get("type") == "input" for block in resp["view"]["blocks"])

        from handlers.export_import import handle_data_migration_review_ack

        ack = handle_data_migration_review_ack({}, MagicMock(), {})
        assert ack["response_action"] == "update"
        assert "submit" not in ack["view"]

        review_body = {"user": {"id": "U1"}, "view": {"id": "V1"}}
        meta = {"data": payload, "group_id": 0, "workspace_id": 1, "team_id_to_workspace_id": {"T1": 1}}
        with (
            patch("handlers.export_import._is_admin", return_value=True),
            patch("helpers._cache._cache_get", return_value=meta),
            patch("handlers.export_import.helpers.get_workspace_by_id", return_value=workspace),
            patch("handlers.export_import.DbManager.find_records", return_value=[]),
            patch("handlers.export_import._run_migration_import"),
            patch("handlers.export_import.helpers.consider_migration_release", return_value=("resumed", None)),
            patch("handlers.export_import.builders.refresh_home_tab_for_workspace"),
            patch("handlers.export_import.helpers.notify_user_dm") as dm,
            patch("handlers._common._close_modal_done"),
        ):
            handle_data_migration_review(review_body, MagicMock(), MagicMock(), {})
        assert "This Workspace's Syncs are running." in dm.call_args.args[2]

    def test_waiting_peer_is_named_when_channels_are_pending(self):
        from types import SimpleNamespace

        from handlers.export_import import handle_data_migration_review

        workspace = SimpleNamespace(id=1, team_id="T1", workspace_name="Workspace A")
        peer = SimpleNamespace(instance_id="a" * 64, name="Partner Org", primary_workspace_name=None)
        pending = SimpleNamespace(status="pending")
        body = {"user": {"id": "U1"}, "view": {"id": "V1"}}
        meta = {"data": {"version": 1, "groups": []}, "group_id": 0, "workspace_id": 1, "team_id_to_workspace_id": {}}
        with (
            patch("handlers.export_import._is_admin", return_value=True),
            patch("helpers._cache._cache_get", return_value=meta),
            patch("handlers.export_import.helpers.get_workspace_by_id", return_value=workspace),
            patch("handlers.export_import.DbManager.find_records", return_value=[pending]),
            patch("handlers.export_import._run_migration_import"),
            patch(
                "handlers.export_import.helpers.consider_migration_release",
                return_value=("waiting_for_uninstall", peer),
            ) as release,
            patch("handlers.export_import.builders.refresh_home_tab_for_workspace"),
            patch("handlers.export_import.helpers.notify_user_dm") as dm,
            patch("handlers._common._close_modal_done"),
        ):
            handle_data_migration_review(body, MagicMock(), MagicMock(), {})
        release.assert_called_once_with("T1")
        message = dm.call_args.args[2]
        assert "`Partner Org`" in message
        assert "Waiting for migration" in message

    def test_finished_dm_says_where_syncbot_is_uninstalled(self):
        from types import SimpleNamespace

        from handlers.export_import import _import_finished_text

        workspace = SimpleNamespace(id=None, team_id="T1")
        peer = SimpleNamespace(instance_id="e" * 64, name="Partner Org")
        waiting = _import_finished_text(
            {},
            workspace,
            "waiting_for_uninstall",
            peer,
            source_name="Partner Org",
            source_kind="connected",
            has_pending=True,
        )
        assert waiting == (
            ":white_check_mark: Import finished. This Workspace's Syncs show `Waiting for migration` "
            "until SyncBot is uninstalled from this Workspace on `Partner Org`. They start on their own after that. "
            "If they do not, import this file again."
        )
        running = _import_finished_text(
            {},
            workspace,
            "resumed",
            None,
            source_name="Partner Org",
            source_kind="connected",
            has_pending=False,
        )
        assert running == (
            ":white_check_mark: Import finished. SyncBot is already uninstalled from this Workspace "
            "on `Partner Org`, so its Syncs are running."
        )

    def test_connected_review_says_syncs_start_once_syncbot_is_uninstalled_there(self):
        from types import SimpleNamespace

        from handlers.export_import import handle_data_migration_submit_ack

        payload = {"version": 1, "workspace": {"team_id": "T1"}, "groups": []}
        workspace = SimpleNamespace(id=1, team_id="T1", workspace_name="Workspace A")
        signer = SimpleNamespace(instance_id="b" * 64, name="Partner Org", primary_workspace_name=None)
        body = {
            "user": {"id": "U1"},
            "team": {"id": "T1"},
            "view": {
                "state": {
                    "values": {
                        actions.CONFIG_DATA_MIGRATION_JSON_INPUT: {
                            actions.CONFIG_DATA_MIGRATION_JSON_INPUT: {
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
            patch(
                "handlers.export_import.ei.get_migration_file_status",
                return_value=("connected", signer),
            ),
            patch("helpers._cache._cache_set"),
        ):
            resp = handle_data_migration_submit_ack(body, MagicMock(token="xoxb"), {})
        text = str(resp["view"])
        assert "This file was exported from this Workspace on `Partner Org`, a connected SyncBot instance." in text
        assert "including the other `Partner Org` Workspaces in those Groups" in text
        assert "Until SyncBot is uninstalled from this Workspace on `Partner Org`" in text
        assert "If it is already uninstalled there, they start right away." in text
        assert not any(block.get("type") == "input" for block in resp["view"]["blocks"])

    def test_finished_dm_covers_running_waiting_unreachable_and_what_was_left_out(self):
        from types import SimpleNamespace

        from handlers.export_import import handle_data_migration_review

        workspace = SimpleNamespace(id=1, team_id="T1", workspace_name="Workspace A")
        peer = SimpleNamespace(instance_id="c" * 64, name="Partner Org", primary_workspace_name=None)
        pending = SimpleNamespace(status="pending")
        data = {
            "version": 1,
            "groups": [
                {
                    "uid": "b-group",
                    "name": "B Group",
                    "member_workspaces": [{"team_id": "T2", "workspace_name": "Workspace B"}],
                }
            ],
        }
        body = {"user": {"id": "U1"}, "view": {"id": "V1"}}
        meta = {"data": data, "group_id": 0, "workspace_id": 1, "team_id_to_workspace_id": {"T1": 1}}
        group = SimpleNamespace(id=9, name="B Group", status="active")
        cases = [
            (("resumed", None), [], "This Workspace's Syncs are running."),
            (("waiting_for_uninstall", peer), [pending], "Waiting for migration"),
            (("unreachable", peer), [pending], "could not reach `Partner Org`"),
            (("refused", peer), [pending], "`Partner Org` did not accept SyncBot's check"),
        ]
        for release, rows, expected in cases:

            def find_records(model, *_args, channels=rows, **_kwargs):
                name = getattr(model, "__name__", "")
                if name == "WorkspaceGroup":
                    return [group]
                if name == "SyncChannel":
                    return channels
                return []

            with (
                patch("handlers.export_import._is_admin", return_value=True),
                patch("helpers._cache._cache_get", return_value=meta),
                patch("handlers.export_import.helpers.get_workspace_by_id", return_value=workspace),
                patch("handlers.export_import.DbManager.find_records", side_effect=find_records),
                patch("handlers.export_import._run_migration_import"),
                patch("handlers.export_import.helpers.consider_migration_release", return_value=release),
                patch("handlers.export_import.builders.refresh_home_tab_for_workspace"),
                patch("handlers.export_import.helpers.notify_user_dm") as dm,
                patch("handlers._common._close_modal_done"),
            ):
                handle_data_migration_review(body, MagicMock(), MagicMock(), {})
            message = dm.call_args.args[2]
            assert expected in message
            assert "Workspace B" in message
            assert "B Group" in message

    def test_active_channels_stay_running_when_a_peer_cannot_be_reached(self):
        from types import SimpleNamespace

        from handlers.export_import import handle_data_migration_review

        workspace = SimpleNamespace(id=1, team_id="T1", workspace_name="Workspace A")
        peer = SimpleNamespace(instance_id="d" * 64, name="Partner Org", primary_workspace_name=None)
        body = {"user": {"id": "U1"}, "view": {"id": "V1"}}
        meta = {"data": {"version": 1, "groups": []}, "group_id": 0, "workspace_id": 1, "team_id_to_workspace_id": {}}
        with (
            patch("handlers.export_import._is_admin", return_value=True),
            patch("helpers._cache._cache_get", return_value=meta),
            patch("handlers.export_import.helpers.get_workspace_by_id", return_value=workspace),
            patch("handlers.export_import.DbManager.find_records", return_value=[]),
            patch("handlers.export_import._run_migration_import"),
            patch(
                "handlers.export_import.helpers.consider_migration_release",
                return_value=("unreachable", peer),
            ),
            patch("handlers.export_import.builders.refresh_home_tab_for_workspace"),
            patch("handlers.export_import.helpers.notify_user_dm") as dm,
            patch("handlers._common._close_modal_done"),
        ):
            handle_data_migration_review(body, MagicMock(), MagicMock(), {})
        message = dm.call_args.args[2]
        assert message == ":white_check_mark: Import finished. This Workspace's Syncs are running."

    def test_review_import_dms_when_expired(self):
        from handlers.export_import import handle_data_migration_review

        body = {"user": {"id": "U1"}, "view": {"id": "V1"}}
        client = MagicMock()
        with (
            patch("handlers.export_import._is_admin", return_value=True),
            patch("helpers._cache._cache_get", return_value=None),
            patch("handlers.export_import.helpers.notify_user_dm") as dm,
        ):
            handle_data_migration_review(body, client, MagicMock(), {})
        dm.assert_called_once()
        assert "import expired" in dm.call_args.args[2]

    def test_review_import_dms_when_failed(self):
        from types import SimpleNamespace

        from handlers.export_import import handle_data_migration_review

        workspace = SimpleNamespace(id=1, team_id="T1")
        body = {"user": {"id": "U1"}, "view": {"id": "V1"}}
        client = MagicMock()
        meta = {
            "data": {"version": 1},
            "group_id": 7,
            "workspace_id": 1,
            "team_id_to_workspace_id": {},
        }
        with (
            patch("handlers.export_import._is_admin", return_value=True),
            patch("helpers._cache._cache_get", return_value=meta),
            patch("handlers.export_import.helpers.get_workspace_by_id", return_value=workspace),
            patch("handlers.export_import._run_migration_import", side_effect=RuntimeError("boom")),
            patch("handlers.export_import.helpers.notify_user_dm") as dm,
        ):
            handle_data_migration_review(body, client, MagicMock(), {})
        dm.assert_called_once()
        assert "Import failed" in dm.call_args.args[2]
