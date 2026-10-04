import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from display_viewer import AgentDisplayViewer, build_parser


class DisplayViewerTest(unittest.TestCase):
    def viewer(self, owner):
        viewer = AgentDisplayViewer.__new__(AgentDisplayViewer)
        viewer.owner_screen = owner
        viewer.adb_run = Mock(return_value=b"\x89PNG\r\n\x1a\nframe")
        viewer.read_display_id = Mock(return_value=2)
        viewer.frame_state = {"displayId": 2}
        return viewer

    def test_both_is_explicit_opt_in(self):
        self.assertFalse(build_parser().parse_args([]).both)
        self.assertTrue(build_parser().parse_args(["--both"]).both)

    def test_owner_preview_uses_local_screencap(self):
        viewer = self.viewer(True)
        self.assertEqual(viewer.read_frame().display_id, 0)
        viewer.adb_run.assert_called_once_with("exec-out", "screencap", "-p")
        viewer.read_display_id.assert_not_called()

    def test_private_preview_only_reads_agent_frame(self):
        viewer = self.viewer(False)
        self.assertEqual(viewer.read_frame().display_id, 2)
        viewer.adb_run.assert_called_once_with("exec-out", "run-as", "ai.openphone.agent", "cat", "files/display/latest.png")

    def test_shell_private_preview_reads_only_matching_session_frame(self):
        viewer = self.viewer(False)
        session = "3a54f8aa-a9dc-47a5-bf64-93f5b9645f03"
        viewer.frame_state = {"backend": "adb-shell", "displayId": 2, "session": session,
                              "displayName": "OpenPhone Isolated Workspace " + session}
        viewer.read_frame()
        viewer.adb_run.assert_called_once_with("exec-out", "cat", "/data/local/tmp/openphone-workspace/frame-" + session + ".png")

    def test_private_failure_never_falls_back_to_owner(self):
        viewer = self.viewer(False)
        viewer.read_display_id.side_effect = RuntimeError("stopped")
        with self.assertRaises(RuntimeError):
            viewer.read_frame()
        viewer.adb_run.assert_not_called()

    def test_display_zero_is_not_a_private_display(self):
        viewer = self.viewer(False)
        viewer.adb = "unused"
        with patch("display_viewer.AndroidController") as controller:
            controller.return_value.require_display.side_effect = RuntimeError("display 0")
            with self.assertRaises(RuntimeError):
                AgentDisplayViewer.read_display_id(viewer)
        viewer.adb_run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
