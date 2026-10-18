# Changelog

What changed in Spola, version by version, the newest first. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions
are still `0.x`, development releases, published as Flatpak updates from
Spola's repository and, since 0.3.11, as Windows installers. The same notes,
shorter, are in the app's metainfo, which software centres show.

## [Unreleased]

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

[Unreleased]: https://github.com/daniel-g-carrasco/spola/compare/v0.3.27...main
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
