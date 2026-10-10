#!/usr/bin/env python3
"""Per-path states for the file manager emblems."""

import unittest

import _setup  # noqa: F401

from spola.states import PathStates, State, ancestors_and_self


class StatesTest(unittest.TestCase):
    def fresh(self):
        s = PathStates(excluded=lambda rel: rel.split("/")[-1].startswith("~$") or rel.startswith("node_modules/"))
        s.load_listing(["a", "a/one.txt", "a/two.txt", "b", "b/three.txt", "root.txt"])
        return s

    def test_synced_and_unknown(self):
        s = self.fresh()
        self.assertIs(s.state_of("a/one.txt"), State.SYNCED)
        self.assertIs(s.state_of("a", True), State.SYNCED)
        self.assertIs(s.state_of("", True), State.SYNCED)
        self.assertIs(s.state_of("new.txt"), State.PENDING)          # on disk, never synced
        self.assertIs(s.state_of("newdir", True), State.PENDING)
        self.assertIs(s.state_of("~$lock.docx"), State.EXCLUDED)
        self.assertIs(s.state_of("node_modules", True), State.EXCLUDED)

    def test_folders_take_the_worst_state_below(self):
        s = self.fresh()
        self.assertEqual(s.mark_pending("a/one.txt"), ["", "a", "a/one.txt"])
        self.assertIs(s.state_of("a/one.txt"), State.PENDING)
        self.assertIs(s.state_of("a", True), State.PENDING)
        self.assertIs(s.state_of("", True), State.PENDING)
        self.assertIs(s.state_of("b", True), State.SYNCED)
        s.run_started(["a/one.txt"])
        self.assertIs(s.state_of("a", True), State.SYNCING)
        s.errors.add("b/three.txt")
        self.assertIs(s.state_of("", True), State.ERROR)             # error beats syncing
        s.conflicts.add("a/two.conflict-local-2026-10-10-1.txt")
        self.assertIs(s.state_of("a", True), State.CONFLICT)
        self.assertIs(s.state_of("a/two.conflict-local-2026-10-10-1.txt"), State.CONFLICT)

    def test_excluded_things_do_not_colour_their_folder(self):
        s = self.fresh()
        s.mark_pending("a/~$draft.docx")
        self.assertIs(s.state_of("a", True), State.SYNCED)
        s.refused.add("Vacanze \U0001F334")
        s.mark_pending("Vacanze \U0001F334/x.jpg")
        self.assertIs(s.state_of("Vacanze \U0001F334/x.jpg"), State.EXCLUDED)
        self.assertIs(s.state_of("", True), State.SYNCED)

    def test_run_finished(self):
        s = self.fresh()
        s.mark_pending("a/one.txt")
        s.mark_pending("during.txt")
        s.run_started(["a/one.txt"])
        changed = s.run_finished(listing=["a", "a/one.txt", "a/two.txt", "b", "b/three.txt", "root.txt", "down.txt"],
                                 touched=["down.txt"], errors=[], conflicts=[], keep_pending=["during.txt"])
        self.assertEqual(changed, ["", "a", "a/one.txt", "down.txt", "during.txt"])
        self.assertIs(s.state_of("a/one.txt"), State.SYNCED)
        self.assertIs(s.state_of("down.txt"), State.SYNCED)
        self.assertIs(s.state_of("during.txt"), State.PENDING)
        self.assertIs(s.state_of("", True), State.PENDING)

    def test_folder_states_and_helpers(self):
        s = self.fresh()
        s.mark_pending("a/one.txt")
        self.assertEqual(s.folder_states("", [("a", True), ("b", True), ("root.txt", False), ("x.txt", False)]),
                         {"a": State.PENDING, "b": State.SYNCED, "root.txt": State.SYNCED, "x.txt": State.PENDING})
        self.assertEqual(ancestors_and_self("a/b/c"), ["", "a", "a/b", "a/b/c"])
        self.assertEqual(ancestors_and_self(""), [""])
        self.assertIs(State.worst([]), State.NONE)
        self.assertIs(State.worst([State.SYNCED, State.PENDING]), State.PENDING)


if __name__ == "__main__":
    unittest.main()
