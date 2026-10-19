import sys
import unittest
import json
import tempfile
import io
from types import SimpleNamespace
from unittest.mock import Mock, patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from chat_bridge import Bridge, failure_message, progress_message, result_summary
from execution_progress import PREFIX


class ProgressMessageTest(unittest.TestCase):
    def test_maps_agent_action(self) -> None:
        self.assertEqual(
            progress_message("[3/10] swipe: inspect more content"),
            "正在查看更多内容…",
        )

    def test_ignores_non_step_output(self) -> None:
        self.assertIsNone(progress_message("Run log: runs/example"))

    def test_maps_workspace_initialization_progress(self):
        self.assertEqual(
            "正在准备独立工作区，确认不会读取主屏…",
            progress_message("[0/15] initialize_workspace: verify content"),
        )

    def test_startup_error_is_explained_in_chinese(self):
        message = failure_message("Agent error: private workspace has no verified content; refusing a mirror")
        self.assertIn("保护主屏", message)
        self.assertNotIn("Agent error", message)
        self.assertEqual("unrelated error", failure_message("unrelated error"))
        self.assertIn("用完 40 步", failure_message("Agent error: stopped at the configured 40-step limit"))
        self.assertIn("来回重复", failure_message(
            "Agent error: stopped after the model alternated between the same two actions three times"))
        self.assertIn("用完 40 步", failure_message("Agent error: stopped at the configured 40-step limit"))
        self.assertIn("来回重复", failure_message(
            "Agent error: stopped after the model alternated between the same two actions three times"))

    def test_model_protocol_error_is_not_exposed_as_english_trace(self):
        message = failure_message("Agent error: model request/action failed: AutoGLM must return exactly one native action")
        self.assertIn("本步操作未执行", message)
        self.assertNotIn("Agent error", message)

    def test_preserves_multiline_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "one"
            run.mkdir()
            (run / "run.json").write_text(json.dumps({"summary": "one\ntwo"}))
            self.assertEqual(result_summary([f"Run log: {run}"], root), "one\ntwo")

    def test_rejects_result_outside_runs(self):
        self.assertIsNone(result_summary(["Run log: /tmp/elsewhere"], Path("runs")))

    def test_structured_progress_does_not_forward_reasoning(self):
        bridge = Bridge.__new__(Bridge)
        bridge.args = SimpleNamespace(provider="mock", model="", runs_dir=Path("runs"),
                                      max_steps=15, initial_url="https://example.com", api_key="")
        bridge.save_state = Mock()
        bridge.update = Mock()
        event = {"stage": "model", "step": 1, "total": 15, "reason": "private reasoning"}
        process = Mock(stdout=io.StringIO(PREFIX + json.dumps(event) +
                       "\n[1/15] tap: private reasoning\nSafe result\n"))
        process.wait.return_value = 0
        process.poll.return_value = 0
        with patch("chat_bridge.subprocess.Popen", return_value=process):
            bridge.run_task({"id": "test-task", "goal": "test"})
        messages = [call.args[2] for call in bridge.update.call_args_list]
        self.assertTrue(any("模型处理中" in value for value in messages))
        self.assertFalse(any("private reasoning" in value for value in messages))

    def test_owner_messages_conflict_has_actionable_copy(self):
        message = failure_message("Agent error: refusing to launch package: it already has a task on the owner's display 0")
        self.assertIn("最近任务", message)
        self.assertIn("避免抢走", message)

    def test_terminal_update_has_no_empty_adb_shell_argument(self):
        bridge = Bridge.__new__(Bridge)
        bridge.adb_run = Mock(return_value=SimpleNamespace(returncode=0))
        bridge.update("task", "failed", "短信操作安全暂停")
        args = bridge.adb_run.call_args.args
        self.assertNotIn("", args)
        self.assertIn("message_b64", args)


if __name__ == "__main__":
    unittest.main()


class StaleAppTaskTest(unittest.TestCase):
    def test_running_or_queued_task_is_reported(self):
        from chat_bridge import stale_app_task
        xml = '<map><string name="task_id">abc</string><string name="status">running</string></map>'
        self.assertEqual(("abc", "running"), stale_app_task(xml))
        done = '<map><string name="task_id">abc</string><string name="status">completed</string></map>'
        self.assertEqual((None, None), stale_app_task(done))
        self.assertEqual((None, None), stale_app_task(""))
