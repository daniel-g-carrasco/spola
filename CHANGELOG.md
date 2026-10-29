# Changelog

What changed in Spola, version by version, the newest first. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions
are still `0.x`, development releases, published as Flatpak updates from
Spola's repository and, since 0.3.11, as Windows installers. The same notes,
shorter, are in the app's metainfo, which software centres show.

## [Unreleased]

## [0.3.35] - 2026-10-29

### Added
- A **Share…** window from the desktop file manager, on Linux and Windows:
  see who has access, invite people by email, change view/edit/custom
  permissions, and remove access. Permissions inherited from parent folders
  show their source and are managed there.
- Create, copy and revoke public links; manage passwords, expiration with a
  calendar and time selector, and the download button. The window explains
  whether a link needs a password or account and shows Koofr plan restrictions.

### Changed
- Opening sharing or copying an existing link never creates public access
  or resets its settings. Older menu commands open the same window; legacy
  D-Bus link retrieval only returns an existing active, unprotected link.
- Sharing changes reload the settings from Koofr, including after partial
  failures. Incomplete access information leaves changes unavailable until
  a successful reload. Owners and inherited members cannot be edited here.
- Sharing respects ordered folder exclusions. Advanced rclone filter syntax
  that cannot be checked locally redirects users to sharing on Koofr.
- Complete Italian translations for the new sharing controls.

## [0.3.34] - 2026-10-29

### Fixed
- Confirming a deletion preview no longer asks for confirmation again when
  identical files have different modification dates. The confirmation uses
  the preliminary count captured with the preview; later changes still
  need confirmation.
- Windows: Preferences opens at a manageable height and can be resized,
  with the settings accessible by scrolling.
- The conflicts window, Resolve All, Preferences and the notification of a
  folder's rule still said that the version not kept goes to the
  wastebasket: since 0.3.27 it stays among the folder's kept copies, where
  Previous Versions finds it.
- Translations: the name of a restored copy ("report (version of …).odt")
  and of the conflict copies could drop out of the Italian catalogue when
  it was refreshed.

### Changed
- Both Koofr trash dialogs show search progress and let you stop and
  continue. A failed page resumes where it stopped, keeping the entries
  already read; closing the last interested dialog stops further requests.
- Slow trash pages use a shorter timeout and show when they are retried.
  Incomplete searches are clearly marked; restoring a whole deletion group
  waits until the search is complete.

## [0.3.33] - 2026-10-20

### Fixed
- The window no longer stops for seconds with big folders: the lists of a
  preview, restoring a kept copy or opening one, the backups a shorter
  retention would delete, the troubleshooting text of "About", "Recently
  Deleted Files" while Koofr's trash is read, and a folder's page during a
  sync. On Linux, the folders to watch are found without stopping it.
- Windows: Add Folder offers "Same place as on this computer" on Koofr,
  which it never did on Windows; the window shows the home folder as "~"
  and groups the synced folders by where they are.
- Linux: a file whose name is not UTF-8 no longer takes away the emblems
  of every file in its folder.
- The kept copies said that Koofr keeps no older versions: it does (120
  days on free accounts, up to two years on paid ones), also of the files
  Spola replaces there.

### Changed
- No sync of a folder starts while a kept copy is put back in it.

## [0.3.32] - 2026-10-20

### Fixed
- Windows: when a synced folder went away and came back (a drive unplugged
  and plugged in again, a folder renamed and renamed back), Spola no longer
  saw its changes until it was restarted, and synced it only at its
  periodic checks. It now watches the folder again as soon as it is back.
- "Later" in the update window, chosen just as the download ended, still
  started the installer.
- A verification whose window was closed while it ran never said that
  everything matched.
- Closing Preferences right after changing how many days backups are kept
  could lose the change.
- The menu of a conflict could close by itself while the folder synced.
- Signing out while Koofr's trash was being read could still show the
  trash of the account just left.
- Linux: copying a Koofr link from the command line ended with an error,
  after copying the link.
- A file being written, with a line break in its name (possible on Linux),
  could keep other files out of that run.
- Folders that Windows links elsewhere (junctions) are no longer looked
  into: rclone does not sync them, and Spola counted their files.

### Changed
- The fragments rclone leaves next to a file when a download is cut short
  (a crash, the power gone), named "….partial", are removed after a day.
- What goes wrong is said instead of passing in silence: the password that
  cannot be read at the start, File Explorer's entries or the file manager
  extension that cannot be written, exclusions that cannot be saved (the
  dialog stays, with the text typed), a password that Sign Out cannot
  remove from the keyring.
- The exclusions are saved whole or not at all.

### Security
- The Koofr app password follows a redirect of Koofr's server only to
  Koofr itself, over https.
- An update downloads no more than the release says, and reads at most
  1 MB of GitHub's description of it.
- Windows: Spola runs only the rclone it ships, whatever the environment
  says (SPOLA_RCLONE, the PATH).
- Windows: an update removes the previous version's libraries before it
  installs the new ones.
- The CI's actions are pinned to exact commits.
- The command line takes a folder's lock before it forgets the folder, and
  says when it cannot tell whether Spola is running.

Not done, from the same review: copies on Koofr of what a first alignment
or a repair replaces there. Koofr keeps the previous versions of a file
itself (120 days on free accounts, two years on paid ones, up to five
versions, files up to 5 GB) and deleted files go to its trash; and rclone's
way of keeping such copies moves the files to replace away first, so that
for as long as the run lasts they are missing on Koofr, which another
computer syncing the same folder would take for deletions.

## [0.3.31] - 2026-10-20

### Fixed
- Two identical files moved in the same run (copies of a document in a
  reorganised folder) could each arrive with the date of the other, and
  the next run took them for changed and uploaded them again (737 files on
  2026-10-19). A move is now matched by content and date.
- A confirmation could go in circles: when files had gone from both sides
  (deleted on the web and here, or by two computers), or had changed the
  same way on both, the confirmed run stopped again at every confirmation.
  The confirmed run now allows what the safety check counted, and no more.
- After a run that made conflicts, the folder's status said "up to date"
  until the next run.
- The window froze for a moment 60 seconds after the start, and once a
  day, while old backups were removed: that now happens in the background.
- Restoring a previous version while the folder was syncing could fail on
  Windows (the file was open for the sync): Spola now says to wait for the
  sync to finish, as it does for conflicts.

### Changed
- A safety stop gives its reason in Spola's words ("20 of 40 files are gone
  from this computer at once") instead of rclone's line.

### Security
- Windows: an update installs only the installer of the version it
  announces (a tampered answer could have offered an older, duly signed
  one); the verified installer stays open until Spola exits, so that
  nothing else can replace it before Setup reads it; an installation for
  this user only is updated with /CURRENTUSER.
- The test jobs of the CI get read-only tokens and keep no credentials, and
  the rclone they run is checked against its SHA-256 first.
- Windows: "Open a Copy" of a previous version no longer runs a program, a
  script or a shortcut (a kept copy may come from anyone who shares the
  folder on Koofr): it shows the copy in its folder.

These come from an independent review of 0.3.30 (data integrity, security,
redundancy, speed, code), which found no path that loses or corrupts a file.

## [0.3.30] - 2026-10-19

### Fixed
- Windows: Spola could stay "Paused, offline" with the network working: the
  network monitor it relied on stopped at "no network" after a change of
  network. Spola now asks Windows itself, as the taskbar's network icon
  does.
- Windows: the icon in the title bar was a blurred smudge. Each window now
  gets Spola's icon at the size the title bar draws, from a version of the
  icon made for small sizes; the dialogs also get a dark title bar with the
  dark style.
- Windows: a folder's status icon in the window was the negative of the one
  in the notification area with the dark style; the window shows the same
  coloured badges.

### Changed
- Windows: the text is a size larger (10.5 points instead of 9, following
  the text size set in Windows): at 100% the smaller labels were hard to
  read.

### Added
- Windows: "Previous Versions…" on the files and "Recently Deleted Files…"
  on the folders of the synced folders, in File Explorer's menu, as in the
  Linux file managers. On Windows the first is on every file: Explorer
  cannot tell which have a kept copy, and the window says when there is
  none.
- Windows: an update runs the installer only with Spola's own signature
  (minisign), besides the SHA-256 GitHub gives: an installer replaced in
  the release is refused. The releases carry a `.minisig` for the installer
  and for the portable zip.

### Fixed
- Windows: each folder inside a synced folder had a change monitor of its
  own, and on Windows each holds half a megabyte: 1.5 GB of memory for a
  synced folder of 2,700 subfolders. One monitor now watches the whole
  tree, with no limit on the folders watched; and changes deep in the tree
  are matched against the exclusions as on Linux (their paths had "\").
- Windows: a sleep that comes regardless of the keep-awake request (the lid
  closed, the battery low) now stops the sync in progress gracefully, so
  that rclone saves its state, and the sync goes again on waking, as on
  Linux.
- Windows: for older versions of one's own edits, Spola points to Windows'
  File History, not to Déjà Dup.
- Windows: starting at login could not be turned on in a user profile that
  had never had a program started at login (no Run key yet).

## [0.3.28] - 2026-10-18

### Fixed
- "Recently Deleted Files" waited for the whole of Koofr's trash before
  showing anything (Koofr gives the trash of the whole account, not of one
  folder: 87 pages, close to 80 seconds for 87,000 files), and a single
  page that timed out made the whole reading fail. The files of the folder
  now show page by page, the newest deletions first, with how much of the
  trash has been read; a page that times out is asked again; what was read
  stays if one keeps failing.

### Changed
- Koofr's trash, once read, serves every window for five minutes (opening
  the dialog again, another folder, Restore from Koofr's Trash), and is read
  again after a restore or a sync that deleted on Koofr.

## [0.3.27] - 2026-10-18

### Added
- Previous versions, built on the copies Spola already keeps of what a sync
  replaces or deletes here. In Files, Nemo and Caja, "Previous Versions…"
  on a file that has kept copies, and "Recently Deleted Files…" on a
  folder; in Dolphin and Thunar the same entries in the Koofr submenu.
  Spola's window lists each copy with its date, size, reason and the day
  it expires; a copy opens read-only, comes back as a copy next to the file
  ("report (version of 2026-10-17 12.30).odt", in the language of the
  conflict copies) or replaces the file, which is kept among the copies
  first, so a replace can be undone. Recently deleted files also lists
  what is in Koofr's trash for that folder, with the day it leaves it. The
  window says before every restore that the next sync uploads it.
- A "Kept Copies" section on each folder's page: how many, how much space,
  since when, and the full list with a search by name.
- Each run's copies carry a small manifest with the reason of each file,
  written whole or not at all and deleted with its run by the retention.
- D-Bus interface `Sync1` version 2 (GetFolderVersions, ListVersions,
  ListDeleted, RestoreVersion, ShowVersions, ShowDeleted, VersionsChanged);
  version 1 methods unchanged, older extensions keep working.

### Changed
- The copy not kept when a conflict is resolved goes among the folder's
  kept copies, with its reason, instead of the system's wastebasket, and
  expires with the same rule. On Koofr it still goes to Koofr's trash.
- **Removed: the "Delete This Backup" and "Delete All" buttons of the local
  backups (0.3.24).** A kept copy is now deleted only by the retention;
  lowering the days in Preferences frees space at once, after saying how
  much.
- The icons follow the common style of my logos: the app icon is a disk with
  a vertical gradient lit from above (ivory on dark backgrounds, dark on
  light ones) and the motif in the complementary gradient; the trails are
  filled shapes joined to the balls. The status icons in colour have the
  disk in a gradient of its colour, the glyphs plain ivory; all glyphs are thinner, with sharp inner
  corners, and the warning triangle has rounded corners. The Files emblem
  for "pending" is a full disk with the clock hands cut out, like the
  other states; the bar of "excluded" is as thick as its ring. Tray, window, Files
  emblems and Windows' notification area all use the new glyphs.

### Added
- Noctalia plugin: the middle click on the bar widget pauses and resumes
  every folder; without Spola installed the widget hides and the panel says
  where to get it.
- This changelog.

## [0.3.26] - 2026-10-17

### Changed
- A folder whose two sides already match no longer waits for a confirmation
  of its first alignment: the preview Spola prepares on its own finds
  nothing to copy and the alignment goes on by itself. With anything to
  copy, the preview still asks.

## [0.3.25] - 2026-10-17

### Added
- A status file (`status.json` in Spola's state folder) kept current with
  what Spola is doing, every folder's status and progress, for the
  desktop's own widgets.
- A plugin for the Noctalia shell, in `noctalia/spola`: a cloud in the bar
  with the sync status, a panel with every folder and its progress, a
  control-centre tile that pauses and resumes, launcher results behind
  `/spola`.
- A preference to switch off the icon in the notification area when such a
  widget takes its place.

## [0.3.24] - 2026-10-17

Includes what was prepared as 0.3.23, whose tag was withdrawn before
anything was published.

### Added
- Verify both sides: a folder's page compares every file here and on Koofr
  by size and checksum, changing nothing, and shows what is only here, only
  on Koofr, different or not comparable, each as a tree. Every confirmed
  alignment is followed by a verification of its own. On the command line:
  `spola verify`.
- Local backups on the folder's page: the backups of past syncs with their
  size, each opened as a tree, a file put back in its place (the one there
  now kept first).
- After every successful sync Spola keeps a copy of the synced state it
  reached: a repair can go back to that state and sync normally from there,
  with every safety check, instead of a new alignment.

### Fixed
- The window no longer freezes while Spola reads the record of a large
  folder, at start and after each of its syncs (close to a second for
  40,000 files, now off the main loop).
- A damaged record of the last synced state stopped Spola from starting,
  or was retried every minute for ever: the folder now asks for a repair.

## [0.3.22] - 2026-10-17

### Added
- Troubleshooting page in the About window: versions, paths, every
  folder's state and last outcome, the room logs, listings and backups
  take, with a Save button. A folder's page opens the log of its last sync.

### Changed
- Settings survive accidents: every save keeps the previous file next to
  it; a damaged settings file is set aside and the previous save used
  (Spola says so at start); a file written by a newer Spola is read for
  what this version knows and written back whole; a save that fails is
  said instead of swallowed.
- Logs leave out the statistics lines past 50 MB; older logs are
  compressed, the newest kept readable.

### Fixed
- A nearly full disk stops a sync before it starts, with a clear message:
  the folder's disk must hold what a preview says comes down, with a
  margin, and the disk of Spola's own files must not be nearly full.

## [0.3.21] - 2026-10-17

### Changed
- The share of a folder a sync may delete unasked (10% by default) also has
  a cap of about 500 files: a folder of 40,000 files no longer allows 4,000
  deletions without a word. A confirmed sync still deletes what its preview
  showed.
- A sync that would upload many files changed on this computer at once (a
  snapshot put back, a program rewriting a whole library) stops before it
  starts and waits for a look at the preview; the threshold is the folder's
  percentage, between 50 and 500 files. A confirmed sync may upload what
  its preview showed and little more. The command line refuses the same
  way and says how to confirm.

## [0.3.20] - 2026-10-17

### Changed
- Lowering the backup retention asks first, saying how many backups and how
  much space would go.
- A restore from Koofr's trash puts back the dates only of files still the
  same here, by date and by Koofr's checksum.

### Fixed
- A sync asked for while one ran (a change on Koofr, a retry) waited for
  the hourly check.
- A sync interrupted by a crash showed an rclone message in red; it now
  says "a previous sync was interrupted: trying again in 3 min".
- A run that could not start went back to "up to date"; it stays an error
  until one does, and is tried again.
- Pausing a folder did not cancel a sync still being prepared; stopping a
  confirmed sync while prepared left the folder "up to date" instead of
  waiting for its confirmation.
- Exclusions changed during a sync now lead to a repair in Spola's words.
- A lost Koofr activity feed falls back to the periodic check whatever
  failed.
- A file name with a quote or a backslash showed as a quoted full path in
  previews; a preview rclone counted but whose lines could not be read
  showed "nothing to do" and is now refused.
- The folder monitor watched the folder's own exclusions; a folder too
  large to watch entirely now says so.
- Long syncs kept one line of statistics a second in memory.

## [0.3.19] - 2026-10-17

### Added
- The computer stays awake while a sync runs, on every Linux desktop
  (through logind, lid closed or not; a switch in Preferences turns it
  off), and a sleep that comes anyway stops the sync gracefully and starts
  it again after the wake.

### Changed
- A confirmation is worth what its preview showed, no more: a confirmed
  sync may delete the share of files the preview listed, with a small
  margin; if more would go, it stops and asks again.
- A preview overtaken by a change (here or on Koofr) can no longer be
  confirmed: the dialog says so and prepares it again.
- Nothing is published to the Flatpak repository, nor packaged for
  Windows, without the tests; the dialog tests run in the build too.

### Fixed
- An unexpected failure while a run was prepared, followed or read could
  leave a folder stuck in "Preparing…" or Spola unable to quit.
- Quitting waits for a preview being prepared too, and ends a stuck rclone
  after two minutes.

## [0.3.18] - 2026-10-17

### Changed
- A folder deleted or emptied on Koofr, or emptied here, no longer shows as
  "needs a repair": Spola says what happened and offers what helps: restore
  it from Koofr's trash, upload this computer's files again (after a
  preview), copy Koofr's files here again, or stop syncing it. The files on
  the other side are never touched meanwhile.

### Fixed
- A banner title with an apostrophe or an ampersand showed its markup.

## [0.3.17] - 2026-10-16

### Added
- A preview says how much of each folder a change touches ("the whole
  folder", "31% of the folder"), under every folder of the tree.

## [0.3.16] - 2026-10-16

### Changed
- Large previews are easy to read: each kind of change opens on a page of
  its own as a tree of folders, each with how many files it holds and how
  big they are; no file is left out of the list any more.

## [0.3.15] - 2026-10-16

### Fixed
- Windows: on a monitor whose scale differs from the main one, the window
  is scaled at the size of the other apps.

## [0.3.14] - 2026-10-16

### Changed
- Files still being written (a download, an export) wait before they are
  synced, at most five minutes, saying which file they wait for; synced
  half written, they looked changed on both sides at the next sync. Sync
  Now never waits.

### Fixed
- Windows: on a monitor with a fractional scale (125%, 150%, 250%), the text
  is as large as in the other apps.

## [0.3.13] - 2026-10-16

### Changed
- The tray icon is always in the panel's own colour, like its other icons;
  its shape says what is going on. The window and the notifications keep
  the coloured status.

## [0.3.12] - 2026-10-16

### Changed
- Windows: the window keeps the system title bar, so it snaps like any
  other. File Explorer shows a folder's status as soon as it is added, each
  file turning green when copied, never on a file changed since; the
  status can be turned off in Preferences.
- Removing a folder removes its state, never its backups; backups of
  removed folders expire as the others do.

### Fixed
- Stop, then Pause, Pause All or Quit ended rclone at once, and the next
  sync needed a repair.
- Two folders empty at their first alignment could settle a file new on
  both sides by date, without a preview.
- A folder removed while its sync was being prepared left its state behind.

### Security
- Windows updates: the installer is downloaded only from Spola's releases,
  with its checksum checked; Setup closes only Spola's own programs.

## [0.3.11] - 2026-10-15

### Added
- Spola for Windows, a first version: an installer and a portable zip, the
  status in the notification area and in File Explorer, Koofr's links from
  File Explorer, updates from the releases.
- Folders can be taken over from Koofr Desktop in the window, on Linux and
  on Windows.

### Fixed
- When many files changed only in their dates, a sync stopped half way
  could leave some of them missing on Koofr for a while: the copy on Koofr
  was deleted well before its new upload. Nothing was ever deleted on this
  computer.

## [0.3.10] - 2026-10-14

### Added
- Restore from Koofr's trash, on a folder's page: what was deleted on Koofr
  in that folder, grouped by when; the deletion you choose comes back,
  never the whole trash, with its empty folders and its files' dates.

## [0.3.9] - 2026-10-14

### Fixed
- A file or folder renamed here by case alone was not renamed on Koofr,
  which does not tell the two names apart; files came back in a second
  folder and the folder stopped for a repair. Spola now renames it on
  Koofr first, and the preview shows one move.

## [0.3.8] - 2026-10-14

### Added
- When less than 10% of the space on Koofr is left, the banner says how
  much remains.

### Changed
- A folder confirmed while another syncs says "Confirmed, starts after the
  sync in progress"; confirmed runs go in the order given, before every
  automatic one.
- A preview left running in the background shows in the banner, then says
  it is ready.

## [0.3.7] - 2026-10-13

### Fixed
- A sync looked stuck while it deleted files on Koofr one at a time; the
  status now counts them and then says when empty folders are removed.

## [0.3.6] - 2026-10-13

### Fixed
- After a big reorganisation the preview counted thousands of conflicts
  that do not exist; only files whose two copies are renamed are counted.
- Some Koofr paths kept a prefix like `koofr{kFw_7}:`, counting moves also
  as uploads and deletions.

## [0.3.5] - 2026-10-13

### Fixed
- A click on "Open Spola" in the tray menu could quit Spola, when the menu
  changed under the click during a sync.

## [0.3.4] - 2026-10-13

### Changed
- Previews run next to the syncs of the other folders, can be closed and
  reopened or cancelled, show their progress, say how much goes each way
  and warn when Koofr has no room. A folder waiting for you has its preview
  prepared ahead (not on a metered connection).
- What waits for you is in a banner under the header bar; the status at the
  top keeps saying what Spola is doing.

### Fixed
- Large previews froze the window for a moment.

## [0.3.3] - 2026-10-13

### Changed
- A move stays a move: a file renamed or moved on one side is moved the
  same way on the other, on Koofr's server, instead of deleted and copied
  again.

### Fixed
- After a file was renamed on Koofr, the next sync uploaded it again.

## [0.3.2] - 2026-10-12

### Added
- Resolve All for conflicts, after showing what happens to each file, and
  a rule to resolve new conflicts on their own, for all folders or for one.
  The version not kept goes to the wastebasket here and to Koofr's trash.
- Suggested names Koofr accepts for the files it refuses, applied once you
  confirm.
- A click on a file in a preview shows it in the file manager, or on Koofr.

## [0.3.1] - 2026-10-12

### Added
- The files being synced under the status, each with what is happening to
  it and its progress; the progress in the tray's tooltip and menu.

### Changed
- Spola no longer syncs while Koofr Desktop runs on the same computer or is
  set to start at login: the two engines on the same folders deleted a file
  on Koofr.
- A sync stops when the network becomes metered (if Spola pauses there)
  and starts again when it no longer is.
- The Flatpak follows the GTK 4 colours set by the desktop.

### Fixed
- Align and Review froze the window for a few seconds on large folders.

## [0.3.0] - 2026-10-12

### Added
- Emblems and the Koofr menu in Nemo and Caja; the Koofr menu in Dolphin and
  Thunar; overlay icons in Dolphin with a small plugin built on your system.

## [0.2.1] - 2026-10-11

### Fixed
- A change in a folder while its preview ran could queue a normal sync on a
  folder waiting for a repair.

## [0.2.0] - 2026-10-11

### Changed
- A new window after the GNOME guidelines: folders grouped by place, the
  overall status at the top, a page for each folder. Adding a folder
  suggests the matching one on Koofr.

### Fixed
- Files showed no emblems after Spola restarted; the Flatpak installed the
  Files extension where Files does not look.

## [0.1.1] - 2026-10-11

### Fixed
- A hidden marker file tells a synced folder from an empty one in its place
  (ext4 reuses inode numbers).

## [0.1.0] - 2026-10-10

### Added
- First development version: one or more folder pairs, two-way sync with
  previews and safety stops, tray icon.

[Unreleased]: https://github.com/daniel-g-carrasco/spola/compare/v0.3.35...main
[0.3.35]: https://github.com/daniel-g-carrasco/spola/releases/tag/v0.3.35
[0.3.34]: https://github.com/daniel-g-carrasco/spola/releases/tag/v0.3.34
[0.3.33]: https://github.com/daniel-g-carrasco/spola/releases/tag/v0.3.33
[0.3.32]: https://github.com/daniel-g-carrasco/spola/releases/tag/v0.3.32
[0.3.31]: https://github.com/daniel-g-carrasco/spola/releases/tag/v0.3.31
[0.3.30]: https://github.com/daniel-g-carrasco/spola/releases/tag/v0.3.30
[0.3.29]: https://github.com/daniel-g-carrasco/spola/releases/tag/v0.3.29
[0.3.28]: https://github.com/daniel-g-carrasco/spola/releases/tag/v0.3.28
[0.3.27]: https://github.com/daniel-g-carrasco/spola/releases/tag/v0.3.27
[0.3.26]: https://github.com/daniel-g-carrasco/spola/releases/tag/v0.3.26
[0.3.25]: https://github.com/daniel-g-carrasco/spola/releases/tag/v0.3.25
[0.3.24]: https://github.com/daniel-g-carrasco/spola/releases/tag/v0.3.24
[0.3.22]: https://github.com/daniel-g-carrasco/spola/releases/tag/v0.3.22
[0.3.21]: https://github.com/daniel-g-carrasco/spola/releases/tag/v0.3.21
[0.3.20]: https://github.com/daniel-g-carrasco/spola/releases/tag/v0.3.20
[0.3.19]: https://github.com/daniel-g-carrasco/spola/releases/tag/v0.3.19
[0.3.18]: https://github.com/daniel-g-carrasco/spola/releases/tag/v0.3.18
[0.3.17]: https://github.com/daniel-g-carrasco/spola/releases/tag/v0.3.17
[0.3.16]: https://github.com/daniel-g-carrasco/spola/releases/tag/v0.3.16
[0.3.15]: https://github.com/daniel-g-carrasco/spola/releases/tag/v0.3.15
[0.3.14]: https://github.com/daniel-g-carrasco/spola/releases/tag/v0.3.14
[0.3.13]: https://github.com/daniel-g-carrasco/spola/releases/tag/v0.3.13
[0.3.12]: https://github.com/daniel-g-carrasco/spola/releases/tag/v0.3.12
[0.3.11]: https://github.com/daniel-g-carrasco/spola/releases/tag/v0.3.11
[0.3.10]: https://github.com/daniel-g-carrasco/spola/releases/tag/v0.3.10
[0.3.9]: https://github.com/daniel-g-carrasco/spola/releases/tag/v0.3.9
[0.3.8]: https://github.com/daniel-g-carrasco/spola/releases/tag/v0.3.8
[0.3.7]: https://github.com/daniel-g-carrasco/spola/releases/tag/v0.3.7
[0.3.6]: https://github.com/daniel-g-carrasco/spola/releases/tag/v0.3.6
[0.3.5]: https://github.com/daniel-g-carrasco/spola/releases/tag/v0.3.5
[0.3.4]: https://github.com/daniel-g-carrasco/spola/releases/tag/v0.3.4
[0.3.3]: https://github.com/daniel-g-carrasco/spola/releases/tag/v0.3.3
[0.3.2]: https://github.com/daniel-g-carrasco/spola/releases/tag/v0.3.2
[0.3.1]: https://github.com/daniel-g-carrasco/spola/releases/tag/v0.3.1
[0.3.0]: https://github.com/daniel-g-carrasco/spola/releases/tag/v0.3.0
[0.2.1]: https://github.com/daniel-g-carrasco/spola/releases/tag/v0.2.1
[0.2.0]: https://github.com/daniel-g-carrasco/spola/releases/tag/v0.2.0
[0.1.1]: https://github.com/daniel-g-carrasco/spola/releases/tag/v0.1.1
[0.1.0]: https://github.com/daniel-g-carrasco/spola/releases/tag/v0.1.0
