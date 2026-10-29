# Confirm a preview once

A deletion review can finish with no uploads, while Spola's preliminary
size/date check counts existing files as changed. The GUI derives a limit
of two changes from that plan and stops the confirmed run again. The
observed case had 154 identical files with different modification times.

The preview must carry the count obtained by the same preliminary check
that the real run uses. Confirmation uses that count together with the
plan to derive its existing bounded allowance. Changes after the preview
remain subject to the bound and preview invalidation.

## Approach

Capture the count in `jobs.prepare`, where the previous listing is already
loaded, and pass it with `Job` to `Preview`. This avoids a new scan on
the UI thread. Widening timestamp tolerance would change synchronization
semantics; bypassing the check would remove protection for later changes.

Scope: the confirmation count and regression tests. Keep deletion limits,
the 2% confirmation margin, rclone arguments, and file contents unchanged.
No dependency or deployment changes are required.
