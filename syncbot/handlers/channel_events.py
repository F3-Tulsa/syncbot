"""Slack Channel membership events: a person leaves, the bot is removed, a Channel is deleted."""

from logging import Logger

from slack_sdk.web import WebClient

import helpers
from db import DbManager, schemas
from db.event_claims import run_claimed
from helpers.leave_sync import soft_delete_sync_channels
from helpers.slack_api import get_own_bot_user_id
from helpers.sync_lifecycle import get_sync_channels, pause_sync_channel, purge_slack_deleted_channel
from logger import log_info


def handle_member_left_channel(body: dict, client: WebClient, logger: Logger, context: dict) -> None:
    """A person left a Channel, or the bot was removed from one."""
    run_claimed(body, lambda: _member_left_channel(body, client, logger, context))


def _member_left_channel(body: dict, client: WebClient, logger: Logger, context: dict) -> None:
    workspace, channel_id = _event_workspace_and_channel(body)
    user_id = (body.get("event") or {}).get("user")
    if not channel_id or not user_id:
        return
    bot_user_id = context.get("bot_user_id") or get_own_bot_user_id(client, context=context)
    if user_id == bot_user_id:
        install_workspace = _workspace_whose_bot_left(body, context, workspace)
        if install_workspace is None:
            return
        _pause_bot_left(install_workspace, channel_id)
        return
    if not workspace:
        return
    helpers.note_private_channel_membership(client, workspace.team_id, user_id, channel_id)
    ended = soft_delete_sync_channels(workspace, channel_id, "manager_left", synced_by_user_id=user_id)
    log_info(
        "member_left_channel",
        team_id=workspace.team_id,
        user_id=user_id,
        channel_id=channel_id,
        rows_ended=ended,
    )


def handle_channel_left(body: dict, client: WebClient, logger: Logger, context: dict) -> None:
    """The bot left or was removed from a Channel (``channel_left`` and ``group_left``)."""
    run_claimed(body, lambda: _channel_left(body, client, logger, context))


def _channel_left(body: dict, client: WebClient, logger: Logger, context: dict) -> None:
    event_workspace, channel_id = _event_workspace_and_channel(body)
    workspace = _workspace_whose_bot_left(body, context, event_workspace)
    if workspace and channel_id:
        _pause_bot_left(workspace, channel_id)


def handle_channel_deleted(body: dict, client: WebClient, logger: Logger, context: dict) -> None:
    """A Channel was deleted (``channel_deleted`` and ``group_deleted``)."""
    run_claimed(body, lambda: _channel_deleted(body, client, logger, context))


def _channel_deleted(body: dict, client: WebClient, logger: Logger, context: dict) -> None:
    workspace, channel_id = _event_workspace_and_channel(body)
    if workspace and channel_id:
        purge_slack_deleted_channel(workspace, channel_id)


def _workspace_whose_bot_left(
    body: dict,
    context: dict | None,
    event_workspace: schemas.Workspace | None,
) -> schemas.Workspace | None:
    """The install whose bot left, or the event Workspace when the install is unknown."""
    install_team = (context or {}).get("team_id")
    if not install_team:
        return event_workspace
    return DbManager.get_record(schemas.Workspace, id=install_team)


def _event_workspace_and_channel(body: dict) -> tuple[schemas.Workspace | None, str | None]:
    channel_id = (body.get("event") or {}).get("channel")
    team_id = helpers.get_team_id_from_body(body)
    workspace = DbManager.get_record(schemas.Workspace, id=team_id) if team_id else None
    return workspace, channel_id


def _pause_bot_left(workspace: schemas.Workspace, channel_id: str) -> None:
    for row in get_sync_channels(workspace.id, channel_id):
        pause_sync_channel(row, via="kick")
