import sys
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import chat_bridge as bridge
from test_workspace_isolation import SESSION, STATE

DUMP = """ACTIVITY MANAGER ACTIVITIES (dumpsys activity activities)
Display #0 (activities from top to bottom):
    topResumedActivity=ActivityRecord{bd02480 u0 ai.openphone.agent/.MainActivity t83}
Display #5 (activities from top to bottom):
    topResumedActivity=ActivityRecord{5f10a4f u0 com.google.android.gm/.ComposeActivityGmail t84}
ActivityTaskSupervisor state:
    topResumedActivity=ActivityRecord{fake u0 evil.app/.X t99}
"""


class PrivateTopTaskTest(unittest.TestCase):
    def test_reads_only_the_private_display_section(self):
        self.assertEqual(("com.google.android.gm", 84), bridge.private_top_task(DUMP, 5))
        self.assertEqual(("ai.openphone.agent", 83), bridge.private_top_task(DUMP, 0))

    def test_missing_display_or_supervisor_noise_is_not_a_target(self):
        self.assertIsNone(bridge.private_top_task(DUMP, 9))
        self.assertIsNone(bridge.private_top_task("Display #5 (activities)\n", 5))


class PreviewCommandTest(unittest.TestCase):
    def test_copies_only_the_session_frame_privately_and_never_blank(self):
        command = bridge.preview_shell_command(STATE)
        self.assertTrue(command.startswith(f"cat /data/local/tmp/openphone-workspace/frame-{SESSION}.png | "))
        self.assertIn("run-as ai.openphone.agent", command)
        self.assertIn("umask 077", command)
        self.assertIn("[ -s files/preview/frame.tmp ]", command)
        self.assertNotIn("screencap", command)

    def test_invalid_workspace_identity_is_refused(self):
        for changes in ({"session": "../../owner"}, {"displayId": 0}, {"backend": "mediaprojection"}):
            with self.subTest(changes=changes), self.assertRaises(bridge.AgentError):
                bridge.preview_shell_command({**STATE, **changes})


class TakeoverRequestTest(unittest.TestCase):
    def test_fresh_unhandled_request_is_accepted_once(self):
        now = 1_800_000_000_000
        request = {"id": str(uuid.uuid4()), "requestedAt": now - 1000}
        self.assertTrue(bridge.valid_takeover(request, set(), now))
        self.assertFalse(bridge.valid_takeover(request, {request["id"]}, now))

    def test_stale_future_or_malformed_requests_are_ignored(self):
        now = 1_800_000_000_000
        good = str(uuid.uuid4())
        for request in ({"id": good, "requestedAt": now - bridge.TAKEOVER_MAX_AGE_MS - 1},
                        {"id": good, "requestedAt": now + 5000},
                        {"id": good, "requestedAt": str(now)},
                        {"id": "../takeover", "requestedAt": now},
                        {"requestedAt": now}, [good], None):
            with self.subTest(request=request):
                self.assertFalse(bridge.valid_takeover(request, set(), now))


if __name__ == "__main__":
    unittest.main()
