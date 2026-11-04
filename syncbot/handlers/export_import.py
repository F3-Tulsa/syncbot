"""Backup/Restore and Data Migration handlers (modals and submissions)."""

import json
import logging
from datetime import UTC, datetime
from logging import Logger

from slack_sdk.web import WebClient

import builders
import constants
import helpers
from db import DbManager, schemas
from handlers._common import _close_modal_done, _update_wait_modal, _wait_modal_ack
from helpers import export_import as ei
from helpers.workspace import invalidate_fed_ws_for_sync_cache
from logger import log_error, log_warning
from slack import actions, orm

# Uploaded JSON (backup / migration) download timeout — size follows Slack's per-file max.
_UPLOAD_DOWNLOAD_TIMEOUT = 10


def _download_uploaded_file(file_url: str, token: str) -> tuple[str | None, str | None]:
    """Download a Slack-hosted uploaded file. Returns ``(utf8_text, None)`` or ``(None, error_message)``."""
    import urllib.error
    import urllib.request

    from helpers.files import is_slack_file_url, max_file_bytes

    if not is_slack_file_url(file_url):
        return None, "Failed to download the uploaded file."

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(file_url, headers={"Authorization": f"Bearer {token}"})
    try:
        with opener.open(req, timeout=_UPLOAD_DOWNLOAD_TIMEOUT) as resp:
            chunks: list[bytes] = []
            total = 0
            limit = max_file_bytes()
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    return None, "Uploaded file exceeds Slack's per-file size limit."
                chunks.append(chunk)
            raw = b"".join(chunks)
    except urllib.error.HTTPError as e:
        log_error("upload_download_http_error", error_type=type(e).__name__, exc_info=True)
        return None, "Failed to download the uploaded file."
    except TimeoutError as e:
        log_error("upload_download_timeout", error_type=type(e).__name__, exc_info=True)
        return None, "Failed to download the uploaded file."
    except OSError as e:
        log_error("upload_download_failed", error_type=type(e).__name__, exc_info=True)
        return None, "Failed to download the uploaded file."
    try:
        return raw.decode("utf-8"), None
    except UnicodeDecodeError as e:
        return None, f"Invalid encoding in uploaded file: {e}"


def _is_admin(client: WebClient, user_id: str, body: dict) -> bool:
    team_id = helpers.get_team_id_from_body(body)
    return helpers.is_workspace_admin(client, user_id) if user_id and team_id else False


def _open_dm_channel(client: WebClient, user_id: str) -> str:
    """Open (or reopen) a DM with *user_id* and return the channel ID."""
    resp = client.conversations_open(users=[user_id])
    return resp["channel"]["id"]


# ---------------------------------------------------------------------------
# Backup/Restore
# ---------------------------------------------------------------------------


def handle_backup_restore(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Open Backup/Restore modal (admin only)."""
    user_id = helpers.get_user_id_from_body(body)
    if not _is_admin(client, user_id, body):
        return
    if not helpers.is_backup_visible_for_workspace(helpers.get_team_id_from_body(body)):
        return
    trigger_id = helpers.safe_get(body, "trigger_id")
    if not trigger_id:
        return

    from slack import orm

    download_blocks = [
        orm.SectionBlock(label="*Backup*\nSend a JSON backup file as a SyncBot DM."),
        orm.ActionsBlock(
            elements=[
                orm.ButtonElement(
                    label=":floppy_disk: Send Backup File",
                    action=actions.CONFIG_BACKUP_DOWNLOAD,
                ),
            ],
        ),
        orm.DividerBlock(),
        orm.SectionBlock(
            label="*Restore*\nUpload a JSON backup file. The integrity of the file will be checked.",
        ),
    ]

    restore_block = {
        "type": "input",
        "block_id": actions.CONFIG_BACKUP_RESTORE_JSON_INPUT,
        "label": {"type": "plain_text", "text": " "},
        "element": {
            "type": "file_input",
            "action_id": actions.CONFIG_BACKUP_RESTORE_JSON_INPUT,
            "filetypes": ["json"],
            "max_files": 1,
        },
    }

    view = orm.BlockView(blocks=download_blocks)
    modal_blocks = view.as_form_field()
    modal_blocks.append(restore_block)

    orm.update_opened_view(
        client,
        body,
        trigger_id,
        {
            "type": "modal",
            "callback_id": actions.CONFIG_BACKUP_RESTORE_SUBMIT,
            "title": {"type": "plain_text", "text": "Backup / Restore"},
            "submit": {"type": "plain_text", "text": "Restore"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": modal_blocks,
        },
    )


def handle_backup_download(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Generate backup and send to user's DM (called from modal button)."""
    user_id = helpers.get_user_id_from_body(body)
    if not _is_admin(client, user_id, body):
        return
    if not helpers.is_backup_visible_for_workspace(helpers.get_team_id_from_body(body)):
        return
    _update_wait_modal(
        client,
        body,
        title="Backup / Restore",
        callback_id=actions.CONFIG_BACKUP_RESTORE_SUBMIT,
        dm=True,
    )
    try:
        payload = ei.build_full_backup()
        json_str = json.dumps(payload, default=ei._json_serializer, indent=2)
        dm_channel = _open_dm_channel(client, user_id)
        client.files_upload_v2(
            content=json_str,
            filename=f"syncbot-backup-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}.json",
            channel=dm_channel,
            initial_comment=":floppy_disk: Here is your SyncBot JSON backup. Keep this file secure.",
        )
    except Exception as e:
        log_error("backup_download_failed", error=str(e), exc_info=True)
        _close_modal_done(
            client,
            body,
            ":warning: SyncBot could not send the backup file. Try Download again.",
        )


def handle_backup_restore_submit_ack(
    body: dict,
    client: WebClient,
    context: dict,
) -> dict | None:
    """Ack phase: validate upload; return errors, push confirm modal, or the wait view."""
    user_id = helpers.get_user_id_from_body(body)
    if not _is_admin(client, user_id, body):
        return None
    if not helpers.is_backup_visible_for_workspace(helpers.get_team_id_from_body(body)):
        return None

    values = helpers.safe_get(body, "view", "state", "values") or {}
    file_data = helpers.safe_get(
        values, actions.CONFIG_BACKUP_RESTORE_JSON_INPUT, actions.CONFIG_BACKUP_RESTORE_JSON_INPUT
    )
    files = file_data.get("files") if file_data else None

    if not files:
        return {
            "response_action": "errors",
            "errors": {actions.CONFIG_BACKUP_RESTORE_JSON_INPUT: "Upload a JSON backup file to restore."},
        }

    file_info = files[0]
    file_url = file_info.get("url_private_download") or file_info.get("url_private")
    if not file_url:
        return {
            "response_action": "errors",
            "errors": {actions.CONFIG_BACKUP_RESTORE_JSON_INPUT: "Could not retrieve the uploaded file."},
        }

    json_text, dl_err = _download_uploaded_file(file_url, client.token)
    if dl_err:
        return {
            "response_action": "errors",
            "errors": {actions.CONFIG_BACKUP_RESTORE_JSON_INPUT: dl_err},
        }

    try:
        data = json.loads(json_text)
    except json.JSONDecodeError as e:
        return {
            "response_action": "errors",
            "errors": {actions.CONFIG_BACKUP_RESTORE_JSON_INPUT: f"Invalid JSON in uploaded file: {e}"},
        }

    if data.get("version") != ei.BACKUP_VERSION:
        return {
            "response_action": "errors",
            "errors": {
                actions.CONFIG_BACKUP_RESTORE_JSON_INPUT: f"Unsupported backup version (expected {ei.BACKUP_VERSION})."
            },
        }

    hmac_ok = ei.verify_backup_hmac(data)
    key_ok = ei.verify_backup_encryption_key(data)

    if not hmac_ok or not key_ok:
        from helpers._cache import _cache_set

        cache_key = f"restore_pending:{user_id}"
        _cache_set(cache_key, data, ttl=600)
        return {
            "response_action": "push",
            "view": {
                "type": "modal",
                "title": {"type": "plain_text", "text": "Confirm Restore"},
                "close": {"type": "plain_text", "text": "Cancel"},
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": (
                                (
                                    "*WARNING: Integrity Check Failed!* The file has been tampered with. Only proceed if you intentionally edited the file.\n\n"
                                    if not hmac_ok
                                    else ""
                                )
                                + (
                                    "*WARNING: Encryption Key Mismatch!* Restored bot tokens will not be usable. Workspaces will have to reinstall the app.\n\n"
                                    if not key_ok
                                    else ""
                                )
                                + "Do you want to proceed with the restore anyway?"
                            ),
                        },
                    },
                    {
                        "type": "actions",
                        "elements": [
                            {
                                "type": "button",
                                "text": {"type": "plain_text", "text": "Proceed Anyway"},
                                "style": "danger",
                                "action_id": actions.CONFIG_BACKUP_RESTORE_PROCEED,
                                "value": user_id,
                            },
                        ],
                    },
                ],
            },
        }

    return _wait_modal_ack(
        title="Backup / Restore",
        callback_id=actions.CONFIG_BACKUP_RESTORE_SUBMIT,
        dm=False,
    )


def handle_backup_restore_submit_work(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Lazy work phase: run restore after the ack shows the wait modal."""
    user_id = helpers.get_user_id_from_body(body)
    if not _is_admin(client, user_id, body):
        return
    if not helpers.is_backup_visible_for_workspace(helpers.get_team_id_from_body(body)):
        return

    values = helpers.safe_get(body, "view", "state", "values") or {}
    file_data = helpers.safe_get(
        values, actions.CONFIG_BACKUP_RESTORE_JSON_INPUT, actions.CONFIG_BACKUP_RESTORE_JSON_INPUT
    )
    files = file_data.get("files") if file_data else None
    if not files:
        return

    file_info = files[0]
    file_url = file_info.get("url_private_download") or file_info.get("url_private")
    if not file_url:
        return

    json_text, dl_err = _download_uploaded_file(file_url, client.token)
    if dl_err:
        return

    try:
        data = json.loads(json_text)
    except json.JSONDecodeError:
        return

    if data.get("version") != ei.BACKUP_VERSION:
        return

    hmac_ok = ei.verify_backup_hmac(data)
    key_ok = ei.verify_backup_encryption_key(data)
    if not hmac_ok or not key_ok:
        return

    _finish_restore(data, client, body, user_id)


def handle_backup_restore_proceed(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Proceed with restore after user clicked the danger button despite warnings."""
    user_id = helpers.get_user_id_from_body(body)
    if not _is_admin(client, user_id, body):
        return
    if not helpers.is_backup_visible_for_workspace(helpers.get_team_id_from_body(body)):
        return
    from helpers._cache import _cache_get

    data = _cache_get(f"restore_pending:{user_id}")
    if not data:
        log_warning("backup_restore_proceed", user_id=user_id)
        return
    _update_wait_modal(client, body, title="Confirm Restore", dm=False)
    _finish_restore(data, client, body, user_id)


def _finish_restore(data: dict, client: WebClient, body: dict, user_id: str) -> None:
    """Run restore, then replace the wait modal with the outcome."""
    try:
        _do_restore(data, client, user_id)
    except Exception:
        _close_modal_done(client, body, ":warning: Restore failed. Try Backup / Restore again.")
        return
    _close_modal_done(client, body, ":white_check_mark: Restore finished.")


def _do_restore(data: dict, client: WebClient, user_id: str) -> None:
    """Run restore, invalidate caches, and refresh the Home tab for all restored workspaces."""
    try:
        team_ids = ei.restore_full_backup(data, skip_hmac_check=True, skip_encryption_key_check=True)
        ei.invalidate_home_tab_caches_for_all_teams(team_ids)
        invalidate_fed_ws_for_sync_cache()
    except Exception as e:
        log_error("restore_failed", error=str(e), exc_info=True)
        raise

    for team_id in team_ids:
        workspace_rows = DbManager.find_records(schemas.Workspace, [schemas.Workspace.team_id == team_id])
        if workspace_rows:
            try:
                builders.refresh_home_tab_for_workspace(workspace_rows[0], logging.getLogger("syncbot"))
            except Exception as e:
                log_warning("home_refresh_failed", team_id=team_id, error=str(e))


# ---------------------------------------------------------------------------
# Data Migration
# ---------------------------------------------------------------------------


def handle_data_migration(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Open Data Migration modal for any Slack workspace admin."""
    user_id = helpers.get_user_id_from_body(body)
    if not _is_admin(client, user_id, body):
        return
    trigger_id = helpers.safe_get(body, "trigger_id")
    if not trigger_id:
        return

    from slack import orm

    export_blocks = [
        orm.SectionBlock(
            label=(
                "*Export*\n"
                "Download this Workspace's SyncBot data (Groups, Syncs and Mappings, no tokens or secrets) "
                "as a JSON file. The file will be sent to you from SyncBot as a DM."
            ),
        ),
        orm.ActionsBlock(
            elements=[
                orm.ButtonElement(
                    label=":outbox_tray: Export",
                    action=actions.CONFIG_DATA_MIGRATION_EXPORT,
                ),
            ],
        ),
    ]
    export_blocks.extend(
        [
            orm.DividerBlock(),
            orm.SectionBlock(
                label="*Import*\nUpload a JSON export file. Existing Groups and Syncs are merged.",
            ),
        ]
    )

    import_block = {
        "type": "input",
        "block_id": actions.CONFIG_DATA_MIGRATION_JSON_INPUT,
        "label": {"type": "plain_text", "text": " "},
        "element": {
            "type": "file_input",
            "action_id": actions.CONFIG_DATA_MIGRATION_JSON_INPUT,
            "filetypes": ["json"],
            "max_files": 1,
        },
    }

    view = orm.BlockView(blocks=export_blocks)
    modal_blocks = view.as_form_field()
    modal_blocks.append(import_block)

    orm.update_opened_view(
        client,
        body,
        trigger_id,
        {
            "type": "modal",
            "callback_id": actions.CONFIG_DATA_MIGRATION_SUBMIT,
            "title": {"type": "plain_text", "text": "Data Migration"},
            "submit": {"type": "plain_text", "text": "Import"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": modal_blocks,
        },
    )


def handle_data_migration_export(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Export workspace migration JSON and send to user's DM."""
    user_id = helpers.get_user_id_from_body(body)
    team_id = helpers.get_team_id_from_body(body)
    if not _is_admin(client, user_id, body):
        return
    workspace_record = helpers.get_workspace_record(team_id, body, context, client)
    if not workspace_record:
        return
    _update_wait_modal(
        client,
        body,
        title="Data Migration",
        callback_id=actions.CONFIG_DATA_MIGRATION_SUBMIT,
        dm=True,
    )
    try:
        payload = ei.build_migration_export(workspace_record.id)
        json_str = json.dumps(payload, default=ei._json_serializer, indent=2)
        dm_channel = _open_dm_channel(client, user_id)
        client.files_upload_v2(
            content=json_str,
            filename=f"syncbot-migration-{workspace_record.team_id}-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}.json",
            channel=dm_channel,
            initial_comment=(
                f":outbox_tray: Here is the SyncBot migration file for "
                f"`{helpers.resolve_workspace_name(workspace_record) or workspace_record.team_id}`."
            ),
        )
    except Exception as e:
        log_error("data_migration_export_failed", error=str(e), exc_info=True)
        _close_modal_done(
            client,
            body,
            ":warning: SyncBot could not send the migration file. Try Export again.",
        )


def _data_migration_prepare(
    body: dict,
    client: WebClient,
    context: dict,
) -> tuple[dict | None, dict | None, int | None, dict[str, int] | None, object | None]:
    """Shared validation for migration ack/work.

    Returns ``(error_ack_dict, data, group_id, team_id_to_workspace_id, workspace_record)``.
    """
    user_id = helpers.get_user_id_from_body(body)
    team_id = helpers.get_team_id_from_body(body)
    if not _is_admin(client, user_id, body):
        return None, None, None, None, None

    values = helpers.safe_get(body, "view", "state", "values") or {}
    file_data = helpers.safe_get(
        values, actions.CONFIG_DATA_MIGRATION_JSON_INPUT, actions.CONFIG_DATA_MIGRATION_JSON_INPUT
    )
    files = file_data.get("files") if file_data else None

    if not files:
        return (
            {
                "response_action": "errors",
                "errors": {actions.CONFIG_DATA_MIGRATION_JSON_INPUT: "Upload a migration JSON file to import."},
            },
            None,
            None,
            None,
            None,
        )

    file_info = files[0]
    file_url = file_info.get("url_private_download") or file_info.get("url_private")
    if not file_url:
        return (
            {
                "response_action": "errors",
                "errors": {actions.CONFIG_DATA_MIGRATION_JSON_INPUT: "Could not retrieve the uploaded file."},
            },
            None,
            None,
            None,
            None,
        )

    json_text, dl_err = _download_uploaded_file(file_url, client.token)
    if dl_err:
        return (
            {
                "response_action": "errors",
                "errors": {actions.CONFIG_DATA_MIGRATION_JSON_INPUT: dl_err},
            },
            None,
            None,
            None,
            None,
        )

    try:
        data = json.loads(json_text)
    except json.JSONDecodeError as e:
        return (
            {
                "response_action": "errors",
                "errors": {actions.CONFIG_DATA_MIGRATION_JSON_INPUT: f"Invalid JSON in uploaded file: {e}"},
            },
            None,
            None,
            None,
            None,
        )

    if data.get("version") != ei.MIGRATION_VERSION:
        return (
            {
                "response_action": "errors",
                "errors": {
                    actions.CONFIG_DATA_MIGRATION_JSON_INPUT: f"Unsupported migration version (expected {ei.MIGRATION_VERSION})."
                },
            },
            None,
            None,
            None,
            None,
        )

    workspace_payload = data.get("workspace", {})
    export_team_id = workspace_payload.get("team_id")
    if not export_team_id:
        return (
            {
                "response_action": "errors",
                "errors": {actions.CONFIG_DATA_MIGRATION_JSON_INPUT: "Migration file missing workspace.team_id."},
            },
            None,
            None,
            None,
            None,
        )

    workspace_record = helpers.get_workspace_record(team_id, body, context, client)
    if not workspace_record or workspace_record.team_id != export_team_id:
        return (
            {
                "response_action": "errors",
                "errors": {
                    actions.CONFIG_DATA_MIGRATION_JSON_INPUT: "This migration file is for a different workspace. Open the app from the workspace that matches the migration file."
                },
            },
            None,
            None,
            None,
            None,
        )

    team_id_to_workspace_id = {workspace_record.team_id: workspace_record.id}
    for row in DbManager.find_records(
        schemas.Workspace,
        [schemas.Workspace.deleted_at.is_(None)],
    ):
        if not row.team_id or row.team_id in team_id_to_workspace_id:
            continue
        if helpers.is_stub_workspace(row):
            team_id_to_workspace_id[row.team_id] = row.id

    return None, data, 0, team_id_to_workspace_id, workspace_record


def _migration_import_cache_key(user_id: str) -> str:
    return f"migration_import_pending:{user_id}"


def _migration_message_count(post_meta: dict, sync_uid: str, channel_id: str) -> int:
    key = f"{sync_uid}:{channel_id}"
    count = 0
    for row in post_meta.get(key) or []:
        if not isinstance(row, dict):
            continue
        kind = row.get("kind") or constants.POST_META_KIND_MESSAGE
        if kind == constants.POST_META_KIND_MESSAGE:
            count += 1
    return count


def _migration_overview_blocks(data: dict) -> list:
    """Home-style Group / sync / mapping / message counts from migration JSON."""
    from slack.blocks import section

    groups = [item for item in (data.get("groups") or []) if isinstance(item, dict)]
    syncs = [item for item in (data.get("syncs") or []) if isinstance(item, dict)]
    channels = [item for item in (data.get("sync_channels") or []) if isinstance(item, dict)]
    mappings = [item for item in (data.get("user_mappings") or []) if isinstance(item, dict)]
    post_meta = data.get("post_meta") if isinstance(data.get("post_meta"), dict) else {}
    channels_by_sync: dict[str, list] = {}
    for channel in channels:
        uid = str(channel.get("sync_uid") or "")
        if uid:
            channels_by_sync.setdefault(uid, []).append(channel)
    mapped_count = len(mappings)
    blocks = [section("*This file will import*")]
    if not groups:
        blocks.append(
            section(
                f"No Workspace Groups.\nMapped Users: `{mapped_count}`\n"
                f"Synced Channels: `{len(channels)}`\nSynced Messages: "
                f"`{sum(_migration_message_count(post_meta, str(s.get('uid') or ''), str(c.get('channel_id') or '')) for s in syncs for c in channels_by_sync.get(str(s.get('uid') or ''), []))}`"
            )
        )
        return blocks
    for group in groups:
        uid = str(group.get("uid") or "")
        name = str(group.get("name") or "Unnamed group").strip() or "Unnamed group"
        group_syncs = [item for item in syncs if str(item.get("group_uid") or "") == uid]
        titles = [str(item.get("title") or "").strip() for item in group_syncs if str(item.get("title") or "").strip()]
        channel_count = 0
        message_count = 0
        for sync in group_syncs:
            sync_uid = str(sync.get("uid") or "")
            sync_channels = channels_by_sync.get(sync_uid, [])
            channel_count += len(sync_channels)
            for channel in sync_channels:
                message_count += _migration_message_count(post_meta, sync_uid, str(channel.get("channel_id") or ""))
        title_line = ", ".join(f"`{title}`" for title in titles) if titles else "`None`"
        blocks.append(
            section(
                f"*{name}*\n"
                f"Synced Channels: `{channel_count}` {title_line}\n"
                f"Mapped Users: `{mapped_count}`\n"
                f"Synced Messages: `{message_count}`"
            )
        )
    return blocks


def _run_migration_import(
    data: dict,
    workspace_record,
    team_id_to_workspace_id: dict,
    group_id: int,
    *,
    client: WebClient,
    acting_user_id: str | None = None,
    context: dict | None = None,
) -> None:
    ei.import_migration_data(
        data,
        workspace_record.id,
        group_id,
        team_id_to_workspace_id=team_id_to_workspace_id,
    )
    ei.invalidate_home_tab_caches_for_team(workspace_record.team_id)


def _remember_migration_team(
    names_by_team: dict[str, str],
    team_id: str,
    workspace_name: str | None,
    this_team_id: str,
) -> None:
    tid = (team_id or "").strip()
    if not tid or tid == this_team_id:
        return
    name = str(workspace_name or "").strip()
    if tid not in names_by_team:
        names_by_team[tid] = name or tid
    elif name and names_by_team[tid] == tid:
        names_by_team[tid] = name


def _migration_workspace_names_not_here(data: dict, this_team_id: str) -> list[str]:
    """Display names of Slack teams in the file that have no Workspace row here."""
    names_by_team: dict[str, str] = {}
    for group in data.get("groups") or []:
        if not isinstance(group, dict):
            continue
        for member in group.get("member_workspaces") or []:
            if isinstance(member, dict):
                _remember_migration_team(
                    names_by_team,
                    str(member.get("team_id") or ""),
                    member.get("workspace_name"),
                    this_team_id,
                )
        for team in group.get("member_team_ids") or []:
            _remember_migration_team(names_by_team, str(team or ""), None, this_team_id)
    for channel in data.get("sync_channels") or []:
        if isinstance(channel, dict):
            _remember_migration_team(names_by_team, str(channel.get("team_id") or ""), None, this_team_id)
    missing = []
    for team_id, name in names_by_team.items():
        rows = DbManager.find_records(schemas.Workspace, [schemas.Workspace.team_id == team_id])
        if not rows:
            missing.append(name)
    missing.sort(key=str.lower)
    return missing


def _migration_groups_not_joined(data: dict, workspace_id: int) -> list[str]:
    """Names of active Groups in the file that this Workspace is not a member of."""
    names: list[str] = []
    for group in data.get("groups") or []:
        if not isinstance(group, dict):
            continue
        uid = str(group.get("uid") or "").strip()
        if not uid:
            continue
        rows = DbManager.find_records(
            schemas.WorkspaceGroup,
            [
                schemas.WorkspaceGroup.uid == uid,
                schemas.WorkspaceGroup.status == "active",
            ],
        )
        if not rows:
            continue
        row = rows[0]
        members = DbManager.find_records(
            schemas.WorkspaceGroupMember,
            [
                schemas.WorkspaceGroupMember.group_id == row.id,
                schemas.WorkspaceGroupMember.workspace_id == workspace_id,
                schemas.WorkspaceGroupMember.status == "active",
                schemas.WorkspaceGroupMember.deleted_at.is_(None),
            ],
        )
        if members:
            continue
        name = str(getattr(row, "name", "") or group.get("name") or "").strip()
        if name:
            names.append(name)
    names.sort(key=str.lower)
    return names


def _left_out_groups_text(names: list[str]) -> str:
    label = _format_workspace_names(names)
    if len(names) == 1:
        return (
            f":warning: {label} is already on this install, and this Workspace is not in it, "
            "so Import leaves it out. Ask an Owner of that Group to invite this Workspace, "
            "then import this file again."
        )
    return (
        f":warning: {label} are already on this install, and this Workspace is not in them, "
        "so Import leaves them out. Ask an Owner of each Group to invite this Workspace, "
        "then import this file again."
    )


def _format_workspace_names(names: list[str]) -> str:
    ticked = [f"`{name}`" for name in names]
    if len(ticked) == 1:
        return ticked[0]
    if len(ticked) == 2:
        return f"{ticked[0]} and {ticked[1]}"
    return ", ".join(ticked[:-1]) + f", and {ticked[-1]}"


def _import_connect_instruction() -> str:
    turn_on = ""
    if not helpers.federation_enabled():
        turn_on = "turns Federation on in Settings, then "
    return (
        f"A primary Workspace admin {turn_on}uses Create Connection, the other install uses Join, "
        "and then this file is imported again."
    )


def _missing_channels_clause(names: list[str]) -> str:
    label = _format_workspace_names(names)
    verb = "is" if len(names) == 1 else "are"
    channels = "its Channels" if len(names) == 1 else "their Channels"
    return f"{label} {verb} not on this install, so {channels} were not imported."


def _import_missing_workspace_text(names: list[str], *, finished: bool) -> str:
    clause = _missing_channels_clause(names)
    connect = _import_connect_instruction()
    if finished:
        return f":white_check_mark: Import finished. :warning: {clause} {connect}"
    return (
        f":warning: {_format_workspace_names(names)} "
        f"{'is' if len(names) == 1 else 'are'} not on this install. "
        "Import keeps this Workspace's Groups and Syncs and leaves out "
        f"{'its Channels' if len(names) == 1 else 'their Channels'}. {connect}"
    )


_IMPORT_FINISH_HELP = (
    "How should this import finish?\n"
    "*Wait for the other install.* This Workspace's Syncs stay `Waiting for migration` "
    "until an External Connection that includes this Workspace exists and the old instance uninstalls SyncBot.\n"
    "*Finish on this install.* This Workspace is not still installed on another SyncBot, "
    "so the Syncs resume now."
)


def _migration_finish_block() -> orm.InputBlock:
    initial = "wait" if helpers.federation_enabled() else "finish"
    return orm.InputBlock(
        label="How this import finishes",
        action=actions.CONFIG_DATA_MIGRATION_FINISH,
        optional=False,
        element=orm.RadioButtonsElement(
            initial_value=initial,
            options=[
                orm.SelectorOption(name="Wait for the other install", value="wait"),
                orm.SelectorOption(name="Finish on this install", value="finish"),
            ],
        ),
    )


def _migration_finish_choice(body: dict) -> str | None:
    value = helpers.safe_get(
        body,
        "view",
        "state",
        "values",
        actions.CONFIG_DATA_MIGRATION_FINISH,
        actions.CONFIG_DATA_MIGRATION_FINISH,
        "selected_option",
        "value",
    )
    if value in ("wait", "finish"):
        return value
    return None


def _import_finished_text(data: dict, workspace, release: str, *, finish_here: bool) -> str:
    missing = _migration_workspace_names_not_here(data, getattr(workspace, "team_id", "") or "")
    if release == "waiting_for_connection":
        held = (
            ":white_check_mark: Import finished. :warning: This install has no External Connection, "
            "so this Workspace stays `Waiting for migration`."
        )
        turn_on = "" if helpers.federation_enabled() else "turns Federation on in Settings, then "
        follow = (
            f"A primary Workspace admin {turn_on}connects the two installs with Create Connection and Join, "
            "and picks this Workspace for that connection."
        )
        if missing:
            return (
                f"{held} {_missing_channels_clause(missing)} {follow} Then import this file again. "
                "The Syncs resume after the old instance uninstalls SyncBot."
            )
        return f"{held} {follow} The Syncs resume after the old instance uninstalls SyncBot."
    if release == "waiting_for_uninstall":
        text = (
            ":white_check_mark: Import finished. :warning: The other install still has this Workspace, "
            "so these Syncs stay `Waiting for migration` until that install uninstalls SyncBot."
        )
        if missing:
            where = "that Workspace" if len(missing) == 1 else "those Workspaces"
            text += (
                f" :warning: {_missing_channels_clause(missing)} "
                f"Import this file again after {where} is on this install."
            )
        return text
    if finish_here:
        if missing:
            return _import_missing_workspace_text(missing, finished=True)
        return ":white_check_mark: Import finished."
    text = (
        ":white_check_mark: Import finished. The old instance already uninstalled SyncBot, so these Syncs are active."
    )
    if missing:
        warning = _import_missing_workspace_text(missing, finished=True).split(":warning: ", 1)[1]
        text += f" :warning: {warning}"
    return text


def handle_data_migration_submit_ack(
    body: dict,
    client: WebClient,
    context: dict,
) -> dict | None:
    """Ack phase: validate the file and update the modal to an import review."""
    user_id = helpers.get_user_id_from_body(body)
    err, data, group_id, team_id_to_workspace_id, workspace_record = _data_migration_prepare(body, client, context)
    if err is not None:
        return err
    if data is None or group_id is None or team_id_to_workspace_id is None or workspace_record is None:
        return None

    kind, signer = ei.migration_file_status(data)
    if kind not in ("this_install", "connected"):
        source = data.get("source_instance") if isinstance(data.get("source_instance"), dict) else {}
        name = ei.migration_source_name(str(source.get("instance_id") or ""), signer)
        return {
            "response_action": "errors",
            "errors": {actions.CONFIG_DATA_MIGRATION_JSON_INPUT: _migration_refused_text(kind, name)},
        }
    source = data.get("source_instance") if isinstance(data.get("source_instance"), dict) else {}
    instance_id = str(source.get("instance_id") or "")
    from helpers._cache import _cache_set

    _cache_set(
        _migration_import_cache_key(user_id),
        {
            "data": data,
            "group_id": group_id,
            "workspace_id": workspace_record.id,
            "team_id_to_workspace_id": team_id_to_workspace_id,
            "source_instance_id": instance_id,
            "source_name": ei.migration_source_name(instance_id, signer),
            "source_kind": kind,
        },
        ttl=600,
    )
    from slack.blocks import section

    blocks = []
    missing = _migration_workspace_names_not_here(data, workspace_record.team_id)
    if missing:
        blocks.append(section(_import_missing_workspace_text(missing, finished=False)))
    left_out = _migration_groups_not_joined(data, workspace_record.id)
    if left_out:
        blocks.append(section(_left_out_groups_text(left_out)))
    blocks.append(section(_IMPORT_FINISH_HELP))
    blocks.append(_migration_finish_block())
    blocks.extend(_migration_overview_blocks(data))
    review = orm.BlockView(blocks=blocks)
    return {
        "response_action": "update",
        "view": review.as_ack_update(
            title_text="Confirm Import",
            callback_id=actions.CONFIG_DATA_MIGRATION_REVIEW,
            submit_button_text="Import",
            close_button_text="Cancel",
        ),
    }


_IMPORT_FAIL_TEXT = ":warning: Import failed. Try the import again."
_IMPORT_EXPIRED_TEXT = ":warning: That import expired. Try the import again."


def _migration_refused_text(kind: str, name: str) -> str:
    if kind == "tampered":
        return (
            "This file was changed after it was exported. SyncBot won't import it. "
            "Export it again from the install that made it."
        )
    if kind == "unchecked":
        return (
            "SyncBot can't check this file. Export it again from this install or from a connected install. "
            "A file from before 1.7.3 needs a new export."
        )
    if kind == "federation_off":
        return (
            f"This file is from `{name}`, and Federation is off. "
            "A primary Workspace admin can turn it on in Settings."
        )
    if kind == "not_connected":
        return (
            f"This file is from `{name}`, and that connection is not active. "
            "A primary Workspace admin can set up the External Connection again, then import this file again."
        )
    return (
        f"SyncBot doesn't know the install that signed this file (`{name}`). "
        "If it came from a SyncBot install you trust, a primary Workspace admin can connect the two installs "
        "under External Connections, then import it again."
    )


def _migration_stopped_text(kind: str, name: str) -> str:
    if kind == "tampered":
        return ":warning: Import stopped. This file was changed after it was exported. SyncBot won't import it."
    if kind == "unchecked":
        return (
            ":warning: Import stopped. SyncBot can't check this file. "
            "Export it again from this install or from a connected install."
        )
    if kind == "federation_off":
        return (
            ":warning: Import stopped. Federation is off. "
            "A primary Workspace admin can turn it on in Settings, then import this file again."
        )
    return (
        f":warning: Import stopped. `{name}` is no longer connected here. "
        "Connect it again under External Connections, then import the file again."
    )


def handle_data_migration_review_ack(body: dict, client: WebClient, context: dict) -> dict | None:
    """Keep the modal open while import runs."""
    if _migration_finish_choice(body) is None:
        return {
            "response_action": "errors",
            "errors": {actions.CONFIG_DATA_MIGRATION_FINISH: "Choose how to finish this import."},
        }
    return _wait_modal_ack(
        title="Confirm Import",
        callback_id=actions.CONFIG_DATA_MIGRATION_REVIEW,
        dm=True,
    )


def _notify_import_result(client: WebClient, body: dict, user_id: str | None, message: str) -> None:
    helpers.notify_user_dm(client, user_id, message)
    _close_modal_done(client, body, message)


def _complete_data_migration_import(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
    *,
    user_id: str | None,
    finish_here: bool,
) -> None:
    if not user_id:
        return
    from helpers._cache import _cache_get

    meta = _cache_get(_migration_import_cache_key(user_id))
    if not meta:
        log_warning("data_migration_import", user_id=user_id)
        _notify_import_result(client, body, user_id, _IMPORT_EXPIRED_TEXT)
        return
    data = meta.get("data")
    group_id = meta.get("group_id")
    workspace_id = meta.get("workspace_id")
    team_id_to_workspace_id = meta.get("team_id_to_workspace_id", {})
    if not data or group_id is None or not workspace_id:
        _notify_import_result(client, body, user_id, _IMPORT_EXPIRED_TEXT)
        return
    workspace_record = helpers.get_workspace_by_id(workspace_id)
    if not workspace_record:
        _notify_import_result(client, body, user_id, _IMPORT_FAIL_TEXT)
        return
    kind, signer = ei.migration_file_status(data)
    if kind not in ("this_install", "connected"):
        source = data.get("source_instance") if isinstance(data.get("source_instance"), dict) else {}
        instance_id = str(source.get("instance_id") or "")
        if signer is not None:
            name = ei.migration_source_name(instance_id, signer)
        else:
            name = meta.get("source_name") or ei.migration_source_name(instance_id, None)
        _notify_import_result(client, body, user_id, _migration_stopped_text(kind, name))
        return
    try:
        _run_migration_import(
            data,
            workspace_record,
            team_id_to_workspace_id,
            group_id,
            client=client,
            acting_user_id=user_id,
            context=context,
        )
        release = "resumed"
        if getattr(workspace_record, "team_id", None):
            release = helpers.consider_migration_release(workspace_record.team_id, finish_here=finish_here)
        builders.refresh_home_tab_for_workspace(
            workspace_record,
            logger,
            context=context,
            user_id=user_id,
        )
    except Exception:
        log_error("data_migration_import_failed", user_id=user_id, exc_info=True)
        _notify_import_result(client, body, user_id, _IMPORT_FAIL_TEXT)
        return
    _notify_import_result(
        client,
        body,
        user_id,
        _import_finished_text(data, workspace_record, release, finish_here=finish_here),
    )


def handle_data_migration_review(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Import after the review modal is submitted."""
    user_id = helpers.get_user_id_from_body(body)
    if not user_id or not _is_admin(client, user_id, body):
        return
    _complete_data_migration_import(
        body,
        client,
        logger,
        context,
        user_id=user_id,
        finish_here=_migration_finish_choice(body) == "finish",
    )
