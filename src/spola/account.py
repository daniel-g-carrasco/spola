"""Checking a Koofr account with rclone, and small read-only remote queries.

All of these only read from Koofr (quota, folder lists). They use the same
environment-only credentials as the sync runs.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field

from .i18n import _
from .paths import Paths
from .rclone import koofr_env, obscure, remote_spec

TIMEOUT = 60


class AccountError(Exception):
    pass


@dataclass
class Quota:
    total: int | None
    used: int | None
    free: int | None


@dataclass
class Credentials:
    """In memory only. `obscured` is for rclone's environment, `password` for
    Spola's own read-only API calls."""
    user: str
    obscured: str
    password: str = field(default="", repr=False)

    @classmethod
    def from_password(cls, user: str, password: str, rclone: str) -> "Credentials":
        return cls(user, obscure(password, rclone), password)


def _run(rclone: str, paths: Paths, creds: Credentials, *args: str) -> str:
    argv = [rclone, *args, "--config", str(paths.rclone_config), "--color", "NEVER",
            "--low-level-retries", "2", "--retries", "1"]
    try:
        proc = subprocess.run(argv, env=koofr_env(creds.user, creds.obscured), capture_output=True,
                              text=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        raise AccountError(_("Koofr did not answer in time.")) from None
    if proc.returncode != 0:
        if "401" in proc.stderr or "nauthorized" in proc.stderr:
            raise AccountError(_("Koofr refused the e-mail or the app password."))
        lines = [line for line in proc.stderr.splitlines() if line.strip()]
        detail = next((line for line in reversed(lines) if "ERROR" in line or "Failed" in line),
                      lines[-1] if lines else "")
        raise AccountError(detail or _("rclone stopped with error {code}").format(code=proc.returncode))
    return proc.stdout


def check(rclone: str, paths: Paths, creds: Credentials) -> Quota:
    """Log in by asking for the quota: cheap, read-only, needs valid credentials."""
    paths.rclone_config.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(_run(rclone, paths, creds, "about", f"{remote_spec('/')}", "--json") or "{}")
    return Quota(data.get("total"), data.get("used"), data.get("free"))


def list_folders(rclone: str, paths: Paths, creds: Credentials, remote_path: str) -> list[str]:
    out = _run(rclone, paths, creds, "lsf", remote_spec(remote_path), "--dirs-only", "--max-depth", "1")
    return sorted(line.rstrip("/") for line in out.splitlines() if line.strip())
