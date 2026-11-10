# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

<!-- version list -->

## [Unreleased]

### Added

- Data Tools on the Primary Workspace: Verify, Repair, and Purge Soft-Deleted Data
- Synced posts carry their post id as Slack message metadata

### Changed

- NOTICE: Database migration in this release; Channels from a Workspace outside their Group are removed, and SQLite stops reusing deleted ids. Back up before upgrading.
- NOTICE: Environment change in this release; a deploy without `ENABLE_DB_RESET` turns Reset Database off. Set it in the env file to keep the button.
- NOTICE: Environment change in this release; `ENABLE_UNSIGNED_IMPORT` stays off until set to `true`, for a file whose signature cannot be checked. Set it back to `false` after that import.
- Data Migration import accepts a signed file from an instance that is not connected, for this Workspace only
- Settings shows this instance's Fingerprint on every Workspace

### Fixed

- A failed Repair leaves the database unchanged, and an active Channel wins over an older paused duplicate
- Syncs no longer show or send to a Channel from a Workspace outside their Group, left over from an old unpublish
- SQLite no longer reuses a deleted row's id, and enforces foreign keys like MySQL and PostgreSQL
- Purging a Workspace removes its settings and clears it from copied messages
- A Workspace still on an External Connection is kept until that connection drops it

## [1.7.6] - 2026-11-06

### Fixed

- A request through one Workspace's install no longer renames, re-tokens, creates, or restores another Workspace
- When SyncBot leaves a shared Channel, only the Workspace whose bot left is paused

## [1.7.5] - 2026-11-04

### Fixed

- An image that an app shows from a Slack file is copied with the message, on this instance and across External Connections
- When Slack refuses a copy's blocks, the copy is posted as text instead of being lost
- Table blocks are copied, and a video from another site is copied as a link

## [1.7.4] - 2026-11-04

### Changed

- NOTICE: Upgrade connected instances too; a Workspace on an instance still on 1.7.3 joins a Group invite without Accept
- Invite Workspace and Join Group say that an Invite Code works only on this SyncBot instance

### Fixed

- An invite to a Workspace on another instance waits for that Workspace to accept, as it does on this instance
- Declining or cancelling a Group invite updates both instances
- A Workspace that left a Group can be invited again, and stays out until it accepts
- Joining a Group with a code tells connected instances
- Join Group shows an error when a code does not match a Group on this instance

## [1.7.3] - 2026-11-04

### Changed

- Data Migration import does not create or join an External Connection. Connect the two instances with Create and Join first
- Import accepts only a file exported from this instance or from a connected instance, and a file from before 1.7.3 needs a new export
- Confirm Import says where the file came from, and Syncs wait while another connected instance still has the Workspace
- The import review and DM say what is left out, such as a Workspace that is not on this instance yet
- Data Migration screens and messages use plain language
- A file hosted outside Slack, such as a Google Drive file, is copied as a link
- Export names the instance that made the file, and Import checks the file's signature with that instance's key
- A new External Connection gets the other instance's Workspaces on its next keep-warm
- Export and Request Connection is removed. Use Export for the file, and Create Connection and Join for the connection

### Fixed

- Federation accepts requests only from peer instances, never from this instance's own id
- A connection is created only from the public key in the connection code
- Federation requests go only to public addresses, and SyncBot downloads files only from Slack
- The Authorize link uses only an address Slack has confirmed
- A form that shows a field error saves nothing
- Join no longer reports a failure when its first request already connected
- The import DM says whether the Syncs are running or waiting, what was left out, and when to try again
- Import changes only the importing Workspace and Workspaces on a connected instance, and joins only Groups it is already in
- Removing a Workspace from a Group gives the Group a new invite code

## [1.7.2] - 2026-10-30

### Changed

- NOTICE: Database migration in this release; stored Sync status values are rewritten
- REQUIRED: Slack manifest needs to be updated; leave and delete events are added, `usergroups:read` is dropped, and reinstall if the bot lacks `emoji:read`
- Pause removes SyncBot from the Channel, removing SyncBot pauses the same way, and a manager adding it back resumes
- Leave Sync, Leave Group, and Remove keep history for the retention period, and Join or Create within it brings it back
- Disband keeps history for the retention period unless its checkbox deletes it now
- Archive pauses a Sync until the Channel is unarchived, and a privacy change or a deleted Channel ends it for that Workspace
- A private Sync ends for that Workspace when the manager who added SyncBot leaves the Channel or private Channels are turned off
- Every Sync change posts one notice in each active Channel of the Sync, and the acting Workspace's managers get the details
- A workspace still installed here posts Install when another instance lists it, and that notice names the app that was installed
- A migration export leaves out a disbanded group, and an older file does not revive it

### Fixed

- Private Syncs show only to managers in the Channel and only sync with private Channels
- A group owner can remove a member, and Promote to Owner, Disband, and succession reach a Workspace on another instance
- A moved Workspace keeps its group role, is listed once, and its Syncs wait until the old instance releases it
- A message across an External Connection lands on the same Sync as on one instance, and a retry does not post twice
- Join Sync and Home Refresh no longer show a connection error while an External Connection syncs
- Leave Group drops the Owner role, so the remaining Owner can disband

## [1.7.1] - 2026-09-23

### Fixed

- Button modals open in the ack and fill in afterward
- Refresh should do nothing when Home has not changed
- Slow modals say you can close and SyncBot will DM, or to stay until the work finishes

## [1.7.0] - 2026-09-22

### Added

- External Connections so two SyncBot installs can sync messages, files, and reactions
- Signed data migration export and import, including Export and Request Connection
- Settings Information section and a Workspace Block List for Team IDs

### Changed

- NOTICE: Database migration in this release; federation tables are replaced, leftover columns are dropped, and existing connections are kept
- Leave Connection pauses remote Workspaces, reconnecting restores them and shows `#channel` names
- MySQL and PostgreSQL prefer `DATABASE_SCHEMA` with `syncbot_${stage}` fallback
- Federation returns 400 or 401 before reading an untrusted body
- File-part and JSON size follow this hop's injected HTTP cap
- After retention, the earliest-joined remaining local member becomes owner, or the group is disbanded
- A file share is the message itself, with the same from line as other messages

### Removed

- Settings Broadcast allow-list

### Fixed

- File shares keep the file up to Slack's 1 GB
- Threads, edits, and reactions follow the message across External Connections
- `#channel` mentions stay a source `#name (Workspace)`
- Hybrid reaction notices stay in the right thread with the mapped person's name
- Sync works after deploy without a Home Refresh
- A one-Workspace group offers Disband Group, not Leave Group
- Reactions keep the later add or remove
- Reinstall and import rejoin public Channels, leave private ones paused, and resume twin Channels on remaining Workspaces
- Create Connection and Import keep a wait screen and DM when they finish
- File copies keep a Slack ts when complete-upload omits it, including External Connections
- External Connection file parts follow the peer hop's advertised receive cap
- Cloud Run advertises a 30 MiB part cap
- The HTTP server finishes a Slack listener before the response
- A failed External Connection file reassembly returns `assemble_failed` and does not ask for the parts again
- A GIF stays on the message when that message also shares a file

## [1.6.2] - 2026-09-11

### Changed

- Code refactor; remove dead code, use verb prefixes for helpers, lockstep group invite Slack IDs
- Message and reaction sync skip Slack lookups until the channel has membership and publish targets
- File shares take the message ts from the upload response when present, then channel history
- Home tab reopen skips a rebuild when the content hash is unchanged
- User-token file shares keep the caption and Block Kit on the same native message as the file
- DEBUG logs include sync.* traces for message skip, pipeline fan-out, and file share ts

### Fixed

- Pause Sync stops inbound until Resume
- User-token file shares skip inbound events by Slack file id and the share ts, so copies are not treated as new origins
- Reactions on a publishing Channel still fan out when the message is a synced copy; inbound creates stay skipped by echo and PostMeta
- Reactions and thread replies find existing messages by a six-decimal Slack ts, so older posts still match
- Thread replies Slack marks as a later share of a parent file still sync
- File-only bot shares record the share ts after Slack omits it on complete-upload, so thread replies still attach


## [1.6.1] - 2026-09-10

### Fixed

- Unlabeled source permalinks and Slack `message_mention` become a `message in #channel (Workspace)` link; existing link text is left alone


## [1.6.0] - 2026-09-09

### Added

- Synced messages and ordinary file shares can post as the mapped person in the other workspace when they have authorized there
- A Channel can publish only, subscribe only, or both; a second publisher stays in the same channel sync until someone subscribes
- One Channel can subscribe to more than one distinct published Channel
- Reaction type Off does not apply incoming reaction add or remove on a subscribing Channel

### Changed

- REQUIRED: Slack manifest needs to be updated; adds `emoji:read` and `usergroups:read`, drops user `im:write`, then reinstall in each Workspace
- Authorize SyncBot no longer asks to send DMs as you
- New publishes are visible to the whole group; a Channel may be published in more than one group
- Reaction send and receive follow publish and subscribe; Reaction type is Hybrid, Direct, or Off
- Home Synced Channels lists each partner as a code-ticked `#channel (Workspace)`
- Home Synced Channels Status includes participation, for example Active (Publish and Subscribe)
- Home action buttons and in-channel sync notices lead with an emoji

### Fixed

- Thread replies, files, and reactions stay on the original message's sync
- Unmapped people in synced messages render as Slack code, not literal backticks

## [1.5.4] - 2026-09-09

### Fixed

- Keep changelog tests aligned with shortened 1.2.0-length notes (#43)


## [1.5.3] - 2026-09-05

### Changed

- REQUIRED: Environment change in this release; set `DATA_ENCRYPTION_KEY` on the running process, and deploy scripts copy `TOKEN_ENCRYPTION_KEY`
- User Mapping leftover match names are gone (`matched_at` is `mapped_at`; old backup keys still import)

## [1.5.2] - 2026-09-03

### Changed

- Synced message `#channel` mentions become a code-ticked `#name (Workspace)`, not a dest twin or a deep link

### Fixed

- Mentions, source permalinks, and federation inbound follow the same mapping rules
- Dest Block Kit posts stay within Slack's block and section limits
- Threaded file shares use the matching dest timestamp
- AWS and GCP deploy keep previous secrets, default `syncbot_${stage}`, and pass Slack scopes from env

## [1.5.1] - 2026-09-03

### Changed

- NOTICE: Environment change in this release; `SYNCBOT_INSTANCE_ID` is ignored and the instance id is the key fingerprint
- User Mapping uses map wording, a 20-row page, and `last_auto_map`
- Federation instance id is a SHA-256 fingerprint of the Ed25519 public key; connection codes carry the peer's full endpoint URL
- Sync hot paths reuse per-request and 60s process caches

### Fixed

- Also-send-to-channel replies, hosted files, and file shares sync at the right thread level
- Block Kit bot posts keep newlines and emoji; unmapped mentions use a code-ticked display name
- On-the-fly mapping uses dest directory email then one `lookupByEmail` and persists a none stub
- Federation connection codes, image blocks, Lambda Function URLs, and mention rewrite work across instances
- AWS migrate reports FunctionError, Lambda timeout is 120s, and MySQL column CHANGE includes the existing type

## [1.5.0] - 2026-09-02

### Added

- User Mapping modal with Auto Map Now
- SyncBot posts in a Channel when it is published, even before anyone subscribes

### Changed

- User Mapping opens from saved mappings; Auto Map Now runs in the background of the modal
- Failure DMs include a copyable details block
- New Channel syncs default to Hybrid reactions, listed first on Publish, Subscribe, and Edit

### Fixed

- Users with the same email across Workspaces are mapped automatically
- Saving Edit on a Channel sync no longer times out the Lambda while listing every member


## [1.4.1] - 2026-09-01

### Changed

- NOTICE: Database migration in this release; token columns are encrypted in place with the current `DATA_ENCRYPTION_KEY`
- REQUIRED: Environment change in this release; turn Federation on in Settings if it was only set with `SYNCBOT_FEDERATION_ENABLED`

### Fixed

- Hybrid reaction notices delete on unreact, including child notices under them
- Deleting a Hybrid notice on one target stays local to that channel
- Bot token refresh no longer rewrites the database on every Home publish
- User OAuth tokens encrypt at rest; they were stored in plaintext
- Leftover `REQUIRE_ADMIN`, `SYNCBOT_FEDERATION_ENABLED`, and `SYNCBOT_PUBLIC_URL` removed from deploy; enable federation in Settings if it was never saved there
- Edit Channel sync keeps the saved reaction type when direction is send only or off


## [1.4.0] - 2026-09-01

### Added

- Federation is enabled from the Settings modal on the primary workspace
- Extra workspace managers can configure groups and syncs without opening Settings
- Per-workspace Allow private Channels in Settings on every installed workspace
- Per-channel send/receive reaction direction and direct vs hybrid reaction type
- Home **Edit** on synced Channel rows for Any vs Specific and this workspace’s reactions

### Changed

- Settings is available on every workspace for Slack admins; instance fields stay primary-only
- NOTICE: Environment change in this release; `SYNCBOT_FEDERATION_ENABLED`, `REQUIRE_ADMIN`, and `ALLOW_PRIVATE_CHANNELS` are seeded once, then ignored

### Fixed

- Reaction sync ignores user-token echo events and no longer threads on `already_reacted` or missing custom emoji
- Workspace-settings upgrade quotes the reserved MySQL `key` column
- Schema errors are not retried, and view acks still return if a handler raises


## [1.3.3] - 2026-09-01

### Changed

- Home no longer keeps leftover DeSync or Create new / Join existing Sync routes; Publish, Subscribe, Unpublish, and Stop Syncing are the Channel Sync path

### Fixed

- Data Migration export/import looks up Workspaces by integer id via `get_workspace_by_id`


## [1.3.2] - 2026-08-31

### Added

- **Authorize SyncBot** on Home for anyone missing user permissions (starts at `/slack/install`)

### Changed

- NOTICE: Environment change in this release; `SYNCBOT_PUBLIC_URL` is ignored and the request Host is used
- `REQUIRE_ADMIN` still limits configuration; every user can open Home, authorize, and Refresh

### Fixed

- Private Channels can be published and subscribed: SyncBot is invited in, stays when added by hand, and a failed invite rolls back with a DM
- OAuth Allow no longer fails with `invalid_browser`; Home updates after Authorize
- Revoking your own token no longer pauses the workspace; Authorize returns, and uninstall still clears all tokens
- Home lists available Channels by name, not Slack ID
- Private-Channel invites no longer fail with `user_not_found` after use in another workspace


## [1.3.1] - 2026-08-31

### Fixed

- Channel pickers search the whole workspace instead of stopping at 100 channels
- Picking a channel already in a Channel Sync explains the problem in the dialog

### Changed

- A channel may belong to only one Channel Sync instance-wide
- Retention, private-channel publishing, and the broadcast allow-list are set only in **Settings**
- Private channels are off by default; the dialog warns that messages will be copied
- **Publish Channel** and **Subscribe** replace Sync Channel and Start Syncing
- A channel SyncBot cannot read is rejected rather than failing later


## [1.3.0] - 2026-08-30

### Added

- Group ownership: **Promote to Owner** and **Give Up Ownership**, keeping at least one owner
- **Disband Group** for a sole owner that is also the sole publisher
- Group owners can cancel a pending invite
- Operator **Settings** for retention, private channels, and the broadcast allow-list

### Changed

- NOTICE: Database migration in this release; group role `creator` is rewritten to `owner` and `created_by_workspace_id` is dropped
- NOTICE: Environment change in this release; retention, private channels, and the broadcast list are seeds, then Settings wins
- Retention changes apply without a restart
- Ownership survives uninstall until the workspace's data is deleted
- Destructive confirmations use a red button


## [1.2.6] - 2026-08-30

### Fixed

- Channel unpublish and publisher teardown no longer leave a broken sync

## [1.2.5] - 2026-08-30

### Fixed

- Group invite accept, decline, and cancel require authorization

## [1.2.4] - 2026-08-30

### Fixed

- GitHub Actions trusts the immutable OIDC subject claim

## [1.2.3] - 2026-08-30

### Fixed

- Lambda builds from source so SAM can reach `syncbot/`

## [1.2.2] - 2026-08-30

### Changed

- NOTICE: Environment change in this release; set `DATABASE_BACKEND` and `CLOUD_PROVIDER`, leftover names warn until 2.0.0
- GitHub Actions picks provider and stage from the job; the first AWS deploy creates the bootstrap stack
- First-time deploy docs and `.env.deploy.example` match the AWS and GCP paths

### Fixed

- `./deploy.sh` no longer looks up leftover Secrets Manager IDs

## [1.2.1] - 2026-08-27

### Changed

- NOTICE: Deploy aborts while the stack still has RDS; back up, follow the removal steps, and do not retry that update

### Fixed

- AWS stack no longer creates RDS; SQLite + Litestream replicas go to S3

## [1.2.0] - 2026-08-27

### Added

- GCP: SQLite + Litestream to GCS as the free Cloud Run default; existing MySQL/TiDB remains opt-in
- Canonical sprocktech/syncbot release automation (python-semantic-release, Dependabot auto-merge)

### Changed

- Cloud Run image updates are CI-only; later `terraform apply` does not revert the image
- `./deploy.sh` does not run `poetry update`; local and GitHub deploys install committed pins
- Bumped Python and GitHub Actions dependencies (patch/minor)

### Fixed

- Slack message and reaction sync is idempotent on envelope `event_id`, so retries do not double-post

## [1.1.0] - 2026-04-21

### Added

- Deploy flags (`--bootstrap`, `--setup-github`, `--update-stack`, `--verbose`), `.env.deploy.example`, and a summary with the OAuth redirect URL
- CI bootstrap sync, `workflow_dispatch`, concurrency groups, and `pip-audit`

### Changed

- REQUIRED: Slack manifest needs to be updated; request, interactivity, and redirect URLs
- REQUIRED: Environment change in this release; set `DATA_ENCRYPTION_KEY` on the running process, and deploy scripts copy `TOKEN_ENCRYPTION_KEY`
- REQUIRED: Environment change in this release; `DATABASE_USER` is a GitHub variable, not a secret
- AWS uses Lambda Function URLs (no API Gateway or Secrets Manager); GCP secrets are Terraform variables

### Fixed

- Interactive GitHub push sets Lambda SG ID and `SLACK_CLIENT_ID` correctly

## [1.0.2] - 2026-03-28

### Added

- External DB deploy parameters for TiDB Cloud and other managed providers (cluster-prefixed usernames, 32-char limits)

### Changed

- Synced message author shows the local display name and avatar for mapped users
- Default DB usernames shortened to `sbadmin_{stage}` and `sbapp_{stage}`; existing RDS master names stay
- REQUIRED: Re-run bootstrap for `lambda:InvokeFunction`; migrations run after deploy and memory is 256 MB

## [1.0.1] - 2026-03-26

### Changed

- REQUIRED: Environment change in this release; set `PRIMARY_WORKSPACE` to the Team ID, and `ENABLE_DB_RESET` is now `true`, `1`, or `yes`
- Cross-workspace `#channel` links resolve to native local channels when the channel is part of the same sync; otherwise use workspace archive URLs with a code-formatted fallback
- `@mentions` and `#channel` links in federated messages are now resolved on the receiving instance (native tags when mapped/synced, fallbacks otherwise)

## [1.0.0] - 2026-03-25

### Added

- Multi-workspace message sync: messages, threads, edits, deletes, reactions, images, videos, and GIFs
- Cross-workspace @mention resolution (email, name, and manual matching)
- Workspace Groups with invite codes (many-to-many collaboration; direct and group-wide sync modes)
- Pause, resume, and stop per-channel sync controls
- App Home tab for configuration (no slash commands)
- Cross-instance federation (optional, HMAC-authenticated)
- Backup/restore and workspace data migration
- Bot token encryption at rest (Fernet)
- AWS deployment (SAM/CloudFormation) with optional CI/CD via GitHub Actions
- GCP deployment (Terraform/Cloud Run) with interactive deploy script; GitHub Actions workflow for GCP is not yet fully wired
- Dev Container and Docker Compose for local development
- Structured JSON logging with correlation IDs and CloudWatch alarms (AWS)
- PostgreSQL, MySQL, and SQLite database backends
- Alembic-managed schema migrations applied at startup
