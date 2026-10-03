import sys
import unittest
import json
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from chat_bridge import progress_message, result_summary


class ProgressMessageTest(unittest.TestCase):
    def test_maps_agent_action(self) -> None:
        self.assertEqual(
            progress_message("[3/10] swipe: inspect more content"),
            "正在查看更多内容…",
        )

    def test_ignores_non_step_output(self) -> None:
        self.assertIsNone(progress_message("Run log: runs/example"))

    def test_preserves_multiline_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "one"
            run.mkdir()
            (run / "run.json").write_text(json.dumps({"summary": "one\ntwo"}))
            self.assertEqual(result_summary([f"Run log: {run}"], root), "one\ntwo")

    def test_rejects_result_outside_runs(self):
        self.assertIsNone(result_summary(["Run log: /tmp/elsewhere"], Path("runs")))


if __name__ == "__main__":
    unittest.main()
