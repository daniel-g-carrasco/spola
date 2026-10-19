#!/usr/bin/env python3
"""A metered connection and power saving as Windows tells them."""

import sys
import unittest
from unittest import mock

import _setup  # noqa: F401

if sys.platform != "win32":
    raise unittest.SkipTest("Windows only")

from spola import winconditions as wc  # noqa: E402


class ConditionsTest(unittest.TestCase):
    def test_what_is_metered(self):
        self.assertFalse(wc.is_metered(wc.NLM_CONNECTION_COST_UNRESTRICTED))
        self.assertFalse(wc.is_metered(0))                                      # unknown
        self.assertTrue(wc.is_metered(wc.NLM_CONNECTION_COST_FIXED))
        self.assertTrue(wc.is_metered(wc.NLM_CONNECTION_COST_VARIABLE))
        self.assertTrue(wc.is_metered(wc.NLM_CONNECTION_COST_UNRESTRICTED | wc.NLM_CONNECTION_COST_ROAMING))
        self.assertTrue(wc.is_metered(wc.NLM_CONNECTION_COST_UNRESTRICTED | wc.NLM_CONNECTION_COST_OVERDATALIMIT))

    def test_what_is_a_network(self):
        self.assertFalse(wc.is_connected(wc.NLM_CONNECTIVITY_DISCONNECTED))
        self.assertFalse(wc.is_connected(wc.NLM_CONNECTIVITY_IPV4_NOTRAFFIC | wc.NLM_CONNECTIVITY_IPV6_NOTRAFFIC))
        self.assertTrue(wc.is_connected(wc.NLM_CONNECTIVITY_IPV4_INTERNET | wc.NLM_CONNECTIVITY_IPV6_NOTRAFFIC))
        self.assertTrue(wc.is_connected(wc.NLM_CONNECTIVITY_IPV6_INTERNET))
        self.assertTrue(wc.is_connected(0x20))                                  # a local network only

    def test_windows_answers(self):
        cost = wc.CostManager().cost()
        self.assertTrue(cost is None or isinstance(cost, int))
        self.assertIsInstance(wc.power_saver_on(), bool)
        # the machine running the tests has a network (they reach Koofr's API elsewhere)
        connectivity = wc.NetworkList().connectivity()
        self.assertIsInstance(connectivity, int)
        self.assertTrue(wc.is_connected(connectivity))

    def test_a_change_calls_back(self):
        state = {"cost": wc.NLM_CONNECTION_COST_UNRESTRICTED, "saver": False,
                 "connectivity": wc.NLM_CONNECTIVITY_IPV4_INTERNET}
        changed = []
        with mock.patch.object(wc.CostManager, "cost", lambda self: state["cost"]), \
                mock.patch.object(wc.NetworkList, "connectivity", lambda self: state["connectivity"]), \
                mock.patch.object(wc, "power_saver_on", lambda: state["saver"]):
            conditions = wc.Conditions(lambda: changed.append(1), poll_s=3600)
            self.addCleanup(conditions.stop)
            self.assertEqual((conditions.metered, conditions.power_saver, conditions.connected), (False, False, True))
            conditions._poll()
            self.assertEqual(changed, [])
            state["cost"] = wc.NLM_CONNECTION_COST_VARIABLE
            conditions._poll()
            self.assertEqual((conditions.metered, changed), (True, [1]))
            state["saver"] = True
            conditions._poll()
            self.assertEqual((conditions.power_saver, changed), (True, [1, 1]))
            state["connectivity"] = wc.NLM_CONNECTIVITY_DISCONNECTED       # the network gone, and back
            conditions._poll()
            self.assertEqual((conditions.connected, changed), (False, [1, 1, 1]))
            state["connectivity"] = wc.NLM_CONNECTIVITY_IPV4_INTERNET
            conditions._poll()
            self.assertEqual((conditions.connected, changed), (True, [1, 1, 1, 1]))
            state["connectivity"] = None                                   # Windows cannot be asked
            conditions._poll()
            self.assertIsNone(conditions.connected)


if __name__ == "__main__":
    unittest.main()
