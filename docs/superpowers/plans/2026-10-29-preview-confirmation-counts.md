# Preview Confirmation Counts Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement this plan.

**Goal:** A valid deletion preview needs one confirmation even when identical files have changed dates.

**Architecture:** `jobs.prepare` records the count used by the preliminary modification check for dry runs too. The engine carries it into the kept preview and uses that snapshot when confirming.

**Tech Stack:** Python, GLib, unittest, rclone 1.75.2.

**Spec:** `docs/superpowers/specs/2026-10-29-preview-confirmation-counts-design.md`

## Global Constraints

- Preserve deletion limits and the 2% confirmation margin.
- Keep filesystem scans off the main loop.
- Do not change user files, installed application, or synchronization settings.

## Review Focus

- Same content with different dates: one confirmation completes the reviewed changes.
- New real changes after a preview: confirmation cannot silently authorize them.
- First alignment without prior listings: count defaults to zero.
- A local-change safety stop: existing confirmation behavior remains valid.
- Stale previews: existing rejection remains valid.

## Task 1: Carry the preliminary count with the preview

**Files:** `src/spola/jobs.py`, `src/spola/engine.py`, `src/spola/rclone.py`, `tests/test_engine.py`.

**Interfaces:** Add a default-zero integer count to `Job` and `Preview`; `confirmed_max_changes` retains its signature.

- [x] Add an integration regression combining remote deletions with identical files whose dates changed, and require a single confirmation to finish.
- [x] Add a regression for further real changes after the preview that still require confirmation.
- [x] Verify that the original-symptom regression fails and the later-change protection already passes.
- [x] Capture the count in preparation, propagate it to the kept preview, and consume it in confirmation.
- [x] Run the regressions, existing confirmation tests, full unittest suite, and `ruff check --select F,E9,B .`.
- [x] Review the final change and commit the verified fix on its dedicated branch.

## Verification

- The original implementation fails the new single-confirmation regression
  with `10 of 30 files changed on this computer at once`.
- Both new regressions and four existing confirmation tests pass with the fix.
- Full suite with Python 3.15 and rclone 1.75.2: 342 tests in 399.852s,
  OK, including 20 Windows-only skips (322 passed).
- `ruff check --select F,E9,B .` and `git diff --check` pass.
- Independent code review found no issues.
