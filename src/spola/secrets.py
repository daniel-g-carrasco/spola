"""The Koofr app password, kept in the keyring through libsecret.

Inside the Flatpak libsecret talks to the Secret portal on its own and keeps
an encrypted keyring file in the app's data folder; outside, it uses the
desktop's Secret Service (gnome-keyring). On Windows it is the Credential
Manager. Either way, never a plain file.
"""

from __future__ import annotations

from . import APP_ID, WINDOWS

if WINDOWS:
    from .win32 import credential_clear as clear, credential_lookup as _lookup, credential_store as store  # noqa: F401

    def lookup(user: str) -> str | None:
        return _lookup(user) if user else None
else:
    import gi

    gi.require_version("Secret", "1")
    from gi.repository import Secret

    SCHEMA = Secret.Schema.new(
        f"{APP_ID}.Account",
        Secret.SchemaFlags.NONE,
        {"user": Secret.SchemaAttributeType.STRING},
    )

    def store(user: str, password: str) -> None:
        Secret.password_store_sync(SCHEMA, {"user": user}, Secret.COLLECTION_DEFAULT,
                                   f"Spola: Koofr ({user})", password, None)

    def lookup(user: str) -> str | None:
        if not user:
            return None
        return Secret.password_lookup_sync(SCHEMA, {"user": user}, None)

    def clear(user: str) -> None:
        Secret.password_clear_sync(SCHEMA, {"user": user}, None)
