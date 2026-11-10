"""Data Tools modal for the primary Workspace."""

import time
from logging import Logger

from slack_sdk.web import WebClient

import helpers
from db.run_lock import DATA_MAINTENANCE, DATA_MAINTENANCE_TTL, hold_lock, is_lock_held
from handlers._common import (
    _MAINTENANCE_BUSY_TEXT,
    _dm_user,
    _get_selected_option_value,
    _is_ack_replay,
    _wait_modal_ack,
)
from helpers.data_repair import REPAIR_CHECKS, RepairReport, run_repair
from helpers.notifications import PurgeResult, purge_soft_deletes
from logger import log_error, log_info, log_warning
from slack import actions, orm

_OPTIONS = (
    ("verify", "Verify", "Check database consistency."),
    ("repair", "Repair", "Fix database issues."),
    ("purge", "Purge Soft-Deleted Data", "Remove soft-deleted data immediately."),
)
_VERIFY, _REPAIR, _PURGE = (row[0] for row in _OPTIONS)
_FAILURE_LINE = "Result: Something went wrong. Nothing was changed. Check the logs."
_PURGE_FAILURE_LINE = "Result: Something went wrong. Some data may already be removed. Check the logs."
_RUNNING_LINE = "Result: A maintenance action is already in progress. Nothing was changed. Try again in a few minutes."


def _allowed(client: WebClient, body: dict) -> bool:
    user_id = helpers.get_user_id_from_body(body)
    team_id = helpers.get_team_id_from_body(body)
    if not user_id or not team_id or not helpers.is_workspace_admin(client, user_id):
        return False
    return helpers.is_backup_visible_for_workspace(team_id)


def _failure_line(option: str | None) -> str:
    """Verify only reads and Repair rolls back. Purge commits as it goes."""
    return _PURGE_FAILURE_LINE if option == _PURGE else _FAILURE_LINE


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
    so the work phase takes and releases it. A failed read lets the work phase
    decide.

    The replay never refuses: Slack already has the first answer, and an error
    here would skip the work, so a submission the first ack accepted would get
    no report. The work phase finds a held lock itself and DMs that.
    """
    if not _allowed(client, body):
        return None
    if not _is_ack_replay(context) and _lock_held():
        log_info(
            "data_tools_busy",
            phase="ack",
            user_id=helpers.get_user_id_from_body(body),
            option=_get_selected_option_value(body, actions.CONFIG_DATA_TOOLS_OPTION),
        )
        return {
            "response_action": "errors",
            "errors": {actions.CONFIG_DATA_TOOLS_OPTION: _MAINTENANCE_BUSY_TEXT},
        }
    label = _option_label(_get_selected_option_value(body, actions.CONFIG_DATA_TOOLS_OPTION))
    return _wait_modal_ack(
        title="Data Tools",
        callback_id=actions.CONFIG_DATA_TOOLS_SUBMIT,
        dm=True,
        text=f"{label} is running. Close this and SyncBot will DM a report.",
    )


def _lock_held() -> bool:
    try:
        return is_lock_held(DATA_MAINTENANCE, DATA_MAINTENANCE_TTL)
    except Exception:
        log_warning("data_tools_lock_read_failed", exc_info=True)
        return False


def handle_data_tools_submit_work(body: dict, client: WebClient, logger: Logger, context: dict) -> None:
    """Run the chosen tool, then DM the report. A run that finds another one going reports that."""
    user_id = helpers.get_user_id_from_body(body)
    team_id = helpers.get_team_id_from_body(body)
    option = _get_selected_option_value(body, actions.CONFIG_DATA_TOOLS_OPTION)
    label = _option_label(option)
    workspace_name = "Workspace"
    manager = None
    started = time.monotonic()
    ran = False
    try:
        if not _allowed(client, body):
            log_warning("data_tools_refused", user_id=user_id, team_id=team_id, option=option)
            return
        workspace_name, manager = _notice_identity(client, user_id, body, context)
        with hold_lock(DATA_MAINTENANCE, DATA_MAINTENANCE_TTL) as token:
            if token is None:
                log_info("data_tools_busy", phase="work", user_id=user_id, option=option)
                details = [_RUNNING_LINE]
            else:
                log_info("data_tools_started", user_id=user_id, team_id=team_id, option=option)
                ran = True
                details = _run_option(option)
                log_info(
                    "data_tools_finished",
                    user_id=user_id,
                    option=option,
                    result=details[-1],
                    duration_ms=_elapsed_ms(started),
                )
    except Exception:
        log_error(
            "data_tools_failed",
            user_id=user_id,
            option=option,
            duration_ms=_elapsed_ms(started),
            exc_info=True,
        )
        details = [_failure_line(option) if ran else _FAILURE_LINE]
    _dm_user(
        client,
        user_id,
        _format_data_tools_notice(action=label, workspace_name=workspace_name, manager=manager, details=details),
    )


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


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


def _failed_sentence(failed: int) -> str:
    if failed == 1:
        return "1 Workspace could not be removed. Check the logs."
    return f"{failed} Workspaces could not be removed. Check the logs."


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
    failed = result.workspaces_failed
    if failed:
        lines.append(f"Workspaces Failed: {failed}")
    row_word = "row" if removed == 1 else "rows"
    removed_sentence = f"Removed {removed} {row_word}."
    if removed == 0 and kept == 0 and failed == 0:
        lines.append("Result: Nothing to purge. Nothing was changed.")
        return lines
    sentences = [removed_sentence] if removed else []
    if kept:
        sentences.append(_kept_sentence(kept))
    if failed:
        sentences.append(_failed_sentence(failed))
    elif removed == 0:
        sentences.append("Nothing was changed.")
    lines.append("Result: " + " ".join(sentences))
    return lines


def _run_option(option: str | None) -> list[str]:
    if option == _REPAIR:
        return _format_repair_lines(run_repair(fix=True))
    if option == _PURGE:
        return _format_purge_lines(purge_soft_deletes(0))
    return _format_verify_lines(run_repair(fix=False))
