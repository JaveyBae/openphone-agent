import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from autoglm_runtime import definitions


class RuntimeConfigurationTest(unittest.TestCase):
    def test_tunnel_is_private_and_restarts(self):
        definitions_by_label = dict(definitions())
        tunnel = definitions_by_label["ai.openphone.autoglm.tunnel"]
        self.assertTrue(tunnel["KeepAlive"])
        self.assertIn("127.0.0.1:18085:127.0.0.1:18085", tunnel["ProgramArguments"])
        self.assertIn("ServerAliveInterval=15", tunnel["ProgramArguments"])

    def test_bridge_is_real_and_device_scoped(self):
        bridge = dict(definitions())["ai.openphone.autoglm.bridge"]
        self.assertIn("autoglm", bridge["ProgramArguments"])
        self.assertNotIn("mock", bridge["ProgramArguments"])
        self.assertEqual(bridge["EnvironmentVariables"]["ANDROID_SERIAL"], "emulator-5554")
