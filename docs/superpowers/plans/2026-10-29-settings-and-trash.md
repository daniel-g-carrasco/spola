# Settings and Trash Responsiveness Implementation Plan

> **For agentic workers:** Use systematic debugging and test-driven development. Independent settings and trash changes are being implemented in parallel.

**Goal:** Usable Windows settings and a trash search whose progress and interruption are under the user's control.

**Architecture:** Bound the settings dialog and enable resizing of its Windows root. Retain the shared background trash reader with cancellable subscriptions and resumable pagination; both trash dialogs expose progress, stop, and retry.

**Tech Stack:** Python, GTK 4/libadwaita, GLib, unittest.

**Spec:** `docs/superpowers/specs/2026-10-29-settings-and-trash-design.md`

## Constraints and Review Focus

- Preserve Linux embedded dialogs and Windows native window handling.
- Closing one of several listeners must leave the others running.
- Cancelled or signed-out workers must not deliver stale pages or retry.
- Failed reads keep their results/cursor; account changes and invalidation discard them.
- Partial results cannot authorize an apparently complete deletion group.
- Do not change installed Spola, sync configuration, or real user files.

## Task 1: Windows settings

Files: `src/spola/window.py` (PreferencesDialog), `tests/test_preferences_dialog.py`.

- [x] Reproduce the natural-height/non-resizable standalone dialog.
- [x] Add an observable GTK regression and implement bounded initial size with Windows resizing.
- [x] Run targeted GTK tests; report the native Windows verification limit.

## Task 2: Trash searches

Files: `src/spola/engine.py`, `src/spola/koofr_api.py`, `src/spola/trash_search.py`, trash dialogs in `src/spola/window.py`, related tests, `po/it.po`.

- [x] Add failing tests for unsubscribe/cancel, resume after a failed page, cached callback cancellation, and progress before completion.
- [x] Preserve page cursor/results in the shared reader, stop unobserved reads, and prevent stale delivery.
- [x] Use a 10-second trash-page timeout; keep other API operations unchanged.
- [x] Add progress, stop/continue/retry to the dialogs and translate the new text.
- [x] Exercise shared readers, account changes, incomplete reads, and retry with controlled API responses.

## Integration

- [x] Include new GTK modules in the explicit CI GTK job.
- [x] Run the full unittest suite and Ruff F/E9/B once after integration.
- [x] Review the resulting diff and commit the verified changes.

## Verification Notes

- Windows-path GTK reproduction requested 1,348 pixels and a non-resizable
  standalone window. Both regressions failed before the fix; all three
  Preferences tests now pass, including preservation of Linux embedding.
- Trash regressions failed before their fixes: progress hidden until the
  final page, unobserved reads continuing, cancelled cached delivery,
  retry restarting at page one, and a page limit reported as completion.
- Independent review found two final-page races: pausing between the last
  page and completion duplicated results on resume; an empty-string cursor
  could leave loading set forever. Both were reproduced and fixed. All ten
  trash-reader tests pass, and the reviewer approved the resulting change.
- Italian catalogue compiles and passes `msgfmt --check`.
- Full Linux suite: 360 tests in 401.499s, OK (20 Windows-only skips).
  Ruff F/E9/B and `git diff --check` pass.

## Release Follow-up

The user subsequently requested commit, push, and Linux/Windows releases.
Version 0.3.34 also includes the previous confirmation fix. Release metadata
and AppStream validation are prepared; the existing tag-triggered workflows
build, test, sign, and publish both packages and the Flatpak repository.
