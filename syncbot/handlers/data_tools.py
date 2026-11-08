"""Data Tools modal for the primary Workspace."""

from logging import Logger

from slack_sdk.web import WebClient

import helpers
from handlers._common import _close_modal_done, _dm_user, _get_selected_option_value, _wait_modal_ack
from helpers._cache import _cache_delete, _cache_get, _cache_set
from helpers.data_repair import REPAIR_CHECKS, RepairReport, run_repair
from helpers.notifications import PurgeResult, purge_soft_deletes
from helpers.settings import soft_delete_retention_days
from logger import log_error
from slack import actions, orm

_RUNNING_KEY = "data_tools_running"
_RUNNING_TTL = 900
_DRY_RUN = "dry_run"
_REPAIR = "repair"
_PURGE = "purge"
_WAIT_TEXT = "Running. This usually takes under a minute. If you close this, the results come by DM."
_FAILED_TEXT = "Something went wrong. Nothing more was changed. Try again, or check the logs."


def _allowed(client: WebClient, body: dict) -> bool:
    user_id = helpers.get_user_id_from_body(body)
    team_id = helpers.get_team_id_from_body(body)
    if not user_id or not team_id or not helpers.is_workspace_admin(client, user_id):
        return False
    return helpers.is_backup_visible_for_workspace(team_id)


def build_data_tools_view() -> dict:
    """The Data Tools form. The first option is selected."""
    days = soft_delete_retention_days()
    options = [
        orm.SelectorOption("Repair (Dry Run)", _DRY_RUN, "Find problems. Changes nothing."),
        orm.SelectorOption("Repair", _REPAIR, "Find and fix problems."),
        orm.SelectorOption(
            "Purge Soft-Deleted Data",
            _PURGE,
            f"Remove uninstalled Workspaces and left Channels now, not in {days} days.",
        ),
    ]
    blocks = [
        orm.SectionBlock(
            label="Check and clean up this instance's database. Every Workspace here is affected."
        ).as_form_field(),
        orm.ContextBlock(element=orm.ContextElement(initial_value="Make a backup first.")).as_form_field(),
        orm.InputBlock(
            label="Choose",
            action=actions.CONFIG_DATA_TOOLS_OPTION,
            optional=False,
            element=orm.RadioButtonsElement(initial_value=_DRY_RUN, options=options),
        ).as_form_field(),
    ]
    return {
        "type": "modal",
        "callback_id": actions.CONFIG_DATA_TOOLS_SUBMIT,
        "title": {"type": "plain_text", "text": "Data Tools"},
        "submit": {"type": "plain_text", "text": "Run"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": blocks,
    }


def handle_data_tools(body: dict, client: WebClient, logger: Logger, context: dict) -> None:
    """Fill the Loading view with Data Tools. Admin on the primary Workspace only."""
    if not _allowed(client, body):
        return
    trigger_id = helpers.safe_get(body, "trigger_id")
    if not trigger_id:
        return
    orm.update_opened_view(client, body, trigger_id, build_data_tools_view())


def handle_data_tools_submit_ack(body: dict, client: WebClient, context: dict) -> dict | None:
    """Ack phase: refuse a second run, then show the wait view."""
    if not _allowed(client, body):
        return None
    if _cache_get(_RUNNING_KEY):
        return {
            "response_action": "errors",
            "errors": {
                actions.CONFIG_DATA_TOOLS_OPTION: "Data Tools is already running. Try again in a few minutes.",
            },
        }
    _cache_set(_RUNNING_KEY, True, ttl=_RUNNING_TTL)
    return _wait_modal_ack(
        title="Data Tools",
        callback_id=actions.CONFIG_DATA_TOOLS_SUBMIT,
        dm=True,
        text=_WAIT_TEXT,
    )


def handle_data_tools_submit_work(body: dict, client: WebClient, logger: Logger, context: dict) -> None:
    """Run the chosen tool, then show the results and send them by DM."""
    user_id = helpers.get_user_id_from_body(body)
    option = _get_selected_option_value(body, actions.CONFIG_DATA_TOOLS_OPTION)
    try:
        if not _allowed(client, body):
            return
        text = _run_option(option)
    except Exception:
        log_error("data_tools_failed", option=option, exc_info=True)
        text = _FAILED_TEXT
    finally:
        _cache_delete(_RUNNING_KEY)
    _dm_user(client, user_id, text)
    _close_modal_done(client, body, text)


def format_repair_text(report: RepairReport, *, fixed: bool) -> str:
    """Results for Repair and Repair (Dry Run). Zero counts are left out."""
    lines = [f"• {label}: {report.count(key)}" for key, label in REPAIR_CHECKS if report.count(key)]
    if not lines:
        return "No problems found."
    header = "Fixed" if fixed else "Found"
    text = f"*{header}*\n" + "\n".join(lines)
    if not fixed:
        text += "\nRun Repair to fix these."
    return text


def format_purge_text(result: PurgeResult) -> str:
    """Results for Purge Soft-Deleted Data."""
    removed = []
    if result.workspaces_purged:
        removed.append(f"• Workspaces: {result.workspaces_purged}")
    if result.channels_purged:
        removed.append(f"• Channels: {result.channels_purged}")
    if result.syncs_purged:
        removed.append(f"• Syncs: {result.syncs_purged}")
    if result.groups_disbanded:
        removed.append(f"• Groups: {result.groups_disbanded}")
    parts = []
    if removed:
        parts.append("*Removed*\n" + "\n".join(removed))
    if result.workspaces_kept:
        parts.append(f"Kept: {result.workspaces_kept} Workspaces still on an External Connection.")
    if not parts:
        return "Nothing to purge."
    return "\n".join(parts)


def _run_option(option: str | None) -> str:
    if option == _REPAIR:
        return format_repair_text(run_repair(fix=True), fixed=True)
    if option == _PURGE:
        return format_purge_text(purge_soft_deletes(0))
    return format_repair_text(run_repair(fix=False), fixed=False)
