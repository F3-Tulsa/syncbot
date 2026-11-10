"""Sync management handlers — Home tab, auth, and database reset."""

from logging import Logger

from slack_sdk.web import WebClient

import builders
import helpers
from db import DbManager, schemas
from db.event_claims import run_claimed
from handlers._common import _MAINTENANCE_BUSY_TEXT, _close_modal_done, _update_wait_modal
from helpers.sync_lifecycle import (
    get_sync_channels,
    leave_channel_without_sync,
    resume_sync_channel,
)
from helpers.synced_by import refusal_code, viewer_context
from logger import log_critical, log_info, log_warning
from slack import actions, orm


def handle_app_home_opened(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Handle the ``app_home_opened`` event by publishing the Home tab."""
    helpers.purge_stale_soft_deletes()
    team_id = helpers.get_team_id_from_body(body)
    user_id = helpers.get_user_id_from_body(body)
    if team_id and user_id:
        workspace_record = helpers.get_workspace_record(team_id, body, context, client)
        if workspace_record:
            is_admin = helpers.is_workspace_admin(client, user_id)
            is_manager = helpers.is_workspace_manager(client, user_id, team_id)
            extra_manager_ids = tuple(sorted(helpers.extra_manager_user_ids(team_id)))
            viewer = viewer_context(workspace_record, user_id, client)
            current_hash = builders._home_tab_content_hash(
                workspace_record,
                user_id,
                is_manager=is_manager,
                is_admin=is_admin,
                extra_manager_ids=extra_manager_ids,
                private_channel_ids=viewer.channel_ids_for_hash(),
            )
            hash_key = builders.home_tab_hash_key(team_id, user_id)
            blocks_key = f"home_tab_blocks:{team_id}:{user_id}"
            if helpers._cache_get(hash_key) == current_hash:
                cached_blocks = helpers._cache_get(blocks_key)
                if cached_blocks is not None:
                    client.views_publish(user_id=user_id, view={"type": "home", "blocks": cached_blocks})
                    helpers.remember_home_viewer(team_id, user_id)
                    return
            builders.build_home_tab(
                body,
                client,
                logger,
                context,
                user_id=user_id,
                workspace=workspace_record,
                content_hash=current_hash,
                viewer=viewer,
            )
            return
    builders.build_home_tab(body, client, logger, context)


def handle_authorize_syncbot(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Acknowledge the Authorize SyncBot button.

    The button is a link: Slack opens the OAuth install from its ``url`` and this
    payload arrives only as a notification. Registering it keeps the click out of
    the ``no_handler`` error log. The Home tab drops the section on the next
    ``app_home_opened`` once the install writes a user token.
    """
    log_info(
        "authorize_syncbot_clicked",
        team_id=helpers.get_team_id_from_body(body),
        user_id=helpers.get_user_id_from_body(body),
    )


def handle_refresh_home(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Handle the Refresh button on the Home tab.

    Available to everyone so a non-admin can reload Home after revoking
    Authorize SyncBot. When the content hash matches the cached Home, Refresh
    does nothing. Otherwise it rebuilds this user's Home from the DB.
    Workspace names refresh at most daily when a workspace is loaded, not via
    an instance-wide ``team_info`` sweep. Refresh does not pulse External
    Connections. ``/health`` and ``/ready`` do that.
    """
    team_id = helpers.get_team_id_from_body(body)
    user_id = helpers.get_user_id_from_body(body)
    if not team_id or not user_id:
        return

    workspace_record = helpers.get_workspace_record(team_id, body, context, client)
    if not workspace_record:
        return

    is_admin = helpers.is_workspace_admin(client, user_id)
    is_manager = helpers.is_workspace_manager(client, user_id, team_id)
    extra_manager_ids = tuple(sorted(helpers.extra_manager_user_ids(team_id)))
    viewer = viewer_context(workspace_record, user_id, client)
    current_hash = builders._home_tab_content_hash(
        workspace_record,
        user_id,
        is_manager=is_manager,
        is_admin=is_admin,
        extra_manager_ids=extra_manager_ids,
        private_channel_ids=viewer.channel_ids_for_hash(),
    )
    hash_key = builders.home_tab_hash_key(team_id, user_id)
    blocks_key = f"home_tab_blocks:{team_id}:{user_id}"

    if helpers.cached_home_blocks(current_hash, hash_key, blocks_key) is not None:
        return

    # Names refresh at most daily in get_workspace_record / _maybe_refresh_workspace_name.
    # Do not team_info() every installed workspace on Refresh.
    block_dicts = builders.build_home_tab(
        body,
        client,
        logger,
        context,
        user_id=user_id,
        return_blocks=True,
        workspace=workspace_record,
        content_hash=current_hash,
        viewer=viewer,
    )
    if block_dicts is None:
        return
    client.views_publish(user_id=user_id, view={"type": "home", "blocks": block_dicts})
    helpers.refresh_after_full(hash_key, blocks_key, current_hash, block_dicts)


def handle_member_joined_channel(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """A person joined a Channel, or SyncBot was added to one.

    A manager adding SyncBot back resumes every paused Sync on that Channel that the
    predicate allows. When nothing resumes, SyncBot says why and leaves; a re-add is
    never a silent no-op and never a way around Home.
    """
    run_claimed(body, lambda: _member_joined_channel(body, client, logger, context))


def _member_joined_channel(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    event = body.get("event", {})
    user_id = event.get("user")
    channel_id = event.get("channel")
    team_id = helpers.get_team_id_from_body(body)
    if not user_id or not channel_id or not team_id:
        return

    own_user_id = helpers.get_own_bot_user_id(client, context)
    if user_id != own_user_id:
        helpers.note_private_channel_membership(client, team_id, user_id, channel_id)
        return

    inviter = event.get("inviter")
    if not inviter:
        # SyncBot's own conversations.join from Create, Join, Resume, or restore.
        log_info("member_joined_channel", outcome="self_join", channel_id=channel_id)
        return
    workspace = DbManager.get_record(schemas.Workspace, id=team_id)
    rows = get_sync_channels(workspace.id, channel_id) if workspace else []
    if any(getattr(row, "status", None) == "active" for row in rows):
        log_info("member_joined_channel", outcome="noop_active", channel_id=channel_id)
        return
    paused = [row for row in rows if getattr(row, "status", None) == "paused"]
    if paused:
        # The invite proves the inviter is in the Channel; a cached answer may predate it.
        helpers.invalidate_private_channel_member(team_id, inviter)
        denials: list[tuple[str, str]] = []
        resumed = 0
        for row in paused:
            denied = resume_sync_channel(row, workspace, inviter, client=client, join=False, context=context)
            if denied:
                denials.append((_sync_title(row), denied))
            else:
                resumed += 1
        if not denials:
            log_info("member_joined_channel", outcome="resumed", channel_id=channel_id, rows=resumed)
            return
        text = ":double_vertical_bar: SyncBot was added back, but these Syncs stayed paused:\n" + "\n".join(
            f"\u2022 {title}: {sentence}" for title, sentence in denials
        )
        helpers.notify_user_dm(client, inviter, text)
        if resumed:
            helpers.notify_synced_channels(client, [channel_id], text)
            log_info("member_joined_channel", outcome="resumed_some", channel_id=channel_id, denials=len(denials))
            return
        leave_channel_without_sync(client, channel_id, workspace=workspace, text=text)
        log_info(
            "member_joined_channel",
            outcome=f"left_{refusal_code(denials[0][1])}",
            channel_id=channel_id,
            denial=denials[0][1],
        )
        return
    if rows:
        # A pending row waits for the origin instance; nothing to resume or to leave for.
        log_info("member_joined_channel", outcome="noop_pending", channel_id=channel_id)
        return
    outcome = leave_channel_without_sync(client, channel_id, workspace=workspace)
    log_info("member_joined_channel", outcome=outcome, channel_id=channel_id)


def _sync_title(row: schemas.SyncChannel) -> str:
    sync = DbManager.get_record(schemas.Sync, id=row.sync_id) if row.sync_id else None
    title = getattr(sync, "title", None)
    return title or f"Sync {row.sync_id}"


# ---------------------------------------------------------------------------
# Database Reset (gated by PRIMARY_WORKSPACE + ENABLE_DB_RESET)
# ---------------------------------------------------------------------------


def handle_db_reset(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Open a confirmation modal warning the user before a full DB reset.

    Only when PRIMARY_WORKSPACE matches and ENABLE_DB_RESET is truthy (see helpers.core).
    """
    team_id = helpers.get_team_id_from_body(body)
    if not helpers.is_db_reset_visible_for_workspace(team_id):
        return

    user_id = helpers.get_user_id_from_body(body)
    if not user_id or not helpers.is_workspace_admin(client, user_id):
        return

    trigger_id = helpers.safe_get(body, "trigger_id")
    if not trigger_id:
        return

    orm.update_opened_view(
        client,
        body,
        trigger_id,
        {
            "type": "modal",
            "title": {"type": "plain_text", "text": "Yikes! Reset Database?"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            ":rotating_light: *This Will Permanently Delete ALL Data!* :rotating_light:\n\n"
                            "Every Slack Install, Workspace Group, Channel Sync, and User Mapping, "
                            "in this database will be erased and the schema will be reinitialized.\n\n"
                            "*NOTE:* _All Slack Workspaces will need to reinstall the SyncBot app to get started again._\n\n"
                            "*This action cannot be undone! MAKE A BACKUP FIRST!*"
                        ),
                    },
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Confirm, Erase Everything!"},
                            "style": "danger",
                            "action_id": actions.CONFIG_DB_RESET_PROCEED,
                        },
                    ],
                },
            ],
        },
    )


def handle_db_reset_proceed(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Execute the database reset after user confirmed via modal.

    Same gating as handle_db_reset (PRIMARY_WORKSPACE + ENABLE_DB_RESET).
    """
    team_id = helpers.get_team_id_from_body(body)
    if not helpers.is_db_reset_visible_for_workspace(team_id):
        return

    user_id = helpers.get_user_id_from_body(body)
    if not user_id or not helpers.is_workspace_admin(client, user_id):
        return

    from db import drop_and_init_db
    from db.run_lock import DATA_MAINTENANCE, DATA_MAINTENANCE_TTL, hold_lock

    # The reset drops the lock row with every other table, so the release after
    # it finds nothing to delete.
    with hold_lock(DATA_MAINTENANCE, DATA_MAINTENANCE_TTL) as token:
        if token is None:
            log_warning("db_reset_busy", user_id=user_id)
            _close_modal_done(client, body, f":warning: Reset did not run. {_MAINTENANCE_BUSY_TEXT}")
            return
        _update_wait_modal(client, body, title="Yikes! Reset Database?", dm=False)
        log_critical("db_reset_triggered", user_id=user_id)
        drop_and_init_db()

    helpers.clear_all_caches()

    reset_done = "*Database Has Been Reset!*\nPlease reinstall SyncBot in your Workspace."
    view_id = helpers.safe_get(body, "view", "id")
    if view_id:
        try:
            client.views_update(
                view_id=view_id,
                view={
                    "type": "modal",
                    "title": {"type": "plain_text", "text": "Reset Complete"},
                    "close": {"type": "plain_text", "text": "Close"},
                    "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": reset_done}}],
                },
            )
        except Exception as e:
            log_warning("db_reset_modal_update_failed", error=str(e))

    if team_id and user_id:
        try:
            client.views_publish(
                user_id=user_id,
                view={
                    "type": "home",
                    "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": reset_done}}],
                },
            )
        except Exception as e:
            log_warning("db_reset_home_publish_failed", error=str(e))
