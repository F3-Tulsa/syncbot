"""Data Tools modal for the primary Workspace."""

from logging import Logger

from slack_sdk.web import WebClient

import helpers
from handlers._common import _dm_user, _get_selected_option_value, _wait_modal_ack
from helpers._cache import _cache_delete, _cache_get, _cache_set
from helpers.data_repair import REPAIR_CHECKS, RepairReport, run_repair
from helpers.notifications import PurgeResult, purge_soft_deletes
from logger import log_error
from slack import actions, orm

_RUNNING_KEY = "data_tools_running"
_RUNNING_TTL = 900
_OPTIONS = (
    ("verify", "Verify", "Check database consistency."),
    ("repair", "Repair", "Fix database issues."),
    ("purge", "Purge Soft-Deleted Data", "Remove soft-deleted data immediately."),
)
_VERIFY, _REPAIR, _PURGE = (row[0] for row in _OPTIONS)
_FAILURE_LINE = "Result: Something went wrong. Nothing more was changed. Check the logs."
_RUNNING_LINE = "Result: Data Tools is already running. Nothing was changed. Try again in a few minutes."


def _allowed(client: WebClient, body: dict) -> bool:
    user_id = helpers.get_user_id_from_body(body)
    team_id = helpers.get_team_id_from_body(body)
    if not user_id or not team_id or not helpers.is_workspace_admin(client, user_id):
        return False
    return helpers.is_backup_visible_for_workspace(team_id)


def _option_label(value: str | None) -> str:
    for option_value, label, _description in _OPTIONS:
        if option_value == value:
            return label
    return "Verify"


def build_data_tools_view() -> dict:
    """The Data Tools form. The first option is selected."""
    options = [orm.SelectorOption(label, value, description) for value, label, description in _OPTIONS]
    blocks = [
        orm.SectionBlock(
            label="Verify and repair the SyncBot database. Every Workspace is affected. Make a backup first!"
        ).as_form_field(),
        orm.InputBlock(
            label="Choose",
            action=actions.CONFIG_DATA_TOOLS_OPTION,
            optional=False,
            element=orm.RadioButtonsElement(initial_value=_VERIFY, options=options),
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
    """Ack phase: refuse while a run is going, else show the wait view. The report is a DM.

    The ack only reads the lock. Production runs the ack again before the work,
    and on Lambda the two can land in different containers, so the work phase
    takes and clears the lock.
    """
    if not _allowed(client, body):
        return None
    if _cache_get(_RUNNING_KEY):
        return {
            "response_action": "errors",
            "errors": {
                actions.CONFIG_DATA_TOOLS_OPTION: "Data Tools is already running. Try again in a few minutes.",
            },
        }
    label = _option_label(_get_selected_option_value(body, actions.CONFIG_DATA_TOOLS_OPTION))
    return _wait_modal_ack(
        title="Data Tools",
        callback_id=actions.CONFIG_DATA_TOOLS_SUBMIT,
        dm=True,
        text=f"{label} is running. Close this and SyncBot will DM a report.",
    )


def handle_data_tools_submit_work(body: dict, client: WebClient, logger: Logger, context: dict) -> None:
    """Run the chosen tool, then DM the report. A run that finds another one going reports that."""
    user_id = helpers.get_user_id_from_body(body)
    option = _get_selected_option_value(body, actions.CONFIG_DATA_TOOLS_OPTION)
    label = _option_label(option)
    workspace_name = "Workspace"
    manager = None
    running = bool(_cache_get(_RUNNING_KEY))
    if not running:
        _cache_set(_RUNNING_KEY, True, ttl=_RUNNING_TTL)
    try:
        if not _allowed(client, body):
            return
        workspace_name, manager = _notice_identity(client, user_id, body, context)
        details = [_RUNNING_LINE] if running else _run_option(option)
    except Exception:
        log_error("data_tools_failed", option=option, exc_info=True)
        details = [_FAILURE_LINE]
    finally:
        if not running:
            _cache_delete(_RUNNING_KEY)
    _dm_user(
        client,
        user_id,
        _format_data_tools_notice(action=label, workspace_name=workspace_name, manager=manager, details=details),
    )


def _notice_identity(client: WebClient, user_id: str | None, body: dict, context: dict) -> tuple[str, str | None]:
    team_id = helpers.get_team_id_from_body(body)
    workspace = helpers.get_workspace_record(team_id, body, context, client) if team_id else None
    name = (getattr(workspace, "workspace_name", None) or "").strip() or "Workspace"
    manager = helpers.format_admin_label(client, user_id, workspace) if user_id else None
    return name, manager


def _format_data_tools_notice(*, action: str, workspace_name: str, manager: str | None, details: list[str]) -> str:
    return helpers.format_action_notice(
        action=action,
        workspace_name=workspace_name,
        manager=manager,
        for_managers=True,
        workspace_line=True,
        details=details,
    )


def _format_verify_lines(report: RepairReport) -> list[str]:
    lines = [f"{label}: {report.count(key)}" for key, label in REPAIR_CHECKS]
    found = sum(report.count(key) for key, _label in REPAIR_CHECKS)
    if found == 0:
        lines.append("Result: No issues found. Nothing was changed.")
    elif found == 1:
        lines.append("Result: 1 issue found. Nothing was changed. Run Repair to fix it.")
    else:
        lines.append(f"Result: {found} issues found. Nothing was changed. Run Repair to fix them.")
    return lines


def _format_repair_lines(report: RepairReport) -> list[str]:
    lines = [f"{label}: {report.count(key)}" for key, label in REPAIR_CHECKS]
    fixed = sum(report.count(key) for key, _label in REPAIR_CHECKS)
    if fixed == 0:
        lines.append("Result: No issues found. Nothing was changed.")
    elif fixed == 1:
        lines.append("Result: Fixed 1 issue.")
    else:
        lines.append(f"Result: Fixed {fixed} issues.")
    return lines


def _kept_sentence(kept: int) -> str:
    if kept == 1:
        return "1 Workspace is still on an External Connection, so it was kept."
    return f"{kept} Workspaces are still on an External Connection, so they were kept."


def _format_purge_lines(result: PurgeResult) -> list[str]:
    lines = [
        f"Workspaces Removed: {result.workspaces_purged}",
        f"Channels Removed: {result.channels_purged}",
        f"Syncs Removed: {result.syncs_purged}",
        f"Groups Disbanded: {result.groups_disbanded}",
        f"Workspaces Kept: {result.workspaces_kept}",
    ]
    removed = result.workspaces_purged + result.channels_purged + result.syncs_purged + result.groups_disbanded
    kept = result.workspaces_kept
    row_word = "row" if removed == 1 else "rows"
    removed_sentence = f"Removed {removed} {row_word}."
    if removed == 0 and kept == 0:
        lines.append("Result: Nothing to purge. Nothing was changed.")
    elif removed == 0:
        lines.append(f"Result: {_kept_sentence(kept)} Nothing was changed.")
    elif kept == 0:
        lines.append(f"Result: {removed_sentence}")
    else:
        lines.append(f"Result: {removed_sentence} {_kept_sentence(kept)}")
    return lines


def _run_option(option: str | None) -> list[str]:
    if option == _REPAIR:
        return _format_repair_lines(run_repair(fix=True))
    if option == _PURGE:
        return _format_purge_lines(purge_soft_deletes(0))
    return _format_verify_lines(run_repair(fix=False))
