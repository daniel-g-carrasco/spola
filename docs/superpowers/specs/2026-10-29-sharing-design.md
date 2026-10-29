# Sharing from Spola

The user approved the sharing proposal on 2026-10-29. This implements its first delivery: a unified desktop sharing dialog with public links and named access. Upload requests and a global sharing inventory remain a later delivery.

## Behaviour

The file manager action is **Share…**, opening a native GTK/libadwaita dialog on Linux and Windows. Opening it reads Koofr state without creating a link or inviting anyone. Existing command line and D-Bus entry points remain understood; legacy link retrieval never silently creates public access.

The dialog separates **People with access** and **Access via link**. It names the item, shows loading/errors/retry, remains closable while requests run, bounds its opening size and allows scrolling/resizing on Windows. Network calls run off the GTK thread, with finite timeouts and no automatic retry of writes. Late replies after closing do not manipulate the dialog.

People can be invited explicitly by email, given viewing, editing or custom permissions, edited and removed. Owner access cannot be removed. Editing includes deleting files: the UI says so. Koofr permissions are authoritative. Files shared individually are read-only; permissions for a shared folder apply throughout that folder. Inherited access is labelled with its source and must never be silently changed through a child. Existing permissions and unknown permission keys survive unrelated edits.

Links are explicitly created and can be copied, password protected, assigned an expiry, and revoked. Copying never changes permissions or expiration. Passwords required by Koofr cannot be disabled; generated passwords are shown and separately copyable. Account/plan restrictions are surfaced using Koofr's response. The dialog describes anonymous access accurately, including expired links and the download-button setting. It must not imply hiding a button prevents saving content. No private credentials or link passwords enter logs.

Creating a link uses Koofr's defaults and immediately shows the returned settings; the API's separate password/validity updates cannot be represented as an atomic create-with-settings operation. Mutation errors trigger a fresh read before enabling copy again; ambiguous failures must not be presented as success. Revoking one link or removing one member does not remove other access routes; inherited routes remain visible.

## Implementation

Keep sync/rclone unchanged. Add `sharing.py` for Koofr API transport, access data and mutations, and `sharing_dialog.py` for UI. Wire desktop actions through `app.py` and the existing D-Bus service. Use the public Koofr OpenAPI schema at https://app.koofr.net/api/v2/swagger.json (API v2.1) and Koofr's official Java SDK as references. The app-password authentication is already used by Spola and listed by the schema.

No new runtime dependencies. English source strings and complete Italian translations. Existing GTK 4/libadwaita version floor and Windows packaging remain supported. Use fake HTTP responses and GTK tests for sharing mutations; do not invite real people or publish the user's files as part of testing.

## Validation

Prove read-only opening and copying, endpoint/body correctness, password and expiry preservation, plan/permission failures, owner and inherited-access protection, remote-only validation for unsynced files, late callbacks and partial writes, and context-menu routing. Run relevant regressions, full tests, lint, translation compilation and GUI smoke checks. Review the whole change before integration.
