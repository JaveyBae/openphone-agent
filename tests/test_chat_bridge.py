import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from chat_bridge import progress_message


class ProgressMessageTest(unittest.TestCase):
    def test_maps_agent_action(self) -> None:
        self.assertEqual(
            progress_message("[3/10] swipe: inspect more content"),
            "正在查看更多内容…",
        )

    def test_ignores_non_step_output(self) -> None:
        self.assertIsNone(progress_message("Run log: runs/example"))


if __name__ == "__main__":
    unittest.main()
