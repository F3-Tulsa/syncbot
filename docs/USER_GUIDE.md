# SyncBot User Guide

This guide is for **workspace admins and people using SyncBot in Slack**. If you are installing or hosting the app (AWS, GCP, Docker, GitHub Actions), see **[DEPLOY.md](DEPLOY.md)** and the root **[README](../README.md)**.

## Getting Started

1. Click the install link from a desktop browser (make sure you have selected the correct workspace in the upper right).
2. Open the **SyncBot** app from the sidebar and click the **Home** tab. Everyone can open it. Workspace admins and owners configure Settings; they can also name extra managers who may create groups, Create Sync, and Join Sync without opening Settings.
3. The Home tab shows everything in one view:
   - **Authorize SyncBot** — at the top, when this person still needs to grant user permissions. See **Authorize SyncBot** below.
   - **SyncBot Configuration** — directly under Authorize.
     - **Refresh** is for everyone (including after you revoke your authorization).
     - **Settings** is for Slack admins on every installed workspace: extra managers and whether private Channels may be published here. Federation, the soft-delete retention period, and the Workspace Block List stay on the primary workspace (`PRIMARY_WORKSPACE` set and redeployed). That number is how long removed data is kept. Other workspaces use it and cannot edit it. The block list is Slack Team IDs (for example `T0123456789`). A listed Workspace is uninstalled and cannot Add to Slack again until you remove the ID; removing it does not reinstall for them. You cannot block the primary Workspace. You also cannot block a Workspace that owns a Group until that Workspace is no longer an Owner. Promoting another Owner does not remove its Owner role. The bottom of Settings lists this install's version and primary Workspace. On the primary Workspace it also shows log level, fingerprint, public URL, and database.
     - **Backup/Restore** is primary-only. **Data Migration** is on that same row for Slack admins (export or import this Workspace's SyncBot data). If you do not see those instance options, ask the operator.
   - **Workspace Groups** — create or join groups of workspaces that can sync channels together (admins).
   - **Per-group sections** — for each group you can **Create Sync**, open **User Mapping** (a modal), and see or manage channel syncs inline. Other workspaces in the group see those syncs as **Available Sync Relationships** and can **Join Sync**. Create, Join, and Edit each offer a participation choice, described under **Create Sync and Join Sync**.
   - **Synced Channels** — each row shows the local channel, then **Status**. An active row shows its participation, for example `` `Publish and Subscribe` ``. A paused row shows `` `Paused` ``. A row waiting on a migration shows `` `Waiting for migration` ``. When this Workspace allows private Channels, **Privacy** shows `` `Public` `` or `` `Private` `` once Slack's flag is known. **Members** lists the other Channels as `` `#general (Workspace A) [Publish and Subscribe]` ``, or `` `#general (Workspace A) [Paused]` `` when that Channel is paused. An empty list says `` `no other Channels` ``. The row also has **Edit Sync**, **Pause Sync** or **Resume Sync**, **Leave Sync**, a synced-since date, and a tracked message count.
   - **External Connections** *(when federation is enabled)* — **Create External Connection** or **Join External Connection**. A created connection appears as a row (Trust Status `Waiting`, Local Workspaces, Remote Workspaces) before the other side joins, with **Show Connection Code**, **Edit Connection**, and **Cancel Connection**. After they join: **Edit Connection**, **Verify Trust**, or **Leave Connection**.

## Things to Know

- Workspace **admins and owners** open Settings, Backup/Restore, Reset Database, and External Connections. **Extra managers** (chosen in Settings) can create groups, Create Sync, and Join Sync, but they cannot open those admin-only screens. Everyone can still open the Home tab, authorize SyncBot, and use **Refresh**.
- Messages, threads, edits, deletes, reactions, images, videos, GIFs, and other hosted files (PDFs, audio, zips) are all synced between workspaces on this instance and across External Connections the same way.
- **@mentions and #channel links** in synced messages are rewritten for each target: mapped users are tagged with the local Slack user, and `#channel` mentions become a code-ticked `#name (Workspace)` (for example `` `#announcements (Workspace A)` ``). SyncBot does not turn those into the target twin channel, because the author named a place in the source workspace. Links to a specific source message keep that URL. A pasted Slack message becomes `message in #channel (Workspace)`; if the author already labeled the link, that text stays. Unmapped people fall back to a code-ticked display name such as `` `Name (Workspace)` ``.
- Messages from other bots are synced; only SyncBot's own messages are filtered to prevent loops.
- Existing messages are not back-filled; syncing starts from the moment a channel is linked.
- Do not add SyncBot manually to channels. SyncBot adds itself when you Create Sync or Join Sync. If it is added to a Channel that is not part of a Sync, it posts a note and leaves.
- When you pick a channel to create or join a Sync, SyncBot uses Slack's own channel search, so you can reach any channel in your workspace by typing a few letters. There is no limit on how many channels it can show.
- A channel may participate in more than one Channel Sync. This allows several published sources to feed one local channel (fan-in), and the same channel may be published in more than one group. A workspace cannot subscribe to the same published source twice; SyncBot reports that duplicate in the dialog. Do not reject a channel merely because it already participates in another Channel Sync.
- Public channels are supported out of the box. Private channels are only available if a Slack admin turned them on for **this** workspace in **Settings**; if they have not, SyncBot asks you to pick a public channel. When they are allowed, you create or join a Sync on a private channel the same way you would a public one, and SyncBot adds itself for you using your permission to invite it — see **Authorize SyncBot** below. If it cannot be added, that Channel Sync is undone and you get a direct message explaining why.

## Authorize SyncBot

Slack does not allow an app to add itself to a private channel. Only someone who is already in that channel can add it, acting as themselves. So the first time you use SyncBot, you may see an **Authorize SyncBot** section at the top of the Home tab with a short explanation, a list of the permissions it is asking for, and a button.

Clicking the button opens this SyncBot instance's own install page, which then sends you to Slack. The screen arrives with this workspace already selected, so if you belong to several you do not have to hunt for the right one. It takes a few seconds. It does not ask for any new permissions from your workspace beyond the list on the Home tab; it simply records that SyncBot may act on your behalf. When you click Allow, the Home tab updates on its own — you do not need to press Refresh — and the section disappears. Creating or joining a Sync on a private channel then works without extra steps, and target messages, files, and native reactions can appear as you in this workspace. Direct messages still come from the bot, not from you. You need to authorize in **each** workspace (including federated ones) where you want SyncBot to act as you. Starting from a slack.com link copied from elsewhere can fail after you click Allow; use the Home tab button.

If SyncBot later needs an additional permission, the section comes back. Permissions you already granted stay listed with checkmarks under **Already allowed permissions**, and only what is new appears under **Needed permissions**, so it is an update rather than starting over. The already-allowed list is omitted the first time, when nothing has been granted yet.

Everyone sees this section until they have granted every current permission, whether or not they are an admin. Whoever installed SyncBot originally will usually never see it, because that first install already stored their own permission. A colleague's authorization is not reused: SyncBot only invites itself into a private channel as the person who picked it, and only posts target messages, shares files, or adds a native reaction as the mapped person who authorized in that workspace. If you pick a private channel before authorizing, SyncBot tells you in the dialog and points you here rather than failing after the dialog closes.

### Revoke your authorization

This is personal: it only drops SyncBot's permission to act as *you*. It does not uninstall the app from the workspace. It does end the private Channel Syncs that run under you: SyncBot leaves those Channels, every active Channel of each Sync gets the Action Notice, and your Workspace's managers get the detail direct message. Public Syncs and other people's private Syncs are not affected. After you revoke, SyncBot can no longer invite itself into a private channel, post target messages or files, or add native reactions as you. Direct messages are unaffected because the bot sends them. **Authorize SyncBot** should come back on its own; if the Home tab still looks the same, click **Refresh** in **SyncBot Configuration** (just under Authorize). Use **Authorize SyncBot** again if you change your mind.

Slack owns this screen (there is no button for it on the Home tab). From the desktop app:

1. Click the workspace name in the sidebar, then **Tools & settings** → **Manage apps**.
2. Open **Installed Apps**, find **SyncBot**, and click **App Details**.
3. Open the **Configuration** tab.
4. Under **Authorizations**, find **Authorized members**, click **See all**, and click **Revoke** next to your own name.

Do not click **Remove App** on that same page unless you mean to uninstall SyncBot for the whole workspace. That is a different action: it soft-deletes the workspace (its Channel rows become uninstalled), as described under **Uninstall / Reinstall** below.

On many workspaces, Slack's default is that any member except guests can open this list and revoke *other people* as well. That is a workspace setting, not something SyncBot can lock. Workspace owners should turn on approved apps so only owners and chosen app managers can do that — see **Security** below. Slack's own walkthrough is [Remove apps and custom integrations from your workspace](https://slack.com/help/articles/360003125231-Remove-apps-and-custom-integrations-from-your-workspace) (use the tab for removing a configuration or authorization, not for removing the app).

## Security

SyncBot cannot hide Slack's app Configuration page or decide who is allowed to revoke authorizations. That is controlled by the Slack workspace. By default, any member except guests can often install apps, uninstall them, and revoke other members' authorizations. For a community workspace, we recommend tightening that before you rely on **Authorize SyncBot** for private channels.

A **Workspace Owner** can do this from the desktop app:

1. Click the workspace name in the sidebar, then **Tools & settings** → **Manage apps**.
2. Open **App Management Settings** in the left sidebar.
3. Turn on **Approve apps** (some workspaces label this **Require approved apps**). Save.
4. Keep **App Managers** as Workspace Owners only, or add specific admins you trust. Do not leave every member able to manage apps.

Once approved apps are required, only Workspace Owners and the people you appointed as app managers can remove apps or revoke someone else's authorization. Members can still use **Authorize SyncBot** for themselves. They may need to request a new app (or new permissions) instead of installing freely, which is the usual tradeoff.

Slack documents this in [Manage app approval for your workspace](https://slack.com/help/articles/222386767-Manage-app-approval-for-your-workspace) and [Security recommendations for approving apps](https://slack.com/help/articles/360001670528-Security-recommendations-for-approving-apps).

## Workspace Groups

Workspaces must belong to the same **group** before they can sync channels or map users. Admins can create a new group (which generates an invite code) or join an existing group by entering a code. Removing a workspace from a group gives the group a new invite code, so the old code stops working. A workspace can be in multiple groups with different combinations of other workspaces.

### Group owners

Every group has at least one **owner** workspace. The workspace that creates a group is its first owner. Owners are the workspaces that can promote other owners and disband the group; everyone else is a member. Inviting another workspace stays open to any member, not just owners.

Every member row shows that Workspace's own name, a globe when it is on another instance, and **Group Role** of `` `Owner` `` or `` `Member` ``, plus Member Since, Synced Channels, and Mapped Users. A pending invite shows **Group Role** `` `Invited` `` and no Member Since line. A manager in an owner workspace can remove a non-owner workspace from the group. That ends that workspace's Channel Syncs in the group the same way leaving does, and keeps the synced history for the retention period. Owners are not removed this way. Give Up Ownership still requires another owner. **Promote to Owner** is on a member row too.

An owner can share that responsibility by clicking **Promote to Owner** next to another workspace in the group, including one on another instance. There is no matching "demote" button for other workspaces — an owner can only step down itself, using **Give Up Ownership**, and only when another owner remains. That keeps one workspace from quietly taking a group over by demoting everyone else.

A globe marks a workspace on another instance. It is still a member of the group, with the same actions. **Give Up Ownership** stays on your own workspace's row, because you do not take ownership from someone else. This install has no bot token there, so the row has no workspace icon, and User Mapping has no photo for a person from that workspace. Mapping uses the directory the two instances exchange. An invite code joins a workspace on this instance. A workspace on another instance is added from **Invite Workspace**, and that instance's Home shows the membership. Inviting a workspace on this instance sends a DM, because the admins are here. Pause, Resume, Edit Sync, and Leave Sync are the Channels of the workspace you are signed into. The other workspace's managers do those from their Home.

Give Up Ownership, Promote to Owner, and Cancel Invite each ask before they change the group. If you are the only owner and other workspaces are still in the group, SyncBot will not let your workspace leave until you have promoted another workspace to owner. It explains this instead of failing silently, so you know what to do next. If no other workspace has joined yet, Home shows **Disband Group** instead of **Leave Group**. Disband is also offered when you are the sole owner *and* the only workspace publishing a Channel into the group.

Uninstalling SyncBot does not hand your ownership to anyone else. The membership stays, and reinstalling within the retention period gives you the group back. The Channel rows are `uninstalled` until then. Other workspaces still see the group, with no owner listed. After that window, if another workspace is still in the group, SyncBot promotes the longest-standing remaining member, including one on another instance. If nobody is left, the group is disbanded. The primary workspace can join or be invited like any other workspace; it is not added or made owner just because it is primary.

### Disbanding a group

An owner can **Disband Group** to remove a group entirely, along with its syncs and user mappings. A workspace on another instance loses the group as well. Because this cannot be undone and affects other workspaces, SyncBot only offers it when your workspace is the sole owner *and* the sole publisher of every channel in the group. If another workspace owns the group or has published a channel into it, disbanding is declined with an explanation of who else is involved — ask them to Leave Sync or leave the group first, or just leave the group yourself instead.

Disbanding always asks for confirmation before anything is removed, and tells you how many workspaces, syncs, and channels it will affect. User mappings for the group are deleted immediately. They can be built again. Message history is kept for the retention period and then deleted, and it does not come back, because the group is gone. A checkbox deletes that history now for every workspace in the group, including one on another instance.

## Create Sync and Join Sync

Use **Create Sync** to start a new relationship in a group. There is no separate workspace picker: the new Sync is available to the group, and each other workspace decides independently whether to **Join Sync**. There are no sync owners. If the last publisher leaves, the Sync stays up. That Workspace drops out of the notice. The Channels that are still active keep syncing.

**Create Sync** offers, with the default first:

- **Publish and Subscribe** — send and receive with workspaces that join.
- **Publish only** — send this channel's new messages, files, edits, deletes, threads, and reactions to workspaces that join. This channel will not receive theirs. Copies arriving here do not start another hop.

**Join Sync** and **Edit Sync** offer those two, then **Subscribe only** last. Subscribe only receives without sending local activity. Join as Publish only is how a workspace sends into an existing Sync without receiving.

A Sync waiting for others to join is listed on Home by its channel name and its status. When this Workspace allows private Channels, an available Sync also shows whether that Channel is private or public. Once another workspace joins, the row becomes a link to your local channel. **Create Sync**, **Join Sync**, and **Edit Sync** are each one screen: they name the Channel and Workspace Group, and they always offer Hybrid, Direct, or Off (that type is used while the Channel subscribes). A local channel may join several different published sources, so several channels can feed one place. SyncBot rejects joining the same workspace to the same published source twice.

## Reactions

Reactions follow the same publishing and subscribing participation as messages and files. A reaction originates only from a channel that publishes and is applied only to channels that subscribe; a synced copy never starts another hop. Each subscribing channel chooses one reaction type, and new Create Sync and Join Sync flows default to **Hybrid**.

- **Hybrid** — try a native reaction first; if that person has not authorized (or their permission there is no longer valid), SyncBot posts a short thread notice instead. Custom emoji the other workspace does not have are skipped, even if SyncBot would otherwise post a thread notice.
- **Direct** — native emoji on the synced message, as the mapped person in that workspace. That person must have clicked **Authorize SyncBot** there. Custom emoji the other workspace does not have are skipped.
- **Off** — do not apply incoming reactions in this workspace, including later unreacts. Messages and files still sync. Turning Off later does not remove reactions that already landed, whether those were native emoji or Hybrid thread notices.

On a Hybrid or Direct target, removing a reaction removes that person's native emoji when they have authorized SyncBot there, and deletes their Hybrid thread notices (including notices that were reactions to those notices). Deleting a Hybrid notice in one workspace only removes it there — other workspaces and the original native reaction stay. Each person's notices are independent; human replies under a notice are not deleted. Reactions are never written back into the channel where they started.

## Pause / Resume / Leave

- **Pause Sync / Resume Sync** — Pause removes SyncBot from the Channel and keeps the row on Home as Paused, with no retention clock. Resume adds it back. SyncBot asks you to confirm. Removing SyncBot from a Channel is the same as Pause, and a manager adding it back is the same as Resume. A private Sync resumes that way only when a manager who has Authorized adds it back.
- **Leave Sync** — Removes this workspace's channel from the Sync and keeps this workspace's synced history for the retention period (default 30 days, changed in Settings on the primary workspace). Creating that Sync again, or joining the same Sync, within that time brings the history back, in either order, including when the other workspace is on another instance. A channel that was never in that Sync starts a new one, with no history. Other workspaces continue. SyncBot asks you to confirm.
- **Last publisher** — If you are the last publisher, the other Channels stay. The notice lists who is still in the Sync, and this Workspace is not in that list. Slack messages stay.
- **Private Channel** — A private Channel only syncs with private Channels, on this instance and across an External Connection. Its Sync shows on Home only to managers who have Authorized SyncBot and are in that Channel, and only they can pause, resume, edit, or leave it. If the manager whose Authorization brought SyncBot in leaves the Channel, revokes Authorization, is deactivated, or is removed as an extra manager, this workspace leaves the Sync. Managers still in that Channel get a direct message. Other Workspaces see the same Channel notice, without the retention days.
- **Archive** — Archiving a Channel pauses the Sync and leaves SyncBot in the Channel. The Sync stays on Home, and the history is kept. The retention timer does not start. Unarchiving resumes it. If SyncBot is not in the Channel, it stays paused. Managers in that Channel are told SyncBot is not in it.
- **Channel deleted** — Deleting a Channel removes that workspace's synced history for it now. Other workspaces in the Sync keep theirs. Their Channel notice says the Channel was deleted. Managers in the Workspace that lost the Channel are told the history was deleted.
- **Public to private** — Making a synced public Channel private ends that Sync for this workspace. History stays with the privacy it had, for the retention period, and comes back only if the Channel returns to that privacy and a manager joins the same Sync. A new Sync starts with no history. There is no archived flag stored on the row.
- **Channel notices** — Create, Join, Edit, Pause, Resume, and Leave post a short notice in every active Channel in the Sync, including Publish only and Subscribe only. A paused Channel does not get one. The notice names the Workspace Group, the acting manager, the action, and each Workspace still in the Sync with that Workspace's status. It does not name the Channel and it does not say how long data is kept. If the action makes SyncBot leave, the notice is posted first. If the action makes SyncBot join, the notice waits until the join succeeds. Managers of the Workspace that acted also get a direct message. That message names the Channel. A soft-delete adds how many days this install keeps the data. A hard delete says the history was deleted. A private Channel's direct message goes only to managers still in that Channel. Other Workspaces do not get that direct message.

## Uninstall / Reinstall

If a workspace uninstalls SyncBot, its Channel rows become `uninstalled` and every stored bot and user token for that workspace is removed. Other active Channels get a Pause notice, and that Workspace is listed as Paused. Primary managers get an Uninstall direct message. Reinstalling within the retention period (default 30 days, which the operator can change in **Settings**) restores groups and channel syncs, including group ownership, unless that Team ID is on the Workspace Block List. The same restore runs if that Workspace had become a remote stub (Leave Connection, or a move to another instance): reinstalling here, or importing that Workspace's migration file, brings it back as a live install and keeps message history. Public Channels join again and then post one Resume notice. Private Channels stay paused. Managers of that Workspace get a direct message asking them to Authorize SyncBot and Resume. Other Channels are not told to Pause on the way back in. A bot-token install, new or returning, sends primary managers an Install direct message, and the Acting Manager is this Slack app's name. When another instance lists a Workspace that is still installed here, managers of every local Workspace that shares a group with it get an Install direct message too. The Acting Manager on that message is the Slack app that was installed. That listing is not the move. When the Workspace has moved, those managers get one Migration direct message. Channels do not get either message. Uninstall uses the same app name. A private Channel Sync that lost its manager, or ended because private Channel syncing was turned off, leaves Home and keeps its history for the retention period. Join or Create within that window brings it back. People who had clicked **Authorize SyncBot** will need to do that again before a private Channel is added. Export does not post in a Channel.

## User Mapping

Admins open **User Mapping** from a group on the Home tab. It opens as a modal with the mappings already saved in SyncBot. Unmapped people appear first.

- **Edit** uses Slack’s native user picker to map someone by hand.
- **Auto Map Now** compares emails (and unique display names) in the member directory and writes a mapping whenever exactly one person in the other workspace matches. It does not crawl Slack’s full member list. While it runs, the modal asks you to wait until it finishes. When it finishes, the list and a last-run line update in the same modal (for example, “Last run on September 2, 2026 with 20 new found”). **0 new found** means this run found nothing new in the current directory data, not that every person is mapped.
- **Refresh List** reloads the list from the database. Incomplete lists usually mean the directory is still filling in (for example after a join).
- The first synced message or reaction from an unmapped author can also create a mapping from that person’s directory email, or one target `users.lookupByEmail` if the directory has no unique hit. **Auto Map Now** still fills in everyone else.

In synced messages (same-instance and External Connections):

- A mapped author appears with their **local** display name and profile photo (no workspace suffix in the author line). An unmapped author uses the remote display name and photo, with the source workspace in parentheses.
- A mapped user in the text is a normal `@` tag. An unmapped user is a code-ticked `` `Name (Workspace)` ``.
- `#channel` mentions stay source-side as a code-ticked `` `#name (Workspace)` ``. SyncBot does not turn them into the target twin channel.

## Refresh Behavior

The Home tab has a **Refresh** button in **SyncBot Configuration** for everyone, not only admins. It rebuilds and publishes your Home tab when something has changed, and does nothing else. `/health`, `/ready`, and keep-warm push External Connection allowlists and the snapshot one slice at a time. After a deploy, remembered Home tabs update on their own; you should not need to click Refresh twice to wake the app. User Mapping’s **Refresh List** only reloads that modal from saved mappings.

## Media Sync

On the **same instance**, Slack-hosted files of any type (photos, video, PDFs, audio, zips, and so on) are downloaded from the source and uploaded to each target channel, including files shared in a thread and also-send-to-channel replies. Slack allows files up to 1 GB.

- Slack often uploads the file a moment before it shares it into the Channel. SyncBot still copies that share.
- If a Slack-hosted file cannot be copied, SyncBot DMs you with the error. The usual Free-plan stop is a workspace whose file storage is full.
- A file whose copy lives outside Slack, such as a Google Drive or Dropbox file, is posted as its link on every target, on this instance and across External Connections. SyncBot does not download it and does not DM about it.
- Slack-hosted video and image blocks are not copied as players. The file itself is uploaded so the other workspace can play it.
- GIFs from the Slack GIF picker or GIPHY stay public image blocks.
- When the mapped author has authorized SyncBot in the target workspace, the share posts as that person. The file is the message. A caption, Block Kit, or a public GIF stays on that same message.
- When they have not authorized, the share still posts as one message, with the same from line as other SyncBot posts: the author's name and photo, plus the workspace name when they are not mapped. A file with no text is just the file.

App posts that use Block Kit (for example a form) sync from the layout blocks, so line breaks and emoji stay intact. Slack's "Show more" control is only how the client folds a long message; SyncBot does not stop at the preview. Buttons that belong to the source app (Edit this post, and similar) are not copied, because they would not work in the other workspace.

**External Connections** use the same apply path. Origin posts file parts to the peer first, then the envelope. Those parts stay in the database only while the transfer is in flight (encrypted at rest) and are not included in backup or export.

| Source message | What appears in target workspace |
|---|---|
| Text only | Single message with text, shown under the original poster's name and avatar |
| GIF (Slack picker / GIPHY) | Single message with the GIF embedded inline via image block, under the poster's name |
| GIF + text | Single message with text and GIF together, under the poster's name |
| File only (no text) | One message that is just the file, under the same from line as a text post |
| Text + file | One message with the caption (and Block Kit or a public GIF, when the source had them) and the file together |
| Multiple files | Same as the matching row above; all files go in one upload |

## External Connections

*(Opt-in — enable **Federation** in Settings on the primary workspace)*

Workspaces running their own SyncBot can connect from **External Connections** on the Home tab, using the same Create / Join language as Channel Syncs. Each instance identifies itself with a fingerprint of its signing key, not a UUID you set at deploy time. Trust is that fingerprint. The name is a label only, and a used connection code is not reused.

**Create External Connection** (primary-workspace admin):

1. Name the connection and pick which of this instance's Workspaces the other SyncBot may see. The primary Workspace does not have to be on that list.
2. SyncBot shows the signed connection code in the modal and DMs a copy (24 hours). The code is signed, so the webhook URL and primary Team ID cannot be swapped in transit. Creating the code can be slow. The modal stays open with Close, and SyncBot DMs you when the code is ready.
3. Home lists that connection immediately: Trust Status `Waiting`, Local Workspaces as you selected, Remote Workspaces `None yet`. **Show Connection Code**, **Edit Connection**, and **Cancel Connection** sit on that row. After 24 hours without a join, the waiting row drops off.

**Join External Connection:** paste the code, review the name, primary Workspace (display only), Team ID, URL, and fingerprint, pick your own allowed Workspaces, and click **Join**. A code made on this same instance is refused.

Each connected row shows **Trust Status**, **Local Workspaces**, and **Remote Workspaces** as code ticks.

- **Edit Connection** can rename the connection and change the local Workspaces allowed for that peer (or, on a waiting offer, the Workspaces this instance will share after they join). A Workspace that owns a group with members on this connection must **Give Up Ownership** before it can be removed. After they are removed, the other side's Remote Workspaces update on their next Home open or Refresh. That Workspace is soft-deleted (its Channel rows become `uninstalled`) for the Settings retention window (default 30 days) unless it is allowed again.
- After they join, this instance pushes the allowlist on Edit and on keep-warm (every 5 minutes) so the other side's Remote Workspaces and primary Workspace name stay current. The joining instance sends its Workspaces right away. The instance that made the code sends its Workspaces on its next keep-warm, so Remote Workspaces can show `None yet` for up to 5 minutes. Keep-warm also pushes Groups and Sync Channels for Workspaces on that allowlist so a Workspace that imported its own data is not left waiting for those members to join.
- If Trust Status is `Untrusted` (for example after the other instance reinstalled with a new key), **Verify Trust** shows the primary Workspace, Team ID, URL, fingerprint, and remote Workspaces so you can confirm them out of band before trusting again.
- **Leave Connection** asks for confirmation, then soft-deletes remote Workspaces on this instance (its Channel rows become `uninstalled`, same retention window as uninstall, default 30 days). Reconnecting that External Connection, or reinstalling SyncBot on the same Workspace, restores groups, Syncs, and message history. After the retention period, that data is deleted. The other instance still lists this connection until they leave too.
- **Cancel Connection** on a waiting offer deletes the unused code before anyone joins.
- There is no Pause/Resume on the connection itself. Turning Federation off in Settings stops new traffic without leaving. Untrusted already blocks inbound delivery.

Messages, edits, deletes, reactions, hosted files, and user mapping work across instances.

- Public GIF/image URLs travel with new posts, threads, and edits.
- Private Slack file bytes are copied over a signed file offer/parts channel before the message envelope.
- The receiving instance rewrites `@` mentions and `#` channel links using the same rules as same-instance sync (native `@` tags when mapped; `#channel` stays a source code-tick).
- If you move an instance to a new hostname but keep the same signing key, create a fresh connection code. The peer updates the URL without requiring a new trust decision.

**Data Migration** on SyncBot Configuration lets any workspace admin export or import this Workspace's SyncBot data. Import accepts a file exported from this instance or from a connected instance, and it does not create or join an External Connection. A file from another instance is imported only while that connection is active. If SyncBot cannot reach a connected instance, or that instance does not accept the check, the import still runs, the Syncs stay waiting, and the DM says what to do next. See [Backup and Migration](BACKUP_AND_MIGRATION.md) for Export and Import.

## Backup / Restore

**Backup/Restore** appears on the Home tab only when the operator has set `PRIMARY_WORKSPACE` to this workspace’s Slack Team ID (env, SAM, Terraform, or GitHub variable) and **redeployed**. When it is unset, backup is hidden everywhere.

Use it to download a full-instance backup (all durable tables as JSON) or restore from a backup file. Intended for disaster recovery (for example after a rebuild). See [Backup and Migration](BACKUP_AND_MIGRATION.md).
