"""Tests for getting SyncBot into the channel it is about to sync.

Slack rejects ``conversations.join`` on a private channel outright, so the bot
has to be invited by a member using that member's own user token. These tests
pin the two paths apart, and pin the ordering that makes the private path work
at all: the ``SyncChannel`` row is written *before* the bot is added, so
``leave_channel_without_sync`` does not show it the door.
"""

from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest
from slack_sdk.errors import SlackApiError

from db import schemas
from handlers.channel_sync import handle_create_sync_submit_work, handle_join_sync_submit
from helpers.conversations import AUTHORIZE_HINT, ConversationAccessError, authorize_url, ensure_bot_in_conversation


def _slack_error(code: str, *, errors=None) -> SlackApiError:
    payload = {"ok": False, "error": code}
    if errors is not None:
        payload["errors"] = errors
    return SlackApiError(code, payload)


@pytest.fixture
def client():
    return MagicMock()


class TestEnsureBotInConversation:
    def test_public_channel_is_joined_with_the_bot_token(self, client):
        client.conversations_info.return_value = {"channel": {"is_private": False, "is_member": False}}

        ensure_bot_in_conversation(client, "C1", team_id="T1", acting_user_id="U1")

        client.conversations_join.assert_called_once_with(channel="C1")
        client.conversations_invite.assert_not_called()

    def test_existing_membership_needs_no_api_call(self, client):
        client.conversations_info.return_value = {"channel": {"is_private": True, "is_member": True}}

        with patch("helpers.conversations.allow_private_channels", return_value=True):
            ensure_bot_in_conversation(client, "C1", team_id="T1", acting_user_id="U1")

        client.conversations_join.assert_not_called()
        client.conversations_invite.assert_not_called()

    def test_existing_private_membership_is_rejected_when_private_syncing_is_off(self, client):
        client.conversations_info.return_value = {"channel": {"is_private": True, "is_member": True}}

        with (
            patch("helpers.conversations.allow_private_channels", return_value=False),
            pytest.raises(ConversationAccessError, match="Private Channels cannot be synced"),
        ):
            ensure_bot_in_conversation(client, "C1", team_id="T1", acting_user_id="U1")

        client.conversations_leave.assert_not_called()

    def test_public_join_that_says_already_in_channel_is_success(self, client):
        client.conversations_info.return_value = {"channel": {"is_private": False, "is_member": False}}
        client.conversations_join.side_effect = _slack_error("already_in_channel")

        ensure_bot_in_conversation(client, "C1", team_id="T1", acting_user_id="U1")

    def test_public_join_failure_is_reported(self, client):
        client.conversations_info.return_value = {"channel": {"is_private": False, "is_member": False}}
        client.conversations_join.side_effect = _slack_error("is_archived")

        with pytest.raises(ConversationAccessError) as exc:
            ensure_bot_in_conversation(client, "C1", team_id="T1", acting_user_id="U1")

        assert "is_archived" in str(exc.value)

    def test_private_channel_is_invited_as_the_acting_user(self, client):
        client.conversations_info.return_value = {"channel": {"is_private": True, "is_member": False}}
        user_client = MagicMock()

        with (
            patch("helpers.conversations.allow_private_channels", return_value=True),
            patch("helpers.conversations.get_own_bot_user_id", return_value="UBOT"),
            patch("helpers.conversations.get_user_token", return_value="xoxp-acting-user"),
            patch("helpers.conversations.WebClient", return_value=user_client) as web_client,
        ):
            ensure_bot_in_conversation(client, "C1", team_id="T1", acting_user_id="U1")

        assert web_client.call_args.kwargs["token"] == "xoxp-acting-user"
        user_client.conversations_invite.assert_called_once_with(channel="C1", users="UBOT")
        client.conversations_join.assert_not_called()

    def test_channel_the_bot_cannot_see_takes_the_invite_path(self, client):
        """A private channel SyncBot has never been in is invisible to the bot token."""
        client.conversations_info.side_effect = _slack_error("channel_not_found")
        user_client = MagicMock()

        with (
            patch("helpers.conversations.allow_private_channels", return_value=True),
            patch("helpers.conversations.get_own_bot_user_id", return_value="UBOT"),
            patch("helpers.conversations.get_user_token", return_value="xoxp-acting-user"),
            patch("helpers.conversations.WebClient", return_value=user_client),
        ):
            ensure_bot_in_conversation(client, "C1", team_id="T1", acting_user_id="U1")

        user_client.conversations_invite.assert_called_once()
        client.conversations_join.assert_not_called()

    def test_invite_that_says_already_in_channel_is_success(self, client):
        client.conversations_info.return_value = {"channel": {"is_private": True, "is_member": False}}
        user_client = MagicMock()
        user_client.conversations_invite.side_effect = _slack_error("already_in_channel")

        with (
            patch("helpers.conversations.allow_private_channels", return_value=True),
            patch("helpers.conversations.get_own_bot_user_id", return_value="UBOT"),
            patch("helpers.conversations.get_user_token", return_value="xoxp-acting-user"),
            patch("helpers.conversations.WebClient", return_value=user_client),
        ):
            ensure_bot_in_conversation(client, "C1", team_id="T1", acting_user_id="U1")

    def test_private_channel_without_the_acting_user_token_points_at_authorize(self, client):
        client.conversations_info.return_value = {"channel": {"is_private": True, "is_member": False}}

        with (
            patch("helpers.conversations.allow_private_channels", return_value=True),
            patch("helpers.conversations.get_own_bot_user_id", return_value="UBOT"),
            patch("helpers.conversations.get_user_token", return_value=None),
            patch("helpers.conversations.WebClient") as web_client,
            pytest.raises(ConversationAccessError) as exc,
        ):
            ensure_bot_in_conversation(client, "C1", team_id="T1", acting_user_id="U1")

        assert "Authorize SyncBot" in str(exc.value)
        web_client.assert_not_called()

    def test_only_the_acting_user_token_is_ever_looked_up(self, client):
        """Another member's token must not be borrowed to reach a private channel.

        The picker only offers a private channel to someone who belongs to it, so
        the acting user's membership is what makes the invite legitimate. Using a
        colleague's token would reach channels the publisher cannot see.
        """
        client.conversations_info.return_value = {"channel": {"is_private": True, "is_member": False}}
        tokens = {"U_AUTHORIZED": "xoxp-somebody-else"}
        asked_for: list = []

        def get_user_token(_team_id, user_id):
            asked_for.append(user_id)
            return tokens.get(user_id)

        with (
            patch("helpers.conversations.allow_private_channels", return_value=True),
            patch("helpers.conversations.get_own_bot_user_id", return_value="UBOT"),
            patch("helpers.conversations.get_user_token", side_effect=get_user_token),
            patch("helpers.conversations.WebClient") as web_client,
            pytest.raises(ConversationAccessError) as exc,
        ):
            ensure_bot_in_conversation(client, "C1", team_id="T1", acting_user_id="U_NO_TOKEN")

        assert asked_for == ["U_NO_TOKEN"]
        assert "Authorize SyncBot" in str(exc.value)
        web_client.assert_not_called()

    def test_invite_uses_the_request_scoped_bot_member_id(self, client):
        """Bolt's context is this workspace. A cached auth.test from another is not."""
        client.conversations_info.return_value = {"channel": {"is_private": True, "is_member": False}}
        user_client = MagicMock()

        with (
            patch("helpers.conversations.allow_private_channels", return_value=True),
            patch("helpers.conversations.get_own_bot_user_id", return_value="U_FROM_OTHER_WORKSPACE"),
            patch("helpers.conversations.get_user_token", return_value="xoxp-acting-user"),
            patch("helpers.conversations.WebClient", return_value=user_client),
        ):
            ensure_bot_in_conversation(
                client,
                "C1",
                team_id="T1",
                acting_user_id="U1",
                context={"bot_user_id": "U_THIS_WORKSPACE"},
            )

        user_client.conversations_invite.assert_called_once_with(channel="C1", users="U_THIS_WORKSPACE")

    def test_invite_skips_a_bot_id_and_uses_the_member_id(self, client):
        """Passing a ``B…`` bot_id to conversations.invite is ``user_not_found``."""
        client.conversations_info.return_value = {"channel": {"is_private": True, "is_member": False}}
        user_client = MagicMock()

        with (
            patch("helpers.conversations.allow_private_channels", return_value=True),
            patch("helpers.conversations.get_own_bot_user_id", return_value="UBOTUSER"),
            patch("helpers.conversations.get_user_token", return_value="xoxp-acting-user"),
            patch("helpers.conversations.WebClient", return_value=user_client),
        ):
            ensure_bot_in_conversation(
                client,
                "C1",
                team_id="T1",
                acting_user_id="U1",
                bot_user_id="B0BOT",
            )

        user_client.conversations_invite.assert_called_once_with(channel="C1", users="UBOTUSER")

    def test_invite_retries_with_a_fresh_identity_on_user_not_found(self, client):
        """A stale cached member ID from another workspace is retried once."""
        client.conversations_info.return_value = {"channel": {"is_private": True, "is_member": False}}
        user_client = MagicMock()
        user_client.conversations_invite.side_effect = [_slack_error("user_not_found"), {"ok": True}]

        with (
            patch("helpers.conversations.allow_private_channels", return_value=True),
            patch("helpers.conversations.get_own_bot_user_id", return_value="U_THIS_WORKSPACE"),
            patch("helpers.conversations.get_user_token", return_value="xoxp-acting-user"),
            patch("helpers.conversations.WebClient", return_value=user_client),
        ):
            ensure_bot_in_conversation(
                client,
                "C1",
                team_id="T1",
                acting_user_id="U1",
                bot_user_id="U_OTHER_WORKSPACE",
            )

        invited = [call.kwargs["users"] for call in user_client.conversations_invite.call_args_list]
        assert invited == ["U_OTHER_WORKSPACE", "U_THIS_WORKSPACE"]

    def test_nested_cant_invite_user_not_found_is_retried(self, client):
        """Slack sometimes wraps the unknown invitee as cant_invite + errors[]."""
        client.conversations_info.return_value = {"channel": {"is_private": True, "is_member": False}}
        user_client = MagicMock()
        user_client.conversations_invite.side_effect = [
            _slack_error(
                "cant_invite",
                errors=[{"ok": False, "error": "user_not_found", "user": "U_OTHER_WORKSPACE"}],
            ),
            {"ok": True},
        ]

        with (
            patch("helpers.conversations.allow_private_channels", return_value=True),
            patch("helpers.conversations.get_own_bot_user_id", return_value="U_THIS_WORKSPACE"),
            patch("helpers.conversations.get_user_token", return_value="xoxp-acting-user"),
            patch("helpers.conversations.WebClient", return_value=user_client),
        ):
            ensure_bot_in_conversation(
                client,
                "C1",
                team_id="T1",
                acting_user_id="U1",
                bot_user_id="U_OTHER_WORKSPACE",
            )

        invited = [call.kwargs["users"] for call in user_client.conversations_invite.call_args_list]
        assert invited == ["U_OTHER_WORKSPACE", "U_THIS_WORKSPACE"]

    def test_user_not_found_with_the_same_id_explains_the_failure(self, client):
        client.conversations_info.return_value = {"channel": {"is_private": True, "is_member": False}}
        user_client = MagicMock()
        user_client.conversations_invite.side_effect = _slack_error("user_not_found")

        with (
            patch("helpers.conversations.allow_private_channels", return_value=True),
            patch("helpers.conversations.get_own_bot_user_id", return_value="UBOTUSER"),
            patch("helpers.conversations.get_user_token", return_value="xoxp-acting-user"),
            patch("helpers.conversations.WebClient", return_value=user_client),
            pytest.raises(ConversationAccessError) as exc,
        ):
            ensure_bot_in_conversation(client, "C1", team_id="T1", acting_user_id="U1")

        assert "recognise this app" in str(exc.value)
        assert user_client.conversations_invite.call_count == 1

    def test_installation_lookup_is_scoped_to_the_acting_user(self):
        """``find_installation`` is always called with a ``user_id``.

        Without one it returns the team's most recent install row, which is
        somebody else's token.
        """
        from helpers.conversations import get_user_token

        store = MagicMock()
        store.find_installation.return_value = SimpleNamespace(user_token="xoxp-acting-user")

        with patch("helpers.conversations._installation_store", return_value=store):
            assert get_user_token("T1", "U1") == "xoxp-acting-user"

        assert store.find_installation.call_args.kwargs["user_id"] == "U1"

    def test_acting_user_outside_the_channel_gets_an_explicit_message(self, client):
        """Only reachable via a stale or hand-built payload, since the picker filters."""
        client.conversations_info.return_value = {"channel": {"is_private": True, "is_member": False}}
        user_client = MagicMock()
        user_client.conversations_invite.side_effect = _slack_error("not_in_channel")

        with (
            patch("helpers.conversations.allow_private_channels", return_value=True),
            patch("helpers.conversations.get_own_bot_user_id", return_value="UBOT"),
            patch("helpers.conversations.get_user_token", return_value="xoxp-acting-user"),
            patch("helpers.conversations.WebClient", return_value=user_client),
            pytest.raises(ConversationAccessError) as exc,
        ):
            ensure_bot_in_conversation(client, "C1", team_id="T1", acting_user_id="U1")

        assert "you are not a member of it" in str(exc.value)

    def test_private_channel_is_refused_when_the_policy_is_off(self, client):
        client.conversations_info.return_value = {"channel": {"is_private": True, "is_member": False}}

        with (
            patch("helpers.conversations.allow_private_channels", return_value=False),
            pytest.raises(ConversationAccessError) as exc,
        ):
            ensure_bot_in_conversation(client, "C1", team_id="T1", acting_user_id="U1")

        assert "Private Channels cannot be synced" in str(exc.value)


class TestAuthorizeUrl:
    """Bolt only accepts OAuth that started at this instance's /slack/install.

    The origin is the Host of incoming Slack requests, not SYNCBOT_PUBLIC_URL.
    """

    def test_install_path_carries_the_team(self, monkeypatch):
        monkeypatch.setenv("SLACK_CLIENT_ID", "111.222")

        url = authorize_url("T1", context={"public_base_url": "https://syncbot.example.com"})

        assert url == "https://syncbot.example.com/slack/install?team=T1"

    def test_install_path_without_a_team_is_unchanged(self, monkeypatch):
        monkeypatch.setenv("SLACK_CLIENT_ID", "111.222")

        url = authorize_url(context={"public_base_url": "https://syncbot.example.com"})

        assert url == "https://syncbot.example.com/slack/install"

    def test_remembered_host_is_used_without_context(self, monkeypatch):
        monkeypatch.setenv("SLACK_CLIENT_ID", "111.222")
        monkeypatch.setenv("SYNCBOT_PUBLIC_URL", "")
        from helpers.oauth import capture_public_base

        capture_public_base({"host": "fn.lambda-url.us-east-1.on.aws", "x-forwarded-proto": "https"})

        assert authorize_url("T1") == "https://fn.lambda-url.us-east-1.on.aws/slack/install?team=T1"

    def test_legacy_env_public_url_is_ignored(self, monkeypatch):
        monkeypatch.setenv("SLACK_CLIENT_ID", "111.222")
        monkeypatch.setenv("SYNCBOT_PUBLIC_URL", "https://syncbot.example.com")

        url = authorize_url("T1", context={"public_base_url": "https://from-host.example"})

        assert url == "https://from-host.example/slack/install?team=T1"

    def test_no_origin_means_no_link(self, monkeypatch):
        monkeypatch.setenv("SLACK_CLIENT_ID", "111.222")
        monkeypatch.setenv("SYNCBOT_PUBLIC_URL", "https://should-not-use.example")
        monkeypatch.setattr("helpers.oauth.get_public_base_url", lambda context=None: None)

        assert authorize_url("T1") is None

    def test_no_client_id_means_no_link(self, monkeypatch):
        monkeypatch.setenv("SLACK_CLIENT_ID", "")

        assert authorize_url("T1", context={"public_base_url": "https://syncbot.example.com"}) is None


class TestCreateSyncWritesRowsBeforeAddingTheBot:
    """Ordering is the whole fix: rows first, membership second."""

    WORKSPACE = SimpleNamespace(id=10, team_id="T1", workspace_name="Workspace A")
    BODY = {"view": {"team_id": "T1"}, "user": {"id": "U1"}}

    def _enter_create_sync_patches(self, stack: ExitStack, created: list) -> MagicMock:
        """Enter the shared patches; returns the group-Home invalidation mock."""

        def create_record(record):
            record.id = 99 + len(created)
            created.append(record)

        for patcher in (
            patch("handlers.channel_sync._get_authorized_workspace", return_value=("U1", self.WORKSPACE)),
            patch("handlers.channel_sync._parse_private_metadata", return_value={"group_id": 7}),
            patch("handlers.channel_sync._get_selected_conversation_or_option", return_value="C1"),
            patch("handlers.channel_sync._get_selected_option_value", return_value=None),
            patch("handlers.channel_sync._validate_channel_selection", return_value=(None, False)),
            patch("handlers.channel_sync.helpers.get_user_token", return_value=None),
            patch("handlers.channel_sync.helpers.lookup_channel_name", return_value="C1"),
            patch("handlers.channel_sync.DbManager.create_record", side_effect=create_record),
            patch("handlers.channel_sync.DbManager.find_records", return_value=[SimpleNamespace(name="Group")]),
            patch("handlers.channel_sync.helpers.format_admin_label", return_value="`Admin (Workspace A)`"),
            patch(
                "helpers.workspace.get_workspace_by_id",
                return_value=SimpleNamespace(id=10, team_id="T1", workspace_name="Workspace A"),
            ),
            patch("helpers.workspace_settings.allow_private_channels", return_value=False),
            patch("handlers.channel_sync.deliver_action_notice"),
            patch("handlers.channel_sync.builders.refresh_home_tab_for_workspace"),
        ):
            stack.enter_context(patcher)
        return stack.enter_context(patch("handlers.channel_sync.invalidate_group_member_homes"))

    def test_sync_channel_exists_before_the_bot_is_added(self):
        created: list = []
        order: list[str] = []
        client = MagicMock()

        def ensure(*_args, **_kwargs):
            order.append(f"membership after {len(created)} rows")
            return False

        with ExitStack() as stack:
            homes = self._enter_create_sync_patches(stack, created)
            stack.enter_context(patch("handlers._common.helpers.ensure_bot_in_conversation", side_effect=ensure))
            stack.enter_context(patch("handlers.channel_sync.DbManager.update_records"))
            handle_create_sync_submit_work(self.BODY, client, MagicMock(), {})

        assert [type(record).__name__ for record in created] == ["Sync", "SyncChannel"]
        assert order == ["membership after 2 rows"]
        assert created[0].uid
        assert len(created[0].uid) == 36
        # The other group members' Home caches are dropped; this workspace's Home was pushed already.
        homes.assert_called_once_with(7, exclude_workspace_id=10)

    def test_failed_membership_rolls_back_and_dms_the_admin(self):
        created: list = []
        client = MagicMock()

        with ExitStack() as stack:
            homes = self._enter_create_sync_patches(stack, created)
            stack.enter_context(
                patch(
                    "handlers._common.helpers.ensure_bot_in_conversation",
                    side_effect=ConversationAccessError("no dice"),
                )
            )
            purge_sync = stack.enter_context(patch("handlers.channel_sync.helpers.purge_sync"))
            handle_create_sync_submit_work(self.BODY, client, MagicMock(), {})

        purge_sync.assert_called_once_with(created[0].id)
        assert client.chat_postMessage.call_args.kwargs["channel"] == "U1"
        assert "no dice" in client.chat_postMessage.call_args.kwargs["text"]
        homes.assert_not_called()

    def test_create_sync_stores_the_real_channel_name_not_the_id(self):
        """A private invite also stamps the manager whose token brought SyncBot in."""
        created: list = []
        client = MagicMock()

        with ExitStack() as stack:
            self._enter_create_sync_patches(stack, created)
            stack.enter_context(patch("handlers.channel_sync.helpers.lookup_channel_name", return_value="2nd-f"))
            stack.enter_context(patch("handlers._common.helpers.ensure_bot_in_conversation", return_value=True))
            stack.enter_context(patch("helpers.synced_by.DbManager.update_records"))
            stack.enter_context(patch("helpers.synced_by.DbManager.find_records", return_value=[]))
            handle_create_sync_submit_work(self.BODY, client, MagicMock(), {})

        assert created[0].title == "2nd-f"
        assert created[1].is_private is True
        assert created[1].synced_by_user_id == "U1"

    def test_create_sync_refreshes_title_after_the_bot_joins(self):
        created: list = []
        client = MagicMock()
        names = iter(["C1", "2nd-f"])

        with ExitStack() as stack:
            self._enter_create_sync_patches(stack, created)
            stack.enter_context(
                patch(
                    "handlers.channel_sync.helpers.lookup_channel_name",
                    side_effect=lambda *_a, **_k: next(names),
                )
            )
            update = stack.enter_context(patch("handlers.channel_sync.DbManager.update_records"))
            stack.enter_context(patch("handlers._common.helpers.ensure_bot_in_conversation", return_value=False))
            handle_create_sync_submit_work(self.BODY, client, MagicMock(), {})

        assert created[0].title == "2nd-f"
        assert update.called


class TestJoinSyncWritesRowsBeforeAddingTheBot:
    """Join Sync has the same ordering requirement as Create Sync."""

    WORKSPACE = SimpleNamespace(id=10, team_id="T1")
    BODY = {"view": {"team_id": "T1"}, "user": {"id": "U1"}}
    CONTEXT = {"bot_user_id": "U_THIS_WORKSPACE"}

    def _enter_join_sync_patches(self, stack: ExitStack, created: list) -> MagicMock:
        """Enter the shared patches; returns the group-Home invalidation mock."""

        def create_record(record):
            record.id = 50 + len(created)
            created.append(record)

        for patcher in (
            patch("handlers.channel_sync._get_authorized_workspace", return_value=("U1", self.WORKSPACE)),
            patch("handlers.channel_sync._parse_private_metadata", return_value={"sync_id": 7}),
            patch("handlers.channel_sync._get_selected_conversation_or_option", return_value="C1"),
            patch("handlers.channel_sync._validate_channel_selection", return_value=(None, False)),
            patch(
                "handlers.channel_sync.DbManager.get_record",
                return_value=SimpleNamespace(id=7, group_id=3, title="2nd-f"),
            ),
            patch("handlers.channel_sync.DbManager.find_records", return_value=[]),
            patch("handlers.channel_sync.DbManager.create_record", side_effect=create_record),
            patch("handlers.channel_sync.helpers.lookup_channel_name", return_value="announcements"),
            patch("handlers.channel_sync.helpers.format_admin_label", return_value="`Admin (Workspace A)`"),
            patch("handlers.channel_sync.helpers.resolve_channel_name", return_value="#c"),
            patch("handlers.channel_sync.builders.refresh_home_tab_for_workspace"),
            patch("helpers.sync_lifecycle.notify_channel_or_managers", return_value=True),
            patch("handlers.channel_sync.deliver_action_notice"),
        ):
            stack.enter_context(patcher)
        return stack.enter_context(patch("handlers.channel_sync.invalidate_group_member_homes"))

    def test_sync_channel_exists_before_the_bot_is_added_and_context_is_passed(self):
        created: list = []
        order: list[str] = []
        seen_context: list = []
        client = MagicMock()

        def ensure(*_args, **kwargs):
            order.append(f"membership after {len(created)} rows")
            seen_context.append(kwargs.get("context"))
            return False

        with ExitStack() as stack:
            homes = self._enter_join_sync_patches(stack, created)
            stack.enter_context(patch("handlers._common.helpers.ensure_bot_in_conversation", side_effect=ensure))
            stack.enter_context(patch("handlers.channel_sync.DbManager.update_records"))
            handle_join_sync_submit(self.BODY, client, MagicMock(), self.CONTEXT)

        assert [type(record).__name__ for record in created] == ["SyncChannel"]
        assert order == ["membership after 1 rows"]
        assert seen_context == [self.CONTEXT]
        homes.assert_called_once_with(3, exclude_workspace_id=10)

    def test_failed_membership_rolls_back_and_dms_the_admin(self):
        created: list = []
        client = MagicMock()

        with ExitStack() as stack:
            homes = self._enter_join_sync_patches(stack, created)
            stack.enter_context(
                patch(
                    "handlers._common.helpers.ensure_bot_in_conversation",
                    side_effect=ConversationAccessError("no dice"),
                )
            )
            purge = stack.enter_context(patch("handlers.channel_sync.helpers.purge_sync_channels"))
            handle_join_sync_submit(self.BODY, client, MagicMock(), self.CONTEXT)

        purge.assert_called_once()
        assert purge.call_args.args[0][0] is created[0]
        assert client.chat_postMessage.call_args.kwargs["channel"] == "U1"
        assert "no dice" in client.chat_postMessage.call_args.kwargs["text"]
        homes.assert_not_called()


class TestUnconfiguredChannelStillLeaves:
    """A message in a Channel with no live Sync is what keeps a random invite from sticking."""

    WORKSPACE = SimpleNamespace(id=10, team_id="T1")

    def _run(self, event: dict):
        from handlers.message import respond_to_message_event

        client = MagicMock()
        body = {"team_id": "T1", "event": {"type": "message", "channel": "C1", "ts": "1700000000.000100", **event}}
        with (
            patch("handlers.message.run_claimed", side_effect=lambda _body, work: work()),
            patch("handlers.message._is_own_bot_message", return_value=False),
            patch("handlers.message.helpers.has_channel_sync", return_value=False),
            patch("handlers.message.DbManager.get_record", return_value=self.WORKSPACE) as get_record,
            patch("handlers.message.leave_channel_without_sync", return_value="left_no_sync") as leave,
        ):
            respond_to_message_event(body, client, MagicMock(), {})
        return client, get_record, leave

    def test_channel_with_no_sync_channel_is_left(self):
        client, get_record, leave = self._run({"user": "U1", "text": "hello"})

        get_record.assert_called_once_with(schemas.Workspace, id="T1")
        leave.assert_called_once_with(client, "C1", workspace=self.WORKSPACE)

    def test_message_without_a_person_does_not_leave(self):
        """A bot post with no ``user`` is not someone to answer; SyncBot waits for a person."""
        _client, get_record, leave = self._run({"subtype": "bot_message", "bot_id": "B9", "text": "hello"})

        get_record.assert_not_called()
        leave.assert_not_called()


class TestBotReAddedToChannel:
    """A manager adding SyncBot back resumes what the predicate allows; otherwise SyncBot says why and leaves."""

    WORKSPACE = SimpleNamespace(id=10, team_id="T1")
    NOT_MANAGER = ":lock: Only a workspace manager can resume it."
    NO_TOKEN = f":lock: {AUTHORIZE_HINT}"
    CONFLICT = ":warning: This Channel is private, but this Sync has public Channels."

    @staticmethod
    def _row(sync_id: int, status: str):
        return SimpleNamespace(id=sync_id * 10, sync_id=sync_id, channel_id="C1", workspace_id=10, status=status)

    def _run(self, event: dict, rows: list, *, denials=()):
        """Run the handler; *denials* is one Resume answer per paused row (None means it resumed)."""
        from handlers.sync import handle_member_joined_channel

        client = MagicMock()
        body = {"team_id": "T1", "event": {"channel": "C1", **event}}
        order: list[str] = []
        answers = iter(denials)

        def get_record(model, **kwargs):
            if model is schemas.Workspace:
                return self.WORKSPACE
            return SimpleNamespace(title=f"Sync {kwargs.get('id')}")

        def resume(*_args, **_kwargs):
            order.append("resume")
            return next(answers, None)

        def drop(*_args, **_kwargs):
            order.append("drop")

        with (
            patch("handlers.sync.helpers.get_own_bot_user_id", return_value="UBOT"),
            patch("handlers.sync.DbManager.get_record", side_effect=get_record),
            patch("handlers.sync.get_sync_channels", return_value=rows) as live,
            patch("handlers.sync.resume_sync_channel", side_effect=resume) as resumed,
            patch("handlers.sync.leave_channel_without_sync", return_value="left_no_sync") as leave,
            patch("handlers.sync.helpers.invalidate_private_channel_member", side_effect=drop) as dropped,
            patch("handlers.sync.helpers.note_private_channel_membership") as noted,
            patch("handlers.sync.helpers.notify_user_dm") as dm,
            patch("handlers.sync.helpers.notify_synced_channels") as notice,
            patch("handlers.sync.log_info") as log,
        ):
            handle_member_joined_channel(body, client, MagicMock(), {})
        outcomes = [
            logged.kwargs.get("outcome")
            for logged in log.call_args_list
            if logged.args and logged.args[0] == "member_joined_channel"
        ]
        return SimpleNamespace(
            client=client,
            live=live,
            resumed=resumed,
            leave=leave,
            dropped=dropped,
            noted=noted,
            dm=dm,
            notice=notice,
            outcomes=outcomes,
            order=order,
        )

    def test_a_person_joining_notes_their_membership_and_stops(self):
        run = self._run({"user": "U1"}, [])

        run.noted.assert_called_once_with(run.client, "T1", "U1", "C1")
        run.live.assert_not_called()
        run.leave.assert_not_called()
        run.client.conversations_leave.assert_not_called()
        assert run.outcomes == []

    def test_self_join_is_a_noop(self):
        """SyncBot's own ``conversations.join`` from Create, Join, Resume, or restore has no inviter."""
        run = self._run({"user": "UBOT"}, [])

        assert run.outcomes == ["self_join"]
        run.live.assert_not_called()
        run.leave.assert_not_called()
        run.client.conversations_leave.assert_not_called()

    def test_active_row_is_kept_and_nothing_resumes(self):
        run = self._run({"user": "UBOT", "inviter": "U_MGR"}, [self._row(7, "active"), self._row(8, "paused")])

        assert run.outcomes == ["noop_active"]
        run.resumed.assert_not_called()
        run.leave.assert_not_called()
        run.client.conversations_leave.assert_not_called()

    def test_paused_rows_resume_for_the_inviter_after_dropping_their_cached_membership(self):
        rows = [self._row(7, "paused"), self._row(8, "paused")]
        run = self._run({"user": "UBOT", "inviter": "U_MGR"}, rows)

        assert run.outcomes == ["resumed"]
        # The invite proves the inviter is in the Channel; a cached answer may predate it.
        run.dropped.assert_called_once_with("T1", "U_MGR")
        assert run.order == ["drop", "resume", "resume"]
        assert [resumed.args for resumed in run.resumed.call_args_list] == [
            (rows[0], self.WORKSPACE, "U_MGR"),
            (rows[1], self.WORKSPACE, "U_MGR"),
        ]
        for resumed in run.resumed.call_args_list:
            assert resumed.kwargs == {"client": run.client, "join": False, "context": {}}
        run.dm.assert_not_called()
        run.notice.assert_not_called()
        run.leave.assert_not_called()
        run.client.conversations_leave.assert_not_called()

    def test_partial_resume_posts_one_notice_dms_the_inviter_and_stays(self):
        rows = [self._row(7, "paused"), self._row(8, "paused")]
        run = self._run({"user": "UBOT", "inviter": "U_MGR"}, rows, denials=(None, self.CONFLICT))

        assert run.outcomes == ["resumed_some"]
        run.dm.assert_called_once()
        assert run.dm.call_args.args[:2] == (run.client, "U_MGR")
        text = run.dm.call_args.args[2]
        assert "Sync 8" in text
        assert self.CONFLICT in text
        assert "Sync 7" not in text
        run.notice.assert_called_once_with(run.client, ["C1"], text)
        run.client.chat_postMessage.assert_not_called()
        run.client.conversations_leave.assert_not_called()
        run.leave.assert_not_called()

    @pytest.mark.parametrize(
        ("denial", "outcome"),
        [(NOT_MANAGER, "left_not_manager"), (NO_TOKEN, "left_no_token")],
    )
    def test_nothing_resumes_posts_dms_and_leaves(self, denial, outcome):
        run = self._run({"user": "UBOT", "inviter": "U_MGR"}, [self._row(7, "paused")], denials=(denial,))

        assert run.outcomes == [outcome]
        run.dm.assert_called_once()
        assert run.dm.call_args.args[:2] == (run.client, "U_MGR")
        assert denial in run.dm.call_args.args[2]
        run.leave.assert_called_once()
        assert denial in run.leave.call_args.kwargs["text"]
        assert run.leave.call_args.args[:2] == (run.client, "C1")
        run.client.chat_postMessage.assert_not_called()
        run.client.conversations_leave.assert_not_called()
        run.notice.assert_not_called()

    def test_pending_row_is_left_alone(self):
        """A pending row waits for the origin instance; nothing to resume or to leave for."""
        run = self._run({"user": "UBOT", "inviter": "U_MGR"}, [self._row(7, "pending")])

        assert run.outcomes == ["noop_pending"]
        run.resumed.assert_not_called()
        run.leave.assert_not_called()
        run.client.conversations_leave.assert_not_called()

    def test_no_rows_leaves_the_unconfigured_channel(self):
        run = self._run({"user": "UBOT", "inviter": "U_MGR"}, [])

        run.leave.assert_called_once_with(run.client, "C1", workspace=self.WORKSPACE)
        assert run.outcomes == ["left_no_sync"]
        run.resumed.assert_not_called()


class TestUserHasPrivateChannel:
    def setup_method(self):
        import helpers

        helpers._CACHE.clear()

    def test_cached_answer_skips_slack(self):
        from helpers.conversations import user_in_private_channel

        with (
            patch("helpers.conversations._cache_get", return_value=frozenset({"G1"})),
            patch("helpers.conversations.get_user_token") as token,
        ):
            assert user_in_private_channel("T1", "U1", "G1") is True
        token.assert_not_called()

    def test_missing_token_is_not_cached(self):
        from helpers.conversations import user_in_private_channel

        with (
            patch("helpers.conversations.get_user_token", return_value=None),
            patch("helpers.conversations._cache_set") as cache_set,
        ):
            assert user_in_private_channel("T1", "U1", "G1") is False
        cache_set.assert_not_called()

    def test_private_channel_ids_are_cached_for_resume(self):
        from helpers.conversations import user_in_private_channel

        client = MagicMock()
        client.users_conversations.return_value = {"channels": [{"id": "G1"}]}
        with (
            patch("helpers.conversations.get_user_token", return_value="xoxp-1"),
            patch("helpers.conversations.WebClient", return_value=client),
        ):
            assert user_in_private_channel("T1", "U1", "G1") is True
            assert user_in_private_channel("T1", "U1", "G2") is False
        client.users_conversations.assert_called_once()
        kwargs = client.users_conversations.call_args.kwargs
        assert kwargs["types"] == "private_channel"
        assert kwargs["exclude_archived"] is True
        assert kwargs["limit"] == 200

    def test_failed_lookup_returns_none_and_caches_nothing(self):
        """A partial list must not be remembered; the next Home build asks Slack again."""
        from helpers.conversations import _user_token_private_channel_ids, user_in_private_channel

        client = MagicMock()
        client.users_conversations.side_effect = _slack_error("missing_scope")
        with (
            patch("helpers.conversations.get_user_token", return_value="xoxp-1"),
            patch("helpers.conversations.WebClient", return_value=client),
            patch("helpers.conversations._cache_set") as cache_set,
        ):
            assert _user_token_private_channel_ids("xoxp-1") == (None, "missing_scope")
            assert user_in_private_channel("T1", "U1", "G1") is False
        cache_set.assert_not_called()

    def test_empty_page_follows_the_cursor(self):
        from helpers.conversations import user_in_private_channel

        client = MagicMock()
        client.users_conversations.side_effect = [
            {"channels": [], "response_metadata": {"next_cursor": "more"}},
            {"channels": [{"id": "G1"}], "response_metadata": {}},
        ]
        with (
            patch("helpers.conversations.get_user_token", return_value="xoxp-1"),
            patch("helpers.conversations.WebClient", return_value=client),
        ):
            assert user_in_private_channel("T1", "U1", "G1") is True
        assert client.users_conversations.call_count == 2
        assert client.users_conversations.call_args_list[1].kwargs["cursor"] == "more"

    def test_private_join_drops_the_cache_and_home(self):
        from helpers.conversations import note_private_channel_membership

        client = MagicMock()
        with (
            patch("helpers.channel_state.get_slack_channel_private", return_value=True) as info,
            patch("helpers.conversations.invalidate_private_channel_member") as drop,
        ):
            note_private_channel_membership(client, "T1", "U1", "C1")
        info.assert_called_once_with(client, "C1")
        drop.assert_called_once_with("T1", "U1")

    def test_invalidation_is_this_person_only(self):
        """Three per-user keys through ``_cache_delete``; never the team-wide Home drop."""
        from helpers.conversations import invalidate_private_channel_member

        with (
            patch("helpers.conversations._cache_delete") as delete,
            patch("helpers.export_import.invalidate_home_tab_caches_for_team") as team_wide,
        ):
            invalidate_private_channel_member("T1", "U1")
        assert delete.call_args_list == [
            call("private_channel_member:T1:U1"),
            call("home_tab_hash:T1:U1"),
            call("home_tab_blocks:T1:U1"),
        ]
        team_wide.assert_not_called()

    def test_invalidation_leaves_other_viewers_alone(self):
        import helpers
        from helpers.conversations import _cache_set, invalidate_private_channel_member

        _cache_set("private_channel_member:T1:U1", frozenset({"G1"}))
        _cache_set("home_tab_hash:T1:U1", "hash-1")
        _cache_set("home_tab_blocks:T1:U1", [])
        _cache_set("private_channel_member:T1:U2", frozenset())
        _cache_set("home_tab_hash:T1:U2", "hash-2")

        invalidate_private_channel_member("T1", "U1")

        assert "private_channel_member:T1:U1" not in helpers._CACHE
        assert "home_tab_hash:T1:U1" not in helpers._CACHE
        assert "home_tab_blocks:T1:U1" not in helpers._CACHE
        assert "private_channel_member:T1:U2" in helpers._CACHE
        assert "home_tab_hash:T1:U2" in helpers._CACHE

    @pytest.mark.parametrize("slack_private", [False, None])
    def test_public_or_unreadable_join_keeps_the_cache(self, slack_private):
        from helpers.conversations import note_private_channel_membership

        with (
            patch("helpers.channel_state.get_slack_channel_private", return_value=slack_private),
            patch("helpers.conversations.invalidate_private_channel_member") as drop,
        ):
            note_private_channel_membership(MagicMock(), "T1", "U1", "C1")
        drop.assert_not_called()


class TestHomeHashCarriesPrivateChannelIds:
    def test_leaving_one_channel_changes_the_hash_while_another_remains(self):
        from builders.home import _home_tab_content_hash

        workspace = SimpleNamespace(id=1, team_id="T1", workspace_name="Workspace A")
        with (
            patch("builders.home._get_groups_for_workspace", return_value=[]),
            patch("builders.home.DbManager.find_records", return_value=[]),
            patch("builders.home.helpers.is_db_reset_visible_for_workspace", return_value=False),
            patch("builders.home.helpers.user_permission_lists", return_value=((), ())),
            patch("builders.home._federation_home_fingerprint", return_value=()),
        ):
            both = _home_tab_content_hash(workspace, "U1", is_manager=True, private_channel_ids=("C1", "C2"))
            one = _home_tab_content_hash(workspace, "U1", is_manager=True, private_channel_ids=("C2",))
            empty = _home_tab_content_hash(workspace, "U1", is_manager=True, private_channel_ids=())
            missing = _home_tab_content_hash(workspace, "U1", is_manager=True, private_channel_ids=None)
            ignored = _home_tab_content_hash(workspace, "U1", is_manager=False, private_channel_ids=("C1",))
            plain = _home_tab_content_hash(workspace, "U1", is_manager=False)

        assert both != one
        assert empty != missing
        assert ignored == plain
