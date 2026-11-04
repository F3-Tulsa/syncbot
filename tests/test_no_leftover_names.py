"""Names retired by the private Channel cleanup must not survive in the app, tests, or operator docs."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WALKED_DIRS = ("syncbot", "tests")
LEFTOVER_NAMES = (
    "_unpause_memberships_and_channels",
    "get_live_sync_channel",
    "channel_has_membership",
    "notify_sibling_sync_channels",
    "heal_sync_channels_sibling_failed",
    "_private_resume_block",
    "channel_info_flags",
    "_available_channel_label",
    "_is_last_publisher",
    "_toggle_sync_status",
    "sync_row_button_mode",
    "private_owner_denial",
    "get_private_sync_denial",
    "claim_unowned_private",
    "_show_local_private_row",
    "_private_channel_action_block",
    "_refresh_group_member_homes",
    "_refresh_homes",
    "_NO_PUBLISHER",
    "_replicate_status",
    "_row_action_buttons",
    "_reason_clause",
    "_looks_private",
    "_dm_member_ids",
    "helpers_bot_token",
    "end_rows_for_user",
    "end_synced_by_assignment",
    "end_private_syncs_for_settings",
    "drop_ended_private_syncs",
    "manager_still_authorized",
    "format_manager_left_dm",
    "_post_channel",
    "apply_workspace_channel_state",
    "ARCHIVE_NOTICE",
    "UNARCHIVE_NOTICE",
    "handle_group_left",
    "handle_group_deleted",
    "handle_channel_archive",
    "handle_channel_unarchive",
    "handle_group_archive",
    "_leave_unconfigured_channel",
    "_parse_optional_dt",
    "leave_workspace_sync_channel",
    "synced_by_ended_at",
    "lookup_channel_meta",
    "failed_to_notify_channel_about",
    "synced_by_ended",
    "channel_archived",
    "channel_unarchived",
    "channel_became_private",
    "failed_to_leave_channel",
    "failed_to_notify_and_leave_unconfigured_channel",
    "failed_to_notify_and_leave_untracked_channel",
    "unhandled_message_subtype",
    "validate_channel_selection_conversations_info_failed_for",
    "looks_private_conversations_info_failed_for",
    "resume_restored_sync_channels_replicate_failed",
    "federation_replicate_sync_edit_failed",
    "left_not_owner",
    "left_no_manager",
    "has_live_sync_channel",
    "get_live_sync_channels",
    "leave_unconfigured_channel",
    "leave_unconfigured_channel_failed",
    "heal_sync_channel_from_info",
    "info_is_private",
    "apply_channel_state",
    "store_channel_flags",
    "drop_deleted_slack_channel",
    "stamp_synced_by",
    "stamp_private_channel",
    "denial_kind",
    "format_twin_notice",
    "notify_twin_sync_channels",
    "notify_twin_sync_channels_failed",
    "notify_twins_after_workspace_soft_delete",
    "_tell_twins_workspace_removed",
    "_resume_fallback",
    "_left_sentence",
    "format_soft_delete_dm",
    "twin_detail",
    "dm_in_channel_managers",
    "channel_member_ids",
    "_channel_sync_lines",
    "_dm_channel_managers",
    "_create_sync_notice",
    "_join_notice",
    "_flow_sentence",
    "_format_other_channel_ref",
    "_restore_sentence",
    "_member_status_word",
    "_format_reason_clause",
    "_format_soft_delete_notice",
    "_REJOIN_REASONS",
    "_named_person",
    "_cap",
    "_syncs_with_groups",
    "_channel_name",
    "format_sync_action_notice",
    "format_current_members",
    "removal_notice",
    "NO_PUBLISHER_NOTE",
    "has_active_publisher",
    "_format_available_channel_label",
    "_DELETED_HISTORY",
    "group_called",
    "is_last_active_publisher",
    "_admin_workspace_label",
    "user_has_private_channel",
    "_sync_channel_by_pk",
    "_workspace_by_pk",
    "handle_data_migration_request",
    "handle_pairing_request_approve",
    "handle_pairing_request_decline",
    "fulfill_pairing_request",
    "_dm_migration_requester",
    "_request_id_from_action",
    "_ensure_migration_connection",
    "_primary_workspace_code_tick",
    "include_source_instance",
    "data_migration_request",
    "pairing_request_approve",
    "pairing_request_decline",
    "Export and Request Connection",
    "Approve and Create",
)

# A whole identifier: no identifier character on either side, so
# ``_refresh_homes_after_status_change`` does not match a shorter name.
_LEFTOVER = re.compile(
    r"(?<![A-Za-z0-9_])(?:" + "|".join(re.escape(name) for name in LEFTOVER_NAMES) + r")(?![A-Za-z0-9_])"
)


def _walked_files():
    this_file = Path(__file__).resolve()
    for folder in WALKED_DIRS:
        for path in sorted((ROOT / folder).rglob("*.py")):
            if path.resolve() != this_file:
                yield path
    for path in sorted((ROOT / "docs").rglob("*.md")):
        yield path
    for name in ("AGENTS.md", "README.md"):
        yield ROOT / name


def _hits(path: Path) -> list[str]:
    hits = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        for match in _LEFTOVER.finditer(line):
            hits.append(f"{path.relative_to(ROOT)}:{number}: {match.group(0)}")
    return hits


def test_pattern_matches_whole_words_only():
    assert _LEFTOVER.search("x = get_live_sync_channel(1)")
    assert _LEFTOVER.search("x = get_live_sync_channels(1)")
    assert not _LEFTOVER.search("x = get_sync_channels(1)")
    assert not _LEFTOVER.search("_refresh_homes_after_status_change()")
    assert _LEFTOVER.search("NO_PUBLISHER_NOTE")
    assert not _LEFTOVER.search("format_action_notice")
    assert _LEFTOVER.search("leave_unconfigured_channel(client, channel_id)")
    assert not _LEFTOVER.search("leave_channel_without_sync(client, channel_id)")
    assert _LEFTOVER.search('patch("helpers.leave_sync._refresh_homes")')


def test_no_leftover_names_in_source_tests_or_docs():
    hits = [hit for path in _walked_files() for hit in _hits(path)]
    assert not hits, "leftover names:\n" + "\n".join(hits)
