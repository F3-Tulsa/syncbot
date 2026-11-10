"""Backup/Restore and Data Migration handlers (modals and submissions)."""

import json
import logging
from datetime import UTC, datetime
from logging import Logger

from slack_sdk.web import WebClient

import builders
import constants
import helpers
from db import DbManager, schemas, transaction
from db.run_lock import DATA_MAINTENANCE, DATA_MAINTENANCE_TTL, hold_lock
from handlers._common import _MAINTENANCE_BUSY_TEXT, _close_modal_done, _update_wait_modal, _wait_modal_ack
from helpers import export_import as ei
from helpers.workspace import invalidate_fed_ws_for_sync_cache
from logger import log_error, log_info, log_warning
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
    """Run restore under the run lock, then replace the wait modal with the outcome."""
    try:
        with hold_lock(DATA_MAINTENANCE, DATA_MAINTENANCE_TTL) as token:
            if token is None:
                log_info("backup_restore_busy", user_id=user_id)
                _close_modal_done(client, body, f":warning: Restore did not run. {_MAINTENANCE_BUSY_TEXT}")
                return
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
                "Save this Workspace's Groups, Syncs, and user mappings to a file. "
                "SyncBot sends it to you in a DM. The file has no tokens or passwords."
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
                label=(
                    "*Import*\n"
                    "Upload a file exported from this instance or from a connected instance. "
                    "You will see what is in it before anything changes."
                ),
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
        file_name = str(workspace_payload.get("workspace_name") or "").strip() or str(export_team_id)
        return (
            {
                "response_action": "errors",
                "errors": {
                    actions.CONFIG_DATA_MIGRATION_JSON_INPUT: (
                        f"This file is for `{file_name}`. Open SyncBot in that Workspace to import it."
                    )
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
    """*File contents*, then one code block per Group with Home's labels.

    Synced Channels are this Workspace's own Channels in that Group's Syncs.
    Mapped Users are the file's mappings for that Group. Synced Messages count
    every Channel in those Syncs.
    """
    from slack.blocks import section

    groups = [item for item in (data.get("groups") or []) if isinstance(item, dict)]
    syncs = [item for item in (data.get("syncs") or []) if isinstance(item, dict)]
    channels = [item for item in (data.get("sync_channels") or []) if isinstance(item, dict)]
    mappings = [item for item in (data.get("user_mappings") or []) if isinstance(item, dict)]
    post_meta = data.get("post_meta") if isinstance(data.get("post_meta"), dict) else {}
    workspace = data.get("workspace") if isinstance(data.get("workspace"), dict) else {}
    this_team_id = str(workspace.get("team_id") or "")
    channels_by_sync: dict[str, list] = {}
    for channel in channels:
        uid = str(channel.get("sync_uid") or "")
        if uid:
            channels_by_sync.setdefault(uid, []).append(channel)
    if not groups:
        return [section("This file has no Groups.")]
    blocks = [section("*File contents*")]
    for group in groups:
        uid = str(group.get("uid") or "")
        name = str(group.get("name") or "Unnamed group").strip() or "Unnamed group"
        own_channels: list[str] = []
        message_count = 0
        for sync in syncs:
            if str(sync.get("group_uid") or "") != uid:
                continue
            sync_uid = str(sync.get("uid") or "")
            for channel in channels_by_sync.get(sync_uid, []):
                channel_id = str(channel.get("channel_id") or "")
                message_count += _migration_message_count(post_meta, sync_uid, channel_id)
                if str(channel.get("team_id") or "") == this_team_id:
                    channel_name = str(channel.get("channel_name") or "").strip().removeprefix("#")
                    own_channels.append(f"#{channel_name or channel_id}")
        channel_line = f"Synced Channels: {len(own_channels)}"
        if own_channels:
            channel_line += f" ({', '.join(own_channels)})"
        mapped_count = sum(1 for item in mappings if str(item.get("group_uid") or "") == uid)
        lines = [
            f"Group Name: {name}",
            channel_line,
            f"Mapped Users: {mapped_count}",
            f"Synced Messages: {message_count}",
        ]
        text = "\n".join(lines).replace("`", "'")
        blocks.append(section(f"```{text}```"))
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
    keep_existing_roles: bool = False,
) -> None:
    """Import in one transaction: a bad row rolls every write back. Take the run lock first."""
    with transaction():
        ei.import_migration_data(
            data,
            workspace_record.id,
            group_id,
            team_id_to_workspace_id=team_id_to_workspace_id,
            keep_existing_roles=keep_existing_roles,
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


def _format_names(names: list[str]) -> str:
    ticked = [f"`{name}`" for name in names]
    if len(ticked) == 1:
        return ticked[0]
    if len(ticked) == 2:
        return f"{ticked[0]} and {ticked[1]}"
    return ", ".join(ticked[:-1]) + f", and {ticked[-1]}"


def _missing_workspaces_text(names: list[str], *, finished: bool = False) -> str:
    label = _format_names(names)
    outcome = "were left out" if finished else "will not come over"
    if len(names) == 1:
        return (
            f":warning: {label} is not on this instance yet, so its Channels {outcome}. "
            "Import this file again once it is here."
        )
    return (
        f":warning: {label} are not on this instance yet, so their Channels {outcome}. "
        "Import this file again once they are here."
    )


def _left_out_groups_text(names: list[str], *, finished: bool = False) -> str:
    label = _format_names(names)
    if len(names) == 1:
        left_out = ", so it was left out" if finished else ""
        return (
            f":warning: {label} is already on this instance, but this Workspace is not in it{left_out}. "
            f"Ask an Owner of {label} to invite this Workspace, then import again."
        )
    left_out = ", so they were left out" if finished else ""
    return (
        f":warning: {label} are already on this instance, but this Workspace is not in them{left_out}. "
        "Ask an Owner of each to invite this Workspace, then import again."
    )


def _workspace_has_pending_channels(workspace_id: int) -> bool:
    rows = DbManager.find_records(
        schemas.SyncChannel,
        [
            schemas.SyncChannel.workspace_id == workspace_id,
            schemas.SyncChannel.deleted_at.is_(None),
            schemas.SyncChannel.status == "pending",
        ],
    )
    return bool(rows)


def _import_finished_text(
    data: dict,
    workspace,
    result: str,
    peer,
    *,
    source_name: str,
    source_kind: str,
    has_pending: bool,
) -> str:
    """One message for the import's result, then anything that was left out.

    With no ``pending`` Channel left, the Syncs are running, so the message
    says that and nothing about a peer.
    """
    peer_name = source_name
    if peer is not None:
        peer_name = ei.format_migration_source_name(getattr(peer, "instance_id", "") or "", peer)
    if has_pending and result == "waiting_for_uninstall":
        text = (
            ":white_check_mark: Import finished. This Workspace's Syncs show `Waiting for migration` "
            f"until SyncBot is uninstalled from this Workspace on `{peer_name}`. They start on their own after that. "
            "If they do not, import this file again."
        )
    elif has_pending and result == "unreachable":
        text = (
            ":warning: Import could not finish. "
            f"SyncBot could not reach `{peer_name}` to check whether it still has this Workspace, "
            "so its Syncs show `Waiting for migration`. Try the import again in a few minutes."
        )
    elif has_pending and result == "refused":
        text = (
            f":warning: Import could not finish. `{peer_name}` did not accept SyncBot's check, "
            "so this Workspace's Syncs show `Waiting for migration`. "
            f"A primary Workspace admin can check the External Connection with `{peer_name}`, "
            "then import this file again."
        )
    elif result == "resumed" and source_kind == "connected":
        text = (
            f":white_check_mark: Import finished. SyncBot is already uninstalled from this Workspace "
            f"on `{source_name}`, so its Syncs are running."
        )
    elif result == "resumed" and source_kind == "unchecked":
        text = (
            ":white_check_mark: Import finished. This Workspace's Syncs are running. "
            "The file had no signature SyncBot could check. "
            "Other Workspaces in the file were left out; invite them to the Groups here."
        )
    elif result == "resumed" and source_kind in _STANDALONE_KINDS:
        text = (
            ":white_check_mark: Import finished. This Workspace's Syncs are running. "
            "Other Workspaces in the file were left out; invite them to the Groups here."
        )
    else:
        text = ":white_check_mark: Import finished. This Workspace's Syncs are running."
    team_id = getattr(workspace, "team_id", "") or ""
    workspace_id = getattr(workspace, "id", None)
    missing = _migration_workspace_names_not_here(data, team_id)
    if missing and source_kind not in _STANDALONE_KINDS:
        text += " " + _missing_workspaces_text(missing, finished=True)
    if workspace_id is not None:
        left_out = _migration_groups_not_joined(data, workspace_id)
        if left_out:
            text += " " + _left_out_groups_text(left_out, finished=True)
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

    kind, signer = ei.get_migration_file_status(data)
    if kind == "unchecked" and not helpers.unsigned_import_enabled():
        return {
            "response_action": "errors",
            "errors": {actions.CONFIG_DATA_MIGRATION_JSON_INPUT: _UNSIGNED_OFF_TEXT},
        }
    if kind not in _IMPORTABLE_KINDS:
        source = data.get("source_instance") if isinstance(data.get("source_instance"), dict) else {}
        name = ei.format_migration_source_name(str(source.get("instance_id") or ""), signer)
        return {
            "response_action": "errors",
            "errors": {actions.CONFIG_DATA_MIGRATION_JSON_INPUT: _migration_refused_text(kind, name)},
        }
    source = data.get("source_instance") if isinstance(data.get("source_instance"), dict) else {}
    source_name = ei.format_migration_source_name(str(source.get("instance_id") or ""), signer)
    from helpers._cache import _cache_set

    _cache_set(
        _migration_import_cache_key(user_id),
        {
            "data": data,
            "group_id": group_id,
            "workspace_id": workspace_record.id,
            "team_id_to_workspace_id": team_id_to_workspace_id,
            "source_name": source_name,
            "source_kind": kind,
        },
        ttl=600,
    )
    from slack.blocks import section

    blocks = []
    if kind == "unchecked":
        blocks.append(
            section(
                ":warning: This file has no signature SyncBot can check, so it may have been changed. "
                "Only import a file you exported."
            )
        )
    elif kind == "connected":
        blocks.append(
            section(
                f":package: This file was exported from this Workspace on `{source_name}`, "
                "a connected SyncBot instance."
            )
        )
    elif kind in _STANDALONE_KINDS:
        blocks.append(
            section(
                f":package: This file was signed by `{source_name}`, which is not connected here. "
                "SyncBot checked that it has not been changed, but cannot check where it came from. "
                "Only import a file you exported."
            )
        )
    else:
        blocks.append(section(":package: This file was exported from this Workspace on this SyncBot instance."))
    if kind != "unchecked":
        fingerprint = str(source.get("instance_id") or "").strip()
        if fingerprint:
            blocks.append(
                section(
                    f"Fingerprint: `{fingerprint}`. Compare this with Fingerprint in Settings "
                    "on the instance that exported the file."
                )
            )
    blocks.extend(_migration_overview_blocks(data))
    missing = _migration_workspace_names_not_here(data, workspace_record.team_id)
    if missing and kind not in _STANDALONE_KINDS:
        blocks.append(section(_missing_workspaces_text(missing)))
    left_out = _migration_groups_not_joined(data, workspace_record.id)
    if left_out:
        blocks.append(section(_left_out_groups_text(left_out)))
    if kind == "connected":
        blocks.append(
            section(
                f"Importing brings over this Workspace's Groups and Syncs from `{source_name}`, "
                f"including the other `{source_name}` Workspaces in those Groups. "
                f"Until SyncBot is uninstalled from this Workspace on `{source_name}`, "
                "these Syncs show `Waiting for migration`. "
                "If it is already uninstalled there, they start right away."
            )
        )
    elif kind in _STANDALONE_KINDS:
        blocks.append(
            section(
                "Importing brings over this Workspace's Groups, Syncs, Channels, and message history. "
                "Other Workspaces in the file are left out. They can join the Groups here with an invite code. "
                "Uninstall SyncBot from this Workspace on the other instance first, or every message posts twice."
            )
        )
    else:
        blocks.append(
            section(
                "Importing adds these Groups and Syncs back to this Workspace, and they start right away, "
                "unless another connected instance still has SyncBot in this Workspace."
            )
        )
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


_UNSIGNED_OFF_TEXT = (
    "This file has no signature SyncBot can check, so SyncBot will not import it. "
    "Whoever runs this deployment can set ENABLE_UNSIGNED_IMPORT for this move, then turn it off."
)
_IMPORT_FAIL_TEXT = ":warning: Import failed. Try the import again."
_IMPORT_EXPIRED_TEXT = ":warning: That import expired. Try the import again."
_STANDALONE_KINDS = frozenset({"unknown", "not_connected", "federation_off", "unchecked"})
_IMPORTABLE_KINDS = frozenset({"this_install", "connected"}) | _STANDALONE_KINDS


def _this_workspace_only_map(workspace_record) -> dict:
    """Map only the Workspace that is importing. Other teams in the file stay out."""
    team_id = getattr(workspace_record, "team_id", None)
    if not team_id:
        return {}
    return {team_id: workspace_record.id}


def _migration_refused_text(kind: str, name: str) -> str:
    if kind == "tampered":
        return (
            "This file was changed after it was exported, so SyncBot will not import it. "
            "Export it again from the instance that made it."
        )
    return "SyncBot will not import this file."


def handle_data_migration_review_ack(body: dict, client: WebClient, context: dict) -> dict | None:
    """Keep the modal open while import runs."""
    return _wait_modal_ack(
        title="Confirm Import",
        callback_id=actions.CONFIG_DATA_MIGRATION_REVIEW,
        dm=True,
    )


def _notify_import_result(client: WebClient, body: dict, user_id: str | None, message: str) -> None:
    helpers.notify_user_dm(client, user_id, message)
    _close_modal_done(client, body, message)


def _narrow_import_map(team_id_to_workspace_id: dict, workspace_record, signer) -> dict:
    """Keep this Workspace and stubs that belong to the instance that signed the file."""
    signer_id = getattr(signer, "instance_id", None)
    this_team = getattr(workspace_record, "team_id", None)
    narrowed = {}
    for team, ws_id in team_id_to_workspace_id.items():
        if team == this_team:
            narrowed[team] = ws_id
            continue
        other = helpers.get_workspace_by_id(ws_id)
        if other is not None and getattr(other, "instance_id", None) == signer_id:
            narrowed[team] = ws_id
    return narrowed


def _complete_data_migration_import(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
    *,
    user_id: str | None,
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
    kind, signer = ei.get_migration_file_status(data)
    if kind == "unchecked" and not helpers.unsigned_import_enabled():
        _notify_import_result(client, body, user_id, ":warning: Import stopped. " + _UNSIGNED_OFF_TEXT)
        return
    if kind not in _IMPORTABLE_KINDS:
        source = data.get("source_instance") if isinstance(data.get("source_instance"), dict) else {}
        instance_id = str(source.get("instance_id") or "")
        if signer is not None:
            name = ei.format_migration_source_name(instance_id, signer)
        else:
            name = meta.get("source_name") or ei.format_migration_source_name(instance_id, None)
        _notify_import_result(client, body, user_id, ":warning: Import stopped. " + _migration_refused_text(kind, name))
        return
    if kind == "connected":
        team_id_to_workspace_id = _narrow_import_map(team_id_to_workspace_id, workspace_record, signer)
    elif kind in _STANDALONE_KINDS:
        team_id_to_workspace_id = _this_workspace_only_map(workspace_record)
    source_name = (
        ei.format_migration_source_name(getattr(signer, "instance_id", "") or "", signer)
        if signer is not None
        else (meta.get("source_name") or "Connection")
    )
    try:
        with hold_lock(DATA_MAINTENANCE, DATA_MAINTENANCE_TTL) as token:
            if token is None:
                log_info("data_migration_import_busy", user_id=user_id)
                busy = True
            else:
                busy = False
                _run_migration_import(
                    data,
                    workspace_record,
                    team_id_to_workspace_id,
                    group_id,
                    client=client,
                    acting_user_id=user_id,
                    context=context,
                    keep_existing_roles=kind in _STANDALONE_KINDS,
                )
                result, peer = "resumed", None
                team_id = getattr(workspace_record, "team_id", None)
                if kind in _STANDALONE_KINDS and team_id:
                    helpers.restore_migrated_workspace(team_id)
                elif team_id:
                    result, peer = helpers.consider_migration_release(team_id)
        if busy:
            _notify_import_result(client, body, user_id, ":warning: Import stopped. " + _MAINTENANCE_BUSY_TEXT)
            return
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
        _import_finished_text(
            data,
            workspace_record,
            result,
            peer,
            source_name=source_name,
            source_kind=kind,
            has_pending=_workspace_has_pending_channels(workspace_record.id),
        ),
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
    )
