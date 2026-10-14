#!/usr/bin/env python3
"""Record rclone bisync's JSON logs on Windows, with Spola's own flags,
between two local folders: a first alignment, then a sync with a new, a
changed, a deleted and a renamed file, as a preview and for real. The logs
become test fixtures (how rclone writes Windows paths)."""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from spola.config import Config, Pair  # noqa: E402
from spola import jobs  # noqa: E402
from spola.paths import Paths  # noqa: E402
from spola.preflight import root_identity, write_marker  # noqa: E402
from spola.rclone import Mode, bisync_args, clean_env  # noqa: E402

out = Path(sys.argv[1]).resolve()
out.mkdir(parents=True, exist_ok=True)
rclone = os.environ["SPOLA_RCLONE"]
base = Path(tempfile.mkdtemp(prefix="spola-win-"))
local, remote = base / "local", base / "remote"
local.mkdir()
remote.mkdir()
for i in range(12):
    (local / f"f{i:02}.txt").write_text(str(i))
(local / "sub").mkdir()
(local / "sub" / "photo.jpg").write_bytes(b"x" * 1000)
dev, ino = root_identity(str(local))
pair = Pair("winsample", str(local), "/remote", root_dev=dev, root_ino=ino)
write_marker(pair)
paths = Paths(base / "config", base / "state", base / "data")
cfg = Config(user="dev")


def run(mode, dry, name):
    job = jobs.prepare(cfg, pair, paths, mode, dry, True, path2_override=str(remote))
    done = subprocess.run(bisync_args(rclone, job.request), env=clean_env(), capture_output=True,
                          text=True, encoding="utf-8", errors="replace")
    (out / name).write_text(done.stderr, encoding="utf-8")
    print(name, done.returncode, "roots:", job.path1, job.path2)


run(Mode.RESYNC, False, "windows-resync.jsonl")
pair.initialized = True
(local / "new.txt").write_text("new")
(local / "f00.txt").write_text("changed")
(local / "f01.txt").unlink()
os.rename(local / "sub" / "photo.jpg", local / "sub" / "renamed.jpg")
(remote / "from-koofr.txt").write_text("there")
run(Mode.SYNC, True, "windows-sync-dry-run.jsonl")
run(Mode.SYNC, False, "windows-sync.jsonl")
(out / "roots.txt").write_text(f"{local}\n{remote}\n", encoding="utf-8")


# -- an interrupted run, as Stop does it, then the next run --------------------------
def interrupted(name):
    import time
    from spola.runner import BisyncRun
    (local / "big.bin").write_bytes(os.urandom(3_000_000))
    job = jobs.prepare(cfg, pair, paths, Mode.SYNC, False, False, path2_override=str(remote))
    argv = bisync_args(rclone, job.request) + ["--bwlimit", "500k"]
    run = BisyncRun(argv, clean_env(), paths.lock_file(pair.id), out / f"{name}-logs", job.path1, job.path2)
    run.start()
    time.sleep(3)
    run.cancel()
    first = run.wait(90)
    job = jobs.prepare(cfg, pair, paths, Mode.SYNC, False, False, path2_override=str(remote))
    done = subprocess.run(bisync_args(rclone, job.request), env=clean_env(), capture_output=True,
                          text=True, encoding="utf-8", errors="replace")
    (out / f"{name}-next.jsonl").write_text(done.stderr, encoding="utf-8")
    with open(out / "interrupted.txt", "a", encoding="utf-8") as f:
        f.write(f"{name}: cancelled run {first.result if first else 'no outcome'} "
                f"({first.message if first else ''}), next run exit {done.returncode}\n")
    (local / "big.bin").unlink()



from spola import win32  # noqa: E402
try:
    interrupted("console" if win32.has_console() else "no-console")
except Exception:
    import traceback
    (out / "error.txt").write_text(traceback.format_exc(), encoding="utf-8")
    raise
shutil.rmtree(base, ignore_errors=True)
