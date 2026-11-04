"""Workspace record management and name resolution."""

import os
from datetime import UTC, datetime

from slack_sdk import WebClient

import constants
from db import DbManager, schemas
from helpers._cache import _cache_get, _cache_set
from helpers.core import safe_get
from helpers.encryption import decrypt_bot_token
from logger import log_debug, log_error, log_info, log_warning


def get_bot_token(workspace: schemas.Workspace | None) -> str | None:
    """Return the bot token for a live workspace on this instance, or None.

    Live vs stub is ``instance_id``, not token presence. Looks up Bolt
    ``slack_bots`` first, then local ``SLACK_BOT_TOKEN`` when OAuth is off.
    """
    if workspace is None or getattr(workspace, "deleted_at", None) is not None:
        return None
    try:
        from federation.core import get_instance_id

        if getattr(workspace, "instance_id", None) != get_instance_id():
            return None
    except Exception:
        return None

    team_id = getattr(workspace, "team_id", None)
    client_id = os.environ.get(constants.SLACK_CLIENT_ID, "").strip()
    if team_id and client_id:
        try:
            from db import get_engine
            from helpers.encryption_installation_store import EncryptedSQLAlchemyInstallationStore

            store = EncryptedSQLAlchemyInstallationStore(client_id=client_id, engine=get_engine())
            bot = store.find_bot(enterprise_id=None, team_id=team_id)
            token = getattr(bot, "bot_token", None) if bot else None
            if token:
                return token
        except Exception:
            log_debug("get_bot_token", reason="installation store lookup failed", exc_info=True)

    if constants.LOCAL_DEVELOPMENT and constants.HAS_REAL_BOT_TOKEN:
        env_token = os.environ.get(constants.SLACK_BOT_TOKEN, "").strip()
        if env_token:
            return env_token

    raw = getattr(workspace, "bot_token", None)
    if raw:
        try:
            return decrypt_bot_token(raw)
        except Exception:
            return None
    miss_key = f"bot_token_missing:{team_id}"
    if team_id and not _cache_get(miss_key):
        _cache_set(miss_key, True)
        log_debug(
            "bot_token_missing",
            team_id=team_id,
            workspace_id=getattr(workspace, "id", None),
        )
    return None


def invalidate_fed_ws_for_sync_cache() -> None:
    """Drop leftover ``fed_ws_for_sync:`` cache keys after pair, unpair, or restore.

    Fan-out no longer uses ``get_federated_workspace_for_sync``; routing goes
    through stub ``Workspace`` rows (``helpers.workspace_kind``). This remains a
    prefix delete for safety on warm containers that still hold old keys.
    """
    from helpers._cache import _cache_delete_prefix

    _cache_delete_prefix("fed_ws_for_sync:")


def replace_federation_allowlist(instance_id: str, workspace_ids: list[int]) -> None:
    """Replace the local workspaces allowed for *instance_id*."""
    instance_id = (instance_id or "").strip()
    if not instance_id:
        return
    ids = sorted({int(wid) for wid in workspace_ids if wid})
    DbManager.delete_records(
        schemas.FederationWorkspaceAllowlist,
        [schemas.FederationWorkspaceAllowlist.instance_id == instance_id],
    )
    for workspace_id in ids:
        DbManager.create_record(
            schemas.FederationWorkspaceAllowlist(
                instance_id=instance_id,
                workspace_id=workspace_id,
            )
        )


def get_allowed_local_workspace_ids(instance_id: str) -> set[int]:
    """Local workspace IDs allowed on *instance_id* (allowlist, else shared groups)."""
    from helpers.workspace_kind import is_local_workspace

    instance_id = (instance_id or "").strip()
    if not instance_id:
        return set()
    allowlist = DbManager.find_records(
        schemas.FederationWorkspaceAllowlist,
        [schemas.FederationWorkspaceAllowlist.instance_id == instance_id],
    )
    if allowlist:
        return {row.workspace_id for row in allowlist if row.workspace_id}

    stubs = DbManager.find_records(
        schemas.Workspace,
        [
            schemas.Workspace.instance_id == instance_id,
            schemas.Workspace.deleted_at.is_(None),
        ],
    )
    stub_ids = {ws.id for ws in stubs}
    if not stub_ids:
        return set()
    stub_members = DbManager.find_records(
        schemas.WorkspaceGroupMember,
        [
            schemas.WorkspaceGroupMember.workspace_id.in_(list(stub_ids)),
            schemas.WorkspaceGroupMember.status == "active",
            schemas.WorkspaceGroupMember.deleted_at.is_(None),
        ],
    )
    group_ids = {m.group_id for m in stub_members}
    ws_ids: set[int] = set()
    for group_id in group_ids:
        group_members = DbManager.find_records(
            schemas.WorkspaceGroupMember,
            [
                schemas.WorkspaceGroupMember.group_id == group_id,
                schemas.WorkspaceGroupMember.workspace_id.isnot(None),
                schemas.WorkspaceGroupMember.status == "active",
                schemas.WorkspaceGroupMember.deleted_at.is_(None),
            ],
        )
        for member in group_members:
            ws = get_workspace_by_id(member.workspace_id)
            if ws and is_local_workspace(ws):
                ws_ids.add(member.workspace_id)
    return ws_ids


# Routing uses stub workspaces (``Workspace.instance_id``) via
# ``helpers.workspace_kind.peer_for_workspace`` — there is no
# ``get_federated_workspace_for_sync`` anymore.


def get_workspace_record(team_id: str, body: dict, context: dict, client: WebClient) -> schemas.Workspace | None:
    """Fetch or create the Workspace record for a Slack workspace."""
    from federation.core import get_instance_id
    from helpers.settings import team_id_is_blocked

    workspace_record: schemas.Workspace | None = DbManager.get_record(schemas.Workspace, id=team_id)
    if team_id_is_blocked(team_id):
        log_info("workspace_blocked", team_id=team_id)
        if workspace_record and workspace_record.deleted_at is None and not _is_stub_workspace(workspace_record):
            slack_apps_uninstall(get_bot_token(workspace_record))
            uninstall_workspace(team_id)
            return DbManager.get_record(schemas.Workspace, id=team_id)
        return workspace_record

    team_domain = safe_get(body, "team", "domain")
    announce_live = False

    if not workspace_record:
        try:
            team_info = client.team_info()
            ws_name = team_info["team"]["name"]
        except Exception as exc:
            log_debug("get_workspace", error=str(exc))
            ws_name = team_domain
        workspace_record: schemas.Workspace = DbManager.create_record(
            schemas.Workspace(
                team_id=team_id,
                workspace_name=ws_name,
                instance_id=get_instance_id(),
            )
        )
        announce_live = True
    elif workspace_record.deleted_at is not None or _is_stub_workspace(workspace_record):
        workspace_record = _restore_workspace(workspace_record, context, client)
        announce_live = True
    else:
        _maybe_refresh_bot_token(workspace_record, context)
        _maybe_refresh_workspace_name(workspace_record, client)

    if announce_live:
        _push_live_team_to_trusted_peers(team_id)
        from helpers.notifications import forget_migration_notices, notify_primary_action

        forget_migration_notices(team_id)
        notify_primary_action(workspace_record, "Install")

    return workspace_record


def _push_live_team_to_trusted_peers(team_id: str) -> None:
    """Best-effort push of the current allowlist to trusted peers."""
    from federation.core import push_allowed_workspaces

    peers = DbManager.find_records(
        schemas.Instance,
        [
            schemas.Instance.private_key_encrypted.is_(None),
            schemas.Instance.status == "active",
            schemas.Instance.trust_status == "trusted",
        ],
    )
    for peer in peers:
        try:
            result = push_allowed_workspaces(peer)
            ok = bool(result and result.get("ok"))
            emit = log_warning if not ok else log_debug
            emit(
                "push_teams",
                team_id=team_id,
                peer_instance_id=peer.instance_id,
                ok=ok,
                convert=result.get("results") if isinstance(result, dict) else None,
            )
        except Exception:
            log_error(
                "push_teams",
                team_id=team_id,
                peer_instance_id=peer.instance_id,
                ok=False,
            )
            log_error(
                "workspace_team_announcement_failed", team_id=team_id, peer_instance_id=peer.instance_id, exc_info=True
            )


def _maybe_refresh_bot_token(workspace_record: schemas.Workspace, context: dict) -> None:
    """Update Bolt ``slack_bots`` when the request context has a newer bot token."""
    new_token = safe_get(context, "bot_token")
    if not new_token or not workspace_record.team_id:
        return

    stored = get_bot_token(workspace_record)
    if stored == new_token:
        return

    client_id = os.environ.get(constants.SLACK_CLIENT_ID, "").strip()
    if not client_id:
        return
    try:
        from datetime import UTC, datetime

        from slack_sdk.oauth.installation_store import Bot

        from db import get_engine
        from helpers.encryption_installation_store import EncryptedSQLAlchemyInstallationStore

        store = EncryptedSQLAlchemyInstallationStore(client_id=client_id, engine=get_engine())
        existing = store.find_bot(enterprise_id=None, team_id=workspace_record.team_id)
        installed_at = getattr(existing, "installed_at", None) if existing else None
        if installed_at is None:
            installed_at = datetime.now(UTC)
        bot = Bot(
            app_id=getattr(existing, "app_id", None) if existing else None,
            enterprise_id=getattr(existing, "enterprise_id", None) if existing else None,
            team_id=workspace_record.team_id,
            bot_token=new_token,
            bot_id=getattr(existing, "bot_id", None) if existing else None,
            bot_user_id=getattr(existing, "bot_user_id", None) if existing else None,
            bot_scopes=getattr(existing, "bot_scopes", None) if existing else None,
            bot_refresh_token=getattr(existing, "bot_refresh_token", None) if existing else None,
            bot_token_expires_at=getattr(existing, "bot_token_expires_at", None) if existing else None,
            is_enterprise_install=getattr(existing, "is_enterprise_install", False) if existing else False,
            installed_at=installed_at,
        )
        store.save_bot(bot)
        log_info("bot_token_refreshed", workspace_id=workspace_record.id, team_id=workspace_record.team_id)
    except Exception:
        log_debug("bot_token_refresh_skipped", exc_info=True)


def _maybe_refresh_workspace_name(workspace_record: schemas.Workspace, client: WebClient) -> None:
    """Refresh the stored workspace name from the Slack API (at most once per day)."""
    cache_key = f"ws_name_refresh:{workspace_record.id}"
    if _cache_get(cache_key):
        return

    _cache_set(cache_key, True, ttl=86400)

    try:
        team_info = client.team_info()
        current_name = team_info["team"]["name"]
    except Exception as exc:
        log_debug("maybe_refresh_workspace_name_team_info_call_failed", error=str(exc))
        return

    if current_name and current_name != workspace_record.workspace_name:
        DbManager.update_records(
            schemas.Workspace,
            [schemas.Workspace.id == workspace_record.id],
            {schemas.Workspace.workspace_name: current_name},
        )
        workspace_record.workspace_name = current_name
        log_info("workspace_name_refreshed", workspace_id=workspace_record.id, new_name=current_name)


def _is_stub_workspace(workspace_record: schemas.Workspace) -> bool:
    from helpers.workspace_kind import is_stub_workspace

    return is_stub_workspace(workspace_record)


def soft_delete_workspace(workspace_record: schemas.Workspace) -> list:
    """Soft-delete a workspace: ``deleted_at`` on the row and its memberships; its rows become ``uninstalled``.

    Does not change ``instance_id`` (live vs stub). No-op if already deleted.
    Channel rows are written first so a crash cannot leave live rows under a deleted workspace.
    """
    if workspace_record is None or getattr(workspace_record, "id", None) is None:
        return []
    if getattr(workspace_record, "deleted_at", None) is not None:
        return []

    now = datetime.now(UTC)
    my_channels = DbManager.find_records(
        schemas.SyncChannel,
        [
            schemas.SyncChannel.workspace_id == workspace_record.id,
            schemas.SyncChannel.deleted_at.is_(None),
        ],
    )
    for sync_channel in my_channels:
        DbManager.update_records(
            schemas.SyncChannel,
            [schemas.SyncChannel.id == sync_channel.id],
            {schemas.SyncChannel.deleted_at: now, schemas.SyncChannel.status: "uninstalled"},
        )
        sync_channel.status = "uninstalled"
        sync_channel.deleted_at = now

    active_memberships = DbManager.find_records(
        schemas.WorkspaceGroupMember,
        [
            schemas.WorkspaceGroupMember.workspace_id == workspace_record.id,
            schemas.WorkspaceGroupMember.status == "active",
            schemas.WorkspaceGroupMember.deleted_at.is_(None),
        ],
    )
    for membership in active_memberships:
        DbManager.update_records(
            schemas.WorkspaceGroupMember,
            [schemas.WorkspaceGroupMember.id == membership.id],
            {schemas.WorkspaceGroupMember.deleted_at: now},
        )

    DbManager.update_records(
        schemas.Workspace,
        [schemas.Workspace.id == workspace_record.id],
        {schemas.Workspace.deleted_at: now},
    )
    workspace_record.deleted_at = now

    from helpers.sync_participation import invalidate_sync_fanout_for_syncs

    invalidate_sync_fanout_for_syncs(c.sync_id for c in my_channels)
    log_info(
        "workspace_soft_deleted",
        workspace_id=workspace_record.id,
        team_id=getattr(workspace_record, "team_id", None),
        memberships_soft_deleted=len(active_memberships),
        channels_uninstalled=len(my_channels),
    )
    return my_channels


def _set_restored_channel_status(
    sync_channel: schemas.SyncChannel,
    status: str,
    workspace: schemas.Workspace,
    *,
    notice: str | None = None,
) -> None:
    """Write SyncChannel status, drop fan-out cache, and replicate when possible."""
    DbManager.update_records(
        schemas.SyncChannel,
        [schemas.SyncChannel.id == sync_channel.id],
        {schemas.SyncChannel.status: status, schemas.SyncChannel.deleted_at: None},
    )
    sync_channel.status = status
    sync_channel.deleted_at = None
    from helpers.sync_lifecycle import announce_sync_channels, replicate_sync_channel_status
    from helpers.sync_participation import invalidate_sync_fanout_for_syncs

    invalidate_sync_fanout_for_syncs([sync_channel.sync_id])
    if notice:
        announce_sync_channels(workspace, sync_channel, notice)
    else:
        replicate_sync_channel_status(sync_channel, workspace)


def resume_restored_sync_channels(
    workspace: schemas.Workspace,
    *,
    client: WebClient,
    acting_user_id: str | None = None,
    context: dict | None = None,
    source: str | None = None,
    rows: list | None = None,
) -> dict[str, int]:
    """Rejoin public Sync Channels after reinstall or import; leave private paused.

    Public Channels are paused with a notice, the bot joins, then the Sync
    resumes with a notice (and a join notice when the bot was not already a
    member). Private Channels stay paused, with a pause notice in that Channel
    when the bot can post, and on the twin Channels otherwise.
    """
    from helpers.conversations import ConversationAccessError, ensure_bot_in_conversation, inspect_bot_channel_access
    from helpers.leave_sync import group_name_for_sync_id
    from helpers.workspace_kind import is_local_workspace

    counts = {"resumed": 0, "joined": 0, "paused_private": 0, "skipped": 0}
    if workspace is None or not is_local_workspace(workspace) or not getattr(workspace, "id", None):
        return counts
    if client is None:
        log_debug("resume_restored_sync_channels", reason="missing_client", workspace_id=workspace.id, source=source)
        return counts

    if rows is None:
        channels = DbManager.find_records(
            schemas.SyncChannel,
            [
                schemas.SyncChannel.workspace_id == workspace.id,
                schemas.SyncChannel.deleted_at.is_(None),
            ],
        )
    else:
        channels = list(rows)
    if source != "migration" and peer_still_hosts_team(getattr(workspace, "team_id", None) or ""):
        for sync_channel in channels:
            if not sync_channel.channel_id or getattr(sync_channel, "status", None) == "paused":
                counts["skipped"] += 1
                continue
            _set_restored_channel_status(sync_channel, "pending", workspace)
            counts["skipped"] += 1
        log_info(
            "resume_restored_sync_channels",
            source=source,
            workspace_id=workspace.id,
            team_id=workspace.team_id,
            resumed=0,
            joined=0,
            paused_private=0,
            skipped=counts["skipped"],
            held="origin_live",
        )
        return counts
    for sync_channel in channels:
        channel_id = sync_channel.channel_id
        if not channel_id:
            continue
        is_private, is_member = inspect_bot_channel_access(client, channel_id)
        group_name = group_name_for_sync_id(sync_channel.sync_id)
        if not is_private and is_member and getattr(sync_channel, "status", None) != "pending":
            from helpers.sync_lifecycle import deliver_action_notice

            deliver_action_notice(workspace, sync_channel, action="Resume", group_name=group_name)
            counts["resumed"] += 1
            counts["skipped"] += 1
            continue

        if is_private:
            if sync_channel.status == "paused":
                counts["skipped"] += 1
                continue
            from helpers.sync_lifecycle import deliver_action_notice

            _set_restored_channel_status(sync_channel, "paused", workspace)
            deliver_action_notice(
                workspace,
                sync_channel,
                action="Pause",
                group_name=group_name,
                note="Authorize SyncBot, then Resume this Channel.",
                post_in_channel=False,
                announce=False,
            )
            counts["paused_private"] += 1
            continue

        _set_restored_channel_status(sync_channel, "paused", workspace)
        joined = False
        if not is_member:
            try:
                ensure_bot_in_conversation(
                    client,
                    channel_id,
                    team_id=workspace.team_id,
                    acting_user_id=acting_user_id,
                    context=context,
                )
                joined = True
            except ConversationAccessError as exc:
                log_warning(
                    "resume_restored_sync_channels_join_failed",
                    channel_id=channel_id,
                    workspace_id=workspace.id,
                    error=str(exc),
                )
                from helpers.sync_lifecycle import deliver_action_notice

                _set_restored_channel_status(sync_channel, "paused", workspace)
                deliver_action_notice(
                    workspace,
                    sync_channel,
                    action="Pause",
                    group_name=group_name,
                    note="SyncBot could not rejoin the Channel.",
                    post_in_channel=False,
                    announce=False,
                )
                counts["paused_private"] += 1
                continue

        from helpers.sync_lifecycle import deliver_action_notice

        _set_restored_channel_status(sync_channel, "active", workspace)
        deliver_action_notice(workspace, sync_channel, action="Resume", group_name=group_name)
        if joined:
            counts["joined"] += 1
        counts["resumed"] += 1

    log_info(
        "resume_restored_sync_channels",
        source=source,
        workspace_id=workspace.id,
        team_id=workspace.team_id,
        resumed=counts["resumed"],
        joined=counts["joined"],
        paused_private=counts["paused_private"],
        skipped=counts["skipped"],
    )
    return counts


def _restore_workspace(
    workspace_record: schemas.Workspace,
    context: dict,
    client: WebClient,
) -> schemas.Workspace:
    """Reclaim a paused or stub workspace as a live install on this instance."""
    from helpers.settings import team_id_is_blocked
    from helpers.workspace_kind import convert_workspace_to_local

    if team_id_is_blocked(workspace_record.team_id):
        log_info("workspace_restore_blocked", team_id=workspace_record.team_id)
        return workspace_record

    new_token = safe_get(context, "bot_token")
    convert_workspace_to_local(workspace_record.team_id, source="reinstall")
    if new_token:
        _maybe_refresh_bot_token(workspace_record, context)

    workspace_record = DbManager.get_record(schemas.Workspace, id=workspace_record.team_id)
    resume_restored_sync_channels(
        workspace_record,
        client=client,
        context=context,
        source="reinstall",
    )
    log_info("workspace_restored", workspace_id=workspace_record.id)
    return workspace_record


def get_workspace_by_id(workspace_id: int, context: dict | None = None) -> schemas.Workspace | None:
    """Look up a workspace by its integer primary-key ``id`` column.

    If *context* is provided, uses request-scoped cache to avoid repeated DB
    lookups for the same workspace_id within one request.
    """
    if context is not None:
        cache = context.setdefault("_workspace_by_id", {})
        if workspace_id in cache:
            return cache[workspace_id]
    rows = DbManager.find_records(schemas.Workspace, [schemas.Workspace.id == workspace_id])
    result = rows[0] if rows else None
    if context is not None:
        context.setdefault("_workspace_by_id", {})[workspace_id] = result
    return result


def get_groups_for_workspace(workspace_id: int) -> list[schemas.WorkspaceGroup]:
    """Return all active groups the workspace belongs to."""
    members = DbManager.find_records(
        schemas.WorkspaceGroupMember,
        [
            schemas.WorkspaceGroupMember.workspace_id == workspace_id,
            schemas.WorkspaceGroupMember.status == "active",
            schemas.WorkspaceGroupMember.deleted_at.is_(None),
        ],
    )
    if not members:
        return []
    group_ids = [m.group_id for m in members]
    return DbManager.find_records(
        schemas.WorkspaceGroup,
        [schemas.WorkspaceGroup.id.in_(group_ids), schemas.WorkspaceGroup.status == "active"],
    )


def get_group_members(group_id: int) -> list[schemas.WorkspaceGroupMember]:
    """Return all active members of a group."""
    return DbManager.find_records(
        schemas.WorkspaceGroupMember,
        [
            schemas.WorkspaceGroupMember.group_id == group_id,
            schemas.WorkspaceGroupMember.status == "active",
            schemas.WorkspaceGroupMember.deleted_at.is_(None),
        ],
    )


def resolve_workspace_name(workspace: schemas.Workspace) -> str:
    """Return a human-readable name for a workspace."""
    if workspace.workspace_name:
        return workspace.workspace_name

    if get_bot_token(workspace):
        try:
            ws_client = WebClient(token=get_bot_token(workspace))
            team_info = ws_client.team_info()
            name = safe_get(team_info, "team", "name")
            if name:
                DbManager.update_records(
                    schemas.Workspace,
                    [schemas.Workspace.id == workspace.id],
                    {schemas.Workspace.workspace_name: name},
                )
                workspace.workspace_name = name
                return name
        except Exception as exc:
            # Name lookup is best-effort; falling back to team_id keeps UI usable
            # even when Slack API calls fail intermittently.
            log_debug(
                "resolve_workspace_name_failed", workspace_id=workspace.id, team_id=workspace.team_id, error=str(exc)
            )

    return workspace.team_id or f"Workspace {workspace.id}"


def resolve_channel_name(channel_id: str, workspace=None) -> str:
    """Resolve a channel ID to a human-readable name."""
    if not channel_id:
        return channel_id

    cache_key = f"chan_name:{channel_id}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    ch_name = lookup_channel_name(channel_id, workspace)
    ws_name = getattr(workspace, "workspace_name", None) if workspace else None

    if ws_name:
        result = f"#{ch_name} ({ws_name})"
    else:
        result = f"#{ch_name}"

    if ch_name != channel_id:
        _cache_set(cache_key, result, ttl=3600)
    return result


def stored_channel_name(channel_id: str, workspace_id: int | None) -> str | None:
    """Display name saved on a SyncChannel for *channel_id* in *workspace_id*."""
    if not channel_id or not workspace_id:
        return None
    rows = DbManager.find_records(
        schemas.SyncChannel,
        [
            schemas.SyncChannel.channel_id == channel_id,
            schemas.SyncChannel.workspace_id == workspace_id,
            schemas.SyncChannel.deleted_at.is_(None),
        ],
    )
    for row in rows:
        name = (getattr(row, "channel_name", None) or "").strip()
        if name and name != channel_id:
            return name[:100]
    return None


def remember_channel_name(channel_id: str, workspace_id: int | None, name: str | None) -> None:
    """Persist a Slack Channel display name on matching SyncChannel rows."""
    if not channel_id or not workspace_id:
        return
    cleaned = (name or "").strip().removeprefix("#")[:100]
    if not cleaned or cleaned == channel_id:
        return
    DbManager.update_records(
        schemas.SyncChannel,
        [
            schemas.SyncChannel.channel_id == channel_id,
            schemas.SyncChannel.workspace_id == workspace_id,
        ],
        {schemas.SyncChannel.channel_name: cleaned},
    )


def lookup_channel_name(
    channel_id: str,
    workspace=None,
    *,
    user_token: str | None = None,
    client: WebClient | None = None,
) -> str:
    """Return the Slack Channel name. Privacy lives on the SyncChannel row.

    Tries *client* (the request bot), then the workspace bot token, then
    *user_token*. The bot cannot see a private Channel it has not joined yet,
    which is why publish used to store the Channel ID as ``sync.title``.
    Federated stubs have no token: fall back to ``sync_channels.channel_name``.
    Never log *user_token*.
    """
    if not channel_id:
        return channel_id

    from helpers._cache import request_scope_get, request_scope_set

    req_key = f"chan_label:{channel_id}"
    cached_req = request_scope_get(req_key)
    if isinstance(cached_req, str):
        return cached_req

    cache_key = f"chan_label:{channel_id}"
    cached = _cache_get(cache_key)
    if isinstance(cached, str):
        request_scope_set(req_key, cached)
        return cached

    clients: list[WebClient] = []
    if client is not None:
        clients.append(client)
    bot_token = get_bot_token(workspace) if workspace is not None else None
    if bot_token:
        try:
            clients.append(WebClient(token=bot_token))
        except Exception as exc:
            log_debug("lookup_channel_name", channel_id=channel_id, error=str(exc))
    if user_token:
        clients.append(WebClient(token=user_token))

    name = channel_id
    for slack_client in clients:
        try:
            info = slack_client.conversations_info(channel=channel_id)
            channel = safe_get(info, "channel") or {}
            found = channel.get("name")
            if isinstance(found, str) and found:
                name = found
                break
        except Exception as exc:
            log_debug("lookup_channel_name", channel_id=channel_id, error=str(exc))

    workspace_id = getattr(workspace, "id", None)
    if name == channel_id:
        stored = stored_channel_name(channel_id, workspace_id)
        if stored:
            name = stored

    request_scope_set(req_key, name)
    if name != channel_id:
        _cache_set(cache_key, name, ttl=3600)
    return name


def slack_apps_uninstall(token: str | None) -> None:
    """Remove this app from a Workspace using Slack ``apps.uninstall``."""
    if not token:
        return
    client_id = os.environ.get(constants.SLACK_CLIENT_ID, "").strip()
    client_secret = os.environ.get(constants.SLACK_CLIENT_SECRET, "").strip()
    if not client_id or not client_secret:
        return
    try:
        WebClient(token=token).apps_uninstall(client_id=client_id, client_secret=client_secret)
    except Exception as exc:
        log_warning("apps_uninstall_failed", error=str(exc))


def restore_migrated_workspace(team_id: str, *, context: dict | None = None) -> dict[str, int]:
    """Resume this workspace's ``pending`` rows after the origin has released it.

    A no-op when the workspace is not local here or it has no ``pending`` rows.
    Messages from the gap are not replayed.
    """
    from helpers.workspace_kind import is_local_workspace

    workspace = DbManager.get_record(schemas.Workspace, id=team_id)
    if workspace is None or not is_local_workspace(workspace):
        return {"resumed": 0, "joined": 0, "paused_private": 0, "skipped": 0}
    pending = DbManager.find_records(
        schemas.SyncChannel,
        [
            schemas.SyncChannel.workspace_id == workspace.id,
            schemas.SyncChannel.deleted_at.is_(None),
            schemas.SyncChannel.status == "pending",
        ],
    )
    if not pending:
        return {"resumed": 0, "joined": 0, "paused_private": 0, "skipped": 0}
    token = get_bot_token(workspace)
    if not token:
        log_warning("restore_migrated_workspace", reason="no_bot_token", team_id=team_id)
        return {"resumed": 0, "joined": 0, "paused_private": 0, "skipped": 0}
    from helpers.notifications import notify_migration

    notify_migration(team_id, "local")
    return resume_restored_sync_channels(
        workspace,
        client=WebClient(token=token),
        context=context,
        source="migration",
        rows=pending,
    )


def migration_peer_hold(team_id: str) -> tuple[str, schemas.Instance | None]:
    """``(hold, peer)`` before pending Channels resume.

    ``hold`` is ``no_peer``, ``released``, ``still_hosted``, or ``unreachable``.
    ``peer`` is the instances row behind ``still_hosted`` or ``unreachable``.
    A missing team, a missing Workspace, or a Workspace that is not local is
    ``released`` and does not call ``/teams``. No trusted peer is ``no_peer``.
    The push is today's allowlist payload with this team appended when it is
    missing, which is what makes the other install remember a pending stub.
    """
    from federation.core import push_teams, teams_allowlist_payload
    from helpers.workspace_kind import is_local_workspace, peer_is_trusted

    team_id = (team_id or "").strip()
    if not team_id:
        return "released", None
    workspace = DbManager.get_record(schemas.Workspace, id=team_id)
    if workspace is None or not is_local_workspace(workspace):
        return "released", None
    peers = DbManager.find_records(
        schemas.Instance,
        [
            schemas.Instance.private_key_encrypted.is_(None),
            schemas.Instance.status == "active",
        ],
    )
    peers = [peer for peer in peers if peer_is_trusted(peer)]
    if not peers:
        return "no_peer", None
    unreachable: schemas.Instance | None = None
    for peer in peers:
        payload = teams_allowlist_payload(peer.instance_id)
        listed = payload.setdefault("workspaces", [])
        if not any(item.get("team_id") == team_id for item in listed):
            listed.append({"team_id": team_id, "name": workspace.workspace_name or team_id})
        result = push_teams(peer, payload)
        if not result:
            if unreachable is None:
                unreachable = peer
            continue
        answer = (result.get("results") or {}).get(team_id)
        if answer in ("refused_live", None):
            return "still_hosted", peer
    if unreachable is not None:
        return "unreachable", unreachable
    return "released", None


def peer_still_hosts_team(team_id: str) -> bool:
    """True when a trusted peer still has *team_id* live, or cannot be asked.

    No trusted peer means this instance is the only one, so the team is not held.
    """
    hold, _peer = migration_peer_hold(team_id)
    return hold in ("still_hosted", "unreachable")


def consider_migration_release(team_id: str) -> tuple[str, schemas.Instance | None]:
    """Hold or resume pending Channels after import.

    Returns ``(result, peer)``. ``resumed`` restores pending Channels.
    ``waiting_for_uninstall`` and ``unreachable`` leave them pending.
    A missing team, a missing Workspace, or a Workspace that is not local
    returns ``resumed`` and does not restore or call ``/teams``.
    """
    from helpers.workspace_kind import is_local_workspace

    team_id = (team_id or "").strip()
    if not team_id:
        return "resumed", None
    workspace = DbManager.get_record(schemas.Workspace, id=team_id)
    if workspace is None or not is_local_workspace(workspace):
        return "resumed", None
    hold, peer = migration_peer_hold(team_id)
    if hold == "still_hosted":
        return "waiting_for_uninstall", peer
    if hold == "unreachable":
        return "unreachable", peer
    restore_migrated_workspace(team_id)
    return "resumed", None


def apply_teams_release_results(results: dict | None) -> None:
    """Restore a local workspace when a peer reports it is no longer live there."""
    if not isinstance(results, dict):
        return
    for team_id, answer in results.items():
        if answer in ("already_stub", "converted"):
            restore_migrated_workspace(str(team_id))


def _push_workspace_release(workspace: schemas.Workspace, peer: schemas.Instance) -> None:
    """Tell *peer* this workspace uninstalled, with each group role, so it can restore."""
    from federation.core import push_workspace_release

    memberships = DbManager.find_records(
        schemas.WorkspaceGroupMember,
        [schemas.WorkspaceGroupMember.workspace_id == workspace.id],
    )
    groups = []
    seen: set[str] = set()
    for membership in memberships:
        group = DbManager.get_record(schemas.WorkspaceGroup, id=membership.group_id)
        uid = getattr(group, "uid", None) if group else None
        if not uid or uid in seen:
            continue
        seen.add(uid)
        role = "owner" if membership.role == "creator" else membership.role
        if role not in ("owner", "member"):
            role = "member"
        groups.append({"uid": uid, "role": role})
    try:
        push_workspace_release(peer, {"team_id": workspace.team_id, "groups": groups})
    except Exception as exc:
        log_warning("workspace_release_push_failed", team_id=workspace.team_id, error=str(exc))


def uninstall_workspace(team_id: str) -> None:
    """Wipe Bolt install rows for this team, then pause SyncBot workspace data."""
    from helpers.conversations import clear_workspace_installations

    clear_workspace_installations(team_id)
    _soft_delete_uninstalled_workspace(team_id)


def _soft_delete_uninstalled_workspace(team_id: str) -> None:
    """Soft-delete the workspace after Slack uninstalled the app."""
    from helpers.notifications import claim_once
    from helpers.sync_lifecycle import deliver_action_notice

    workspace_record = DbManager.get_record(schemas.Workspace, team_id)
    if not workspace_record:
        log_warning("uninstall_workspace_unknown", team_id=team_id)
        return
    if not claim_once(f"uninstall_notice:{team_id}"):
        return
    if workspace_record.deleted_at is not None:
        return

    my_channels = soft_delete_workspace(workspace_record)

    pending_stubs = DbManager.find_records(
        schemas.FederationPendingStub,
        [schemas.FederationPendingStub.workspace_id == workspace_record.id],
    )
    for pending in pending_stubs:
        peer = DbManager.get_record(schemas.Instance, id=pending.instance_id)
        if not peer or peer.status != "active" or peer.trust_status != "trusted":
            continue
        from helpers.workspace_kind import convert_workspace_to_stub, is_self_instance

        if is_self_instance(peer):
            continue

        if convert_workspace_to_stub(team_id, peer.instance_id, source="uninstall") == "converted":
            from helpers.sync_participation import invalidate_sync_fanout_for_syncs

            invalidate_sync_fanout_for_syncs(c.sync_id for c in my_channels)
            _push_workspace_release(workspace_record, peer)
            break

    from helpers.leave_sync import group_name_for_sync_id

    seen: set[int] = set()
    for row in my_channels or []:
        if not getattr(row, "sync_id", None) or row.sync_id in seen:
            continue
        seen.add(row.sync_id)
        deliver_action_notice(
            workspace_record,
            row,
            action="Pause",
            group_name=group_name_for_sync_id(row.sync_id),
            post_in_channel=False,
            dm=False,
            label_for={workspace_record.id: "Paused"},
        )
    from helpers.notifications import notify_primary_action

    notify_primary_action(workspace_record, "Uninstall")
