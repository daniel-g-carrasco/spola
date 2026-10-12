#!/usr/bin/env python3
"""The file-by-file activity of a run, as the window lists it: from the
lines rclone writes, which file is waiting, copying (and how far), done or
failed, and which way it goes."""

import unittest

import _setup  # noqa: F401

from spola import activity as act
from spola.engine import Engine
from spola.rclone import parse_line, relative_path

ROOTS = ["/home/u/work/", "koofr:data/work"]


def line(msg, **fields):
    import json
    return json.dumps({"level": "info", "msg": msg, **fields})


class ActivityTest(unittest.TestCase):
    def setUp(self):
        self.now = [0.0]
        self.a = act.RunActivity(clock=lambda: self.now[0])

    def feed(self, *lines):
        for text in lines:
            self.now[0] += 1
            Engine._track(self.a, parse_line(text), lambda p: relative_path(p, ROOTS))

    def test_a_move_is_one_row_not_a_delete_and_a_copy(self):
        # renamed here, moved on Koofr; renamed on Koofr, moved here; and a
        # conflict copy, renamed by bisync with the same line but never confirmed
        self.feed(line("- Path1    Queue copy to Path2    - koofr{ab}:data/work/2026/trip.jpg"),
                  line("- Path2    Queue delete           - koofr{ab}:data/work/IMG_1.jpg"),
                  line("- Path1    Queue delete           - local{cd}:/home/u/work/IMG_2.jpg"),
                  line("- Path2    Queue copy to Path1    - local{cd}:/home/u/work/from-there.jpg"),
                  line("Moved (server-side) to: both.conflict-local-2026-10-13-1.md", object="both.md"),
                  line("- Path1    Do queued copies to    - Path2"),
                  line("Moved (server-side) to: 2026/trip.jpg", object="IMG_1.jpg"),
                  line('Renamed from "IMG_1.jpg"', object="2026/trip.jpg"),
                  line("- Path2    Do queued copies to    - Path1"),
                  line("Moved (server-side) to: from-there.jpg", object="IMG_2.jpg"),
                  line('Renamed from "IMG_2.jpg"', object="from-there.jpg"))
        ops = {o.path: o for o in self.a.ops.values()}
        self.assertEqual(sorted(ops), ["2026/trip.jpg", "from-there.jpg"])      # the deletes are gone
        self.assertEqual((ops["2026/trip.jpg"].kind, ops["2026/trip.jpg"].origin), (act.MOVE_KOOFR, "IMG_1.jpg"))
        self.assertEqual((ops["from-there.jpg"].kind, ops["from-there.jpg"].origin), (act.MOVE_HERE, "IMG_2.jpg"))
        self.assertTrue(all(o.state == act.DONE for o in ops.values()))

    def test_queue_lines_say_what_and_which_way(self):
        self.feed(line("- Path2    Queue copy to Path1    - koofr{ab}:data/work/a/new.pdf"),
                  line("- Path1    Queue copy to Path2    - local{cd}:/home/u/work/b.odt"),
                  line("- Path1    Queue delete           - local{cd}:/home/u/work/old.txt"),
                  line("- Path2    Queue delete           - koofr{ab}:data/work/gone.txt"),
                  line("- WARNING  New or changed in both paths - both.md"))
        kinds = {o.path: o.kind for o in self.a.ops.values()}
        self.assertEqual(kinds, {"a/new.pdf": act.DOWNLOAD, "b.odt": act.UPLOAD, "old.txt": act.DELETE_HERE,
                                 "gone.txt": act.DELETE_KOOFR, "both.md": act.CONFLICT})
        self.assertTrue(all(o.state == act.WAITING for o in self.a.ops.values()))

    def test_progress_then_done(self):
        self.feed(line("- Path2    Queue copy to Path1    - koofr{ab}:data/work/a/new.pdf"))
        stats = {"transferring": [{"name": "a/new.pdf", "size": 1000, "bytes": 450,
                                   "srcFs": "koofr{ab}:data/work", "dstFs": "local{cd}:/home/u/work"}]}
        import json
        self.feed(json.dumps({"level": "info", "msg": "stats", "stats": stats}))
        op = self.a.ops["a/new.pdf"]
        self.assertEqual((op.state, op.fraction), (act.ACTIVE, 0.45))
        self.feed(line("Copied (new)", object="a/new.pdf", objectType="*local.Object", size=1000))
        self.assertEqual((op.state, op.fraction), (act.DONE, 1.0))

    def test_a_first_alignment_queues_nothing_but_copies_show(self):
        import json
        stats = {"transferring": [{"name": "photo.jpg", "size": 10, "bytes": 1, "srcFs": "local{cd}:/home/u/work"}]}
        self.feed(json.dumps({"level": "info", "msg": "stats", "stats": stats}))
        self.assertEqual(self.a.ops["photo.jpg"].kind, act.UPLOAD)
        self.feed(line("Copied (new)", object="photo.jpg", objectType="*koofr.Object", size=10))
        self.assertEqual(self.a.ops["photo.jpg"].state, act.DONE)

    def test_a_replaced_file_going_to_the_backup_is_not_a_finished_download(self):
        # downloading a newer version: the old one goes to the backup first
        # ("Deleted" here when the backup is on another file system)
        self.feed(line("- Path2    Queue copy to Path1    - koofr{ab}:data/work/MEMORIA.md"),
                  line("Deleted", object="MEMORIA.md", objectType="*local.Object"),
                  line("Moved (server-side)", object="MEMORIA.md"))
        self.assertEqual(self.a.ops["MEMORIA.md"].state, act.WAITING)
        self.feed(line("Copied (replaced existing)", object="MEMORIA.md", objectType="*local.Object"))
        self.assertEqual(self.a.ops["MEMORIA.md"].state, act.DONE)

    def test_deletions_and_failures(self):
        self.feed(line("- Path1    Queue delete           - local{cd}:/home/u/work/old.txt"),
                  line("- Path2    Queue delete           - koofr{ab}:data/work/gone.txt"),
                  line("- Path1    Queue delete           - local{cd}:/home/u/work/vanished.docx"),
                  line("Moved (server-side)", object="old.txt"),
                  line("Deleted", object="gone.txt", objectType="*koofr.Object"))
        self.feed(json_error("vanished.docx"))
        states = {o.path: o.state for o in self.a.ops.values()}
        self.assertEqual(states, {"old.txt": act.DONE, "gone.txt": act.DONE, "vanished.docx": act.FAILED})

    def test_rows_in_progress_first_then_latest_done_then_waiting(self):
        for name in ("w1", "w2", "d1", "d2", "a1"):
            self.feed(line(f"- Path2    Queue copy to Path1    - koofr{{ab}}:data/work/{name}"))
        self.feed(line("Copied (new)", object="d1", objectType="*local.Object"),
                  line("Copied (new)", object="d2", objectType="*local.Object"))
        import json
        self.feed(json.dumps({"level": "info", "msg": "s", "stats": {"transferring": [
            {"name": "a1", "size": 5, "bytes": 1, "srcFs": "koofr{ab}:data/work"}]}}))
        self.assertEqual([o.path for o in self.a.rows(4)], ["a1", "d2", "d1", "w1"])

    def test_a_run_that_succeeded_did_everything(self):
        self.feed(line("- Path2    Queue copy to Path1    - koofr{ab}:data/work/folder"))
        self.a.run_ended(ok=False)
        self.assertEqual(self.a.ops["folder"].state, act.WAITING)
        self.a.run_ended(ok=True)                       # folders are made without a "Copied" line
        self.assertEqual(self.a.ops["folder"].state, act.DONE)


def json_error(path):
    import json
    return json.dumps({"level": "error", "msg": "Couldn't move into backup dir", "object": path,
                       "objectType": "*local.Object"})


if __name__ == "__main__":
    unittest.main()
