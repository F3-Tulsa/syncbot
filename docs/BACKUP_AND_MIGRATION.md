# Backup, Restore, and Data Migration

This page covers three tools:

- **Backup** saves a copy of your SyncBot instance, so you can restore it after something goes wrong.
- **Reset Database** wipes the instance and starts it over, empty.
- **Data Migration** moves one Workspace from one SyncBot instance to another.

Restore, Reset Database, a Data Migration import, Data Tools, and the retention purge take turns. Only one runs at a time, even when SyncBot runs on several servers. If another one is running, SyncBot says a maintenance action is already in progress and changes nothing; try again in a few minutes. A run that stopped without finishing frees its turn after 10 minutes.

## Full-instance backup and restore

Only the primary Workspace has this. Its Slack admins see **Backup/Restore** on the Home tab, next to Refresh. If your instance does not have `PRIMARY_WORKSPACE` set, then nobody sees the button.

### Download a backup

Choose **Download backup**, and SyncBot sends you the file in a DM. The file holds:

- Your Groups, Syncs, and Channels.
- The message history that keeps threads and reactions connected.
- People, and who is mapped to whom.
- Your External Connections.
- Your settings.
- Each Workspace's SyncBot install.

Keep the file somewhere safe.

Restore it on an instance that uses the same encryption key (`DATA_ENCRYPTION_KEY`). With a different key the restore still runs, but every Workspace has to reinstall SyncBot and authorize again.

### Restore a backup

Use restore on a new or empty instance, for example after a rebuild.

1. Open **Backup/Restore** and upload your backup file.
2. Submit. If the file was changed after it was made, or came from an instance with a different encryption key, SyncBot warns you and asks before it goes on.
3. When the restore finishes, choose **Refresh** on the Home tab.

## Reset the database

**Reset Database** deletes everything on this instance and starts it over, empty. You cannot undo it, so download a backup first.

The button is hidden unless whoever runs your SyncBot deployment sets `ENABLE_DB_RESET=true` in the env file for that deploy. A later deploy without it hides the button again. Even when it is on, it shows only on the primary Workspace's Home tab.

## Data Tools

Data Tools is on the primary Workspace's Home tab, for a Slack admin there. It checks this instance's database. Every Workspace on the instance is affected, so download a backup first.

- **Verify** checks the database and changes nothing.
- **Repair** fixes what Verify finds.
- **Purge Soft-Deleted Data** removes soft-deleted data now, instead of waiting for the retention period.

Choose Run. The modal tells you to close it, and the report arrives by DM, even when nothing was found. A finished Repair deletes leftover rows for good.

If a run fails, the report says so:

- A failed Verify or Repair leaves the database as it was.
- A failed Purge may already have removed some data. A Workspace that could not be removed is counted as Workspaces Failed, and the next Purge tries it again.

The app logs each run with who ran it, the option, the result, and how long it took. Filter the logs for `data_tools_`, `data_repair_`, `purge_`, and `run_lock_` to follow one.

## Move a Workspace

Moving a Workspace to another SyncBot instance keeps its Groups, Syncs, and history. In the steps below, Workspace A moves from its previous SyncBot instance to this one. The previous instance does not go away. It keeps running its other Workspaces, and once step 5 connects the two instances, each one shows on the other's Home tab as an **External Connection**. In this example, that connection is named `Partner Org`.

You do the work from **Data Migration**. Workspace A's Slack admins find it in the SyncBot Configuration section of the Home tab.

For a while, Workspace A has SyncBot installed from both instances. The previous instance keeps syncing it until you uninstall that app in the last step. Then this instance takes over.

### Steps

1. **Update the instance you are moving to.** An older export can still be imported. Confirm Import warns you when that file has no signature SyncBot can check, if `ENABLE_UNSIGNED_IMPORT=true` is set. A file that was changed after it was signed is refused.
2. **Keep the previous instance's SyncBot installed in Workspace A.** You need it to export, and you remove it in the last step.
3. **Install SyncBot in Workspace A from this instance.** A Slack admin of Workspace A uses this instance's install link.
4. **Turn on Federation** in **Settings** on both instances. A primary Workspace admin does this.
5. **Connect the two instances.**
   - A primary Workspace admin on one instance chooses **Create Connection** and shares the code.
   - A primary Workspace admin on the other instance chooses **Join** and pastes the code.
   - Each of them picks the Workspaces their side shares, and both lists include Workspace A.

   The previous instance now shows on this instance's Home tab as the `Partner Org` External Connection. Wait a few minutes for the two instances to catch up with each other.
6. **Export from the previous instance.** In Workspace A, open the previous instance's SyncBot app, go to **Data Migration**, and choose **Export**. SyncBot sends you the file in a DM.
7. **Import here.** In Workspace A, open this instance's SyncBot app, go to **Data Migration**, and upload the file. Look over **Confirm Import**, then choose **Import**.
8. **Uninstall the previous instance's SyncBot from Workspace A.** In Slack, remove that app from the Workspace with **Remove App**. One person's **Revoke** under Authorize does not do this. Workspace A's Syncs then start here on their own, with no second import.

### Do this in this order

- **Export before you uninstall.** Once the previous instance's SyncBot is gone from Workspace A, there is nothing left to export.
- **Connect before you import when the other Workspaces in the file should come along.** Without a connection, Import writes only this Workspace. Connect, then import the same file, when those other Workspaces should come along.
- **Uninstall last.** If you uninstall after exporting but before importing, the move still works. The Syncs just start right away, without the `Waiting for migration` stage, so you get no chance to check them first.

### What Export puts in the file

The file holds Workspace A's Groups, Syncs, and Channels, the message history that keeps threads and reactions connected, and its people and their mappings. For a private Channel, it can include the Channel and who added SyncBot to it, so treat the file as private.

The file does not include:

- Tokens or passwords, so it cannot be used to act as Workspace A.
- Groups that were disbanded, or that Workspace A has left.

Do not edit the file. SyncBot can tell where a file came from and whether it was changed, and it refuses one that was edited.

### What Confirm Import shows

- Where the file came from (here, the `Partner Org` External Connection).
- Each Group in the file, with Workspace A's Channels in it, how many people are mapped, and how many messages are synced.
- Anything that will be left out, and why.

After you choose **Import**, SyncBot sends you the result in a DM. You can import the same file again later to pick up anything that was left out.

### What you should see

Right after the import, Workspace A's Syncs show `Waiting for migration`. They wait until you uninstall the previous instance's SyncBot from Workspace A, so the two instances never both post the same messages.

Once you do, the Syncs start on their own:

- SyncBot joins the public Channels and posts a short notice that they have resumed.
- Private Channels stay paused, with a notice, until a manager who is in the Channel authorizes SyncBot and resumes it from the Home tab.
- Workspace A keeps syncing with the Workspaces in its Groups that are still on the previous instance, through the `Partner Org` connection.

If the file came from this same instance, for example to put back a Workspace you removed, the Syncs start right away. The exception is when another connected instance still has SyncBot installed in Workspace A.

### When something is left out or refused

- **The file was changed.** Export a fresh copy from the instance that made it.
- **The file has no signature SyncBot can check.** It usually came from a version before 1.7.3. SyncBot refuses it until `ENABLE_UNSIGNED_IMPORT=true` is set and deployed. Confirm Import then warns you, and only this Workspace is written. A signature that does not match is a changed file, and that file is refused.
- **A Workspace in the file is not on this instance yet.** Its Channels are left out. Import the file again once it is here.
- **A Group is already here without Workspace A.** Import does not add Workspace A to a Group it is not already in. Ask an Owner of that Group to invite Workspace A, then import again. Sharing Workspace A on both sides in step 5 usually prevents this.
- **SyncBot could not reach the previous instance.** The Syncs keep waiting. Try the import again in a few minutes.
- **The previous instance did not accept SyncBot's check.** Ask a primary Workspace admin to look at that External Connection, then import again.
- **The previous instance has been shut down.** The move cannot finish, so Workspace A starts fresh on this instance.

Import also follows these rules:

- A Group that is new to this instance starts with just Workspace A. Its other members show up as the two instances catch up.
- Other Workspaces on this instance are not touched.
- People are matched up only when one is in Workspace A and the other is on a connected instance.

### Moving without a connection

You can import a file from an instance this one is not connected to, including when Federation is off.

A signed file carries that instance's fingerprint. Settings on every Workspace shows this instance's Fingerprint. Confirm Import shows the fingerprint from the file. They match when the file was signed on that instance and has not been changed. SyncBot can tell that a signed file was not edited, but it cannot tell who signed it unless that instance is connected here, so only import a file you exported.

A file from SyncBot before 1.7.3 cannot be checked, because it does not carry the key that signed it. SyncBot refuses it until whoever runs this deployment sets `ENABLE_UNSIGNED_IMPORT=true` and deploys again. Confirm Import then warns that the file may have been changed. You can import it, and only this Workspace is written. When the move is done, set it back to `false` and deploy again. If `--setup-github` copied it to GitHub, change it there too. A file that has a signature which does not match is refused, because that file was changed after it was signed.

That import brings over only this Workspace's Groups, Syncs, Channels, and message history. In a Group that already exists here, this Workspace keeps its role; the file cannot make it an owner. Other Workspaces in the file are left out. They join the Groups here with an invite code. When two Workspaces move, the first one imports, the second joins each Group, and then the second imports. Uninstall SyncBot from this Workspace on the old instance first, or every message posts twice.

If an import fails, nothing from the file is kept. Fix the file or export it again, then import again.

## A Workspace that is still installed on both instances

Between steps 3 and 8, managers of Workspaces that share a Group with Workspace A get one message saying SyncBot was installed from another instance. During a move, that is expected.

If you leave an External Connection, the Workspaces on the other side are set aside, not deleted. They stay that way for the retention period in **Settings**, and reconnecting within that time brings them back with their history.

For how this works under the hood, see [Architecture](ARCHITECTURE.md).
