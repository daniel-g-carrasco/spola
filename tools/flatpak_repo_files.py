#!/usr/bin/env python3
"""Write the files that sit next to the published Flatpak repository:

  <pages>/spola.flatpakref     one-click install (GNOME Software, KDE Discover)
  <pages>/spola.flatpakrepo    adds the remote only
  <pages>/index.html           short landing page with the commands
  <pages>/.nojekyll            keep GitHub Pages from touching the ostree tree

Usage: flatpak_repo_files.py PAGES_DIR BASE_URL GPG_KEY_BASE64 VERSION
"""

import sys
from pathlib import Path

APP_ID = "io.github.daniel_g_carrasco.spola"
TITLE = "Spola"
HOMEPAGE = "https://spola.neistar.com"
COMMENT = "Keep your folders in sync with Koofr"
DESCRIPTION = (
    "Spola keeps folders on your computer and folders on Koofr identical, in both "
    "directions, in the background. Built on rclone, with previews before every big step."
)

pages, base, key, version = Path(sys.argv[1]), sys.argv[2].rstrip("/"), sys.argv[3], sys.argv[4]
repo_url = f"{base}/repo"

(pages / "spola.flatpakref").write_text(f"""[Flatpak Ref]
Name={APP_ID}
Branch=stable
Title={TITLE}
Comment={COMMENT}
Description={DESCRIPTION}
Homepage={HOMEPAGE}
Icon={base}/icon.png
Url={repo_url}
SuggestRemoteName=spola
IsRuntime=false
RuntimeRepo=https://dl.flathub.org/repo/flathub.flatpakrepo
GPGKey={key}
""", encoding="utf-8")

(pages / "spola.flatpakrepo").write_text(f"""[Flatpak Repo]
Title={TITLE}
Comment={COMMENT}
Description={DESCRIPTION}
Homepage={HOMEPAGE}
Icon={base}/icon.png
Url={repo_url}
GPGKey={key}
""", encoding="utf-8")

(pages / ".nojekyll").write_text("")

(pages / "index.html").write_text(f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Spola, Flatpak repository</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font: 17px/1.6 system-ui, sans-serif; max-width: 40rem; margin: 3rem auto; padding: 0 1rem; }}
  code, pre {{ background: rgba(127, 127, 127, .14); border-radius: 6px; padding: .1rem .35rem; }}
  pre {{ padding: .75rem .9rem; overflow-x: auto; }}
  a.btn {{ display: inline-block; background: #1a5fb4; color: #fff; padding: .7rem 1.2rem; border-radius: 8px; text-decoration: none; font-weight: 600; }}
  .dim {{ opacity: .75; font-size: .95rem; }}
</style>
</head>
<body>
<h1>Spola, Flatpak repository</h1>
<p>This is where the Flatpak of <a href="{HOMEPAGE}">Spola</a> lives. Current version: <strong>{version}</strong>.</p>
<p><a class="btn" href="{base}/spola.flatpakref">Install Spola</a></p>
<p>Or from a terminal:</p>
<pre>flatpak install {base}/spola.flatpakref</pre>
<p>Updates arrive on their own, through GNOME Software or <code>flatpak update</code>. The GNOME runtime comes from Flathub.</p>
<p class="dim">The repository is signed. The key's fingerprint is in the
<a href="https://github.com/daniel-g-carrasco/spola#flatpak">README</a>.
<a href="https://github.com/daniel-g-carrasco/spola">Source and issues</a> are on GitHub.</p>
</body>
</html>
""", encoding="utf-8")
print(f"written in {pages}: spola.flatpakref, spola.flatpakrepo, index.html, .nojekyll (url {repo_url})")
