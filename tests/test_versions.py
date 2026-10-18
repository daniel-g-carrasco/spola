"""Previous versions: the copies Spola keeps of what a sync replaced or
deleted here, how they are found, labelled and restored, and that nothing
but the retention ever deletes one."""

import gettext
import json
import os
import re
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

import _setup  # noqa: F401
from _setup import rclone_binary
from spola import jobs, versions
from spola.config import Config, Pair
from spola.i18n import translation_for
from spola.paths import Paths

RUN1, RUN2 = "20261015-090000", "20261017-123000"
# the Italian names need the compiled catalogue (tools/compile_po.py): CI may not have it
ITALIAN = type(translation_for("it")) is not gettext.NullTranslations


class VersionsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.paths = Paths(self.tmp / "config", self.tmp / "state", self.tmp / "data")
        self.local = self.tmp / "local"
        (self.local / "docs").mkdir(parents=True)
        self.pair = Pair("p1", str(self.local), "/d")
        (self.local / "relazione.odt").write_text("today")
        (self.local / "docs" / "nota.txt").write_text("nota today")
        self.root = self.paths.backup_dir("p1")
        for run, files in ((RUN1, {"relazione.odt": "old", "docs/via.txt": "gone"}),
                           (RUN2, {"relazione.odt": "yesterday", "docs/nota.txt": "nota yesterday"})):
            for rel, text in files.items():
                (self.root / run / rel).parent.mkdir(parents=True, exist_ok=True)
                (self.root / run / rel).write_text(text)
        (self.root / "not-a-run").mkdir()
        (self.root / "not-a-run" / "x").write_text("x")

    def index(self):
        return versions.Index(self.pair, versions.scan(self.paths, self.pair))

    def test_found_newest_first_with_a_reason(self):
        versions.write_manifest(self.paths, "p1", RUN2, {"relazione.odt": "replaced", "docs/nota.txt": "conflict"})
        idx = self.index()
        self.assertEqual([(v.run, v.rel) for v in idx.of("relazione.odt")], [(RUN2, "relazione.odt"), (RUN1, "relazione.odt")])
        self.assertEqual(idx.of("docs/nota.txt")[0].reason, "conflict")
        # no manifest for RUN1: the reason comes from what is here now
        self.assertEqual(idx.by_id(f"{RUN1}/relazione.odt").reason, "replaced")
        self.assertEqual(idx.by_id(f"{RUN1}/docs/via.txt").reason, "deleted")
        self.assertEqual([v.rel for v in idx.deleted_under("docs")], ["docs/via.txt"])
        self.assertEqual([v.rel for v in idx.deleted_under("")], ["docs/via.txt"])
        self.assertEqual(idx.names_in(""), {"relazione.odt"})
        self.assertEqual(idx.names_in("docs"), {"nota.txt", "via.txt"})
        self.assertNotIn("x", {v.rel for v in idx.versions})                # not a run: ignored
        s = idx.summary()
        self.assertEqual((s.files, s.size), (4, len("old") + len("gone") + len("yesterday") + len("nota yesterday")))
        self.assertEqual(s.oldest, versions.run_time(RUN1))
        self.assertEqual(idx.of("relazione.odt")[0].expires(30) - idx.of("relazione.odt")[0].time, 30 * 86400)
        self.assertEqual(versions.Index(self.pair).summary().files, 0)    # nothing kept: says so

    def test_manifest_written_whole_and_merged(self):
        versions.write_manifest(self.paths, "p1", RUN2, {"relazione.odt": "replaced"})
        versions.write_manifest(self.paths, "p1", RUN2, {"docs/nota.txt": "deleted", "bad": "nonsense"})
        data = json.loads((self.root / (RUN2 + ".spola.json")).read_text())
        self.assertEqual(data["files"], {"relazione.odt": "replaced", "docs/nota.txt": "deleted"})
        self.assertEqual([n for n in os.listdir(self.root) if n.endswith(".tmp")], [])
        with self.assertRaises(ValueError):
            versions.write_manifest(self.paths, "p1", "../x", {})
        (self.root / (RUN1 + ".spola.json")).write_text("{broken")
        self.assertEqual(versions.read_manifest(self.paths, "p1", RUN1), {})    # unreadable: the fallback labels

    def test_restore_as_a_copy_next_to_the_file(self):
        v = self.index().of("relazione.odt")[0]
        rel = versions.restore(self.paths, self.pair, v, "copy", language="it")
        stamp = time.strftime("%Y-%m-%d %H.%M", time.localtime(versions.run_time(RUN2)))
        self.assertEqual(rel, versions.version_name("relazione.odt", v.time, "it"))
        if ITALIAN:
            self.assertEqual(rel, f"relazione (versione del {stamp}).odt")
        self.assertEqual((self.local / rel).read_text(), "yesterday")
        self.assertEqual((self.local / "relazione.odt").read_text(), "today")      # the file is untouched
        self.assertEqual(versions.restore(self.paths, self.pair, v, "copy", language="it"),
                         rel[:-len(".odt")] + " (2).odt")                          # again: a free name
        self.assertTrue(versions.copy_path(self.paths, v).exists())               # the copy stays
        self.assertEqual(versions.restore(self.paths, self.pair, v, "copy", language="en")[:31],
                         "relazione (version of 2026-10-1")

    def test_replace_keeps_the_current_file_and_can_be_undone(self):
        v = self.index().of("relazione.odt")[0]
        self.assertEqual(versions.restore(self.paths, self.pair, v, "replace", now=1_900_000_000), "relazione.odt")
        self.assertEqual((self.local / "relazione.odt").read_text(), "yesterday")
        aside = [x for x in self.index().of("relazione.odt") if x.reason == "restored"]
        self.assertEqual(len(aside), 1)
        self.assertEqual(versions.copy_path(self.paths, aside[0]).read_text(), "today")
        # undone from the same list
        versions.restore(self.paths, self.pair, aside[0], "replace", now=1_900_000_000)
        self.assertEqual((self.local / "relazione.odt").read_text(), "today")
        self.assertEqual(len(self.index().of("relazione.odt")), 4)                # nothing lost on the way
        runs = {x.run for x in self.index().of("relazione.odt")}
        self.assertEqual(len(runs), 4)                                           # one run each, never shared

    def test_a_deleted_file_goes_back_in_its_place(self):
        v = self.index().deleted_under("docs")[0]
        self.assertEqual(versions.restore(self.paths, self.pair, v, "copy"), "docs/via.txt")
        self.assertEqual((self.local / "docs" / "via.txt").read_text(), "gone")
        self.assertEqual(self.index().deleted_under("docs"), [])

    def test_bad_ids_and_paths(self):
        v = self.index().of("relazione.odt")[0]
        for bad in ("../x", "/etc/passwd", "", "a//b"):
            with self.assertRaises(ValueError):
                versions.restore(self.paths, self.pair, versions.Version("p1", RUN2, bad, 0, 0, "replaced"), "copy")
        with self.assertRaises(ValueError):
            versions.restore(self.paths, self.pair, v, "delete")
        with self.assertRaises(FileNotFoundError):
            versions.restore(self.paths, self.pair, versions.Version("p1", RUN2, "missing.txt", 0, 0, "deleted"), "copy")

    def test_names(self):
        when = time.mktime((2026, 10, 17, 12, 30, 0, 0, 0, -1))
        self.assertEqual(versions.version_name("a/report.odt", when, "en"), "a/report (version of 2026-10-17 12.30).odt")
        self.assertEqual(versions.version_name(".bashrc", when, "en"), ".bashrc (version of 2026-10-17 12.30)")
        if ITALIAN:
            self.assertEqual(versions.version_name("Makefile", when, "it"), "Makefile (versione del 2026-10-17 12.30)")

    def test_open_copy_is_read_only_and_elsewhere(self):
        v = self.index().of("relazione.odt")[0]
        out = versions.open_copy(self.paths, v, str(self.tmp / "cache"))
        self.assertEqual(Path(out).read_text(), "yesterday")
        self.assertFalse(os.access(out, os.W_OK) and os.geteuid() != 0)
        self.assertTrue(versions.copy_path(self.paths, v).exists())

    def test_only_the_retention_deletes_and_it_takes_the_manifest(self):
        versions.write_manifest(self.paths, "p1", RUN1, {"relazione.odt": "replaced"})
        versions.write_manifest(self.paths, "p1", "20261001-000000", {"x": "deleted"})      # run gone by hand
        (self.root / ".manifest-abc.tmp").write_text("cut short")
        now = versions.run_time(RUN2) + 3600
        days = (now - versions.run_time(RUN1)) / 86400 - 1                                 # RUN1 expired, RUN2 not
        self.assertEqual(jobs.prune_backups(self.paths, "p1", int(days), now=now), 1)
        names = sorted(os.listdir(self.root))
        self.assertEqual(names, [RUN2, "not-a-run"])
        self.assertEqual({v.run for v in self.index().versions}, {RUN2})                # no ghost runs

    def test_no_other_code_deletes_a_kept_copy(self):
        # criterion: no path deletes a kept copy but the retention. The one
        # rmtree on the backups is in jobs.prune_backups; versions.py has none.
        src = Path(_setup.ROOT) / "src" / "spola"
        text = (src / "versions.py").read_text()
        self.assertNotIn("rmtree", text)
        self.assertFalse(re.search(r"\.unlink\((?!tmp)", text.replace("os.unlink(tmp)", "")))
        for path in src.glob("*.py"):
            body = path.read_text()
            if "backup_dir" in body and re.search(r"rmtree\(", body):
                self.assertIn(path.name, ("jobs.py", "paths.py"), path)
        self.assertNotIn("delete_backup_run", (src / "jobs.py").read_text() + (src / "window.py").read_text())


@unittest.skipUnless(rclone_binary(), "rclone not found")
class RealRunTest(unittest.TestCase):
    """The layout, checked with a real bisync run from jobs.prepare: a file
    replaced from Koofr and one deleted on Koofr land in the run's folder
    under their own path and name, and the manifest tells them apart."""

    def test_a_real_run(self):
        from spola.preflight import root_identity
        from spola.rclone import Mode, bisync_args, clean_env, parse_line, summarize
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        local, remote = tmp / "local", tmp / "remote"
        (local / "docs").mkdir(parents=True)
        remote.mkdir()
        for name, text in {"relazione.odt": "v1", "docs/nota.txt": "n1", "cancellami.pdf": "pdf"}.items():
            (local / name).write_text(text)
        paths = Paths(tmp / "config", tmp / "state", tmp / "data")
        dev, ino = root_identity(str(local))
        pair = Pair("p1", str(local), "/r", root_dev=dev, root_ino=ino, max_delete_percent=100)
        cfg = Config(user="dev", pairs=[pair])

        def run(mode):
            job = jobs.prepare(cfg, pair, paths, mode, False, False, path2_override=str(remote), koofr_files=lambda: set())
            r = subprocess.run(bisync_args(rclone_binary(), job.request), env=clean_env(), capture_output=True, text=True)
            events = [e for e in (parse_line(line) for line in r.stderr.splitlines()) if e]
            return job, summarize(r.returncode, events, job.path1, job.path2)
        run(Mode.RESYNC)
        pair.initialized = True
        run(Mode.SYNC)
        time.sleep(1.1)
        (remote / "relazione.odt").write_text("v2 from koofr")
        (remote / "docs" / "nota.txt").write_text("n2 from koofr")
        (remote / "cancellami.pdf").unlink()
        job, outcome = run(Mode.SYNC)
        run_name = job.request.backup_dir1.name
        self.assertEqual(versions.files_of_run(paths, "p1", run_name),
                         [("cancellami.pdf", 3), ("docs/nota.txt", 2), ("relazione.odt", 2)])   # own path and name
        versions.record_run(paths, pair, run_name, outcome.plan.delete_local, realigned=False)
        idx = versions.Index(pair, versions.scan(paths, pair))
        self.assertEqual({v.rel: v.reason for v in idx.versions},
                         {"cancellami.pdf": "deleted", "docs/nota.txt": "replaced", "relazione.odt": "replaced"})
        v = idx.of("relazione.odt")[0]
        self.assertEqual(versions.copy_path(paths, v).read_text(), "v1")


if __name__ == "__main__":
    unittest.main()
