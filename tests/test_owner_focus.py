import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from agent_common import AgentError  # noqa: E402
from android_device import AndroidController  # noqa: E402

OVERVIEW = """  FocusedDisplayId: 0
  FocusedWindows:
    displayId=2, name='41f269a com.MobileTicket/com.MobileTicket.ui.activity.MainActivity'
  FocusRequests:
"""
STOLEN = OVERVIEW.replace("FocusedDisplayId: 0", "FocusedDisplayId: 2")


def controller(dump):
    c = AndroidController.__new__(AndroidController)
    c._run = Mock(return_value=dump)
    return c


class OwnerFocusTest(unittest.TestCase):
    def test_the_owner_in_overview_does_not_stop_the_agent(self):
        # Overview/recents leaves display 0 without a focused window of its own.
        self.assertEqual({"top_display": 0, "focused_window_displays": [2]},
                         controller(OVERVIEW).require_owner_focus(2, private_window=True))

    def test_global_focus_on_another_display_still_stops(self):
        with self.assertRaisesRegex(AgentError, "focused display 2"):
            controller(STOLEN).require_owner_focus(2)
