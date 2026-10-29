# Settings and Koofr trash responsiveness

Windows settings must open at a usable height and allow resizing. The
standalone libadwaita dialog currently requests its full natural height
and creates a non-resizable window. Bound the initial content height and
enable resizing on its standalone Windows root; preserve embedded dialogs.

Trash searches read the whole Koofr account. The restore dialog currently
waits for completion without progress, and a stalled page can take three
30-second attempts. Show the number of entries read and explain the wait.
Allow stopping and continuing a read, preserving its entries and cursor;
closing the last interested dialog stops further requests. Shared readers
must not cancel each other. Use a shorter timeout specifically for trash
pages, keep partial results after errors, and make retry available.

Keep a complete reading cached for five minutes. Invalidation and account
changes must prevent stale results being treated as complete or resumed.
An incomplete read must never be described as an empty or complete trash.
Restore groups stay unavailable until the complete search is available.

No deployment, real-file restoration, or user configuration changes.
Native Windows verification is unavailable on the Linux development host.
