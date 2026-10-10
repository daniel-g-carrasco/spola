"""The Koofr app password, kept in the keyring through libsecret.

Inside the Flatpak libsecret talks to the Secret portal on its own and keeps
an encrypted keyring file in the app's data folder; outside, it uses the
desktop's Secret Service (gnome-keyring). Either way, never a plain file.
"""

from __future__ import annotations

import gi

gi.require_version("Secret", "1")
from gi.repository import Secret  # noqa: E402

from . import APP_ID  # noqa: E402

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
